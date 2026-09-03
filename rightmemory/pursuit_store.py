"""Durable Pursuit actions with validated, batched Git checkpoints."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import subprocess
import tempfile
import time
from types import SimpleNamespace
import uuid
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .graph import (
    MEMORY_DETAIL_FILE_RE,
    PURSUIT_DETAIL_FILE_RE,
    GraphManifest,
    build_graph_manifest,
)
from .isolated_write import GIT_TIMEOUT_SECONDS, IsolatedWriteSupervisor, MainMemoryDirtyError, OPERATION_TRAILER
from .pursuit_journal import PursuitJournal
from .session import MemoryWriteLock
from .pursuit_tree import PursuitEdit, PursuitOperationError, apply_operation, load_pursuit_tree
from .tools import CORRECTIONS_PATH, MemoryTools


EDITOR_ROLE = "pursuit-map"
_ACTIONS = frozenset({"create", "rename", "rename_many", "move", "delete", "edit_body", "set_focus"})
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRAILERS = {
    "editor": "RightMemory-Pursuit-Editor",
    "operation": OPERATION_TRAILER,
    "session": "RightMemory-Pursuit-Session",
    "action": "RightMemory-Pursuit-Action",
    "target": "RightMemory-Pursuit-Target",
    "id_remaps": "RightMemory-Pursuit-ID-Remaps",
    "signature": "RightMemory-Pursuit-Signature",
}


class PursuitStoreError(RuntimeError):
    """A stable error boundary for the existing Web API."""

    def __init__(
        self,
        code: str,
        message: str,
        status: int = 400,
        diagnostics: Iterable[str] = (),
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.diagnostics = tuple(diagnostics)


@dataclass(frozen=True)
class _RepositoryState:
    head: str = ""
    branch: str = ""
    dirty_paths: tuple[str, ...] = ()
    diagnostics: tuple[str, ...] = ()


class PursuitStore:
    def __init__(self, root: Path):
        self.root = Path(root).expanduser().resolve()
        self.root_key = hashlib.sha256(os.path.normcase(str(self.root)).encode("utf-8")).hexdigest()
        self._reader = IsolatedWriteSupervisor(self.root, EDITOR_ROLE)
        self._journal = PursuitJournal(self.root, self.root_key)

    def snapshot(self, session_id: str | None = None) -> dict[str, Any]:
        if session_id is not None and self._journal.path.exists():
            with self._journal.locked():
                record = self._load_record()
                if record:
                    return self._snapshot_record(record, session_id)
        return self._canonical_snapshot()

    def _canonical_snapshot(self) -> dict[str, Any]:
        """Return a readable projection, including why a root cannot be edited.

        Reads do not create runtime state. The two revision reads prevent a
        concurrent publication from producing an apparently editable mixed view.
        """
        snapshot: dict[str, Any] = {}
        for _attempt in range(2):
            state = self._repository_state()
            revision = self._revision(state.head, state.branch)
            errors: list[str] = []
            try:
                snapshot = load_pursuit_tree(self.root).to_dict()
            except (OSError, ValueError) as exc:
                snapshot = {"items": [], "root_ids": [], "focus_ids": [], "diagnostics": []}
                errors.append(str(exc))
            try:
                errors.extend(_validation_errors(self.root))
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
            current_head = self._head_or_empty()
            current_revision = self._revision(current_head)
            stable = revision == current_revision
            diagnostics = list(dict.fromkeys([
                *snapshot.get("diagnostics", []),
                *errors,
                *state.diagnostics,
                *(["Uncommitted changes: " + ", ".join(state.dirty_paths)] if state.dirty_paths else []),
            ]))
            snapshot.update(
                pending=False,
                history={"undo": [], "redo": []},
                revision=current_revision,
                git_head=current_head,
                root_key=self.root_key,
                valid=not errors,
                writable=bool(stable and not errors and state.head and not state.diagnostics and not state.dirty_paths),
                dirty_paths=list(state.dirty_paths),
                diagnostics=diagnostics,
            )
            if stable:
                return snapshot
        snapshot["writable"] = False
        snapshot["diagnostics"].append("The root changed while it was being read. Reload the map.")
        return snapshot

    def pending_state(self) -> dict[str, Any]:
        if not self._journal.path.exists():
            return {"pending": False}
        with self._journal.locked():
            record = self._load_record()
            return {
                "pending": bool(record and _record_pending(record)),
                **({key: record[key] for key in ("owner", "started_at", "updated_at")} if record else {}),
            }

    def owns_pending(self, session_id: str) -> bool:
        state = self.pending_state()
        return not state["pending"] or state["owner"] == _session_hash(session_id)

    def apply(
        self, operation: Mapping[str, Any], expected_revision: str, session_id: str,
    ) -> dict[str, Any]:
        if (not isinstance(operation, Mapping) or not isinstance(operation.get("type"), str)
                or operation["type"] not in _ACTIONS):
            raise PursuitStoreError("invalid_operation", "Unknown Pursuit map operation.", 422)
        _validate_request_identity(expected_revision, session_id)
        with self._journal.locked():
            record = self._editable_record(expected_revision, session_id)
            before = self._journal.read_files(record["files"])
            with self._candidate(before) as candidate:
                previous = build_graph_manifest(candidate)
                try:
                    output = apply_operation(candidate, dict(operation))
                except PursuitOperationError as exc:
                    raise PursuitStoreError("invalid_operation", str(exc), 422) from exc
                after = self._candidate_files(candidate)
                paths = {name for name in before.keys() | after.keys() if before.get(name) != after.get(name)}
                if paths != set(output.changed_paths):
                    raise PursuitStoreError("write_failed", "The editor changed files outside its declared operation.", 500)
                if any(not self._reader._is_role_write_path(name) for name in paths):
                    raise PursuitStoreError("write_failed", "The editor changed files outside its permitted role.", 500)
                self._validate_pending(candidate)
                memory_paths = {name for name in paths if name == "MEMORY.md" or MEMORY_DETAIL_FILE_RE.fullmatch(name)}
                if memory_paths:
                    current = build_graph_manifest(candidate)
                    old = SimpleNamespace(graph=previous, files=before, modes={name: "100644" for name in before})
                    new = SimpleNamespace(graph=current, files=after, modes={name: "100644" for name in after})
                    if not self._reader._is_pursuit_edge_removal(old, new, memory_paths):
                        raise PursuitStoreError("write_failed", "Pursuit edits may change Memory only to repair deleted references.", 500)
                if not paths:
                    return self._action_result(record, session_id, output, None, False)
                # The complete candidate and Git checkout bytes are checked
                # before any success becomes durable.
                self._check_edited_checkout(candidate, paths, after)
                self._fence(record)
                after_refs = self._journal.store_files(after)
                after_modes = {name: record["modes"].get(name, "100644") for name in after_refs}
                action_id = uuid.uuid4().hex
                entry = {
                    "id": action_id, "operation": dict(operation),
                    "before": {name: record["files"].get(name) for name in sorted(paths)},
                    "after": {name: after_refs.get(name) for name in sorted(paths)},
                    "before_modes": {name: record["modes"].get(name) for name in sorted(paths)},
                    "after_modes": {name: after_modes.get(name) for name in sorted(paths)},
                    "selected_id": output.selected_id, "description": output.description,
                    "id_remaps": list(output.id_remaps),
                }
                record["actions"] = record["actions"][:record["cursor"]] + [entry]
                record["cursor"] += 1
                record["files"] = after_refs
                record["modes"] = after_modes
                record["last_description"] = output.description
                self._advance_record(record)
                self._journal.save(record)
                return self._action_result(record, session_id, output, action_id, True)

    def undo(self, operation_id: str, expected_revision: str, session_id: str) -> dict[str, Any]:
        return self._history(operation_id, expected_revision, session_id, undo=True)

    def redo(self, operation_id: str, expected_revision: str, session_id: str) -> dict[str, Any]:
        return self._history(operation_id, expected_revision, session_id, undo=False)

    def _history(self, operation_id: str, expected_revision: str, session_id: str, *, undo: bool) -> dict[str, Any]:
        _validate_request_identity(expected_revision, session_id)
        if not isinstance(operation_id, str) or re.fullmatch(r"[0-9a-f]{32}", operation_id) is None:
            raise PursuitStoreError("history_forbidden", "History requires an exact action id from this editor session.", 403)
        with self._journal.locked():
            record = self._editable_record(expected_revision, session_id, history=True)
            index = record["cursor"] - 1 if undo else record["cursor"]
            if index < 0 or index >= len(record["actions"]) or record["actions"][index]["id"] != operation_id:
                raise PursuitStoreError("history_conflict", "Only the next action in this session's history can be changed.", 409)
            entry = record["actions"][index]
            expected = entry["after"] if undo else entry["before"]
            desired = entry["before"] if undo else entry["after"]
            if any(record["files"].get(name) != content for name, content in expected.items()):
                raise PursuitStoreError("history_conflict", "The saved action does not match the current map.", 409)
            files = dict(record["files"])
            for name, digest in desired.items():
                if digest is None:
                    files.pop(name, None)
                else:
                    files[name] = digest
            modes = dict(record["modes"])
            desired_modes = entry["before_modes"] if undo else entry["after_modes"]
            for name, mode in desired_modes.items():
                if mode is None:
                    modes.pop(name, None)
                else:
                    modes[name] = mode
            before = self._journal.read_files(record["files"])
            after = self._journal.read_files(files)
            with self._candidate(before) as candidate:
                previous = build_graph_manifest(candidate)
                self._write_files(candidate, after)
                self._validate_pending(candidate)
                self._check_edited_checkout(candidate, set(desired), after)
                repairs = _reference_changes(previous, build_graph_manifest(candidate))
            self._fence(record)
            record["files"] = files
            record["modes"] = modes
            record["cursor"] += -1 if undo else 1
            record["last_description"] = "pursuit: undo action" if undo else "pursuit: redo action"
            self._advance_record(record)
            self._journal.save(record)
            remaps = entry["id_remaps"]
            if undo:
                remaps = [{"from": mapping["to"], "to": mapping["from"]} for mapping in remaps]
            output = PursuitEdit(tuple(desired), tuple(repairs), None if undo else entry["selected_id"],
                                 "pursuit: undo action" if undo else "pursuit: redo action", tuple(remaps))
            return self._action_result(record, session_id, output, operation_id, True)

    def flush(self, session_id: str, expected_revision: str | None = None) -> dict[str, Any]:
        _validate_request_identity(expected_revision or "0" * 64, session_id)
        return self._flush(session_id, expected_revision)

    def flush_pending(self) -> dict[str, Any]:
        """Checkpoint the durable owner on service shutdown or a recovery timer."""
        return self._flush(None, None)

    def _flush(self, session_id: str | None, expected_revision: str | None) -> dict[str, Any]:
        if not self._journal.path.exists():
            snapshot = self._canonical_snapshot()
            if expected_revision is not None and expected_revision != snapshot["revision"]:
                raise PursuitStoreError("conflict", "The map changed. Reload before retrying.", 409)
            return self._empty_result(snapshot)
        with self._journal.locked():
            record = self._load_record()
            assert record is not None
            pending = _record_pending(record)
            if session_id is not None and record["owner"] != _session_hash(session_id):
                if pending:
                    raise PursuitStoreError("session_conflict", "Another browser session has saved edits waiting to finish. Return to that session or wait for its checkpoint.", 409)
                return self._empty_result(self._canonical_snapshot())
            if expected_revision is not None and expected_revision != record["revision"]:
                raise PursuitStoreError("conflict", "The map changed. Reload before retrying.", 409)
            if not pending:
                if record["batch_actions"] and record["base_revision"] == self._canonical_snapshot()["revision"]:
                    self._finish_checkpoint(record)
                    self._journal.save(record)
                return self._empty_result(self._snapshot_record(record, session_id))
            self._fence(record)
            before = self._journal.read_files(record["base_files"])
            after = self._journal.read_files(record["files"])

            def edit(candidate: Path, supervisor: _PursuitSupervisor) -> PursuitEdit:
                supervisor.journal_files = after
                supervisor.journal_modes = record["modes"]
                supervisor.batch_record = record
                self._write_files(candidate, after)
                supervisor.mode_only_paths = {
                    name for name in before.keys() | after.keys()
                    if before.get(name) == after.get(name) and record["base_modes"].get(name) != record["modes"].get(name)
                }
                paths = tuple(sorted({name for name in before.keys() | after.keys() if before.get(name) != after.get(name)} | supervisor.mode_only_paths))
                description = "pursuit: edit map"
                if record["batch_actions"] == 1:
                    description = record["last_description"]
                return PursuitEdit(paths, (), None, description)

            result = self._transact(edit, record["base_revision"], record["owner"], action="batch", owner=record["owner"])
            result["snapshot"] = self._snapshot_record(record, session_id)
            result["operation_id"] = None
            result["undoable"] = False
            return result

    @staticmethod
    def _empty_result(snapshot: dict[str, Any]) -> dict[str, Any]:
        return {"snapshot": snapshot, "commit": None, "operation_id": None,
                "repaired_references": [], "undoable": False, "selected_id": None, "id_remaps": []}

    def _load_record(self) -> dict[str, Any] | None:
        try:
            record = self._journal.load()
            if record and record.get("publishing"):
                commit = record["publishing"]
                with MemoryWriteLock(self.root):
                    if self._head_or_empty() == commit:
                        self._authenticate_commit(commit, owner=record["owner"])
                        state = self._repository_state()
                        if state.dirty_paths or state.diagnostics:
                            return record
                        if self._read_files() != self._journal.read_files(record["files"]):
                            raise PursuitStoreError("conflict", "The published map differs from its saved recovery state. Preserve the recovery data for review.", 409)
                        revision = self._revision(commit, state.branch)
                        if self._head_or_empty() != commit:
                            return record
                        self._finish_checkpoint(record, commit, revision)
                        self._journal.save(record)
            return record
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PursuitStoreError("recovery_failed", "Saved Pursuit edits need recovery. Preserve .runtime/pursuit-editor and review the reported problem.", 409, (str(exc),)) from exc

    def _editable_record(self, expected_revision: str, session_id: str, *, history: bool = False) -> dict[str, Any]:
        record = self._load_record()
        canonical = self._canonical_snapshot()
        owner = _session_hash(session_id)
        if record:
            pending = _record_pending(record)
            if record["owner"] != owner:
                if history:
                    raise PursuitStoreError("history_forbidden", "This action belongs to another editor session.", 403)
                if pending:
                    raise PursuitStoreError("session_conflict", "Another browser session has saved edits waiting to finish. Return to that session or wait for its checkpoint.", 409)
                record = None
            elif record["base_revision"] != canonical["revision"]:
                if pending or history:
                    raise PursuitStoreError("conflict" if pending else "history_conflict", "The root changed outside this editor. Saved edits are retained; review the current root and recovery state.", 409)
                record = None
        if record:
            if expected_revision != record["revision"]:
                raise PursuitStoreError("conflict", "The map changed. Reload before retrying.", 409)
            self._fence(record)
            return record
        if history:
            raise PursuitStoreError("history_forbidden", "This action is not in the current editor session.", 403)
        if expected_revision != canonical["revision"]:
            raise PursuitStoreError("conflict", "The map changed. Reload before retrying.", 409)
        self._require_writable(canonical)
        state = self._repository_state()
        files = self._read_files()
        # Reproduce Git's checkout conversion before acknowledging the first
        # action, without creating any action commit or modifying active files.
        with tempfile.TemporaryDirectory(prefix="pursuit-checkout-", dir=self.root / ".runtime") as temporary:
            candidate = Path(temporary)
            self._reader._run_git(self.root, "checkout-index", "--all", f"--prefix={candidate.as_posix()}/")
            converted = [name for name, content in files.items() if not (candidate / name).is_file() or (candidate / name).read_bytes() != content]
            if converted:
                raise PursuitStoreError("read_only", "Git checkout would change existing file bytes. Review line-ending and filter settings before editing.", 409, converted)
        immutable = self._reader._commit_graph_snapshot(self.root, state.head)
        if any(not _same_git_bytes(content, immutable.files.get(name)) for name, content in files.items() if name != CORRECTIONS_PATH):
            raise PursuitStoreError("read_only", "Git filters change semantic file bytes. Review filter settings before editing.", 409)
        refs = self._journal.store_files(files)
        modes = {name: immutable.modes[name] for name in refs if name in immutable.modes}
        if CORRECTIONS_PATH in refs:
            modes[CORRECTIONS_PATH] = "100644"
        now = time.time()
        return {"version": 1, "root_key": self.root_key, "owner": owner,
                "base_head": state.head, "base_revision": canonical["revision"],
                "base_files": refs, "files": refs, "base_modes": modes, "modes": modes,
                "revision": canonical["revision"],
                "batch_id": uuid.uuid4().hex, "batch_actions": 0,
                "started_at": now, "updated_at": now, "actions": [], "cursor": 0}

    @staticmethod
    def _require_writable(snapshot: dict[str, Any]) -> None:
        if snapshot["dirty_paths"]:
            raise PursuitStoreError("dirty_root", "The root has uncommitted changes. Commit or discard them before editing.", 409, snapshot["dirty_paths"])
        if not snapshot["valid"]:
            raise PursuitStoreError("invalid_root", "The root is invalid and can only be viewed.", 422, snapshot["diagnostics"])
        if not snapshot["writable"]:
            raise PursuitStoreError("read_only", "The root is not ready for a Git transaction.", 409, snapshot["diagnostics"])

    def _fence(self, record: dict[str, Any]) -> None:
        state = self._repository_state()
        if state.dirty_paths:
            raise PursuitStoreError("dirty_root", "The root changed outside this editor. Saved edits are retained until the root is clean.", 409, state.dirty_paths)
        if state.diagnostics:
            raise PursuitStoreError("read_only", "The Git root is not ready for editing.", 409, state.diagnostics)
        if state.head != record["base_head"] or self._revision(state.head, state.branch) != record["base_revision"]:
            raise PursuitStoreError("conflict", "The root changed outside this editor. Saved edits are retained; review the current root and recovery state.", 409)

    def _snapshot_record(self, record: dict[str, Any], session_id: str | None) -> dict[str, Any]:
        canonical = self._canonical_snapshot()
        pending = _record_pending(record)
        same_owner = session_id is None or record["owner"] == _session_hash(session_id)
        stable = record["base_revision"] == canonical["revision"]
        if not same_owner:
            if pending:
                canonical["writable"] = False
                canonical["pending"] = True
                if not stable:
                    canonical["error_code"] = "conflict"
                    canonical["recovery"] = True
                    canonical["diagnostics"].append("Saved Pursuit edits conflict with changes made outside the editor. Review the current root and .runtime/pursuit-editor; the saved recovery data has been preserved.")
                else:
                    canonical["error_code"] = "session_conflict"
                    canonical["diagnostics"].append("Another browser session has saved edits waiting to finish. Return to that session or wait for its checkpoint.")
            return canonical
        if not stable and not pending:
            return canonical
        with self._candidate(self._journal.read_files(record["files"])) as candidate:
            snapshot = load_pursuit_tree(candidate).to_dict()
        snapshot.update({key: canonical[key] for key in ("git_head", "root_key", "valid", "writable", "dirty_paths", "diagnostics")})
        snapshot.update(revision=record["revision"], pending=pending,
                        history={"undo": [entry["id"] for entry in record["actions"][:record["cursor"]]],
                                 "redo": [entry["id"] for entry in reversed(record["actions"][record["cursor"]:])]})
        if not stable:
            snapshot["writable"] = False
            snapshot["error_code"] = "conflict"
            snapshot["recovery"] = True
            snapshot["diagnostics"].append("The root changed outside this editor. Your saved edits are retained. Review the current root and .runtime/pursuit-editor before resolving the conflict.")
        return snapshot

    def _action_result(self, record: dict[str, Any], session_id: str, output: PursuitEdit,
                       action_id: str | None, undoable: bool) -> dict[str, Any]:
        return {"snapshot": self._snapshot_record(record, session_id), "commit": None,
                "operation_id": action_id, "repaired_references": list(output.repaired_references),
                "undoable": undoable, "selected_id": output.selected_id, "id_remaps": list(output.id_remaps)}

    @staticmethod
    def _advance_record(record: dict[str, Any]) -> None:
        now = time.time()
        if record["batch_actions"] == 0:
            record["started_at"] = now
        record["updated_at"] = now
        record["batch_actions"] += 1
        if not _record_pending(record):
            record["batch_actions"] = 0
        record["revision"] = hashlib.sha256(uuid.uuid4().bytes).hexdigest()

    def _finish_checkpoint(self, record: dict[str, Any], head: str | None = None, revision: str | None = None) -> None:
        record.update(base_head=head or record["base_head"], base_revision=revision or record["base_revision"],
                      base_files=dict(record["files"]), base_modes=dict(record["modes"]), batch_id=uuid.uuid4().hex, batch_actions=0)
        record.pop("publishing", None)

    def _check_edited_checkout(self, candidate: Path, paths: set[str], files: dict[str, bytes]) -> None:
        objects = {
            name: self._reader._git_stdout(self.root, "hash-object", "-w", f"--path={name}", str(candidate / name))
            for name in sorted(paths) if name in files
        }
        blobs = self._reader._read_git_blobs(self.root, objects.values())
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "true"}
        for name, object_id in objects.items():
            if not _same_git_bytes(files[name], blobs[object_id]):
                raise PursuitStoreError("read_only", "Git filters change the edit's semantic bytes. Review filter settings before editing.", 409, (name,))
            try:
                checkout = subprocess.run(
                    ["git", "cat-file", "--filters", f"--path={name}", object_id], cwd=self.root,
                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    env=env, timeout=GIT_TIMEOUT_SECONDS,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise PursuitStoreError("read_only", "Git could not verify the edit's checkout bytes.", 409, (name,)) from exc
            if checkout.returncode or checkout.stdout != files[name]:
                raise PursuitStoreError("read_only", "Git checkout would change the edit's file bytes. Review line-ending and filter settings before editing.", 409, (name,))

    def _read_files(self) -> dict[str, bytes]:
        return {name: (self.root / name).read_bytes() for name in self._semantic_names() if (self.root / name).is_file()}

    @staticmethod
    def _candidate_files(candidate: Path) -> dict[str, bytes]:
        return {path.name: path.read_bytes() for path in candidate.iterdir() if path.is_file()}

    @staticmethod
    def _write_files(candidate: Path, files: dict[str, bytes]) -> None:
        for path in candidate.iterdir():
            if path.is_file() and (path.name in {"MEMORY.md", "PURSUITS.md", CORRECTIONS_PATH}
                                  or MEMORY_DETAIL_FILE_RE.fullmatch(path.name) or PURSUIT_DETAIL_FILE_RE.fullmatch(path.name)) and path.name not in files:
                path.unlink()
        for name, content in files.items():
            _require_relative_path(name)
            (candidate / name).write_bytes(content)

    @contextmanager
    def _candidate(self, files: dict[str, bytes]) -> Iterator[Path]:
        with tempfile.TemporaryDirectory(prefix="pursuit-edit-", dir=self.root / ".runtime") as temporary:
            candidate = Path(temporary)
            self._write_files(candidate, files)
            yield candidate

    @staticmethod
    def _validate_pending(candidate: Path) -> None:
        errors = _validation_errors(candidate)
        if errors:
            raise PursuitStoreError("invalid_operation", "The edit would invalidate the graph.", 422, errors)

    def _transact(
        self,
        edit: Callable[[Path, _PursuitSupervisor], PursuitEdit],
        expected_revision: str,
        session_id: str,
        *,
        action: str,
        target: str = "-",
        owner: str | None = None,
    ) -> dict[str, Any]:
        _validate_request_identity(expected_revision, session_id)
        # Mutations need the base fence and validation, not a second projected
        # tree. The shared supervisor repeats this fence before publication.
        base = self._repository_state()
        if self._revision(base.head, base.branch) != expected_revision:
            raise PursuitStoreError("conflict", "The map changed. Reload it before retrying your edit.", 409)
        if base.dirty_paths:
            raise PursuitStoreError("dirty_root", "The root has uncommitted changes. Commit or discard them before editing.", 409, base.dirty_paths)
        try:
            errors = _validation_errors(self.root)
        except (OSError, ValueError) as exc:
            errors = [str(exc)]
        if errors:
            raise PursuitStoreError("invalid_root", "The root is invalid and can only be viewed.", 422, errors)
        if not base.head or base.diagnostics:
            raise PursuitStoreError("read_only", "The root is not ready for a Git transaction.", 409, base.diagnostics)
        baseline_files = {
            name: (self.root / name).read_bytes()
            for name in self._semantic_names() if (self.root / name).is_file()
        }

        operation_id = uuid.uuid4().hex
        supervisor = _PursuitSupervisor(
            self,
            expected_revision,
            base.head,
            session_id,
            owner=owner or _session_hash(session_id),
        )

        def prepare(candidate: Path) -> PursuitEdit:
            supervisor.candidate_root = candidate
            converted = [
                name for name, content in baseline_files.items()
                if not (candidate / name).is_file() or (candidate / name).read_bytes() != content
            ]
            if converted:
                raise PursuitStoreError(
                    "read_only",
                    "Git checkout would change existing file bytes. Review line-ending and filter settings before editing.",
                    409, converted,
                )
            result = edit(candidate, supervisor)
            actual_paths = set(_status_paths(supervisor, candidate))
            declared_paths = set(result.changed_paths)
            if actual_paths | supervisor.mode_only_paths != declared_paths:
                raise PursuitStoreError("write_failed", "The editor changed files outside its declared operation.", 500)
            if not declared_paths:
                return result
            for path in declared_paths:
                _require_relative_path(path)
            intended_files = {
                path: (candidate / path).read_bytes()
                for path in declared_paths if (candidate / path).is_file()
            }
            supervisor._run_git(candidate, "add", "-A", "--", *sorted(declared_paths))
            if supervisor.journal_modes is not None:
                for mode, flag in (("100755", "+x"), ("100644", "-x")):
                    names = sorted(name for name in declared_paths if supervisor.journal_modes.get(name) == mode)
                    if names:
                        supervisor._run_git(candidate, "update-index", f"--chmod={flag}", "--", *names)
            if supervisor._run_git(candidate, "diff", "--cached", "--quiet", check=False).returncode == 0:
                return result
            parent = supervisor._git_stdout(candidate, "rev-parse", "HEAD")
            tree = supervisor._git_stdout(candidate, "write-tree")
            metadata = {
                "editor": "1",
                "operation": operation_id,
                "session": owner or _session_hash(session_id),
                "action": action,
                "target": target,
                "id_remaps": _encode_id_remaps(result.id_remaps),
            }
            metadata["signature"] = self._signature(metadata, parent, tree, session_id)
            subject = result.description.strip().splitlines()[0] if result.description.strip() else f"pursuit: {action}"
            message = subject + "\n\n" + "\n".join(
                f"{name}: {metadata[key]}" for key, name in _TRAILERS.items()
            )
            # A hook could add changes after validation or during publication.
            # A linked worktree's .git is a file, so this hook directory cannot
            # contain executable hooks. Do not change the user's Git config.
            supervisor._run_git(
                candidate, "-c", f"core.hooksPath={candidate / '.git' / 'hooks'}",
                "commit", "-m", message,
            )
            if intended_files:
                # Exercise the same index checkout conversion before touching
                # active files. A valid blob alone does not prove that Git's
                # CRLF or custom filters preserve the intended physical bytes.
                supervisor._run_git(candidate, "checkout-index", "--force", "--", *sorted(intended_files))
                converted = [
                    path for path, content in intended_files.items()
                    if (candidate / path).read_bytes() != content
                ]
                if converted:
                    raise PursuitStoreError(
                        "read_only",
                        "Git checkout would change the edit's file bytes. Review line-ending and filter settings before editing.",
                        409, converted,
                    )
            return result

        try:
            result = supervisor.run(prepare)
        except PursuitStoreError:
            raise
        except MainMemoryDirtyError as exc:
            raise PursuitStoreError("dirty_root", "The root changed during the edit. No editor changes were published.", 409, exc.paths) from exc
        except (RuntimeError, OSError, ValueError) as exc:
            message = str(exc)
            code = "invalid_operation" if message.startswith("validation failed:") else "write_failed"
            raise PursuitStoreError(code, message, 422 if code == "invalid_operation" else 500) from exc
        output: PursuitEdit = result.output
        return {
            "snapshot": supervisor.landed_snapshot or self.snapshot(),
            "commit": result.landed_commit if result.commits_landed else None,
            "operation_id": operation_id,
            "repaired_references": list(output.repaired_references),
            "undoable": bool(result.commits_landed),
            "selected_id": output.selected_id,
            "id_remaps": list(output.id_remaps),
        }

    def _authenticate_commit(self, commit: str, session_id: str | None = None, *, owner: str | None = None) -> dict[str, str]:
        result = self._reader._run_git(
            self.root, "show", "--no-patch", "--format=%H%x00%P%x00%T%x00%B", commit, check=False,
        )
        try:
            resolved, parent, tree, message = result.stdout.split("\0", 3)
            if result.returncode or resolved != commit:
                raise ValueError("unresolved commit")
            return self._authenticate_record(resolved, parent, tree, message, session_id, owner=owner)
        except ValueError as exc:
            raise PursuitStoreError("history_forbidden", "This is not an authenticated commit from this editor session.", 403) from exc

    def _authenticate_record(
        self, commit: str, parent: str, tree: str, message: str, session_id: str | None, *, owner: str | None = None,
    ) -> dict[str, str]:
        try:
            if _COMMIT_RE.fullmatch(commit) is None or _COMMIT_RE.fullmatch(parent) is None:
                raise ValueError("not a single-parent commit")
            values: dict[str, str] = {}
            trailer_names = {value: key for key, value in _TRAILERS.items()}
            for line in message.strip().rsplit("\n\n", 1)[-1].splitlines():
                name, separator, value = line.partition(": ")
                if separator and name in trailer_names:
                    key = trailer_names[name]
                    if key in values:
                        raise ValueError("duplicate trailer")
                    values[key] = value
            if set(values) != set(_TRAILERS):
                raise ValueError("missing editor trailers")
            if values["editor"] != "1" or values["session"] != (owner or _session_hash(session_id or "")):
                raise ValueError("different editor session")
            if values["action"] != "batch":
                raise ValueError("invalid editor operation")
            if re.fullmatch(r"[0-9a-f]{32}", values["operation"]) is None:
                raise ValueError("invalid operation identity")
            if _DIGEST_RE.fullmatch(values["signature"]) is None:
                raise ValueError("invalid signature encoding")
            _decode_id_remaps(values["id_remaps"])
            if values["target"] != "-":
                raise ValueError("invalid checkpoint target")
            expected = self._signature(values, parent, tree, session_id or "")
            if not hmac.compare_digest(values["signature"], expected):
                raise ValueError("invalid editor signature")
            return values
        except (ValueError, KeyError) as exc:
            raise PursuitStoreError("history_forbidden", "This is not an authenticated commit from this editor session.", 403) from exc

    def _signature(self, metadata: Mapping[str, str], parent: str, tree: str, session_id: str) -> str:
        payload = {
            **{key: value for key, value in metadata.items() if key != "signature"},
            "root": self.root_key,
            "parent": parent,
            "tree": tree,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hmac.new(self._journal.key(), encoded, hashlib.sha256).hexdigest()

    def _head_or_empty(self) -> str:
        result = self._reader._run_git(self.root, "rev-parse", "--verify", "HEAD", check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _repository_state(self) -> _RepositoryState:
        result = self._reader._run_git(
            self.root, "rev-parse", "--show-toplevel", "--absolute-git-dir", "HEAD", check=False,
        )
        fields = result.stdout.strip().splitlines()
        if result.returncode or len(fields) != 3 or Path(fields[0]).resolve() != self.root:
            return _RepositoryState(diagnostics=("The memory root must be a Git repository root with an initial commit.",))
        git_dir = Path(fields[1])
        head = fields[2]
        branch = self._reader._run_git(self.root, "symbolic-ref", "--quiet", "HEAD", check=False)
        diagnostics = []
        if branch.returncode:
            diagnostics.append("The memory root has a detached HEAD. Check out its active branch before editing.")
        if any((git_dir / name).exists() for name in (
            "MERGE_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "rebase-merge", "rebase-apply", "sequencer",
        )):
            diagnostics.append("Finish the existing Git merge, rebase, or history operation before editing.")
        try:
            dirty = set(_status_paths(self._reader, self.root))
            indexed = self._reader._run_git(self.root, "ls-files", "-v", "-z").stdout
            tracked: set[str] = set()
            hidden: list[str] = []
            for entry in indexed.split("\0"):
                if not entry:
                    continue
                tracked.add(entry[2:])
                if entry[0].islower() or entry[0] == "S":
                    hidden.append(entry[2:])
            if hidden:
                diagnostics.append("Git index flags hide file changes: " + ", ".join(sorted(hidden)))
            # Git may silently overwrite an ignored, untracked backing file
            # during checkout. Such files are real semantic data, not runtime
            # artifacts, and must be admitted to Git before editor publication.
            for name in self._semantic_names():
                if name not in tracked and (self.root / name).exists():
                    dirty.add(name)
        except RuntimeError as exc:
            dirty = set()
            diagnostics.append(str(exc))
        return _RepositoryState(head, branch.stdout.strip(), tuple(sorted(dirty)), tuple(diagnostics))

    def _revision(self, head: str, branch: str | None = None) -> str:
        digest = hashlib.sha256()
        digest.update(self.root_key.encode("ascii") + b"\0")
        digest.update(head.encode("ascii", errors="replace") + b"\0")
        if branch is None:
            result = self._reader._run_git(self.root, "symbolic-ref", "--quiet", "HEAD", check=False)
            branch = result.stdout.strip() if result.returncode == 0 else ""
        digest.update(branch.encode("utf-8") + b"\0")
        for name in sorted(self._semantic_names()):
            digest.update(name.encode("utf-8") + b"\0")
            path = self.root / name
            try:
                file_stat = path.lstat()
                if stat.S_ISLNK(file_stat.st_mode):
                    content = b"link\0" + os.readlink(path).encode("utf-8")
                elif stat.S_ISREG(file_stat.st_mode):
                    content = b"file\0" + path.read_bytes()
                else:
                    content = b"non-regular\0"
            except FileNotFoundError:
                content = b"missing\0"
            except OSError as exc:
                content = f"unreadable\0{exc.errno}".encode("ascii")
            digest.update(hashlib.sha256(content).digest())
        return digest.hexdigest()

    def _semantic_names(self) -> set[str]:
        names = {"MEMORY.md", "PURSUITS.md", CORRECTIONS_PATH}
        try:
            names.update(path.name for path in self.root.iterdir() if (
                MEMORY_DETAIL_FILE_RE.fullmatch(path.name) or PURSUIT_DETAIL_FILE_RE.fullmatch(path.name)
            ))
        except OSError:
            pass  # Required missing/unreadable roots still enter the revision.
        return names


class _PursuitSupervisor(IsolatedWriteSupervisor):
    """Keep the shared candidate lifecycle, but never rebase an editor action."""

    def __init__(
        self,
        store: PursuitStore,
        expected_revision: str,
        expected_head: str,
        session_id: str,
        *,
        owner: str,
    ):
        super().__init__(store.root, EDITOR_ROLE)
        self.store = store
        self.expected_revision = expected_revision
        self.expected_head = expected_head
        self.session_id = session_id
        self.owner = owner
        self.journal_files: dict[str, bytes] | None = None
        self.journal_modes: dict[str, str] | None = None
        self.mode_only_paths: set[str] = set()
        self.batch_record: dict[str, Any] | None = None
        self.candidate_root: Path | None = None
        self.landed_snapshot: dict[str, Any] | None = None

    def _dirty_memory_files(self) -> list[str]:
        state = self.store._repository_state()
        if state.dirty_paths:
            return list(state.dirty_paths)
        if state.diagnostics:
            raise PursuitStoreError("read_only", "The Git root is not ready for this edit.", 409, state.diagnostics)
        if state.head != self.expected_head or self.store._revision(state.head, state.branch) != self.expected_revision:
            raise PursuitStoreError("conflict", "The root changed during the edit. No editor changes were published.", 409)
        return []

    def _validate_candidate(self, worktree: Path, commits: list[str], **kwargs: Any) -> None:
        if len(commits) != 1:
            raise PursuitStoreError("write_failed", "A nonempty map checkpoint must produce exactly one commit.", 500)
        parent = self._git_stdout(worktree, "show", "--no-patch", "--format=%P", commits[0])
        if parent != self.expected_head:
            raise PursuitStoreError("conflict", "The editor candidate no longer has its expected base.", 409)
        super()._validate_candidate(worktree, commits, **kwargs)
        immutable_graph = self._commit_graph_snapshot(worktree, commits[0]).graph
        if immutable_graph.errors:
            raise PursuitStoreError(
                "invalid_operation", "The committed candidate is not a valid graph.", 422, immutable_graph.errors,
            )
        self.store._authenticate_commit(commits[0], owner=self.owner)

    def _validate_pursuit_memory_changes(self, worktree: Path, commit: str, changed_paths: set[str]) -> None:
        # Every action was independently fenced against non-reference Memory
        # edits before its exact bytes entered the authenticated journal. Undo
        # uses only those saved inverses; the final commit must match them exactly.
        if self.journal_files is None or self.batch_record is None:
            raise PursuitStoreError("write_failed", "Missing authenticated batch state.", 500)
        immutable = self._commit_graph_snapshot(worktree, commit)
        for name in changed_paths:
            path = worktree / name
            intended = self.journal_files.get(name)
            actual = path.read_bytes() if path.is_file() else None
            if (actual != intended or not _same_git_bytes(intended, immutable.files.get(name))
                    or immutable.modes.get(name) != (self.journal_modes or {}).get(name)):
                raise PursuitStoreError("write_failed", "The candidate differs from the saved editor actions.", 500)

    def _land_commits(self, commits: list[str]) -> None:
        if len(commits) != 1 or self.candidate_root is None:
            raise PursuitStoreError("write_failed", "Missing validated editor candidate.", 500)
        # The shared supervisor holds MemoryWriteLock here. Repeat the HEAD and
        # byte fence immediately before publishing the exact validated commit.
        dirty = self._dirty_memory_files()
        if dirty:
            raise MainMemoryDirtyError(dirty)
        if self.batch_record is None:
            raise PursuitStoreError("write_failed", "Missing durable checkpoint recovery state.", 500)
        self.batch_record["publishing"] = commits[0]
        self.store._journal.save(self.batch_record)
        result = self._run_git(
            self.memory_root, "-c", f"core.hooksPath={self.candidate_root / '.git' / 'hooks'}",
            "merge", "--ff-only", commits[0], check=False,
        )
        if result.returncode:
            raise PursuitStoreError("conflict", "The validated edit could not be fast-forwarded. Reload the map.", 409)
        if self._git_stdout(self.memory_root, "rev-parse", "HEAD") != commits[0]:
            raise PursuitStoreError("write_failed", "The published HEAD does not match the validated editor commit.", 500)
        if _status_paths(self, self.memory_root):
            raise PursuitStoreError("write_failed", "The root changed immediately after editor publication.", 500)
        if self.store._read_files() != self.journal_files:
            raise PursuitStoreError("conflict", "The published root differs from the saved editor bytes. Saved recovery data is retained for review.", 409)
        self.landed_snapshot = self.store.snapshot()
        # Finalize while the shared MemoryWriteLock still protects publication.
        # Never adopt a later writer's HEAD as the base of these saved bytes.
        if self.landed_snapshot["git_head"] != commits[0]:
            raise PursuitStoreError("conflict", "The root advanced after checkpoint publication. Saved recovery data is retained.", 409)
        self.store._finish_checkpoint(self.batch_record, commits[0], self.landed_snapshot["revision"])
        self.store._journal.save(self.batch_record)


def _validate_request_identity(expected_revision: str, session_id: str) -> None:
    if not isinstance(expected_revision, str) or _DIGEST_RE.fullmatch(expected_revision) is None:
        raise PursuitStoreError("invalid_operation", "A snapshot revision is required.", 422)
    if not isinstance(session_id, str) or not session_id or len(session_id) > 1024 or any(
        character in session_id for character in "\0\r\n"
    ):
        raise PursuitStoreError("history_forbidden", "An authenticated editor session is required.", 403)


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _encode_id_remaps(remaps: Iterable[Mapping[str, str]]) -> str:
    return json.dumps(list(remaps), ensure_ascii=True, separators=(",", ":"))


def _decode_id_remaps(encoded: str) -> tuple[dict[str, str], ...]:
    value = json.loads(encoded)
    if not isinstance(value, list):
        raise ValueError("invalid id remaps")
    result: list[dict[str, str]] = []
    sources: set[str] = set()
    for mapping in value:
        if not isinstance(mapping, dict) or set(mapping) != {"from", "to"}:
            raise ValueError("invalid id remap")
        source = mapping["from"]
        target = mapping["to"]
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or not source
            or not target
            or source == target
            or source in sources
        ):
            raise ValueError("invalid id remap")
        sources.add(source)
        result.append({"from": source, "to": target})
    return tuple(result)


def _validation_errors(root: Path) -> list[str]:
    validation = MemoryTools(root, role=EDITOR_ROLE).validate_memory(enforce_correction_capacity=False)
    if validation.startswith("validation failed:"):
        return [line.removeprefix("- ") for line in validation.splitlines()[1:]]
    return []


def _status_paths(supervisor: IsolatedWriteSupervisor, root: Path) -> list[str]:
    output = supervisor._run_git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    tokens = output.split("\0")
    paths: set[str] = set()
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if not entry:
            continue
        if len(entry) < 4:
            raise RuntimeError("could not read Git worktree status")
        paths.add(entry[3:])
        if "R" in entry[:2] or "C" in entry[:2]:
            if index >= len(tokens) or not tokens[index]:
                raise RuntimeError("could not read Git rename status")
            paths.add(tokens[index])
            index += 1
    return sorted(paths)


def _require_relative_path(path: str) -> None:
    if not isinstance(path, str) or not path or Path(path).is_absolute() or any(
        part in {"", ".", ".."} for part in path.replace("\\", "/").split("/")
    ):
        raise PursuitStoreError("write_failed", "The editor produced an unsafe file path.", 500)


def _reference_changes(before: GraphManifest, after: GraphManifest) -> list[dict[str, Any]]:
    repaired: list[dict[str, Any]] = []
    for source_id in sorted(set(before.items) & set(after.items)):
        original, current = before.items[source_id], after.items[source_id]
        for action, edges in (
            ("removed", [edge for edge in original.edges if edge not in current.edges]),
            ("restored", [edge for edge in current.edges if edge not in original.edges]),
        ):
            for edge_type, target_id in edges:
                repaired.append({
                    "source_id": source_id,
                    "source_path": current.file.relative_to(after.root).as_posix(),
                    "kind": "edge", "target_id": target_id, "edge_type": edge_type, "action": action,
                })
    original_focus = {item_id for item_id, _path, _line in before.focus_ids}
    current_focus = {item_id for item_id, _path, _line in after.focus_ids}
    for action, ids in (("removed", original_focus - current_focus), ("restored", current_focus - original_focus)):
        for item_id in sorted(ids):
            repaired.append({"source_path": "PURSUITS.md", "kind": "focus", "target_id": item_id, "action": action})
    return repaired


def _same_git_bytes(physical: bytes | None, blob: bytes | None) -> bool:
    # Git's standard CRLF normalization is the sole accepted byte conversion.
    # A reversible custom filter must not smuggle different graph semantics into
    # the immutable checkpoint while returning the intended working-tree bytes.
    return physical == blob or (physical is not None and physical.replace(b"\r\n", b"\n") == blob)


def _record_pending(record: dict[str, Any]) -> bool:
    return record["files"] != record["base_files"] or record["modes"] != record["base_modes"]

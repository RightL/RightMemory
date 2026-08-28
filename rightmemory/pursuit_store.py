"""Validated Git transactions for the human-owned Pursuit map.

The browser owns its transient undo stack. Git owns the map and its history;
there is no saved editor draft or second map database here.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .graph import (
    MEMORY_DETAIL_FILE_RE,
    PURSUIT_DETAIL_FILE_RE,
    GraphManifest,
    build_graph_manifest,
)
from .isolated_write import IsolatedWriteSupervisor, MainMemoryDirtyError, OPERATION_TRAILER
from .pursuit_tree import PursuitEdit, PursuitOperationError, apply_operation, load_pursuit_tree
from .tools import CORRECTIONS_PATH, MemoryTools


EDITOR_ROLE = "pursuit-map"
_ACTIONS = frozenset({"create", "rename", "move", "delete", "edit_body", "set_focus"})
_HISTORY_ACTIONS = frozenset({"undo", "redo"})
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_DIGEST_RE = re.compile(r"[0-9a-f]{64}\Z")
_TRAILERS = {
    "editor": "RightMemory-Pursuit-Editor",
    "operation": OPERATION_TRAILER,
    "session": "RightMemory-Pursuit-Session",
    "action": "RightMemory-Pursuit-Action",
    "target": "RightMemory-Pursuit-Target",
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

    def snapshot(self) -> dict[str, Any]:
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

    def apply(
        self,
        operation: Mapping[str, Any],
        expected_revision: str,
        session_id: str,
    ) -> dict[str, Any]:
        if (
            not isinstance(operation, Mapping)
            or not isinstance(operation.get("type"), str)
            or operation["type"] not in _ACTIONS
        ):
            raise PursuitStoreError("invalid_operation", "Unknown Pursuit map operation.", 422)
        operation = dict(operation)
        action = operation["type"]

        def edit(candidate: Path, supervisor: _PursuitSupervisor) -> PursuitEdit:
            try:
                return apply_operation(candidate, operation)
            except PursuitOperationError as exc:
                raise PursuitStoreError("invalid_operation", str(exc), 422) from exc

        return self._transact(edit, expected_revision, session_id, action=action)

    def undo(self, commit: str, expected_revision: str, session_id: str) -> dict[str, Any]:
        return self._revert(commit, expected_revision, session_id, action="undo")

    def redo(self, commit: str, expected_revision: str, session_id: str) -> dict[str, Any]:
        return self._revert(commit, expected_revision, session_id, action="redo")

    def _revert(
        self,
        commit: str,
        expected_revision: str,
        session_id: str,
        *,
        action: str,
    ) -> dict[str, Any]:
        if not isinstance(commit, str) or _COMMIT_RE.fullmatch(commit) is None:
            raise PursuitStoreError("history_forbidden", "History requires an exact editor commit id.", 403)

        def edit(candidate: Path, supervisor: _PursuitSupervisor) -> PursuitEdit:
            self._verify_history(commit, supervisor.expected_head, session_id, action)
            before = build_graph_manifest(candidate)
            result = supervisor._run_git(candidate, "revert", "--no-commit", commit, check=False)
            if result.returncode:
                raise PursuitStoreError(
                    "history_conflict", "This history change conflicts with the current map. Reload the map.", 409,
                )
            after = build_graph_manifest(candidate)
            if after.errors:
                raise PursuitStoreError("history_conflict", "This history change would invalidate the graph.", 409, after.errors)
            paths = _status_paths(supervisor, candidate)
            return PursuitEdit(
                changed_paths=tuple(paths),
                repaired_references=tuple(_reference_changes(before, after)),
                selected_id=None,
                description=f"pursuit: {action} {commit[:12]}",
            )

        return self._transact(
            edit,
            expected_revision,
            session_id,
            action=action,
            target=commit,
        )

    def _transact(
        self,
        edit: Callable[[Path, _PursuitSupervisor], PursuitEdit],
        expected_revision: str,
        session_id: str,
        *,
        action: str,
        target: str = "-",
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
            # The role fence independently proves a historical deletion before
            # allowing its exact inverse to restore any Memory edge bytes.
            pursuit_restore_commit=target if action in _HISTORY_ACTIONS else None,
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
            if actual_paths != declared_paths:
                raise PursuitStoreError("write_failed", "The editor changed files outside its declared operation.", 500)
            if not actual_paths:
                return result
            for path in declared_paths:
                _require_relative_path(path)
            intended_files = {
                path: (candidate / path).read_bytes()
                for path in declared_paths if (candidate / path).is_file()
            }
            supervisor._run_git(candidate, "add", "-A", "--", *sorted(declared_paths))
            if supervisor._run_git(candidate, "diff", "--cached", "--quiet", check=False).returncode == 0:
                return result
            parent = supervisor._git_stdout(candidate, "rev-parse", "HEAD")
            tree = supervisor._git_stdout(candidate, "write-tree")
            metadata = {
                "editor": "1",
                "operation": operation_id,
                "session": _session_hash(session_id),
                "action": action,
                "target": target,
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
        }

    def _verify_history(self, commit: str, head: str, session_id: str, action: str) -> None:
        metadata = self._authenticate_commit(commit, session_id)
        permitted = _ACTIONS | {"redo"} if action == "undo" else {"undo"}
        if metadata["action"] not in permitted:
            raise PursuitStoreError("history_forbidden", "This commit cannot be used for that history operation.", 403)
        if not self._reader._is_ancestor(commit, head):
            raise PursuitStoreError("history_forbidden", "This editor commit is not in the active history.", 403)
        later = self._reader._git_stdout(
            self.root, "log", "--first-parent", "--format=%H%x00%P%x00%T%x00%B%x00",
            f"{commit}..{head}",
        ).split("\0")
        for index in range(0, len(later) - 1, 4):
            try:
                current = self._authenticate_record(
                    later[index].strip(), later[index + 1], later[index + 2], later[index + 3], session_id,
                )
            except PursuitStoreError as exc:
                raise PursuitStoreError("history_conflict", "History changed outside this editor session. Reload the map.", 409) from exc
            if current["target"] == commit:
                raise PursuitStoreError("history_conflict", "This history change has already been applied.", 409)

    def _authenticate_commit(self, commit: str, session_id: str) -> dict[str, str]:
        result = self._reader._run_git(
            self.root, "show", "--no-patch", "--format=%H%x00%P%x00%T%x00%B", commit, check=False,
        )
        try:
            resolved, parent, tree, message = result.stdout.split("\0", 3)
            if result.returncode or resolved != commit:
                raise ValueError("unresolved commit")
            return self._authenticate_record(resolved, parent, tree, message, session_id)
        except ValueError as exc:
            raise PursuitStoreError("history_forbidden", "This is not an authenticated commit from this editor session.", 403) from exc

    def _authenticate_record(
        self, commit: str, parent: str, tree: str, message: str, session_id: str,
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
            if values["editor"] != "1" or values["session"] != _session_hash(session_id):
                raise ValueError("different editor session")
            if values["action"] not in _ACTIONS | _HISTORY_ACTIONS:
                raise ValueError("invalid editor operation")
            if re.fullmatch(r"[0-9a-f]{32}", values["operation"]) is None:
                raise ValueError("invalid operation identity")
            if _DIGEST_RE.fullmatch(values["signature"]) is None:
                raise ValueError("invalid signature encoding")
            if (values["action"] in _HISTORY_ACTIONS and _COMMIT_RE.fullmatch(values["target"]) is None) or (
                values["action"] in _ACTIONS and values["target"] != "-"
            ):
                raise ValueError("invalid history target")
            expected = self._signature(values, parent, tree, session_id)
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
        return hmac.new(session_id.encode("utf-8"), encoded, hashlib.sha256).hexdigest()

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
        pursuit_restore_commit: str | None = None,
    ):
        super().__init__(store.root, EDITOR_ROLE, pursuit_restore_commit=pursuit_restore_commit)
        self.store = store
        self.expected_revision = expected_revision
        self.expected_head = expected_head
        self.session_id = session_id
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
            raise PursuitStoreError("write_failed", "A map interaction must produce exactly one commit.", 500)
        parent = self._git_stdout(worktree, "show", "--no-patch", "--format=%P", commits[0])
        if parent != self.expected_head:
            raise PursuitStoreError("conflict", "The editor candidate no longer has its expected base.", 409)
        super()._validate_candidate(worktree, commits, **kwargs)
        immutable_graph = self._commit_graph_snapshot(worktree, commits[0]).graph
        if immutable_graph.errors:
            raise PursuitStoreError(
                "invalid_operation", "The committed candidate is not a valid graph.", 422, immutable_graph.errors,
            )
        self.store._authenticate_commit(commits[0], self.session_id)

    def _land_commits(self, commits: list[str]) -> None:
        if len(commits) != 1 or self.candidate_root is None:
            raise PursuitStoreError("write_failed", "Missing validated editor candidate.", 500)
        # The shared supervisor holds MemoryWriteLock here. Repeat the HEAD and
        # byte fence immediately before publishing the exact validated commit.
        dirty = self._dirty_memory_files()
        if dirty:
            raise MainMemoryDirtyError(dirty)
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
        self.landed_snapshot = self.store.snapshot()


def _validate_request_identity(expected_revision: str, session_id: str) -> None:
    if not isinstance(expected_revision, str) or _DIGEST_RE.fullmatch(expected_revision) is None:
        raise PursuitStoreError("invalid_operation", "A snapshot revision is required.", 422)
    if not isinstance(session_id, str) or not session_id or len(session_id) > 1024 or any(
        character in session_id for character in "\0\r\n"
    ):
        raise PursuitStoreError("history_forbidden", "An authenticated editor session is required.", 403)


def _session_hash(session_id: str) -> str:
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


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

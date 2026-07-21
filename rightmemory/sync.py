from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import SyncConfig
from .isolated_write import WorktreeLease
from .semantic_operation import FINAL_PHASES, OperationEffect, SemanticOperationRecord, SemanticOperationStore
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory
from .tools import MemoryTools
from .update_queue import validate_update_queue


MEMORY_SYNC_PATHS = (
    "MEMORY.md",
    "MEMORY_*.md",
    "PURSUITS.md",
    "PURSUIT_*.md",
    "PURSUIT_RULES.md",
    "corrections.md",
    "shared_views.toml",
    "shares.toml",
    "shared_views/*/view.md",
    "shared_views/*/retriever.md",
    "shared_views/*/recipe.toml",
    "shared_views/*/question.toml",
    "shared_views/*/.gitignore",
    "insight_logs/*.md",
    "update_queue/candidates/*.json",
    "update_queue/recovery/*.json",
    "update_queue/lease.json",
)
REQUIRED_ROOT_DOCUMENTS = ("MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md")
GIT_TIMEOUT_SECONDS = 30
SYNC_BRANCH_PREFIX = "rightmemory-sync-"
SYNC_REPAIR_POLICY_VERSION = "staged-sync-repair-v1"
UPDATE_QUEUE_CANDIDATE_PATH_RE = re.compile(r"update_queue/candidates/[0-9a-f]{32}\.json")
UPDATE_QUEUE_RECOVERY_PATH_RE = re.compile(r"update_queue/recovery/update-batch-[0-9a-f]{64}\.json")


@dataclass(frozen=True)
class SyncResult:
    status: str
    message: str
    files: list[str] = field(default_factory=list)
    operation_id: str | None = None

    def context_block(self) -> str:
        files = ", ".join(self.files) if self.files else "none"
        return "\n".join(
            (
                "<rightmemory_sync>",
                "authority: This Runtime sync context is authoritative for the current caller message. "
                "Ignore older Runtime sync context blocks from prior message history.",
                f"status: {self.status}",
                f"message: {self.message}",
                f"files: {files}",
                "</rightmemory_sync>",
            )
        )


@dataclass(frozen=True)
class SyncRepairOutcome:
    output: str
    effects: tuple[OperationEffect, ...] = ()


SyncRepair = Callable[[Path, SyncResult, str], str | SyncRepairOutcome]


@dataclass
class _Candidate:
    branch: str
    path: Path
    lease: WorktreeLease
    removed: bool = False


@dataclass(frozen=True)
class _CandidateInspection:
    repair: SyncResult | None
    fatal: SyncResult | None
    pre_repair_tip: str
    expected_merge_parent: str | None
    merge_conflicted: bool


class SyncManager:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.memory_root = Path(config.memory_root).resolve()
        self.state_path = self.memory_root / ".runtime" / "sync" / "state.json"

    def pull(self, repair: SyncRepair | None = None) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")

        upstream = self._upstream()
        if upstream is None:
            return self._record_failure(SyncResult("unconfigured", "sync unconfigured"))
        fetch = self._git("fetch")
        if fetch.returncode != 0:
            return self._record_failure(SyncResult("offline", "sync offline: git fetch failed"))
        upstream_commit = self._resolve_commit(upstream)
        if upstream_commit is None:
            return self._record_failure(SyncResult("error", "sync failed: upstream commit is unavailable"))

        store = SemanticOperationStore(self.memory_root)
        try:
            # This matches automatic-write lock order: operation execution, then active memory.
            with store.execution_locked(), MemoryWriteLock(self.memory_root):
                result = self._pull_locked(upstream_commit, repair, store)
        except Exception as exc:
            return self._record_failure(SyncResult("error", f"sync failed: {type(exc).__name__}: {exc}"))
        if result.status == "synced":
            return self._record_success("pull", result)
        return self._record_failure(result)

    def preflight(self, repair: SyncRepair | None = None) -> SyncResult:
        """Compatibility alias for callers migrating to truthful pull naming."""
        return self.pull(repair=repair)

    def push(self, repair: SyncRepair | None = None) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")

        upstream = self._upstream()
        if upstream is None:
            return self._record_failure(SyncResult("unconfigured", "sync unconfigured"))
        push_target = self._push_target(upstream)
        if push_target is None:
            return self._record_failure(SyncResult("unconfigured", "sync upstream is not pushable"))
        upstream_commit = self._resolve_commit(upstream)
        if upstream_commit is None:
            return self._record_failure(SyncResult("error", "sync failed: upstream commit is unavailable"))

        captured = self._capture_valid_tip(upstream_commit)
        if isinstance(captured, SyncResult):
            return self._record_failure(captured)
        first_push = self._push(push_target, captured)
        if first_push.returncode == 0:
            return self._record_success("push", SyncResult("pushed", "local memory pushed"))

        pulled = self.pull(repair=repair)
        if pulled.status != "synced":
            return pulled
        upstream_commit = self._resolve_commit(upstream)
        if upstream_commit is None:
            return self._record_failure(SyncResult("error", "sync failed: upstream commit is unavailable"))
        captured = self._capture_valid_tip(upstream_commit)
        if isinstance(captured, SyncResult):
            return self._record_failure(captured)
        retry = self._push(push_target, captured)
        if retry.returncode == 0:
            return self._record_success(
                "push",
                SyncResult(
                    "pushed",
                    "local memory reconciled and pushed",
                    operation_id=pulled.operation_id,
                ),
            )
        status = "offline" if _looks_like_transport_failure(retry) else "error"
        message = "sync offline: git push failed" if status == "offline" else "sync failed: git push rejected"
        return self._record_failure(
            SyncResult(status, message, operation_id=pulled.operation_id)
        )

    def background_sync(self, repair: SyncRepair | None = None) -> SyncResult:
        if not self.config.enabled:
            return self.pull(repair=repair)

        upstream = self._upstream()
        if upstream is None:
            return self._record_failure(SyncResult("unconfigured", "sync unconfigured"))
        fetch = self._git("fetch")
        if fetch.returncode != 0:
            return self._record_failure(SyncResult("offline", "sync offline: git fetch failed"))
        upstream_commit = self._resolve_commit(upstream)
        if upstream_commit is None:
            return self._record_failure(SyncResult("error", "sync failed: upstream commit is unavailable"))
        captured = self._capture_valid_tip(upstream_commit)
        if isinstance(captured, SyncResult):
            return self._record_failure(captured)
        ahead_behind = self._ahead_behind(captured, upstream_commit)
        if ahead_behind is None:
            return self._record_failure(SyncResult("error", "sync failed: could not compare upstream"))
        ahead, behind = ahead_behind
        if ahead > 0:
            return self.push(repair=repair)
        if behind > 0:
            return self.pull(repair=repair)

        last_pull = self._last_successful_pull_at()
        stale_after = timedelta(hours=self.config.stale_pull_after_hours)
        if last_pull is not None and datetime.now(UTC) - last_pull < stale_after:
            return SyncResult("fresh", "last successful pull is fresh")
        return self.pull(repair=repair)

    def background_pull(self, repair: SyncRepair | None = None) -> SyncResult:
        """Compatibility alias for callers migrating to background_sync."""
        return self.background_sync(repair=repair)

    def repair_message(self, result: SyncResult) -> str:
        files = ", ".join(result.files) if result.files else "memory files"
        if result.status == "dirty":
            return f"{result.message}; inspect and repair dirty memory state in {files}"
        if result.status != "conflict":
            return result.message
        return (
            f"{result.message}; repair the staged incoming candidate in {files}; "
            "the active memory root is unchanged"
        )

    def _pull_locked(
        self,
        upstream_commit: str,
        repair: SyncRepair | None,
        store: SemanticOperationStore,
    ) -> SyncResult:
        recovered = self._recover_prepared_sync(store)
        if recovered is not None:
            return recovered
        blocked = self._active_preflight()
        if blocked is not None:
            return blocked
        start_commit = self._required_head(self.memory_root)
        ahead_behind = self._ahead_behind(start_commit, upstream_commit)
        if ahead_behind is None:
            return SyncResult("error", "sync failed: could not compare upstream")
        _ahead, behind = ahead_behind
        if behind == 0:
            return SyncResult("synced", "local memory is current")

        unexpected = [path for path in self._incoming_paths(start_commit, upstream_commit) if not _is_sync_path(path)]
        if unexpected:
            return SyncResult(
                "error",
                "sync refused incoming paths outside the synchronized state",
                sorted(unexpected),
            )

        stage_id = uuid.uuid4().hex
        candidate = self._create_candidate(
            start_commit,
            f"{SYNC_BRANCH_PREFIX}stage-{stage_id}",
            f"stage-{stage_id}",
            self._worktree_root() / f"sync-stage-{stage_id}",
        )
        try:
            inspection = self._inspect_new_candidate(candidate, start_commit, upstream_commit)
            if inspection.fatal is not None:
                return inspection.fatal
            if inspection.repair is None:
                candidate_commit = self._required_head(candidate.path)
                self._publish_candidate(candidate_commit, start_commit, candidate)
                return SyncResult("synced", "validated incoming memory published")
            if repair is None:
                return SyncResult(
                    "conflict",
                    f"incoming memory could not be admitted: {inspection.repair.message}; "
                    "active memory was left unchanged",
                    inspection.repair.files,
                )

            repair_input_sha256 = _sha256_json(
                {
                    "status": inspection.repair.status,
                    "message": inspection.repair.message,
                    "files": inspection.repair.files,
                    "merge_conflicted": inspection.merge_conflicted,
                }
            )
            policy_sha256 = self._repair_policy_sha256()
            operation_digest = _sha256_json(
                {
                    "active_start_commit": start_commit,
                    "upstream_commit": upstream_commit,
                    "repair_input_sha256": repair_input_sha256,
                    "policy_sha256": policy_sha256,
                }
            )
            operation_id = f"sync-repair-{operation_digest}"
            final_branch = f"{SYNC_BRANCH_PREFIX}{operation_digest}"
            final_path = self._worktree_root() / f"sync-{operation_digest}"
            input_data = {
                "kind": "sync-repair",
                "role": "sync-reconciler",
                "active_start_commit": start_commit,
                "upstream_commit": upstream_commit,
                "candidate_branch": final_branch,
                "candidate_worktree": final_path.relative_to(self.memory_root).as_posix(),
                "pre_repair_tip": inspection.pre_repair_tip,
                "expected_merge_parent": inspection.expected_merge_parent,
                "merge_conflicted": inspection.merge_conflicted,
                "repair_input_sha256": repair_input_sha256,
                "policy_sha256": policy_sha256,
                # The full diagnostic binds operation identity but is not retained in the receipt.
                "repair_diagnostic": {
                    "status": inspection.repair.status,
                    "message": inspection.repair.message,
                    "files": inspection.repair.files,
                },
            }
        finally:
            self._remove_candidate(candidate)

        existing = store.read(operation_id)
        candidate: _Candidate | None = None
        if existing is not None:
            # A divergent merge commit is not reproducible byte-for-byte, so recovery
            # uses the exact pre-repair tip captured by the first durable receipt.
            for key in ("pre_repair_tip", "expected_merge_parent", "merge_conflicted"):
                if key in existing.input_data:
                    input_data[key] = existing.input_data[key]
        else:
            self._discard_unrecorded_candidate(final_branch, final_path, operation_digest)
            candidate = self._create_candidate(
                start_commit,
                final_branch,
                operation_digest,
                final_path,
            )
            final_inspection = self._inspect_new_candidate(candidate, start_commit, upstream_commit)
            if final_inspection.repair is None or final_inspection.fatal is not None:
                self._remove_candidate(candidate)
                raise RuntimeError("final sync candidate no longer has the bounded repair input")
            final_input_hash = _sha256_json(
                {
                    "status": final_inspection.repair.status,
                    "message": final_inspection.repair.message,
                    "files": final_inspection.repair.files,
                    "merge_conflicted": final_inspection.merge_conflicted,
                }
            )
            if final_input_hash != repair_input_sha256:
                self._remove_candidate(candidate)
                raise RuntimeError("final sync candidate repair input changed during preparation")
            input_data["pre_repair_tip"] = final_inspection.pre_repair_tip
            input_data["expected_merge_parent"] = final_inspection.expected_merge_parent
            input_data["merge_conflicted"] = final_inspection.merge_conflicted

        record = store.begin(operation_id, input_data)
        if record.phase in FINAL_PHASES:
            if candidate is not None:
                self._remove_candidate(candidate)
            return self._result_from_record(record)

        if candidate is None:
            candidate = self._open_or_recreate_operation_candidate(
                record,
                start_commit,
                upstream_commit,
                repair_input_sha256,
            )
        retain_candidate = True
        try:
            if record.phase == "prepared":
                result = self._publish_prepared(store, record, candidate)
                retain_candidate = False
                return result

            result = self._run_or_adopt_repair(
                store,
                record,
                candidate,
                inspection.repair,
                repair,
            )
            retain_candidate = result.status == "error"
            return result
        except Exception as exc:
            latest = store.read(operation_id)
            if latest is not None and latest.phase == "running":
                store.record_failure(operation_id, f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if retain_candidate:
                candidate.lease.release()
            else:
                self._remove_candidate(candidate)

    def _recover_prepared_sync(self, store: SemanticOperationStore) -> SyncResult | None:
        records = [
            record
            for record in store.list_outstanding_records()
            if record.phase == "prepared" and record.input_data.get("kind") == "sync-repair"
        ]
        records.sort(
            key=lambda record: (
                record.outcome.sequence if record.outcome is not None else 0,
                record.operation_id,
            )
        )
        for record in records:
            claimed = store.claim_prepared(record.operation_id)
            outcome = claimed.outcome
            if outcome is None:
                raise RuntimeError("prepared sync operation has no outcome")
            candidate_commit = outcome.metadata.get("candidate_commit")
            if not isinstance(candidate_commit, str) or not candidate_commit:
                # Prepared no-change is completed without touching active history.
                self._cleanup_recorded_candidate(claimed, require_removed=True)
                store.complete_no_change(claimed.operation_id, outcome.start_commit)
                return self._result_from_record(store.read(claimed.operation_id) or claimed)
            current_head = self._required_head(self.memory_root)
            if self._is_ancestor(candidate_commit, current_head):
                self._cleanup_recorded_candidate(claimed, require_removed=True)
                store.complete_commit(claimed.operation_id, candidate_commit)
                return self._result_from_record(store.read(claimed.operation_id) or claimed)
            if current_head != outcome.start_commit:
                raise RuntimeError("active HEAD changed incompatibly with a prepared sync candidate")
            candidate = self._open_recorded_candidate(claimed)
            try:
                result = self._publish_prepared(store, claimed, candidate)
            except Exception:
                candidate.lease.release()
                raise
            self._remove_candidate(candidate)
            return result
        return None

    def _run_or_adopt_repair(
        self,
        store: SemanticOperationStore,
        record: SemanticOperationRecord,
        candidate: _Candidate,
        diagnostic: SyncResult,
        repair: SyncRepair,
    ) -> SyncResult:
        pre_tip = _required_record_string(record, "pre_repair_tip")
        current_tip = self._required_head(candidate.path)
        effects: tuple[OperationEffect, ...] = ()
        if current_tip == pre_tip:
            raw_outcome = repair(candidate.path, diagnostic, record.operation_id)
            repair_outcome = (
                raw_outcome
                if isinstance(raw_outcome, SyncRepairOutcome)
                else SyncRepairOutcome(str(raw_outcome))
            )
            effects = repair_outcome.effects
            current_tip = self._required_head(candidate.path)
        elif SemanticOperationStore(self.memory_root).state_root(record.operation_id).exists():
            effects = (
                OperationEffect(
                    "session-state",
                    metadata={"role": "sync-reconciler", "session_id": "runtime-sync-repair"},
                ),
            )

        if current_tip == pre_tip:
            conflict = SyncResult(
                "conflict",
                "incoming memory repair made no valid change; active memory was left unchanged",
                diagnostic.files,
                operation_id=record.operation_id,
            )
            store.prepare_outcome(
                record.operation_id,
                output=_encode_sync_result(conflict),
                start_commit=_required_record_string(record, "active_start_commit"),
                changed_paths=(),
                effects=effects,
                metadata={"candidate_commit": None},
            )
            self._remove_candidate(candidate, require_removed=True)
            store.complete_no_change(
                record.operation_id,
                _required_record_string(record, "active_start_commit"),
            )
            return conflict

        self._validate_repair_commit(record, candidate, current_tip)
        changed_paths = tuple(
            self._diff_paths(
                candidate.path,
                _required_record_string(record, "active_start_commit"),
                current_tip,
            )
        )
        prepared_result = SyncResult(
            "synced",
            "validated incoming memory repair published",
            list(changed_paths),
            operation_id=record.operation_id,
        )
        store.prepare_outcome(
            record.operation_id,
            output=_encode_sync_result(prepared_result),
            start_commit=_required_record_string(record, "active_start_commit"),
            changed_paths=changed_paths,
            effects=effects,
            metadata={
                "candidate_commit": current_tip,
                "candidate_branch": candidate.branch,
            },
        )
        prepared = store.read(record.operation_id)
        if prepared is None:
            raise RuntimeError("sync repair operation disappeared after preparation")
        return self._publish_prepared(store, prepared, candidate)

    def _publish_prepared(
        self,
        store: SemanticOperationStore,
        record: SemanticOperationRecord,
        candidate: _Candidate,
    ) -> SyncResult:
        outcome = record.outcome
        if outcome is None:
            raise RuntimeError("prepared sync operation has no outcome")
        candidate_commit = outcome.metadata.get("candidate_commit")
        if not isinstance(candidate_commit, str) or not candidate_commit:
            raise RuntimeError("prepared sync operation has no candidate commit")
        current_head = self._required_head(self.memory_root)
        if self._is_ancestor(candidate_commit, current_head):
            self._remove_candidate(candidate, require_removed=True)
            store.complete_commit(record.operation_id, candidate_commit)
            return self._result_from_record(store.read(record.operation_id) or record)
        if current_head != outcome.start_commit:
            raise RuntimeError("active HEAD changed before prepared sync candidate could publish")

        branch_commit = self._resolve_commit(candidate.branch)
        if branch_commit != candidate_commit:
            raise RuntimeError("prepared sync candidate branch is missing or moved")
        invalid = self._validate_candidate(candidate.path, outcome.start_commit, candidate_commit)
        if invalid is not None:
            raise RuntimeError(invalid.message)
        self._publish_candidate(candidate_commit, outcome.start_commit, candidate)
        self._remove_candidate(candidate, require_removed=True)
        store.complete_commit(record.operation_id, candidate_commit)
        completed = store.read(record.operation_id)
        if completed is None:
            raise RuntimeError("sync repair operation disappeared after publication")
        return self._result_from_record(completed)

    def _publish_candidate(self, candidate_commit: str, start_commit: str, candidate: _Candidate) -> None:
        if self._required_head(self.memory_root) != start_commit:
            raise RuntimeError("active HEAD changed before sync publication")
        blocked = self._active_preflight()
        if blocked is not None:
            raise RuntimeError(blocked.message)
        if self._resolve_commit(candidate.branch) != candidate_commit:
            raise RuntimeError("sync candidate branch is missing or moved")
        merge = self._git("merge", "--ff-only", candidate_commit)
        if merge.returncode != 0:
            raise RuntimeError("exact sync candidate fast-forward failed")
        if self._required_head(self.memory_root) != candidate_commit:
            raise RuntimeError("active HEAD does not equal published sync candidate")
        dirty = self._dirty_memory_files()
        if dirty:
            raise RuntimeError("published sync candidate left synchronized files dirty")

    def _inspect_new_candidate(
        self,
        candidate: _Candidate,
        start_commit: str,
        upstream_commit: str,
    ) -> _CandidateInspection:
        merge = self._run_git(candidate.path, "merge", "--no-edit", upstream_commit)
        if merge.returncode != 0:
            conflicts = self._conflicted_files(candidate.path)
            queue_conflicts = [path for path in conflicts if _is_update_queue_path(path)]
            if queue_conflicts:
                return _CandidateInspection(
                    None,
                    SyncResult(
                        "error",
                        "incoming update queue has a coordination conflict",
                        sorted(queue_conflicts),
                    ),
                    start_commit,
                    upstream_commit,
                    True,
                )
            invalid_conflicts = [path for path in conflicts if not _is_sync_path(path)]
            if not conflicts or invalid_conflicts:
                files = invalid_conflicts or conflicts
                return _CandidateInspection(
                    None,
                    SyncResult("error", "sync candidate merge failed outside repairable state", files),
                    start_commit,
                    upstream_commit,
                    True,
                )
            invalid_queue = _invalid_update_queue_result(candidate.path)
            if invalid_queue is not None:
                return _CandidateInspection(
                    None,
                    invalid_queue,
                    start_commit,
                    upstream_commit,
                    True,
                )
            return _CandidateInspection(
                SyncResult("conflict", "incoming candidate has merge conflicts", conflicts),
                None,
                start_commit,
                upstream_commit,
                True,
            )

        candidate_commit = self._required_head(candidate.path)
        invalid = self._validate_candidate(candidate.path, start_commit, candidate_commit)
        if invalid is None:
            return _CandidateInspection(None, None, candidate_commit, None, False)
        if invalid.status == "error":
            return _CandidateInspection(None, invalid, candidate_commit, None, False)
        return _CandidateInspection(invalid, None, candidate_commit, None, False)

    def _validate_repair_commit(
        self,
        record: SemanticOperationRecord,
        candidate: _Candidate,
        candidate_commit: str,
    ) -> None:
        pre_tip = _required_record_string(record, "pre_repair_tip")
        commits = self._git_stdout(
            candidate.path,
            "rev-list",
            "--first-parent",
            "--reverse",
            f"{pre_tip}..{candidate_commit}",
        ).splitlines()
        if len(commits) != 1 or commits[0].strip() != candidate_commit:
            raise RuntimeError("sync repair must create exactly one first-parent commit")
        parents = self._git_stdout(candidate.path, "show", "-s", "--format=%P", candidate_commit).split()
        if record.input_data.get("merge_conflicted"):
            upstream = _required_record_string(record, "expected_merge_parent")
            if not parents or parents[0] != pre_tip or upstream not in parents[1:]:
                raise RuntimeError("sync repair commit does not complete the expected merge")
        elif parents != [pre_tip]:
            raise RuntimeError("sync repair commit is not directly based on the staged merge")

        status = self._git_stdout(candidate.path, "status", "--porcelain")
        if status:
            raise RuntimeError("sync repair candidate has uncommitted changes")
        invalid = self._validate_candidate(
            candidate.path,
            _required_record_string(record, "active_start_commit"),
            candidate_commit,
        )
        if invalid is not None:
            raise RuntimeError(invalid.message)

    def _validate_candidate(
        self,
        candidate_root: Path,
        start_commit: str,
        candidate_commit: str,
    ) -> SyncResult | None:
        changed_paths = self._diff_paths(candidate_root, start_commit, candidate_commit)
        unexpected = [path for path in changed_paths if not _is_sync_path(path)]
        if unexpected:
            return SyncResult(
                "error",
                "sync candidate changes paths outside the synchronized state",
                unexpected,
            )
        invalid_files = self._non_regular_paths(candidate_root, changed_paths)
        if invalid_files:
            return SyncResult("error", "sync candidate contains a missing or non-regular required path", invalid_files)
        invalid_queue = _invalid_update_queue_result(candidate_root)
        if invalid_queue is not None:
            return invalid_queue
        return self._invalid_graph_result(candidate_root)

    def _active_preflight(self) -> SyncResult | None:
        if not self._is_git_repo():
            return SyncResult("unconfigured", "sync unconfigured")
        conflicted = self._conflicted_files(self.memory_root)
        if conflicted:
            return SyncResult("conflict", "local synchronized state is already conflicted", conflicted)
        dirty = self._dirty_memory_files()
        if dirty:
            return SyncResult("dirty", "local memory has uncommitted changes", dirty)
        invalid_files = self._non_regular_paths(self.memory_root, ())
        if invalid_files:
            return SyncResult("conflict", "local memory root is incomplete", invalid_files)
        invalid_queue = _invalid_update_queue_result(self.memory_root)
        if invalid_queue is not None:
            return invalid_queue
        return self._invalid_graph_result(self.memory_root)

    def _capture_valid_tip(self, upstream_commit: str) -> str | SyncResult:
        store = SemanticOperationStore(self.memory_root)
        try:
            with store.execution_locked(), MemoryWriteLock(self.memory_root):
                blocked = self._active_preflight()
                if blocked is not None:
                    return blocked
                captured = self._required_head(self.memory_root)
                unexpected = [
                    path
                    for path in self._outgoing_paths(upstream_commit, captured)
                    if not _is_sync_path(path)
                ]
                if unexpected:
                    return SyncResult(
                        "error",
                        "sync refused local commits containing paths outside the synchronized state",
                        sorted(unexpected),
                    )
                return captured
        except Exception as exc:
            return SyncResult("error", f"sync failed: {type(exc).__name__}: {exc}")

    def _create_candidate(
        self,
        start_commit: str,
        branch: str,
        lease_id: str,
        path: Path,
    ) -> _Candidate:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        relative = path.relative_to(self.memory_root).as_posix()
        ignored = self._git("check-ignore", "-q", relative)
        if ignored.returncode != 0:
            raise RuntimeError("sync candidate worktree path is not ignored")
        lease = WorktreeLease(self.memory_root, "sync", lease_id)
        lease.acquire()
        candidate = _Candidate(branch, path, lease)
        try:
            added = self._git("worktree", "add", "-b", branch, str(path), start_commit)
            if added.returncode != 0:
                raise RuntimeError("could not create staged sync candidate")
            return candidate
        except Exception:
            lease.release()
            raise

    def _discard_unrecorded_candidate(self, branch: str, path: Path, lease_id: str) -> None:
        if self._resolve_commit(branch) is None and not path.exists():
            return
        lease = WorktreeLease(self.memory_root, "sync", lease_id)
        lease.acquire()
        try:
            self._git("worktree", "remove", "--force", str(path))
            self._git("worktree", "prune")
            self._git("branch", "-D", branch)
        finally:
            lease.release()

    def _open_or_recreate_operation_candidate(
        self,
        record: SemanticOperationRecord,
        start_commit: str,
        upstream_commit: str,
        repair_input_sha256: str,
    ) -> _Candidate:
        branch, path, lease_id = self._recorded_candidate_identity(record)
        branch_commit = self._resolve_commit(branch)
        if branch_commit is None:
            if record.phase == "prepared":
                raise RuntimeError("prepared sync candidate branch is missing")
            candidate = self._create_candidate(start_commit, branch, lease_id, path)
            inspection = self._inspect_new_candidate(candidate, start_commit, upstream_commit)
            if inspection.repair is None or inspection.fatal is not None:
                self._remove_candidate(candidate)
                raise RuntimeError("recreated sync candidate no longer has the recorded repair input")
            recreated_hash = _sha256_json(
                {
                    "status": inspection.repair.status,
                    "message": inspection.repair.message,
                    "files": inspection.repair.files,
                    "merge_conflicted": inspection.merge_conflicted,
                }
            )
            if recreated_hash != repair_input_sha256:
                self._remove_candidate(candidate)
                raise RuntimeError("recreated sync repair input differs from its durable receipt")
            if (
                inspection.pre_repair_tip != _required_record_string(record, "pre_repair_tip")
                or inspection.merge_conflicted != bool(record.input_data.get("merge_conflicted"))
            ):
                self._remove_candidate(candidate)
                raise RuntimeError("recorded sync candidate commit is missing and cannot be reconstructed")
            return candidate

        return self._open_recorded_candidate(record)

    def _open_recorded_candidate(self, record: SemanticOperationRecord) -> _Candidate:
        branch, path, lease_id = self._recorded_candidate_identity(record)
        branch_commit = self._resolve_commit(branch)
        if branch_commit is None:
            raise RuntimeError("recorded sync candidate branch is missing")
        lease = WorktreeLease(self.memory_root, "sync", lease_id)
        lease.acquire()
        candidate = _Candidate(branch, path, lease)
        try:
            if not path.exists():
                self._git("worktree", "prune")
                added = self._git("worktree", "add", str(path), branch)
                if added.returncode != 0:
                    raise RuntimeError("could not reopen staged sync candidate")
            if self._required_head(path) != branch_commit:
                raise RuntimeError("staged sync worktree does not match its candidate branch")
            return candidate
        except Exception:
            lease.release()
            raise

    def _cleanup_recorded_candidate(
        self,
        record: SemanticOperationRecord,
        *,
        require_removed: bool = False,
    ) -> None:
        branch, path, lease_id = self._recorded_candidate_identity(record)
        lease = WorktreeLease(self.memory_root, "sync", lease_id)
        lease.acquire()
        if self._resolve_commit(branch) is None and not path.exists():
            lease.release()
            return
        self._remove_candidate(
            _Candidate(branch, path, lease),
            require_removed=require_removed,
        )

    def _remove_candidate(self, candidate: _Candidate, *, require_removed: bool = False) -> None:
        if candidate.removed:
            return
        try:
            self._git("worktree", "remove", "--force", str(candidate.path))
            self._git("worktree", "prune")
            self._git("branch", "-D", candidate.branch)
            if require_removed and (
                candidate.path.exists() or self._resolve_commit(candidate.branch) is not None
            ):
                raise RuntimeError("could not remove settled sync candidate")
            candidate.removed = not candidate.path.exists() and self._resolve_commit(candidate.branch) is None
        finally:
            candidate.lease.release()

    def _recorded_candidate_identity(
        self,
        record: SemanticOperationRecord,
    ) -> tuple[str, Path, str]:
        prefix = "sync-repair-"
        if not record.operation_id.startswith(prefix):
            raise RuntimeError("sync operation id does not identify a repair candidate")
        digest = record.operation_id.removeprefix(prefix)
        if re.fullmatch(r"[0-9a-f]{64}", digest) is None:
            raise RuntimeError("sync operation id has an invalid repair digest")
        branch = f"{SYNC_BRANCH_PREFIX}{digest}"
        relative = f".runtime/worktrees/sync-{digest}"
        if _required_record_string(record, "candidate_branch") != branch:
            raise RuntimeError("sync operation candidate branch does not match its operation id")
        if _required_record_string(record, "candidate_worktree") != relative:
            raise RuntimeError("sync operation candidate worktree does not match its operation id")
        return branch, (self.memory_root / relative).resolve(), digest

    def _worktree_root(self) -> Path:
        return self.memory_root / ".runtime" / "worktrees"

    def _incoming_paths(self, start_commit: str, upstream_commit: str) -> list[str]:
        merge_base = self._git_stdout(self.memory_root, "merge-base", start_commit, upstream_commit)
        if not merge_base:
            raise RuntimeError("sync histories are unrelated")
        return self._diff_paths(self.memory_root, merge_base, upstream_commit)

    def _outgoing_paths(self, upstream_commit: str, captured_commit: str) -> list[str]:
        result = self._run_git(
            self.memory_root,
            "rev-list",
            "--reverse",
            f"{upstream_commit}..{captured_commit}",
        )
        if result.returncode != 0:
            raise RuntimeError("could not inspect outgoing sync history")
        paths: set[str] = set()
        for commit in result.stdout.splitlines():
            commit = commit.strip()
            if not commit:
                continue
            changed = self._run_git(
                self.memory_root,
                "diff-tree",
                "--root",
                "-m",
                "--no-commit-id",
                "--name-status",
                "-r",
                "-M",
                "-z",
                commit,
            )
            if changed.returncode != 0:
                raise RuntimeError("could not inspect an outgoing sync commit")
            paths.update(_name_status_paths(changed.stdout))
        return sorted(paths)

    def _diff_paths(self, cwd: Path, start_commit: str, end_commit: str) -> list[str]:
        result = self._run_git(
            cwd,
            "diff",
            "--name-status",
            "-M",
            "-z",
            start_commit,
            end_commit,
        )
        if result.returncode != 0:
            raise RuntimeError("could not inspect sync candidate paths")
        return sorted(set(_name_status_paths(result.stdout)))

    def _non_regular_paths(self, root: Path, changed_paths: Iterable[str]) -> list[str]:
        invalid: set[str] = set()
        for name in REQUIRED_ROOT_DOCUMENTS:
            path = root / name
            if path.is_symlink() or not path.is_file():
                invalid.add(name)
        for name in changed_paths:
            path = root / name
            if not path.exists() and not path.is_symlink():
                continue
            if path.is_symlink() or not path.is_file():
                invalid.add(name)
        return sorted(invalid)

    def _upstream(self) -> str | None:
        if not self._is_git_repo():
            return None
        result = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if result.returncode != 0:
            return None
        upstream = result.stdout.strip()
        return upstream or None

    def _is_git_repo(self) -> bool:
        if not self.memory_root.exists():
            return False
        result = self._git("rev-parse", "--show-toplevel")
        if result.returncode != 0 or not result.stdout.strip():
            return False
        return Path(result.stdout.strip()).resolve() == self.memory_root

    def _resolve_commit(self, revision: str) -> str | None:
        result = self._git("rev-parse", "--verify", f"{revision}^{{commit}}")
        if result.returncode != 0:
            return None
        commit = result.stdout.strip()
        return commit if re.fullmatch(r"[0-9a-fA-F]{40,64}", commit) else None

    def _required_head(self, cwd: Path) -> str:
        result = self._run_git(cwd, "rev-parse", "--verify", "HEAD")
        commit = result.stdout.strip()
        if result.returncode != 0 or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit):
            raise RuntimeError("could not resolve repository HEAD")
        return commit

    def _ahead_behind(self, left: str, right: str) -> tuple[int, int] | None:
        result = self._git("rev-list", "--left-right", "--count", f"{left}...{right}")
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        if len(parts) != 2:
            return None
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            return None

    def _push_target(self, upstream: str) -> tuple[str, str] | None:
        if "/" not in upstream:
            return None
        remote, branch = upstream.split("/", 1)
        if not remote or not branch:
            return None
        return remote, branch

    def _push(self, target: tuple[str, str], commit: str) -> subprocess.CompletedProcess[str]:
        remote, branch = target
        return self._git("push", remote, f"{commit}:{branch}")

    def _dirty_memory_files(self) -> list[str]:
        result = self._git("status", "--porcelain", "--", *MEMORY_SYNC_PATHS)
        if result.returncode != 0:
            return []
        return _porcelain_paths(result.stdout)

    def _conflicted_files(self, root: Path) -> list[str]:
        result = self._run_git(root, "diff", "--name-only", "--diff-filter=U")
        if result.returncode != 0:
            return []
        return sorted(line.strip() for line in result.stdout.splitlines() if line.strip())

    def _invalid_graph_result(self, root: Path) -> SyncResult | None:
        validation = MemoryTools(root, role="sync-reconciler").validate_memory(
            enforce_correction_capacity=False
        )
        if not validation.startswith("validation failed:"):
            return None
        files = [name for name in REQUIRED_ROOT_DOCUMENTS if (root / name).exists()]
        if (root / "corrections.md").exists():
            files.append("corrections.md")
        return SyncResult("conflict", validation, files)

    def _repair_policy_sha256(self) -> str:
        prompt = Path(__file__).with_name("prompts") / "sync-reconciler.md"
        try:
            prompt_bytes = prompt.read_bytes()
        except OSError:
            prompt_bytes = b""
        digest = hashlib.sha256()
        digest.update(SYNC_REPAIR_POLICY_VERSION.encode("utf-8"))
        digest.update(b"\0")
        digest.update(prompt_bytes)
        return digest.hexdigest()

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git("merge-base", "--is-ancestor", ancestor, descendant).returncode == 0

    def _result_from_record(self, record: SemanticOperationRecord) -> SyncResult:
        if record.outcome is None:
            raise RuntimeError("sync operation has no durable outcome")
        result = _decode_sync_result(record.outcome.output)
        return SyncResult(result.status, result.message, result.files, record.operation_id)

    def _last_successful_pull_at(self) -> datetime | None:
        value = self._read_state().get("last_successful_pull_at")
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _record_success(self, operation: str, result: SyncResult) -> SyncResult:
        now = datetime.now(UTC).isoformat()
        state = self._read_state()
        state[f"last_successful_{operation}_at"] = now
        state["last_status"] = result.status
        state["last_message"] = result.message
        state["last_files"] = result.files
        state.pop("last_failure_at", None)
        state.pop("last_failure_status", None)
        state.pop("last_failure_message", None)
        state.pop("last_failure_files", None)
        self._write_state(state)
        return result

    def _record_failure(self, result: SyncResult) -> SyncResult:
        state = self._read_state()
        state["last_status"] = result.status
        state["last_message"] = result.message
        state["last_files"] = result.files
        state["last_failure_at"] = datetime.now(UTC).isoformat()
        state["last_failure_status"] = result.status
        state["last_failure_message"] = result.message
        state["last_failure_files"] = result.files
        self._write_state(state)
        return result

    def _read_state(self) -> dict[str, object]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, data: dict[str, object]) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.state_path)
        _fsync_directory(self.state_path.parent)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self._run_git(self.memory_root, *args)

    def _git_stdout(self, cwd: Path, *args: str) -> str:
        result = self._run_git(cwd, *args)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _run_git(self, cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        try:
            return subprocess.run(
                ["git", *args],
                cwd=cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except FileNotFoundError as exc:
            return subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
        except subprocess.TimeoutExpired as exc:
            stdout = _timeout_output(exc.stdout)
            stderr = _timeout_output(exc.stderr)
            message = f"git command timed out after {GIT_TIMEOUT_SECONDS} seconds"
            if stderr:
                message = f"{stderr}\n{message}"
            return subprocess.CompletedProcess(["git", *args], 124, stdout, message)


def _is_sync_path(path: str) -> bool:
    if path.startswith("update_queue/"):
        return _is_update_queue_path(path)
    for pattern in MEMORY_SYNC_PATHS:
        expression = re.escape(pattern).replace(r"\*", "[^/]*")
        if re.fullmatch(expression, path):
            return True
    return False


def _is_update_queue_path(path: str) -> bool:
    return (
        path == "update_queue/lease.json"
        or UPDATE_QUEUE_CANDIDATE_PATH_RE.fullmatch(path) is not None
        or UPDATE_QUEUE_RECOVERY_PATH_RE.fullmatch(path) is not None
    )


def _invalid_update_queue_result(root: Path) -> SyncResult | None:
    diagnostics = validate_update_queue(root)
    if not diagnostics:
        return None
    files = sorted(
        {
            diagnostic.partition(":")[0]
            for diagnostic in diagnostics
            if diagnostic.partition(":")[0] == "update_queue"
            or diagnostic.partition(":")[0].startswith("update_queue/")
        }
    )
    return SyncResult(
        "error",
        "invalid synchronized update queue:\n" + "\n".join(f"- {item}" for item in diagnostics),
        files,
    )


def _required_record_string(record: SemanticOperationRecord, key: str) -> str:
    value = record.input_data.get(key)
    if not isinstance(value, str) or not value:
        raise RuntimeError(f"sync operation is missing {key}")
    return value


def _encode_sync_result(result: SyncResult) -> str:
    return json.dumps(
        {"status": result.status, "message": result.message, "files": result.files},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_sync_result(value: str) -> SyncResult:
    try:
        data = json.loads(value)
    except json.JSONDecodeError as exc:
        raise RuntimeError("sync operation contains an invalid durable result") from exc
    if not isinstance(data, dict):
        raise RuntimeError("sync operation contains an invalid durable result")
    status = data.get("status")
    message = data.get("message")
    files = data.get("files")
    if not isinstance(status, str) or not isinstance(message, str) or not isinstance(files, list):
        raise RuntimeError("sync operation contains an invalid durable result")
    if any(not isinstance(path, str) for path in files):
        raise RuntimeError("sync operation contains invalid durable paths")
    return SyncResult(status, message, files)


def _sha256_json(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _name_status_paths(output: str) -> list[str]:
    paths: list[str] = []
    tokens = [token for token in output.split("\0") if token]
    index = 0
    while index < len(tokens):
        status = tokens[index]
        index += 1
        path_count = 2 if status.startswith(("R", "C")) else 1
        paths.extend(tokens[index : index + path_count])
        index += path_count
    return paths


def _porcelain_paths(output: str) -> list[str]:
    files: set[str] = set()
    for line in output.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        if " -> " in path:
            path = path.rsplit(" -> ", 1)[1]
        if path:
            files.add(path)
    return sorted(files)


def _looks_like_transport_failure(result: subprocess.CompletedProcess[str]) -> bool:
    detail = f"{result.stdout}\n{result.stderr}".casefold()
    return any(
        marker in detail
        for marker in (
            "could not resolve host",
            "connection timed out",
            "connection refused",
            "network is unreachable",
            "unable to access",
        )
    )


def _timeout_output(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value

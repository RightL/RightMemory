from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .async_update import (
    UPDATE_DEBOUNCE_SECONDS,
    UPDATE_MAX_AUTOMATIC_ATTEMPTS,
    UPDATE_RETRY_COOLDOWN_SECONDS,
    AsyncUpdateJob,
    AsyncUpdateSessionBatch,
    AsyncUpdateStore,
    _format_batch_message,
)
from .config import SyncConfig
from .platform import lock_file, unlock_file
from .semantic_operation import SemanticOperationStore
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory
from .sync import SyncManager
from .update_queue import (
    UpdateCandidate,
    UpdateQueueFormatError,
    UpdateQueueLease,
    UpdateQueueRecovery,
    UpdateQueueSnapshot,
    UpdateQueueStore,
    candidate_evidence_matches,
    parse_update_candidate_json,
    parse_update_queue_lease_json,
    update_candidate_batch_id,
    validate_update_queue,
)
from .update_coordination import is_update_coordination_path
from .update_review import (
    UpdateReviewOutcome,
    UpdateReviewStore,
    tracked_review_blob_oid,
    tracked_review_commit,
    validate_update_reviews,
)


GIT_TIMEOUT_SECONDS = 30
QUEUE_PUSH_ATTEMPTS = 4
QUEUE_LEASE_SECONDS = 6 * 60 * 60
QUEUE_FINALIZER = "update-queue"
QUEUE_COMMIT_NAME = "RightMemory"
QUEUE_COMMIT_EMAIL = "rightmemory@local"
QUEUE_TOKEN_TRAILER = "RightMemory-Queue-Token"
QUEUE_BATCH_TRAILER = "RightMemory-Queue-Batch"
QUEUE_CANCEL_TRAILER = "RightMemory-Queue-Cancel"
QUEUE_SUPERSEDE_TRAILER = "RightMemory-Queue-Supersede"
QUEUE_RETRY_TRAILER = "RightMemory-Queue-Retry"
_OID_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")


class UpdateQueueUnavailable(RuntimeError):
    """Raised when synchronized queue work cannot safely proceed online."""


class UpdateQueueLeaseLost(RuntimeError):
    """Raised when another device owns or has replaced the Git lease."""


class UpdateQueueSemanticBaseChanged(RuntimeError):
    """Raised when a prepared model result was based on stale semantic state."""


@dataclass(frozen=True)
class QueuePublicationResult:
    settled_uids: tuple[str, ...] = ()
    published_uids: tuple[str, ...] = ()
    unresolved_uids: tuple[str, ...] = ()
    online: bool = False


@dataclass(frozen=True)
class ClaimedUpdateBatch:
    lease: UpdateQueueLease
    candidates: tuple[UpdateCandidate, ...]
    lease_commit: str

    def __post_init__(self) -> None:
        kinds = {candidate.kind for candidate in self.candidates}
        if len(kinds) != 1:
            raise UpdateQueueFormatError("one synchronized batch cannot mix update and review work")
        if self.kind == "review" and len(self.candidates) != 1:
            raise UpdateQueueFormatError("one synchronized review batch must contain one candidate")

    @property
    def batch_id(self) -> str:
        return self.lease.batch_id

    @property
    def session_id(self) -> str:
        return self.batch_id

    @property
    def kind(self) -> str:
        if not self.candidates:
            raise UpdateQueueFormatError("claimed synchronized batch has no candidates")
        return self.candidates[0].kind

    @property
    def review_candidate(self) -> UpdateCandidate:
        if self.kind != "review":
            raise UpdateQueueFormatError("claimed batch is not an update review")
        return self.candidates[0]

    @property
    def message(self) -> str:
        if self.kind != "update":
            raise UpdateQueueFormatError("review batches do not use the Update batch prompt")
        return _format_batch_message(_session_batches(self.candidates))


@dataclass(frozen=True)
class QueueClaimResult:
    claim: ClaimedUpdateBatch | None = None
    next_attempt_at: datetime | None = None
    online: bool = False


@dataclass(frozen=True)
class _Upstream:
    remote: str
    branch: str
    revision: str
    commit: str

    @property
    def branch_ref(self) -> str:
        return f"refs/heads/{self.branch}"


class GitUpdateQueueCoordinator:
    """Git compare-and-swap transport and lease coordinator for update candidates."""

    def __init__(self, config: SyncConfig, *, device_id: str | None = None):
        self.config = config
        self.memory_root = Path(config.memory_root).resolve()
        self.store = UpdateQueueStore(self.memory_root)
        self.device_id = device_id or _load_device_id(self.memory_root)

    def publish_outbox(
        self,
        candidate_uids: set[str] | frozenset[str] | None = None,
    ) -> QueuePublicationResult:
        candidates = self.store.outbox_candidates()
        if candidate_uids is not None:
            candidates = tuple(item for item in candidates if item.uid in candidate_uids)
        if not self.config.enabled or not candidates:
            return QueuePublicationResult(online=False)
        upstream = self._fetch_upstream()
        if upstream is None:
            # No publication marker is written until the first successful fetch.
            return QueuePublicationResult(
                unresolved_uids=tuple(item.uid for item in candidates),
                online=False,
            )

        settled: list[str] = []
        published: list[str] = []
        unresolved: list[str] = []
        for candidate in candidates:
            outcome = self._publish_candidate(candidate, upstream)
            if outcome == "published":
                published.append(candidate.uid)
                settled.append(candidate.uid)
            elif outcome == "settled":
                settled.append(candidate.uid)
            else:
                unresolved.append(candidate.uid)
            refreshed = self._fetch_upstream()
            if refreshed is not None:
                upstream = refreshed
        return QueuePublicationResult(
            settled_uids=tuple(settled),
            published_uids=tuple(published),
            unresolved_uids=tuple(unresolved),
            online=True,
        )

    def claim_next(
        self,
        *,
        target_batch_candidates: int,
        max_wait_seconds: int,
        now: datetime | None = None,
    ) -> QueueClaimResult:
        if not self.config.enabled:
            return QueueClaimResult()
        now = _utc_now() if now is None else now.astimezone(UTC)
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                return QueueClaimResult(online=False)
            # Surface malformed remote-owned queue state before local sync
            # preflight can turn it into an indistinguishable no-op.
            with self._worktree(upstream.commit) as validation_worktree:
                UpdateQueueStore(validation_worktree).snapshot()
            if not self._advance_active(upstream.commit, queue_only=True):
                return QueueClaimResult(online=True)
            with self._worktree(upstream.commit) as worktree:
                snapshot = UpdateQueueStore(worktree).snapshot()
                if snapshot.lease is not None and not _lease_expired(snapshot.lease, now):
                    if snapshot.lease.holder == self.device_id:
                        by_uid = {item.uid: item for item in snapshot.candidates}
                        candidates = tuple(
                            by_uid[uid] for uid in snapshot.lease.candidate_uids
                        )
                        return QueueClaimResult(
                            claim=ClaimedUpdateBatch(
                                snapshot.lease,
                                candidates,
                                upstream.commit,
                            ),
                            online=True,
                        )
                    return QueueClaimResult(
                        next_attempt_at=_parse_time(snapshot.lease.expires_at),
                        online=True,
                    )
                candidates, deadline = _select_candidates(
                    snapshot,
                    target_batch_candidates=target_batch_candidates,
                    max_wait_seconds=max_wait_seconds,
                    now=now,
                )
                if not candidates:
                    return QueueClaimResult(next_attempt_at=deadline, online=True)
                batch_id = update_candidate_batch_id(candidates)
                if snapshot.lease is not None:
                    expected = tuple(sorted(snapshot.lease.candidate_uids))
                    actual = tuple(sorted(item.uid for item in candidates))
                    if expected != actual or snapshot.lease.batch_id != batch_id:
                        raise UpdateQueueFormatError("expired lease does not match its candidate batch")
                lease = UpdateQueueLease(
                    holder=self.device_id,
                    token=uuid.uuid4().hex,
                    base_commit=upstream.commit,
                    batch_id=batch_id,
                    candidate_uids=tuple(sorted(item.uid for item in candidates)),
                    expires_at=(now + timedelta(seconds=QUEUE_LEASE_SECONDS)).isoformat(),
                )
                UpdateQueueStore(worktree).write_lease(lease)
                lease_commit = self._commit_paths(
                    worktree,
                    ["update_queue/lease.json"],
                    f"update queue: claim {batch_id}",
                )
            claim = ClaimedUpdateBatch(lease, candidates, lease_commit)
            published_but_uninstalled = False
            operation_store = SemanticOperationStore(self.memory_root)
            with operation_store.execution_locked(), MemoryWriteLock(self.memory_root):
                blocked = SyncManager(self.config)._active_preflight()
                if blocked is not None or self._resolve("HEAD") != upstream.commit:
                    # A semantic writer won the gap between preparation and CAS.
                    return QueueClaimResult(online=True)
                if self._push_cas(upstream, lease_commit):
                    if self._advance_active_locked(lease_commit, queue_only=True):
                        return QueueClaimResult(claim=claim, online=True)
                    published_but_uninstalled = True
                else:
                    # A failed push can still mean the remote accepted it before
                    # the transport outcome became ambiguous.
                    refreshed = self._fetch_upstream()
                    if refreshed is None:
                        return QueueClaimResult(online=False)
                    with self._worktree(refreshed.commit) as worktree:
                        refreshed_snapshot = UpdateQueueStore(worktree).snapshot()
                    refreshed_lease = refreshed_snapshot.lease
                    if (
                        refreshed_lease is not None
                        and refreshed_lease.token == lease.token
                        and refreshed_lease.holder == self.device_id
                    ):
                        by_uid = {
                            item.uid: item for item in refreshed_snapshot.candidates
                        }
                        recovered_claim = ClaimedUpdateBatch(
                            refreshed_lease,
                            tuple(by_uid[uid] for uid in refreshed_lease.candidate_uids),
                            refreshed.commit,
                        )
                        if self._advance_active_locked(
                            refreshed.commit,
                            queue_only=True,
                        ):
                            return QueueClaimResult(claim=recovered_claim, online=True)
                        claim = recovered_claim
                        published_but_uninstalled = True
            if published_but_uninstalled:
                try:
                    self._settle_failed_claim(claim, recovery=None)
                except Exception as exc:
                    raise UpdateQueueUnavailable(
                        "queue lease published but could not be installed or released"
                    ) from exc
                raise UpdateQueueUnavailable(
                    "queue lease was released because the active checkout could not advance"
                )
        return QueueClaimResult(online=True)

    def finalize(
        self,
        claim: ClaimedUpdateBatch,
        candidate_commit: str | None,
        *,
        prepared_start_commit: str | None = None,
        review_outcome: UpdateReviewOutcome | None = None,
    ) -> str:
        if claim.kind == "review" and review_outcome is None:
            raise ValueError("review batch finalization requires a review outcome")
        if claim.kind == "update" and review_outcome is not None:
            raise ValueError("Update batch finalization cannot settle an update review")
        operation_store = SemanticOperationStore(self.memory_root)
        with operation_store.execution_locked():
            return self._finalize_locked(
                claim,
                candidate_commit,
                prepared_start_commit=prepared_start_commit,
                review_outcome=review_outcome,
            )

    def _finalize_locked(
        self,
        claim: ClaimedUpdateBatch,
        candidate_commit: str | None,
        *,
        prepared_start_commit: str | None,
        review_outcome: UpdateReviewOutcome | None,
    ) -> str:
        semantic_base = prepared_start_commit or claim.lease.base_commit
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable("cannot finalize synchronized update while Git is offline")
            recovered = self.finalized_batch_commit(
                upstream.commit,
                claim.batch_id,
                token=claim.lease.token,
            )
            if recovered is not None:
                if not self._advance_active(
                    recovered,
                    queue_only=False,
                    execution_locked=True,
                ):
                    raise UpdateQueueUnavailable("published update could not advance the active checkout")
                return recovered
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                self._require_claim(snapshot, claim)
                if not self._is_ancestor(semantic_base, upstream.commit):
                    raise UpdateQueueSemanticBaseChanged(
                        "prepared semantic base is no longer in synchronized history"
                    )
                changed = self._changed_paths(semantic_base, upstream.commit)
                active_head = self._resolve("HEAD")
                if active_head is None or not self._is_ancestor(
                    semantic_base,
                    active_head,
                ):
                    raise UpdateQueueSemanticBaseChanged(
                        "active semantic state diverged after the synchronized batch was claimed"
                    )
                changed.extend(self._changed_paths(semantic_base, active_head))
                semantic = [path for path in changed if not is_update_coordination_path(path)]
                if semantic:
                    raise UpdateQueueSemanticBaseChanged(
                        "semantic state changed after the synchronized batch was claimed"
                    )
                if candidate_commit is not None:
                    cherry_pick = self._git(
                        "cherry-pick",
                        "--allow-empty",
                        candidate_commit,
                        cwd=worktree,
                    )
                    if cherry_pick.returncode != 0:
                        self._git("cherry-pick", "--abort", cwd=worktree, check=False)
                        raise UpdateQueueSemanticBaseChanged(
                            "prepared update no longer applies to the synchronized queue base"
                        )
                review_path: str | None = None
                if review_outcome is not None:
                    review = claim.review_candidate
                    if (
                        review.review_id is None
                        or review.review_commit is None
                        or review.review_blob_oid is None
                    ):
                        raise UpdateQueueFormatError(
                            "review candidate has incomplete source identity"
                        )
                    review_path = UpdateReviewStore(worktree).settle_tracked(
                        review.review_id,
                        review_outcome,
                        operation_id=claim.batch_id,
                        expected_commit=review.review_commit,
                        expected_blob_oid=review.review_blob_oid,
                    ).relative_to(worktree).as_posix()
                had_recovery = any(
                    item.batch_id == claim.batch_id for item in snapshot.recoveries
                )
                for uid in claim.lease.candidate_uids:
                    queue_store.remove_candidate(uid)
                queue_store.remove_recovery(claim.batch_id)
                queue_store.remove_lease()
                diagnostics = validate_update_queue(worktree)
                if diagnostics:
                    raise UpdateQueueFormatError("\n".join(diagnostics))
                review_diagnostics = validate_update_reviews(worktree)
                if review_diagnostics:
                    raise UpdateQueueFormatError("\n".join(review_diagnostics))
                paths = [
                    *(f"update_queue/candidates/{uid}.json" for uid in claim.lease.candidate_uids),
                    "update_queue/lease.json",
                ]
                if had_recovery:
                    paths.append(f"update_queue/recovery/{claim.batch_id}.json")
                if review_path is not None:
                    paths.append(review_path)
                final_commit = self._commit_paths(
                    worktree,
                    paths,
                    "\n".join(
                        (
                            f"update queue: finalize {claim.batch_id}",
                            "",
                            f"{QUEUE_TOKEN_TRAILER}: {claim.lease.token}",
                            f"{QUEUE_BATCH_TRAILER}: {claim.batch_id}",
                        )
                    ),
                )
            if self._push_cas(upstream, final_commit):
                if not self._advance_active(
                    final_commit,
                    queue_only=False,
                    execution_locked=True,
                ):
                    raise UpdateQueueUnavailable("update published but active checkout could not advance")
                return final_commit
        upstream = self._fetch_upstream()
        recovered = None if upstream is None else self.finalized_batch_commit(
            upstream.commit,
            claim.batch_id,
            token=claim.lease.token,
        )
        if recovered is not None and self._advance_active(
            recovered,
            queue_only=False,
            execution_locked=True,
        ):
            return recovered
        raise UpdateQueueLeaseLost("synchronized update finalization lost its Git lease")

    def release(self, claim: ClaimedUpdateBatch) -> None:
        self._settle_failed_claim(claim, recovery=None)

    def supersede_review(self, claim: ClaimedUpdateBatch) -> str:
        """Terminally consume stale review work without touching its current document."""
        if claim.kind != "review":
            raise ValueError("only a review claim can be superseded as stale")
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable(
                    "cannot supersede stale review work while Git is offline"
                )
            recovered = self.superseded_batch_commit(upstream.commit, claim.batch_id)
            if recovered is not None:
                if not self._advance_active(recovered, queue_only=True):
                    raise UpdateQueueUnavailable(
                        "superseded review could not advance the active checkout"
                    )
                return recovered
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                self._require_claim(snapshot, claim)
                had_recovery = any(
                    item.batch_id == claim.batch_id for item in snapshot.recoveries
                )
                for uid in claim.lease.candidate_uids:
                    queue_store.remove_candidate(uid)
                queue_store.remove_recovery(claim.batch_id)
                queue_store.remove_lease()
                diagnostics = validate_update_queue(worktree)
                if diagnostics:
                    raise UpdateQueueFormatError("\n".join(diagnostics))
                paths = [
                    *(f"update_queue/candidates/{uid}.json" for uid in claim.lease.candidate_uids),
                    "update_queue/lease.json",
                ]
                if had_recovery:
                    paths.append(f"update_queue/recovery/{claim.batch_id}.json")
                commit = self._commit_paths(
                    worktree,
                    paths,
                    "\n".join(
                        (
                            f"update queue: supersede stale review {claim.batch_id}",
                            "",
                            f"{QUEUE_TOKEN_TRAILER}: {claim.lease.token}",
                            f"{QUEUE_SUPERSEDE_TRAILER}: {claim.batch_id}",
                        )
                    ),
                )
            if self._push_cas(upstream, commit):
                if not self._advance_active(commit, queue_only=True):
                    raise UpdateQueueUnavailable(
                        "stale review was superseded but the active checkout could not advance"
                    )
                return commit
        upstream = self._fetch_upstream()
        recovered = None if upstream is None else self.superseded_batch_commit(
            upstream.commit,
            claim.batch_id,
        )
        if recovered is not None and self._advance_active(recovered, queue_only=True):
            return recovered
        raise UpdateQueueLeaseLost("stale review supersession lost its Git lease")

    def fail(self, claim: ClaimedUpdateBatch, *, reason_code: str) -> UpdateQueueRecovery:
        upstream = self._fetch_upstream()
        if upstream is None:
            raise UpdateQueueUnavailable("cannot record synchronized retry state while Git is offline")
        with self._worktree(upstream.commit) as worktree:
            queue_store = UpdateQueueStore(worktree)
            snapshot = queue_store.snapshot()
            self._require_claim(snapshot, claim)
            previous = next(
                (item for item in snapshot.recoveries if item.batch_id == claim.batch_id),
                None,
            )
            attempts = 1 if previous is None else previous.attempts + 1
            manual = attempts >= UPDATE_MAX_AUTOMATIC_ATTEMPTS
            recovery = UpdateQueueRecovery(
                batch_id=claim.batch_id,
                candidate_uids=claim.lease.candidate_uids,
                attempts=attempts,
                reason_code=reason_code,
                retry_at=None
                if manual
                else (_utc_now() + timedelta(seconds=UPDATE_RETRY_COOLDOWN_SECONDS)).isoformat(),
                manual_recovery=manual,
            )
        self._settle_failed_claim(claim, recovery=recovery)
        return recovery

    def retry_manual(self) -> int:
        """Make synchronized manual-recovery batches immediately claimable again."""
        if not self.config.enabled:
            return 0
        retry_token = uuid.uuid4().hex
        intended_count: int | None = None
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable(
                    "cannot retry synchronized batches while Git is offline"
                )
            if intended_count is not None:
                recovered = self._find_commit_with_trailers(
                    upstream.commit,
                    (f"{QUEUE_RETRY_TRAILER}: {retry_token}",),
                )
                if recovered is not None:
                    self._advance_active(recovered, queue_only=True)
                    return intended_count
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                recoveries = [item for item in snapshot.recoveries if item.manual_recovery]
                if not recoveries:
                    return 0
                intended_count = len(recoveries)
                for item in recoveries:
                    queue_store.write_recovery(
                        UpdateQueueRecovery(
                            batch_id=item.batch_id,
                            candidate_uids=item.candidate_uids,
                            attempts=0,
                            reason_code="manual_retry",
                            retry_at=_utc_now().isoformat(),
                        )
                    )
                commit = self._commit_paths(
                    worktree,
                    [f"update_queue/recovery/{item.batch_id}.json" for item in recoveries],
                    "\n".join(
                        (
                            "update queue: retry blocked batches",
                            "",
                            f"{QUEUE_RETRY_TRAILER}: {retry_token}",
                        )
                    ),
                )
            if self._push_cas(upstream, commit):
                self._advance_active(commit, queue_only=True)
                return len(recoveries)
        upstream = self._fetch_upstream()
        if upstream is None:
            raise UpdateQueueUnavailable(
                "cannot settle synchronized manual retry while Git is offline"
            )
        if intended_count is not None:
            recovered = self._find_commit_with_trailers(
                upstream.commit,
                (f"{QUEUE_RETRY_TRAILER}: {retry_token}",),
            )
            if recovered is not None:
                self._advance_active(recovered, queue_only=True)
                return intended_count
        with self._worktree(upstream.commit) as worktree:
            if not any(
                item.manual_recovery
                for item in UpdateQueueStore(worktree).snapshot().recoveries
            ):
                return 0
        raise UpdateQueueUnavailable(
            "synchronized manual retry lost repeated Git races"
        )

    def cancel(
        self,
        session_id: str,
        candidate_reference: int | str,
    ) -> UpdateCandidate | None:
        """Consume one unleased synchronized candidate through a Git CAS."""
        if not self.config.enabled:
            return None
        cancel_token = uuid.uuid4().hex
        selected: UpdateCandidate | None = None
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable("cannot cancel synchronized candidate while Git is offline")
            if selected is not None:
                recovered = self._find_commit_with_trailers(
                    upstream.commit,
                    (f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",),
                )
                if recovered is not None:
                    self._advance_active(recovered, queue_only=True)
                    return selected
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                candidate = _resolve_candidate(
                    snapshot.candidates,
                    session_id,
                    candidate_reference,
                )
                if candidate is None:
                    return None
                selected = candidate
                if snapshot.lease is not None and candidate.uid in snapshot.lease.candidate_uids:
                    raise UpdateQueueLeaseLost("candidate is already in a synchronized update batch")
                changed_paths = [f"update_queue/candidates/{candidate.uid}.json"]
                superseded_batches = self._candidate_lease_batches(
                    upstream.commit,
                    candidate.uid,
                )
                for recovery in snapshot.recoveries:
                    if candidate.uid not in recovery.candidate_uids:
                        continue
                    superseded_batches.add(recovery.batch_id)
                    queue_store.remove_recovery(recovery.batch_id)
                    changed_paths.append(f"update_queue/recovery/{recovery.batch_id}.json")
                    remaining_uids = tuple(
                        uid for uid in recovery.candidate_uids if uid != candidate.uid
                    )
                    if remaining_uids:
                        by_uid = {item.uid: item for item in snapshot.candidates}
                        remaining = tuple(by_uid[uid] for uid in remaining_uids)
                        next_batch_id = update_candidate_batch_id(remaining)
                        replacement = UpdateQueueRecovery(
                            batch_id=next_batch_id,
                            candidate_uids=tuple(sorted(remaining_uids)),
                            attempts=recovery.attempts,
                            reason_code=recovery.reason_code,
                            retry_at=recovery.retry_at,
                            manual_recovery=recovery.manual_recovery,
                        )
                        queue_store.write_recovery(replacement)
                        changed_paths.append(
                            f"update_queue/recovery/{replacement.batch_id}.json"
                        )
                queue_store.remove_candidate(candidate.uid)
                diagnostics = validate_update_queue(worktree)
                if diagnostics:
                    raise UpdateQueueFormatError("\n".join(diagnostics))
                commit = self._commit_paths(
                    worktree,
                    changed_paths,
                    "\n".join(
                        [
                            f"update queue: cancel {candidate.uid}",
                            "",
                            f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",
                            *(
                                f"{QUEUE_SUPERSEDE_TRAILER}: {batch_id}"
                                for batch_id in sorted(superseded_batches)
                            ),
                        ]
                    ),
                )
            if self._push_cas(upstream, commit):
                self._advance_active(commit, queue_only=True)
                return candidate
        upstream = self._fetch_upstream()
        if upstream is not None and selected is not None:
            recovered = self._find_commit_with_trailers(
                upstream.commit,
                (f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",),
            )
            if recovered is not None:
                self._advance_active(recovered, queue_only=True)
                return selected
        raise UpdateQueueUnavailable("candidate cancellation lost repeated Git races")

    def cancel_attempted(self, candidate: UpdateCandidate) -> str:
        """Cancel a local candidate after publication started, fencing an in-flight push."""
        if not self.config.enabled:
            raise UpdateQueueUnavailable(
                "cannot cancel an attempted synchronized candidate without Git sync"
            )
        cancel_token = uuid.uuid4().hex
        path = f"update_queue/candidates/{candidate.uid}.json"
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable(
                    "cannot cancel an attempted synchronized candidate while Git is offline"
                )
            recovered = self._find_commit_with_trailers(
                upstream.commit,
                (f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",),
            )
            if recovered is not None:
                self._advance_active(recovered, queue_only=True)
                return "canceled"

            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                queue_store.snapshot()
                current = queue_store.read_candidate(candidate.uid)
            if current is not None:
                if not candidate_evidence_matches(current, candidate):
                    raise UpdateQueueFormatError(
                        "candidate uid already belongs to different synchronized evidence"
                    )
                removed = self.cancel(candidate.session_id, candidate.uid)
                if removed is not None:
                    return "canceled"
                continue

            published = self._candidate_was_published(upstream.commit, path, candidate)
            if published:
                return "settled"

            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                queue_store.write_candidate(candidate)
                self._commit_paths(
                    worktree,
                    [path],
                    f"update queue: publish {candidate.uid} for cancellation",
                )
                queue_store.remove_candidate(candidate.uid)
                commit = self._commit_paths(
                    worktree,
                    [path],
                    "\n".join(
                        [
                            f"update queue: cancel unpublished {candidate.uid}",
                            "",
                            f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",
                        ]
                    ),
                )
            if self._push_cas(upstream, commit):
                self._advance_active(commit, queue_only=True)
                return "canceled"
        upstream = self._fetch_upstream()
        if upstream is not None:
            recovered = self._find_commit_with_trailers(
                upstream.commit,
                (f"{QUEUE_CANCEL_TRAILER}: {cancel_token}",),
            )
            if recovered is not None:
                self._advance_active(recovered, queue_only=True)
                return "canceled"
        raise UpdateQueueUnavailable(
            "attempted synchronized candidate cancellation lost repeated Git races"
        )

    def clear_local_candidates(self, candidate_uids: tuple[str, ...]) -> None:
        """Clear outbox records only after local scheduler state acknowledges Git authority."""
        for uid in candidate_uids:
            self.store.remove_outbox(uid)
            self.store.clear_publication_marker(uid)

    def finalized_batch_commit(
        self,
        revision: str,
        batch_id: str,
        *,
        token: str | None = None,
    ) -> str | None:
        """Find a finalization with exact batch and optional lease-token trailers."""
        trailers = [f"{QUEUE_BATCH_TRAILER}: {batch_id}"]
        if token is not None:
            trailers.append(f"{QUEUE_TOKEN_TRAILER}: {token}")
        return self._find_commit_with_trailers(revision, tuple(trailers))

    def superseded_batch_commit(
        self,
        revision: str,
        batch_id: str,
    ) -> str | None:
        """Find a terminal queue cancellation for a prepared batch."""
        return self._find_commit_with_trailers(
            revision,
            (f"{QUEUE_SUPERSEDE_TRAILER}: {batch_id}",),
        )

    def fetch_and_integrate_finalized_batch(
        self,
        batch_id: str,
        *,
        token: str | None = None,
    ) -> str | None:
        """Fetch and safely fast-forward through a proven finalized batch."""
        if not self.config.enabled:
            return None
        upstream = self._fetch_upstream()
        if upstream is None:
            raise UpdateQueueUnavailable(
                "cannot recover synchronized finalization while Git is offline"
            )
        commit = self.finalized_batch_commit(
            upstream.commit,
            batch_id,
            token=token,
        )
        if commit is None:
            return None
        if not self._advance_active(commit, queue_only=False):
            raise UpdateQueueUnavailable(
                "finalized update could not advance the active checkout"
            )
        return commit

    def fetch_and_integrate_superseded_batch(
        self,
        batch_id: str,
    ) -> str | None:
        """Fetch and fast-forward through a terminal queue cancellation."""
        if not self.config.enabled:
            return None
        upstream = self._fetch_upstream()
        if upstream is None:
            raise UpdateQueueUnavailable(
                "cannot recover synchronized cancellation while Git is offline"
            )
        commit = self.superseded_batch_commit(upstream.commit, batch_id)
        if commit is None:
            return None
        if not self._advance_active(commit, queue_only=True):
            raise UpdateQueueUnavailable(
                "synchronized cancellation could not advance the active checkout"
            )
        return commit

    def _publish_candidate(self, candidate: UpdateCandidate, upstream: _Upstream) -> str:
        path = f"update_queue/candidates/{candidate.uid}.json"
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            if self._candidate_was_published(upstream.commit, path, candidate):
                return "settled"
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                if candidate.kind == "review":
                    if (
                        candidate.review_id is None
                        or candidate.review_commit is None
                        or candidate.review_blob_oid is None
                    ):
                        raise UpdateQueueFormatError(
                            "review candidate has incomplete source identity"
                        )
                    review_path = f"update_reviews/{candidate.review_id}.md"
                    source_blob = self._git_stdout(
                        "rev-parse",
                        f"{candidate.review_commit}:{review_path}",
                    )
                    if source_blob != candidate.review_blob_oid:
                        raise UpdateQueueFormatError(
                            "review candidate source commit does not contain its review blob"
                        )
                    if not self._is_ancestor(candidate.review_commit, upstream.commit):
                        # The Update or prior question may not have synchronized yet.
                        return "unresolved"
                    current_blob = tracked_review_blob_oid(
                        worktree,
                        candidate.review_id,
                    )
                    current_commit = tracked_review_commit(
                        worktree,
                        candidate.review_id,
                    )
                    if (
                        current_commit != candidate.review_commit
                        or current_blob != candidate.review_blob_oid
                    ):
                        return "settled"
                    pending = next(
                        (
                            item
                            for item in snapshot.candidates
                            if item.kind == "review"
                            and item.review_id == candidate.review_id
                        ),
                        None,
                    )
                    if pending is not None:
                        return (
                            "settled"
                            if candidate_evidence_matches(pending, candidate)
                            else "unresolved"
                        )
                    marker = self.store.begin_publication(
                        candidate.uid,
                        attempted_at=_utc_now().isoformat(),
                    )
                else:
                    marker = AsyncUpdateStore(self.memory_root, "update").begin_publication(
                        candidate,
                        attempted_at=_utc_now().isoformat(),
                    )
                if marker is None:
                    return "unresolved"
                queue_store.write_candidate(candidate)
                diagnostics = validate_update_queue(worktree)
                if diagnostics:
                    raise UpdateQueueFormatError("\n".join(diagnostics))
                commit = self._commit_paths(
                    worktree,
                    [path],
                    f"update queue: publish {candidate.uid}",
                )
            self.store.record_publication_commit(candidate.uid, commit)
            if self._push_cas(upstream, commit):
                return "published"
            refreshed = self._fetch_upstream()
            if refreshed is None:
                return "unresolved"
            upstream = refreshed
        return "unresolved"

    def _settle_failed_claim(
        self,
        claim: ClaimedUpdateBatch,
        *,
        recovery: UpdateQueueRecovery | None,
    ) -> None:
        for _attempt in range(QUEUE_PUSH_ATTEMPTS):
            upstream = self._fetch_upstream()
            if upstream is None:
                raise UpdateQueueUnavailable("cannot release synchronized lease while Git is offline")
            with self._worktree(upstream.commit) as worktree:
                queue_store = UpdateQueueStore(worktree)
                snapshot = queue_store.snapshot()
                if snapshot.lease is None:
                    return
                self._require_claim(snapshot, claim)
                if recovery is not None:
                    queue_store.write_recovery(recovery)
                queue_store.remove_lease()
                paths = ["update_queue/lease.json"]
                if recovery is not None:
                    paths.append(f"update_queue/recovery/{recovery.batch_id}.json")
                commit = self._commit_paths(
                    worktree,
                    paths,
                    f"update queue: release {claim.batch_id}",
                )
            if self._push_cas(upstream, commit):
                self._advance_active(commit, queue_only=True)
                return
        raise UpdateQueueLeaseLost("could not release synchronized update lease")

    def _require_claim(self, snapshot: UpdateQueueSnapshot, claim: ClaimedUpdateBatch) -> None:
        lease = snapshot.lease
        if lease is None or lease.token != claim.lease.token:
            raise UpdateQueueLeaseLost("synchronized update lease is no longer current")
        if lease.holder != self.device_id or lease.batch_id != claim.batch_id:
            raise UpdateQueueLeaseLost("synchronized update lease belongs to another device")
        if lease.candidate_uids != claim.lease.candidate_uids:
            raise UpdateQueueLeaseLost("synchronized update lease candidate set changed")

    def _fetch_upstream(self) -> _Upstream | None:
        upstream_name = self._git_stdout("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if not upstream_name or "/" not in upstream_name:
            return None
        remote, branch = upstream_name.split("/", 1)
        if not remote or not branch:
            return None
        fetched = self._git("fetch", remote, check=False)
        if fetched.returncode != 0:
            return None
        commit = self._resolve(upstream_name)
        if commit is None:
            return None
        return _Upstream(remote, branch, upstream_name, commit)

    def _push_cas(self, upstream: _Upstream, commit: str) -> bool:
        result = self._git(
            "push",
            f"--force-with-lease={upstream.branch_ref}:{upstream.commit}",
            upstream.remote,
            f"{commit}:{upstream.branch_ref}",
            check=False,
        )
        return result.returncode == 0

    def _advance_active(
        self,
        commit: str,
        *,
        queue_only: bool,
        execution_locked: bool = False,
    ) -> bool:
        operation_store = SemanticOperationStore(self.memory_root)
        execution = (
            nullcontext()
            if execution_locked
            else operation_store.execution_locked()
        )
        with execution, MemoryWriteLock(self.memory_root):
            return self._advance_active_locked(commit, queue_only=queue_only)

    def _advance_active_locked(self, commit: str, *, queue_only: bool) -> bool:
        """Advance HEAD while both semantic-execution and memory-write locks are held."""
        blocked = SyncManager(self.config)._active_preflight()
        if blocked is not None:
            return False
        head = self._resolve("HEAD")
        if head is None:
            return False
        if head == commit or self._is_ancestor(commit, head):
            return True
        if not self._is_ancestor(head, commit):
            return False
        if queue_only and any(
            not is_update_coordination_path(path) for path in self._changed_paths(head, commit)
        ):
            return False
        merged = self._git("merge", "--ff-only", commit, check=False)
        if merged.returncode != 0 or self._resolve("HEAD") != commit:
            return False
        return SyncManager(self.config)._active_preflight() is None

    @contextmanager
    def _worktree(self, commit: str):
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        root = self.memory_root / ".runtime" / "worktrees"
        root.mkdir(parents=True, exist_ok=True)
        path = root / f"update-queue-{uuid.uuid4().hex}"
        added = self._git("worktree", "add", "--detach", str(path), commit, check=False)
        if added.returncode != 0:
            raise UpdateQueueUnavailable(_git_error(added))
        try:
            yield path
        finally:
            self._git("worktree", "remove", "--force", str(path), check=False)
            shutil.rmtree(path, ignore_errors=True)
            self._git("worktree", "prune", check=False)

    def _commit_paths(self, worktree: Path, paths: list[str], message: str) -> str:
        added = self._git("add", "-f", "-A", "--", *paths, cwd=worktree, check=False)
        if added.returncode != 0:
            raise UpdateQueueUnavailable(_git_error(added))
        committed = self._git(
            "-c",
            f"user.name={QUEUE_COMMIT_NAME}",
            "-c",
            f"user.email={QUEUE_COMMIT_EMAIL}",
            "commit",
            "-m",
            message,
            cwd=worktree,
            check=False,
        )
        if committed.returncode != 0:
            raise UpdateQueueUnavailable(_git_error(committed))
        commit = self._git_stdout("rev-parse", "HEAD", cwd=worktree)
        if not _OID_RE.fullmatch(commit):
            raise UpdateQueueUnavailable("could not resolve update queue commit")
        return commit

    def _find_commit_with_trailers(
        self,
        revision: str,
        trailers: tuple[str, ...],
    ) -> str | None:
        if not trailers:
            raise ValueError("at least one commit trailer is required")
        result = self._git(
            "log",
            "--fixed-strings",
            f"--grep={trailers[0]}",
            "--format=%H",
            revision,
            check=False,
        )
        if result.returncode != 0:
            return None
        for commit in result.stdout.splitlines():
            commit = commit.strip()
            if not _OID_RE.fullmatch(commit):
                continue
            message = self._git("show", "-s", "--format=%B", commit, check=False)
            if message.returncode != 0:
                continue
            lines = set(message.stdout.splitlines())
            if all(trailer in lines for trailer in trailers):
                return commit
        return None

    def _candidate_was_published(
        self,
        revision: str,
        path: str,
        candidate: UpdateCandidate,
    ) -> bool:
        history = self._git(
            "log",
            "--format=%H",
            revision,
            "--",
            path,
            check=False,
        )
        if history.returncode != 0:
            raise UpdateQueueUnavailable("could not inspect synchronized candidate history")
        revisions = [revision, *history.stdout.splitlines()]
        found = False
        inspected: set[str] = set()
        for commit in revisions:
            commit = commit.strip()
            if not commit or commit in inspected:
                continue
            inspected.add(commit)
            shown = self._git("show", f"{commit}:{path}", check=False)
            if shown.returncode != 0:
                continue
            try:
                existing = parse_update_candidate_json(shown.stdout)
            except (json.JSONDecodeError, UpdateQueueFormatError) as exc:
                raise UpdateQueueFormatError(
                    f"published candidate history is malformed: {path}"
                ) from exc
            found = True
            if not candidate_evidence_matches(existing, candidate):
                raise UpdateQueueFormatError(
                    "candidate uid already belongs to different synchronized evidence"
                )
        return found

    def _candidate_lease_batches(self, revision: str, candidate_uid: str) -> set[str]:
        path = "update_queue/lease.json"
        history = self._git(
            "log",
            "--format=%H",
            revision,
            "--",
            path,
            check=False,
        )
        if history.returncode != 0:
            raise UpdateQueueUnavailable("could not inspect synchronized lease history")
        batches: set[str] = set()
        inspected: set[str] = set()
        for commit in [revision, *history.stdout.splitlines()]:
            commit = commit.strip()
            if not commit or commit in inspected:
                continue
            inspected.add(commit)
            shown = self._git("show", f"{commit}:{path}", check=False)
            if shown.returncode != 0:
                continue
            try:
                lease = parse_update_queue_lease_json(shown.stdout)
            except (json.JSONDecodeError, UpdateQueueFormatError) as exc:
                raise UpdateQueueFormatError("published queue lease history is malformed") from exc
            if candidate_uid in lease.candidate_uids:
                batches.add(lease.batch_id)
        return batches

    def _changed_paths(self, start: str, end: str) -> list[str]:
        if start == end:
            return []
        result = self._git("diff", "--name-only", "-z", start, end, check=False)
        if result.returncode != 0:
            raise UpdateQueueUnavailable("could not inspect synchronized queue history")
        return sorted(path for path in result.stdout.split("\0") if path)

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return self._git(
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
            check=False,
        ).returncode == 0

    def _resolve(self, revision: str) -> str | None:
        value = self._git_stdout("rev-parse", "--verify", f"{revision}^{{commit}}")
        return value if _OID_RE.fullmatch(value) else None

    def _git_stdout(self, *args: str, cwd: Path | None = None) -> str:
        result = self._git(*args, cwd=cwd, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""

    def _git(
        self,
        *args: str,
        cwd: Path | None = None,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "true"
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.memory_root if cwd is None else cwd,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                timeout=GIT_TIMEOUT_SECONDS,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            result = subprocess.CompletedProcess(["git", *args], 1, "", str(exc))
        if check and result.returncode != 0:
            raise UpdateQueueUnavailable(_git_error(result))
        return result


def _select_candidates(
    snapshot: UpdateQueueSnapshot,
    *,
    target_batch_candidates: int,
    max_wait_seconds: int,
    now: datetime,
) -> tuple[tuple[UpdateCandidate, ...], datetime | None]:
    by_uid = {item.uid: item for item in snapshot.candidates}
    if snapshot.lease is not None and _lease_expired(snapshot.lease, now):
        return tuple(by_uid[uid] for uid in snapshot.lease.candidate_uids), None

    deadlines: list[datetime] = []
    for recovery in sorted(snapshot.recoveries, key=lambda item: item.batch_id):
        if recovery.manual_recovery:
            continue
        retry_at = _parse_time(recovery.retry_at or "")
        if retry_at <= now:
            candidates = tuple(by_uid[uid] for uid in recovery.candidate_uids)
            expected_batch_id = update_candidate_batch_id(candidates)
            if recovery.batch_id != expected_batch_id:
                raise UpdateQueueFormatError(
                    "recovery batch_id does not match its candidate evidence"
                )
            return candidates, None
        deadlines.append(retry_at)

    recovered_sessions = {
        by_uid[uid].session_id
        for recovery in snapshot.recoveries
        for uid in recovery.candidate_uids
    }
    reviews = sorted(
        (
            candidate
            for candidate in snapshot.candidates
            if candidate.kind == "review" and candidate.session_id not in recovered_sessions
        ),
        key=lambda item: (item.submitted_at, item.uid),
    )
    if reviews:
        return (reviews[0],), None

    sessions: dict[str, list[UpdateCandidate]] = {}
    for candidate in snapshot.candidates:
        if candidate.kind == "update" and candidate.session_id not in recovered_sessions:
            sessions.setdefault(candidate.session_id, []).append(candidate)

    eligible: list[tuple[datetime, str, tuple[UpdateCandidate, ...]]] = []
    for session_id, raw_candidates in sessions.items():
        candidates = tuple(sorted(raw_candidates, key=lambda item: (item.submitted_at, item.uid)))
        ready_at = max(_parse_time(item.submitted_at) for item in candidates) + timedelta(
            seconds=UPDATE_DEBOUNCE_SECONDS
        )
        if ready_at <= now or len(candidates) >= target_batch_candidates:
            eligible.append((ready_at, session_id, candidates))
        else:
            deadlines.append(ready_at)

    eligible.sort(key=lambda item: (item[0], item[1]))
    selected: list[UpdateCandidate] = []
    for _ready_at, _session_id, candidates in eligible:
        selected.extend(candidates)
        if len(selected) >= target_batch_candidates:
            return tuple(selected), None
    if not eligible:
        return (), min(deadlines) if deadlines else None
    fallback = eligible[0][0] + timedelta(seconds=max_wait_seconds)
    if fallback <= now:
        return tuple(selected), None
    deadlines.append(fallback)
    return (), min(deadlines)


def _session_batches(candidates: tuple[UpdateCandidate, ...]) -> list[AsyncUpdateSessionBatch]:
    if any(candidate.kind != "update" for candidate in candidates):
        raise UpdateQueueFormatError("review candidates cannot use Update session batching")
    sessions: dict[str, list[UpdateCandidate]] = {}
    for candidate in candidates:
        sessions.setdefault(candidate.session_id, []).append(candidate)
    batches = []
    for session_id in sorted(sessions):
        jobs = [
            AsyncUpdateJob(
                id=item.display_id,
                candidate_uid=item.uid,
                message=item.message,
                submitted_at=item.submitted_at,
            )
            for item in sorted(sessions[session_id], key=lambda item: (item.submitted_at, item.uid))
        ]
        batches.append(AsyncUpdateSessionBatch(session_id, _utc_now(), jobs))
    return batches


def _resolve_candidate(
    candidates: tuple[UpdateCandidate, ...],
    session_id: str,
    reference: int | str,
) -> UpdateCandidate | None:
    scoped = [item for item in candidates if item.session_id == session_id]
    if isinstance(reference, int) and not isinstance(reference, bool):
        matches = [item for item in scoped if item.display_id == reference]
    elif isinstance(reference, str):
        if not re.fullmatch(r"[0-9a-f]{8,32}", reference):
            raise ValueError("candidate uid prefix must contain 8 to 32 lowercase hexadecimal characters")
        matches = [item for item in scoped if item.uid.startswith(reference)]
    else:
        raise ValueError("candidate reference must be an integer id or uid prefix")
    if len(matches) > 1:
        raise ValueError("candidate reference is ambiguous; use its uid prefix")
    return matches[0] if matches else None


def _lease_expired(lease: UpdateQueueLease, now: datetime) -> bool:
    return _parse_time(lease.expires_at) <= now


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise UpdateQueueFormatError("queue datetime must include a timezone")
    return parsed.astimezone(UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _load_device_id(memory_root: Path) -> str:
    runtime_root = memory_root / ".runtime"
    path = runtime_root / "update_queue" / "device.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name("device.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        lock_file(lock_handle)
        try:
            _ensure_runtime_gitignore(runtime_root)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                data = None
            if isinstance(data, dict) and re.fullmatch(
                r"[0-9a-f]{32}",
                str(data.get("device_id", "")),
            ):
                return str(data["device_id"])
            device_id = uuid.uuid4().hex
            temporary = path.with_name(
                f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
            )
            content = json.dumps({"device_id": device_id}, indent=2, sort_keys=True) + "\n"
            with temporary.open("w", encoding="utf-8") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            _fsync_directory(path.parent)
            return device_id
        finally:
            unlock_file(lock_handle)


def _git_error(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "Git command failed").strip()

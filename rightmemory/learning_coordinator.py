"""One Git-backed claim and publication lifecycle for all candidate consumers."""
from __future__ import annotations

import re
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .config import SyncConfig, load_curation_batch_config
from .curation_batch import select_curation_batch
from .git_storage import git, git_result, require_repository_root
from .isolated_write import IsolatedWriteSupervisor
from .learning import (
    LEARNING_DOMAINS, LearningCandidate, LearningClaim, learning_claims,
    learning_validation_errors, list_learning_candidates, parse_learning_claim,
    record_learning_processed, remove_learning_claim, withdraw_learning_candidate,
    write_learning_claim,
    learning_batch_candidate_ids, write_learning_batch,
)
from .platform import lock_file, unlock_file
from .semantic_operation import SemanticOperationStore
from .session import MemoryWriteLock, _ensure_runtime_gitignore, _fsync_directory
from .sync import SyncManager
from .tools import MemoryTools


_TOKEN_TRAILER = "RightMemory-Curation-Token"
_TASK_TRAILER = "RightMemory-Curation-Task"
_PUSH_ATTEMPTS = 4


class LearningUnavailable(RuntimeError):
    """The authoritative state could not be reached or published safely."""


class LearningClaimLost(RuntimeError):
    """The task has been replaced, withdrawn, or completed by another owner."""


class LearningSemanticBaseChanged(RuntimeError):
    """The prepared judgment must be revisited against newer domain state."""


@dataclass(frozen=True)
class ClaimResult:
    claim: LearningClaim | None = None
    candidates: tuple[LearningCandidate, ...] = ()
    claimed: bool = False
    online: bool = True
    foreign: bool = False
    reason: str = ""


@dataclass(frozen=True)
class _Upstream:
    remote: str
    branch: str
    revision: str
    commit: str


class LearningCoordinator:
    """Independent domain claims over the same immutable evidence collection.

    A claim has no clock-based expiry. Explicit replacement changes its token,
    fencing every older worker. Local and synchronized roots use the same state;
    enabling sync changes publication authority, never the candidate lifecycle.
    """

    def __init__(self, config: SyncConfig, holder_id: str | None = None):
        self.config = config
        self.memory_root = Path(config.memory_root).expanduser().resolve()
        self.batch_config = load_curation_batch_config(self.memory_root)
        self.root = self.memory_root / ".runtime" / "learning"
        self._holder_id = holder_id
        if holder_id is not None:
            _require_token(holder_id)

    @property
    def holder_id(self) -> str | None:
        """Read the local identity without creating runtime state for notices."""
        if self._holder_id is not None:
            return self._holder_id
        self._check_runtime_directories()
        path = self.root / "holder"
        if path.is_symlink():
            raise ValueError("curator holder must not be a symbolic link")
        try:
            value = path.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return None
        _require_token(value)
        return value

    def current_claim(self, domain: str) -> LearningClaim | None:
        _require_domain(domain)
        return next((claim for claim in learning_claims(self.memory_root) if claim.domain == domain), None)

    def claim(
        self, domain: str, *, mode: str = "update", scope: str = "",
        task_id: str | None = None, candidate_ids: tuple[str, ...] | list[str] | None = None,
        replace_token: str | None = None,
    ) -> ClaimResult:
        _require_domain(domain)
        if mode not in {"update", "maintain"} or not isinstance(scope, str):
            raise ValueError("invalid curation mode or scope")
        if replace_token is not None:
            _require_token(replace_token)
        with self._locked():
            holder = self._ensure_holder()
            self._synchronize()
            if mode == "update" and replace_token is None and self.current_claim(domain) is None:
                pending = list_learning_candidates(self.memory_root, domain=domain)
                if not select_curation_batch(
                    pending, self.batch_config,
                    admitted_candidate_ids=learning_batch_candidate_ids(self.memory_root, domain),
                ).candidates:
                    return ClaimResult(reason=(
                        "batch is waiting for more candidates or its maximum wait"
                        if pending else "no pending candidates"
                    ))
            for _attempt in range(_PUSH_ATTEMPTS):
                base, upstream = self._base()
                with self._worktree(base) as worktree:
                    batch_path = None
                    claims = {item.domain: item for item in learning_claims(worktree)}
                    existing = claims.get(domain)
                    pending = {item.candidate_id: item for item in list_learning_candidates(worktree, domain=domain)}
                    if existing is not None and replace_token is None:
                        return ClaimResult(
                            existing, tuple(pending[item] for item in existing.candidate_ids),
                            foreign=existing.holder != holder, reason="domain already has an active task",
                        )
                    if replace_token is not None:
                        if existing is None or existing.token != replace_token:
                            raise LearningClaimLost("only the current domain claim can be replaced")
                        mode, scope = existing.mode, existing.scope
                        identifiers = existing.candidate_ids
                        remove_learning_claim(worktree, existing)
                    else:
                        admitted = learning_batch_candidate_ids(worktree, domain)
                        batch = select_curation_batch(
                            tuple(pending.values()), self.batch_config, admitted_candidate_ids=admitted,
                        ) if mode == "update" else None
                        if batch is not None and not batch.candidates:
                            return ClaimResult(reason=(
                                "batch is waiting for more candidates or its maximum wait"
                                if pending else "no pending candidates"
                            ))
                        identifiers = (
                            (() if batch is None else tuple(item.candidate_id for item in batch.candidates))
                            if candidate_ids is None else tuple(candidate_ids)
                        )
                        if batch is not None and not set(identifiers).issubset(
                            item.candidate_id for item in batch.candidates
                        ):
                            raise LearningClaimLost("selected evidence is not ready in this domain batch")
                    if mode == "maintain":
                        if identifiers:
                            raise ValueError("maintenance does not claim candidate evidence")
                    elif not identifiers:
                        return ClaimResult(reason="no pending candidates")
                    if any(item not in pending for item in identifiers):
                        raise LearningClaimLost("selected evidence is no longer pending for this domain")
                    if replace_token is None and batch is not None and batch.admitted_candidate_ids != admitted:
                        batch_path = write_learning_batch(worktree, domain, batch.admitted_candidate_ids)
                    claim = parse_learning_claim(vars(LearningClaim(
                        domain, holder, uuid.uuid4().hex,
                        task_id or f"{domain}-{uuid.uuid4().hex}", base, identifiers,
                        tuple(pending[item].evidence_sha256 for item in identifiers), mode, scope,
                    )))
                    path = write_learning_claim(worktree, claim)
                    commit = self._commit(
                        worktree, [path] + ([] if batch_path is None else [batch_path]), f"learning: claim {domain}",
                    )
                if self._publish(base, upstream, commit):
                    return ClaimResult(claim, tuple(pending[item] for item in identifiers), claimed=True)
            raise LearningUnavailable("curator claim lost repeated Git publication races")

    def finalize(
        self, claim: LearningClaim, candidate_commit: str | None, *, prepared_start_commit: str,
    ) -> str:
        claim = parse_learning_claim(vars(claim))
        _require_commit(prepared_start_commit)
        if candidate_commit is not None:
            _require_commit(candidate_commit)
        with self._locked():
            if claim.holder != self.holder_id:
                raise LearningClaimLost("curator claim belongs to another device")
            landed = self.finalized_commit(claim)
            if landed is not None:
                return landed
            self._synchronize()
            for _attempt in range(_PUSH_ATTEMPTS):
                base, upstream = self._base()
                landed = self._find_finalized(base, claim)
                if landed is not None:
                    self._advance_active(base)
                    return landed
                with self._worktree(base) as worktree:
                    self._require_claim(worktree, claim)
                    self._check_semantic_base(claim, prepared_start_commit, base)
                    role = "update" if claim.domain == "memory" else f"{claim.domain}-curator"
                    if candidate_commit is not None:
                        parents = git(self.memory_root, "rev-list", "--parents", "-n", "1", candidate_commit).split()
                        if parents != [candidate_commit, prepared_start_commit]:
                            raise LearningSemanticBaseChanged("prepared result is not one commit on its declared base")
                        supervisor = IsolatedWriteSupervisor(self.memory_root, role)
                        supervisor._validate_commits(self.memory_root, [candidate_commit])
                        result = git_result(worktree, "cherry-pick", "--allow-empty", candidate_commit)
                        if result.returncode:
                            git_result(worktree, "cherry-pick", "--abort")
                            raise LearningSemanticBaseChanged("prepared result no longer applies to domain state")
                        supervisor._validate_commits(worktree, [git(worktree, "rev-parse", "HEAD")])
                    paths = [remove_learning_claim(worktree, claim)]
                    if claim.mode == "update":
                        paths.extend(record_learning_processed(worktree, claim.domain, claim.candidate_ids))
                    self._validate(worktree, role)
                    commit = self._commit(
                        worktree, paths,
                        f"learning: complete {claim.domain}\n\n"
                        f"{_TOKEN_TRAILER}: {claim.token}\n{_TASK_TRAILER}: {claim.task_id}",
                    )
                if self._publish(base, upstream, commit):
                    return commit
            base, _upstream = self._base()
            landed = self._find_finalized(base, claim)
            if landed is not None:
                self._advance_active(base)
                return landed
            raise LearningUnavailable("curator result remains prepared after repeated Git publication races")

    def finalized_commit(self, claim: LearningClaim) -> str | None:
        """Read terminal publication proof from local history; never fetch or write."""
        return self._find_finalized("HEAD", parse_learning_claim(vars(claim)))

    def release(self, claim: LearningClaim) -> None:
        claim = parse_learning_claim(vars(claim))
        with self._locked():
            self._synchronize()
            for _attempt in range(_PUSH_ATTEMPTS):
                base, upstream = self._base()
                with self._worktree(base) as worktree:
                    current = {item.domain: item for item in learning_claims(worktree)}.get(claim.domain)
                    if current is None:
                        return
                    self._require_claim(worktree, claim)
                    path = remove_learning_claim(worktree, claim)
                    commit = self._commit(worktree, [path], f"learning: release {claim.domain}")
                if self._publish(base, upstream, commit):
                    return
            raise LearningUnavailable("claim release lost repeated Git publication races")

    def withdraw(self, candidate_id: str) -> str:
        """Stop unclaimed work; this never undoes an already curated consequence."""
        with self._locked():
            self._synchronize()
            for _attempt in range(_PUSH_ATTEMPTS):
                base, upstream = self._base()
                with self._worktree(base) as worktree:
                    paths = withdraw_learning_candidate(worktree, candidate_id)
                    withdrawn = (worktree / "learning" / "withdrawn" / f"{candidate_id}.json").is_file()
                    status = "withdrawn" if withdrawn else "completed"
                    if not paths:
                        return status
                    commit = self._commit(worktree, list(paths), f"learning: withdraw {candidate_id}")
                if self._publish(base, upstream, commit):
                    return status
            raise LearningUnavailable("candidate withdrawal lost repeated Git publication races")

    def _check_semantic_base(self, claim: LearningClaim, prepared_base: str, current: str) -> None:
        role = "update" if claim.domain == "memory" else f"{claim.domain}-curator"
        supervisor = IsolatedWriteSupervisor(self.memory_root, role)
        for end in {current, git(self.memory_root, "rev-parse", "HEAD")}:
            if git_result(self.memory_root, "merge-base", "--is-ancestor", prepared_base, end).returncode:
                raise LearningSemanticBaseChanged("prepared result is no longer based on current history")
            changed = git(self.memory_root, "diff", "--name-only", "--no-renames", "-z", prepared_base, end)
            semantic = [path for path in changed.split("\0") if path and supervisor._is_semantic_read_path(path)]
            if semantic:
                raise LearningSemanticBaseChanged("domain context changed after preparation: " + ", ".join(semantic))

    def _require_claim(self, root: Path, claim: LearningClaim) -> None:
        current = next((item for item in learning_claims(root) if item.domain == claim.domain), None)
        if current != claim:
            raise LearningClaimLost("curator claim was replaced or completed")

    def _synchronize(self) -> None:
        require_repository_root(self.memory_root)
        if self.config.enabled:
            result = SyncManager(self.config).push()
            if result.status != "pushed":
                raise LearningUnavailable(f"candidate synchronization required: {result.message}")
        blocked = SyncManager(self.config)._active_preflight()
        if blocked is not None:
            raise LearningUnavailable(blocked.message)

    def _base(self) -> tuple[str, _Upstream | None]:
        if not self.config.enabled:
            return git(self.memory_root, "rev-parse", "HEAD"), None
        revision = git(self.memory_root, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if "/" not in revision:
            raise LearningUnavailable("candidate coordination requires an upstream branch")
        remote, branch = revision.split("/", 1)
        fetched = git_result(self.memory_root, "fetch", remote)
        if fetched.returncode:
            raise LearningUnavailable("cannot reach synchronized candidate authority")
        base = git(self.memory_root, "rev-parse", revision)
        self._advance_active(base)
        return base, _Upstream(remote, branch, revision, base)

    def _publish(self, base: str, upstream: _Upstream | None, commit: str) -> bool:
        if upstream is not None:
            result = git_result(
                self.memory_root, "push", f"--force-with-lease=refs/heads/{upstream.branch}:{base}",
                upstream.remote, f"{commit}:refs/heads/{upstream.branch}",
            )
            if result.returncode:
                return False
            self._advance_active(commit)
            return True
        with SemanticOperationStore(self.memory_root).execution_locked(), MemoryWriteLock(self.memory_root):
            if git(self.memory_root, "rev-parse", "HEAD") != base:
                return False
            blocked = SyncManager(self.config)._active_preflight()
            if blocked is not None:
                raise LearningUnavailable(blocked.message)
            with self._worktree(commit) as incoming:
                invalid = SyncManager(self.config)._validate_candidate(incoming, base, commit)
                if invalid is not None:
                    raise LearningUnavailable(invalid.message)
            git(self.memory_root, "merge", "--ff-only", "--no-overwrite-ignore", commit)
        return True

    def _advance_active(self, commit: str) -> None:
        with SemanticOperationStore(self.memory_root).execution_locked(), MemoryWriteLock(self.memory_root):
            head = git(self.memory_root, "rev-parse", "HEAD")
            if head == commit or git_result(self.memory_root, "merge-base", "--is-ancestor", commit, head).returncode == 0:
                return
            if git_result(self.memory_root, "merge-base", "--is-ancestor", head, commit).returncode:
                raise LearningUnavailable("published candidate state requires sync before the active root can advance")
            blocked = SyncManager(self.config)._active_preflight()
            if blocked is not None:
                raise LearningUnavailable(blocked.message)
            with self._worktree(commit) as incoming:
                invalid = SyncManager(self.config)._validate_candidate(incoming, head, commit)
                if invalid is not None:
                    raise LearningUnavailable(invalid.message)
            git(self.memory_root, "merge", "--ff-only", "--no-overwrite-ignore", commit)

    def _find_finalized(self, revision: str, claim: LearningClaim) -> str | None:
        token_line = f"{_TOKEN_TRAILER}: {claim.token}"
        result = git_result(self.memory_root, "log", "--fixed-strings", f"--grep={token_line}", "--format=%H", revision)
        if result.returncode:
            return None
        expected = {f"learning: complete {claim.domain}", token_line, f"{_TASK_TRAILER}: {claim.task_id}"}
        for commit in result.stdout.splitlines():
            message = git(self.memory_root, "show", "-s", "--format=%B", commit)
            if expected <= set(message.splitlines()):
                return commit
        return None

    def _validate(self, root: Path, role: str) -> None:
        errors = learning_validation_errors(root)
        if errors:
            raise ValueError("invalid candidate lifecycle: " + "; ".join(errors))
        result = MemoryTools(root, role=role).validate_memory(enforce_correction_capacity=False)
        if result.startswith("validation failed:"):
            raise ValueError(result)

    def _commit(self, root: Path, paths: list[Path], message: str) -> str:
        relative = [path.relative_to(root).as_posix() for path in paths]
        before = git(root, "rev-parse", "HEAD")
        git(root, "add", "-f", "-A", "--", *relative)
        git(root, "-c", "user.name=RightMemory", "-c", "user.email=rightmemory@local", "commit", "-m", message)
        commit = git(root, "rev-parse", "HEAD")
        changed = set(git(root, "diff", "--name-only", "--no-renames", "-z", before, commit).split("\0")) - {""}
        if changed != set(relative) or git(root, "status", "--porcelain"):
            raise LearningUnavailable("candidate transaction changed paths outside its declared artifacts")
        return commit

    def _ensure_holder(self) -> str:
        existing = self.holder_id
        if existing is not None:
            return existing
        value = uuid.uuid4().hex
        path = self.root / "holder"
        with path.open("x", encoding="utf-8") as handle:
            handle.write(value + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        _fsync_directory(path.parent)
        return value

    @contextmanager
    def _locked(self):
        self._check_runtime_directories()
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.root.mkdir(parents=True, exist_ok=True)
        if self.root.is_symlink():
            raise ValueError("curator runtime must not be a symbolic link")
        path = self.root / "coordination.lock"
        if path.is_symlink():
            raise ValueError("curator coordination lock must not be a symbolic link")
        with path.open("a+") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    @contextmanager
    def _worktree(self, base: str):
        self._check_runtime_directories()
        path = self.memory_root / ".runtime" / "worktrees" / f"curation-{uuid.uuid4().hex}"
        try:
            git(self.memory_root, "worktree", "add", "--detach", str(path), base)
            yield path
        finally:
            git_result(self.memory_root, "worktree", "remove", "--force", str(path))

    def _check_runtime_directories(self) -> None:
        for path in (self.memory_root / ".runtime", self.root, self.memory_root / ".runtime" / "worktrees"):
            if path.is_symlink():
                raise ValueError("curator runtime directory must not be a symbolic link")


def _require_token(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("invalid curator ownership token")


def _require_commit(value: str) -> None:
    if not isinstance(value, str) or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value) is None:
        raise ValueError("curator publication requires a full commit hash")


def _require_domain(value: str) -> None:
    if value not in LEARNING_DOMAINS:
        raise ValueError("unknown curation domain")

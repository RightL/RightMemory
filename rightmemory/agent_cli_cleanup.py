from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from .codex_app_server import CodexAppServerClient, CodexThreadDeleteResult
from .platform import lock_file, unlock_file
from .provider_prefixes import ProviderPrefixStore
from .provider_sessions import ProviderSessionStore
from .provider_threads import ProviderThreadRecord, ProviderThreadStore
from .recent_submitted import RecentSubmittedMemoryDeliveryStore
from .retrieve_context import RetrieveContextStore
from .session import MessageSessionStore, _ensure_runtime_gitignore


CODEX_THREAD_RETENTION = timedelta(hours=24)


def provider_thread_is_expired(
    record: ProviderThreadRecord,
    *,
    now: datetime | None = None,
) -> bool:
    current = _utc(now or datetime.now(UTC))
    return _is_due(record, current - CODEX_THREAD_RETENTION)


@dataclass(frozen=True)
class AgentCliCleanupResult:
    deleted: int = 0
    pending: int = 0
    skipped: int = 0
    malformed: int = 0
    errors: tuple[str, ...] = ()

    def format(self) -> str:
        lines = [
            f"deleted: {self.deleted}",
            f"pending: {self.pending}",
            f"skipped: {self.skipped}",
            f"malformed: {self.malformed}",
        ]
        lines.extend(f"error: {error}" for error in self.errors)
        return "\n".join(lines)


class AgentCliThreadCleanup:
    def __init__(
        self,
        memory_root: Path,
        *,
        now: Callable[[], datetime] | None = None,
        client: Any | None = None,
    ):
        self.memory_root = Path(memory_root)
        self.store = ProviderThreadStore(self.memory_root)
        self._now = now or (lambda: datetime.now(UTC))
        self.client = client or CodexAppServerClient(self.memory_root)

    def has_expired_codex_threads(self) -> bool:
        now = _utc(self._now())
        return any(provider_thread_is_expired(record, now=now) for record in self.store.scan("codex").records)

    def run(self) -> AgentCliCleanupResult:
        with _CleanupLock(self.memory_root):
            now = _utc(self._now())
            now_text = now.isoformat()
            cutoff = now - CODEX_THREAD_RETENTION
            scan = self.store.scan("codex")
            errors = [f"{item.path}: {item.error}" for item in scan.malformed]
            skipped = 0
            deleted = 0
            pending = 0

            non_base_due: list[ProviderThreadRecord] = []
            fork_bases: list[ProviderThreadRecord] = []
            for scanned in scan.records:
                if scanned.policy == "fork-base":
                    fork_bases.append(scanned)
                    continue
                if not _is_due(scanned, cutoff):
                    skipped += 1
                    continue
                prepared = self._prepare_for_deletion(scanned, cutoff=cutoff, attempted_at=now_text)
                if prepared is None:
                    skipped += 1
                    continue
                non_base_due.append(prepared)

            stage_deleted, stage_pending, stage_errors = self._delete_records(
                non_base_due,
                attempted_at=now_text,
            )
            deleted += stage_deleted
            pending += stage_pending
            errors.extend(stage_errors)

            base_due: list[ProviderThreadRecord] = []
            for scanned in fork_bases:
                if not _is_due(scanned, cutoff):
                    skipped += 1
                    continue
                prepared = self._prepare_for_deletion(scanned, cutoff=cutoff, attempted_at=now_text)
                if prepared is None:
                    skipped += 1
                    continue
                base_due.append(prepared)

            stage_deleted, stage_pending, stage_errors = self._delete_records(
                base_due,
                attempted_at=now_text,
            )
            deleted += stage_deleted
            pending += stage_pending
            errors.extend(stage_errors)

            return AgentCliCleanupResult(
                deleted=deleted,
                pending=pending,
                skipped=skipped,
                malformed=len(scan.malformed),
                errors=tuple(errors),
            )

    def _delete_records(
        self,
        records: list[ProviderThreadRecord],
        *,
        attempted_at: str,
    ) -> tuple[int, int, list[str]]:
        if not records:
            return 0, 0, []
        try:
            delete_results = self.client.delete_threads(
                [record.provider_session_id for record in records]
            )
        except Exception as exc:
            detail = f"Codex thread cleanup failed: {type(exc).__name__}: {exc}"
            delete_results = [
                CodexThreadDeleteResult(record.provider_session_id, False, detail)
                for record in records
            ]
        by_id = {result.thread_id: result for result in delete_results}
        deleted = 0
        pending = 0
        errors: list[str] = []
        for record in records:
            result = by_id.get(record.provider_session_id)
            if result is not None and result.deleted:
                self.store.delete(record.provider, record.provider_session_id)
                deleted += 1
                continue
            detail = result.error if result is not None else "missing Codex thread/delete result"
            self.store.mark_delete_pending(record, attempted_at=attempted_at, error=detail)
            errors.append(f"{record.provider_session_id}: {detail}")
            pending += 1
        return deleted, pending, errors

    def _prepare_for_deletion(
        self,
        scanned: ProviderThreadRecord,
        *,
        cutoff: datetime,
        attempted_at: str,
    ) -> ProviderThreadRecord | None:
        if scanned.policy == "fork-base":
            prefixes = ProviderPrefixStore(self.memory_root)
            with prefixes.locked(scanned.provider, scanned.rightmemory_session_id):
                current = self.store.load(scanned.provider, scanned.provider_session_id)
                if current is None or not _is_due(current, cutoff):
                    return None
                if self._has_fork_child(current):
                    return None
                prefixes.delete_if_matches(
                    current.provider,
                    current.rightmemory_session_id,
                    current.provider_session_id,
                )
                return self.store.mark_delete_pending(current, attempted_at=attempted_at)

        if scanned.policy != "persistent":
            current = self.store.load(scanned.provider, scanned.provider_session_id)
            if current is None or not _is_due(current, cutoff):
                return None
            return self.store.mark_delete_pending(current, attempted_at=attempted_at)

        sessions = MessageSessionStore(self.memory_root, scanned.role)
        with sessions.locked(scanned.rightmemory_session_id):
            current = self.store.load(scanned.provider, scanned.provider_session_id)
            if current is None or not _is_due(current, cutoff):
                return None
            mapping_removed = ProviderSessionStore(self.memory_root, current.role).delete_if_matches(
                current.rightmemory_session_id,
                current.provider_session_id,
                provider=current.provider,
            )
            if mapping_removed and current.role == "retrieve":
                RetrieveContextStore(self.memory_root).reset(current.rightmemory_session_id)
                RecentSubmittedMemoryDeliveryStore(self.memory_root).reset(current.rightmemory_session_id)
            return self.store.mark_delete_pending(current, attempted_at=attempted_at)

    def _has_fork_child(self, base: ProviderThreadRecord) -> bool:
        return any(
            child.forked_from_provider_session_id == base.provider_session_id
            for child in self.store.scan(base.provider).records
        )


class _CleanupLock:
    def __init__(self, memory_root: Path):
        self.runtime_root = Path(memory_root) / ".runtime"
        self.path = self.runtime_root / "agent_cli_threads" / "cleanup.lock"
        self._handle: Any | None = None

    def __enter__(self) -> _CleanupLock:
        _ensure_runtime_gitignore(self.runtime_root)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a+", encoding="utf-8")
        lock_file(self._handle)
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if self._handle is None:
            return
        try:
            unlock_file(self._handle)
        finally:
            self._handle.close()
            self._handle = None


def _is_due(record: ProviderThreadRecord, cutoff: datetime) -> bool:
    if record.status == "delete-pending":
        return True
    return _parse_timestamp(record.activity_at) <= cutoff


def _parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid provider thread timestamp: {value}") from exc
    return _utc(parsed)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("provider thread timestamps must include a timezone")
    return value.astimezone(UTC)

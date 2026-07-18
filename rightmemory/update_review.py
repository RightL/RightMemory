from __future__ import annotations

import hashlib
import json
import os
import re
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .platform import lock_file, unlock_file
from .session import _ensure_runtime_gitignore, _fsync_directory


DEFAULT_STABLE_SECONDS = 60
DEFAULT_BLANK_REVIEW_LIMIT = 50
DEFAULT_BLANK_REVIEW_EXPIRY_DAYS = 30
STATE_VERSION = 2

COMMENT_START = "<!-- rightmemory-update-review-comment:start -->"
COMMENT_END = "<!-- rightmemory-update-review-comment:end -->"
STATUS_START = "<!-- rightmemory-update-review-status:start -->"
STATUS_END = "<!-- rightmemory-update-review-status:end -->"
METADATA_PREFIX = "<!-- rightmemory-update-review:"
METADATA_SUFFIX = "-->"
PROCESSED_COMMENT_PREFIX = "<!-- rightmemory-update-review-processed-comment-sha256:"
PROCESSED_COMMENT_SUFFIX = "-->"

_REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_OUTCOME_STATUSES = {"resolved", "needs_input"}
_CORRECTION_SECTIONS = ("Background", "Proposed edit", "Accepted edit")


class UpdateExecutionLock:
    """Serialize whole update turns, including semantic review corrections."""

    def __init__(self, memory_root: Path):
        self.runtime_root = Path(memory_root).resolve() / ".runtime"
        self.lock_path = self.runtime_root / "update" / "execution.lock"
        self._handle: Any | None = None

    def __enter__(self) -> UpdateExecutionLock:
        _ensure_runtime_gitignore(self.runtime_root)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.lock_path.open("a+", encoding="utf-8")
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


@dataclass(frozen=True)
class UpdateReviewRecord:
    review_id: str
    base_commit: str
    update_commit: str
    write_surface: str
    created_at: str
    original_diff_sha256: str
    processed_comment_sha256: str | None = None
    inflight_comment_sha256: str | None = None
    status: str = "open"
    status_message: str | None = None


@dataclass(frozen=True)
class ParsedUpdateReview:
    review_id: str
    base_commit: str
    update_commit: str
    write_surface: str
    created_at: str
    comment: str
    original_diff: str
    original_diff_sha256: str
    processed_comment_sha256: str | None = None


@dataclass(frozen=True)
class UpdateReviewRequest:
    review_id: str
    document_path: Path
    base_commit: str
    update_commit: str
    write_surface: str
    document: str
    comment: str
    comment_sha256: str
    original_diff: str = ""


@dataclass(frozen=True)
class UpdateReviewOutcome:
    status: str
    message: str | None = None
    correction_commit: str | None = None

    @classmethod
    def resolved(cls, *, correction_commit: str | None = None, message: str | None = None) -> UpdateReviewOutcome:
        return cls("resolved", message=message, correction_commit=correction_commit)

    @classmethod
    def needs_input(cls, message: str) -> UpdateReviewOutcome:
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("needs-input outcome requires a message")
        return cls("needs_input", message=clean_message)


@dataclass(frozen=True)
class UpdateReviewProcessResult:
    processed: int = 0
    resolved: int = 0
    needs_input: int = 0
    failed: int = 0
    blank: int = 0
    unstable: int = 0
    unchanged: int = 0
    malformed: int = 0
    missing: int = 0
    pruned_blank: int = 0
    errors: tuple[str, ...] = ()


class UpdateReviewStore:
    """Own local, human-editable review documents for landed update turns."""

    def __init__(
        self,
        memory_root: Path,
        *,
        stable_seconds: int = DEFAULT_STABLE_SECONDS,
        blank_review_limit: int = DEFAULT_BLANK_REVIEW_LIMIT,
        blank_review_expiry_days: int = DEFAULT_BLANK_REVIEW_EXPIRY_DAYS,
    ):
        if isinstance(stable_seconds, bool) or not isinstance(stable_seconds, int) or stable_seconds < 0:
            raise ValueError("stable_seconds must be a nonnegative integer")
        if isinstance(blank_review_limit, bool) or not isinstance(blank_review_limit, int) or blank_review_limit < 1:
            raise ValueError("blank_review_limit must be a positive integer")
        if (
            isinstance(blank_review_expiry_days, bool)
            or not isinstance(blank_review_expiry_days, int)
            or blank_review_expiry_days < 1
        ):
            raise ValueError("blank_review_expiry_days must be a positive integer")
        self.memory_root = Path(memory_root).resolve()
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.runtime_root / "update-review"
        self.reviews_root = self.root / "reviews"
        self.pending_root = self.root / "pending"
        self.state_path = self.root / "state.json"
        self.lock_path = self.root / "state.lock"
        self.process_lock_path = self.root / "process.lock"
        self.stable_seconds = stable_seconds
        self.blank_review_limit = blank_review_limit
        self.blank_review_expiry_days = blank_review_expiry_days

    def create_review(
        self,
        *,
        base_commit: str,
        update_commit: str,
        write_surface: str,
        summary: str,
        diff: str,
        review_id: str | None = None,
        created_at: str | None = None,
    ) -> UpdateReviewRecord:
        clean_update_commit = _single_line(update_commit, "update_commit")
        clean_review_id = _review_id(review_id or clean_update_commit)
        clean_base_commit = _single_line(base_commit, "base_commit")
        clean_surface = _single_line(write_surface, "write_surface")
        clean_created_at = created_at or datetime.now(UTC).isoformat()
        _parse_time(clean_created_at, "created_at")
        requested = UpdateReviewRecord(
            review_id=clean_review_id,
            base_commit=clean_base_commit,
            update_commit=clean_update_commit,
            write_surface=clean_surface,
            created_at=clean_created_at,
            original_diff_sha256=_text_sha256(diff.rstrip()),
        )
        path = self.review_path(clean_review_id)

        with self._locked():
            records = self._load_records_locked()
            existing = records.get(clean_review_id)
            if path.exists():
                parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
                _assert_same_review(parsed, requested)
                if existing is None:
                    existing = _record_from_parsed(parsed)
                    records[clean_review_id] = existing
                    self._save_records_locked(records)
                self._prune_blank_locked(records, now=datetime.now(UTC))
                self.pending_path(clean_review_id).unlink(missing_ok=True)
                return existing

            record = existing or requested
            text = render_review_markdown(
                record,
                summary=summary,
                diff=diff,
            )
            self.reviews_root.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, text)
            records[clean_review_id] = record
            self._save_records_locked(records)
            self._prune_blank_locked(records, now=datetime.now(UTC))
            self.pending_path(clean_review_id).unlink(missing_ok=True)
            return record

    def queue_review(
        self,
        *,
        base_commit: str,
        update_commit: str,
        write_surface: str,
        summary: str,
        diff: str,
        review_id: str | None = None,
        created_at: str | None = None,
    ) -> str:
        """Persist a review-creation obligation before best-effort materialization."""
        clean_update_commit = _single_line(update_commit, "update_commit")
        clean_review_id = _review_id(review_id or clean_update_commit)
        payload = {
            "version": STATE_VERSION,
            "review_id": clean_review_id,
            "base_commit": _single_line(base_commit, "base_commit"),
            "update_commit": clean_update_commit,
            "write_surface": _single_line(write_surface, "write_surface"),
            "summary": str(summary),
            "diff": str(diff),
            "created_at": created_at or datetime.now(UTC).isoformat(),
        }
        _parse_time(payload["created_at"], "created_at")
        path = self.pending_path(clean_review_id)
        with self._locked():
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                comparable_existing = dict(existing) if isinstance(existing, dict) else existing
                comparable_payload = dict(payload)
                if isinstance(comparable_existing, dict):
                    comparable_existing.pop("created_at", None)
                comparable_payload.pop("created_at", None)
                if comparable_existing != comparable_payload:
                    raise ValueError(f"pending update review has different content: {clean_review_id}")
                return clean_review_id
            self.pending_root.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        return clean_review_id

    def materialize_pending(self, *, limit: int | None = None) -> int:
        if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int) or limit < 1):
            raise ValueError("pending review limit must be a positive integer")
        if not self.pending_root.exists():
            return 0
        created = 0
        for path in sorted(self.pending_root.glob("*.json")):
            if limit is not None and created >= limit:
                break
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
                raise ValueError(f"pending update review has unsupported format: {path}")
            review_id = _review_id(_required_string(data, "review_id"))
            if path != self.pending_path(review_id):
                raise ValueError(f"pending update review path does not match review id: {path}")
            self.create_review(
                review_id=review_id,
                base_commit=_required_string(data, "base_commit"),
                update_commit=_required_string(data, "update_commit"),
                write_surface=_required_string(data, "write_surface"),
                summary=_required_string_allow_empty(data, "summary"),
                diff=_required_string_allow_empty(data, "diff"),
                created_at=_required_string(data, "created_at"),
            )
            path.unlink(missing_ok=True)
            created += 1
        return created

    def review_path(self, review_id: str) -> Path:
        return self.reviews_root / f"{_review_id(review_id)}.md"

    def pending_path(self, review_id: str) -> Path:
        return self.pending_root / f"{_review_id(review_id)}.json"

    def list_records(self) -> list[UpdateReviewRecord]:
        with self._locked():
            records = self._load_records_locked()
            _missing, changed = self._discover_and_clean_locked(records)
            if changed:
                self._save_records_locked(records)
            return sorted(records.values(), key=lambda item: (item.created_at, item.review_id))

    def prune_blank_reviews(self, *, now: datetime | None = None) -> tuple[str, ...]:
        if now is not None and now.tzinfo is None:
            raise ValueError("blank review pruning time must include a timezone")
        with self._locked():
            records = self._load_records_locked()
            self._discover_and_clean_locked(records)
            current = (now or datetime.now(UTC)).astimezone(UTC)
            return self._prune_blank_locked(records, now=current)

    def process_ready(
        self,
        run_correction: Callable[[UpdateReviewRequest], UpdateReviewOutcome],
        *,
        now: float | None = None,
        limit: int = 1,
    ) -> UpdateReviewProcessResult:
        with self._processing_locked():
            return self._process_ready_locked(run_correction, now=now, limit=limit)

    def _process_ready_locked(
        self,
        run_correction: Callable[[UpdateReviewRequest], UpdateReviewOutcome],
        *,
        now: float | None = None,
        limit: int = 1,
    ) -> UpdateReviewProcessResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        self.materialize_pending()
        now = time.time() if now is None else now
        counts: dict[str, int] = {
            "processed": 0,
            "resolved": 0,
            "needs_input": 0,
            "failed": 0,
            "blank": 0,
            "unstable": 0,
            "unchanged": 0,
            "malformed": 0,
            "missing": 0,
            "pruned_blank": 0,
        }
        errors: list[str] = []

        while counts["processed"] < limit:
            request = self._claim_ready(now, counts, errors)
            if request is None:
                break
            counts["processed"] += 1
            try:
                outcome = run_correction(request)
                _validate_outcome(outcome)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(f"{request.review_id}: {message}")
                self._finish_failed(request, message)
                counts["failed"] += 1
                continue

            if outcome.status == "resolved":
                deleted = self._finish_resolved(request, outcome)
                if deleted:
                    counts["resolved"] += 1
                else:
                    counts["unchanged"] += 1
                continue

            self._finish_needs_input(request, outcome)
            counts["needs_input"] += 1

        pruned = self.prune_blank_reviews(now=datetime.fromtimestamp(now, UTC))
        counts["pruned_blank"] += len(pruned)
        return UpdateReviewProcessResult(**counts, errors=tuple(errors))

    def _claim_ready(
        self,
        now: float,
        counts: dict[str, int],
        errors: list[str],
    ) -> UpdateReviewRequest | None:
        with self._locked():
            records = self._load_records_locked()
            missing, changed = self._discover_and_clean_locked(records)
            counts["missing"] += missing
            for record in sorted(records.values(), key=lambda item: (item.created_at, item.review_id)):
                path = self.review_path(record.review_id)
                try:
                    document = path.read_text(encoding="utf-8")
                    parsed = parse_review_markdown(document)
                    _assert_same_review(parsed, record)
                except FileNotFoundError:
                    records.pop(record.review_id, None)
                    counts["missing"] += 1
                    changed = True
                    continue
                except (OSError, ValueError) as exc:
                    counts["malformed"] += 1
                    errors.append(f"{record.review_id}: {type(exc).__name__}: {exc}")
                    continue

                comment = normalize_review_comment(parsed.comment)
                if record.inflight_comment_sha256 is not None:
                    interrupted_hash = record.inflight_comment_sha256
                    message = "Previous processing was interrupted. Edit the human review comment to retry."
                    record = replace(
                        record,
                        processed_comment_sha256=interrupted_hash,
                        inflight_comment_sha256=None,
                        status="failed",
                        status_message=message,
                    )
                    records[record.review_id] = record
                    self._write_status_locked(
                        path,
                        "Processing interrupted",
                        message,
                        processed_comment_sha256=interrupted_hash,
                    )
                    counts["failed"] += 1
                    errors.append(f"{record.review_id}: previous processing was interrupted")
                    changed = True
                if not comment:
                    counts["blank"] += 1
                    continue
                comment_sha256 = review_comment_sha256(comment)
                if comment_sha256 == record.processed_comment_sha256:
                    counts["unchanged"] += 1
                    continue
                try:
                    mtime = path.stat().st_mtime
                except OSError as exc:
                    counts["malformed"] += 1
                    errors.append(f"{record.review_id}: {type(exc).__name__}: {exc}")
                    continue
                if now - mtime < self.stable_seconds:
                    counts["unstable"] += 1
                    continue

                claimed = replace(
                    record,
                    inflight_comment_sha256=comment_sha256,
                    status="processing",
                    status_message=None,
                )
                records[record.review_id] = claimed
                self._save_records_locked(records)
                return UpdateReviewRequest(
                    review_id=record.review_id,
                    document_path=path,
                    base_commit=record.base_commit,
                    update_commit=record.update_commit,
                    write_surface=record.write_surface,
                    document=document,
                    comment=comment,
                    comment_sha256=comment_sha256,
                    original_diff=parsed.original_diff,
                )
            if changed:
                self._save_records_locked(records)
            return None

    def _finish_resolved(self, request: UpdateReviewRequest, outcome: UpdateReviewOutcome) -> bool:
        with self._locked():
            records = self._load_records_locked()
            record = records.get(request.review_id)
            if record is None:
                return True
            path = self.review_path(request.review_id)
            current_comment = ""
            if path.exists():
                try:
                    current_comment = normalize_review_comment(
                        parse_review_markdown(path.read_text(encoding="utf-8")).comment
                    )
                except (OSError, ValueError):
                    current_comment = request.comment
            current_hash = review_comment_sha256(current_comment) if current_comment else None
            if current_hash not in {None, request.comment_sha256}:
                message = outcome.message or "The previous comment was applied. A newer comment is pending."
                records[request.review_id] = replace(
                    record,
                    processed_comment_sha256=request.comment_sha256,
                    inflight_comment_sha256=None,
                    status="open",
                    status_message=message,
                )
                if path.exists():
                    self._write_status_locked(
                        path,
                        "Applied",
                        message,
                        processed_comment_sha256=request.comment_sha256,
                    )
                self._save_records_locked(records)
                return False
            path.unlink(missing_ok=True)
            records.pop(request.review_id, None)
            self._save_records_locked(records)
            return True

    def _finish_needs_input(self, request: UpdateReviewRequest, outcome: UpdateReviewOutcome) -> None:
        message = (outcome.message or "More input is needed before this correction can be applied.").strip()
        with self._locked():
            records = self._load_records_locked()
            record = records.get(request.review_id)
            if record is None:
                return
            records[request.review_id] = replace(
                record,
                processed_comment_sha256=request.comment_sha256,
                inflight_comment_sha256=None,
                status="needs_input",
                status_message=message,
            )
            path = self.review_path(request.review_id)
            if path.exists():
                self._write_status_locked(
                    path,
                    "Needs input",
                    message,
                    processed_comment_sha256=request.comment_sha256,
                )
            self._save_records_locked(records)

    def _finish_failed(self, request: UpdateReviewRequest, message: str) -> None:
        user_message = f"Processing failed: {message}. Edit the human review comment to retry."
        with self._locked():
            records = self._load_records_locked()
            record = records.get(request.review_id)
            if record is None:
                return
            records[request.review_id] = replace(
                record,
                processed_comment_sha256=request.comment_sha256,
                inflight_comment_sha256=None,
                status="failed",
                status_message=user_message,
            )
            path = self.review_path(request.review_id)
            if path.exists():
                self._write_status_locked(
                    path,
                    "Processing failed",
                    user_message,
                    processed_comment_sha256=request.comment_sha256,
                )
            self._save_records_locked(records)

    def _write_status_locked(
        self,
        path: Path,
        label: str,
        message: str,
        *,
        processed_comment_sha256: str,
    ) -> None:
        text = path.read_text(encoding="utf-8")
        block = (
            f"{STATUS_START}\n"
            f"{PROCESSED_COMMENT_PREFIX}{_sha256(processed_comment_sha256)}{PROCESSED_COMMENT_SUFFIX}\n"
            "## RightMemory status\n\n"
            f"**{label}.** {message.strip()}\n"
            f"{STATUS_END}"
        )
        start = text.find(STATUS_START)
        end = text.find(STATUS_END)
        if start >= 0 and end >= start:
            end += len(STATUS_END)
            updated = text[:start].rstrip() + "\n\n" + block + text[end:]
        else:
            updated = text.rstrip() + "\n\n" + block + "\n"
        _atomic_write_text(path, updated)

    def _discover_and_clean_locked(self, records: dict[str, UpdateReviewRecord]) -> tuple[int, bool]:
        missing = 0
        changed = False
        for review_id in list(records):
            if not self.review_path(review_id).exists():
                records.pop(review_id, None)
                missing += 1
                changed = True
        if not self.reviews_root.exists():
            return missing, changed
        for path in sorted(self.reviews_root.glob("*.md")):
            try:
                parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if path != self.review_path(parsed.review_id):
                continue
            if parsed.review_id not in records:
                records[parsed.review_id] = _record_from_parsed(parsed)
                changed = True
        return missing, changed

    def _prune_blank_locked(
        self,
        records: dict[str, UpdateReviewRecord],
        *,
        now: datetime,
    ) -> tuple[str, ...]:
        self._discover_and_clean_locked(records)
        blank: list[UpdateReviewRecord] = []
        for record in records.values():
            path = self.review_path(record.review_id)
            try:
                parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                not normalize_review_comment(parsed.comment)
                and record.status == "open"
                and record.processed_comment_sha256 is None
                and record.inflight_comment_sha256 is None
            ):
                blank.append(record)
        blank.sort(key=lambda item: (item.created_at, item.review_id))
        expiry_seconds = self.blank_review_expiry_days * 24 * 60 * 60
        expired = [
            record
            for record in blank
            if (now - _parse_time(record.created_at, "created_at")).total_seconds() >= expiry_seconds
        ]
        expired_ids = {record.review_id for record in expired}
        retained = [record for record in blank if record.review_id not in expired_ids]
        excess = max(0, len(retained) - self.blank_review_limit)
        selected = [*expired, *retained[:excess]]
        pruned = []
        for record in selected:
            self.review_path(record.review_id).unlink(missing_ok=True)
            records.pop(record.review_id, None)
            pruned.append(record.review_id)
        self._save_records_locked(records)
        return tuple(pruned)

    def _load_records_locked(self) -> dict[str, UpdateReviewRecord]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"update review state is not valid JSON: {self.state_path}") from exc
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            raise ValueError(f"update review state has unsupported format: {self.state_path}")
        raw_records = data.get("reviews")
        if not isinstance(raw_records, dict):
            raise ValueError(f"update review state must contain an object field `reviews`: {self.state_path}")
        records: dict[str, UpdateReviewRecord] = {}
        for key, value in raw_records.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                raise ValueError(f"update review state contains an invalid record: {self.state_path}")
            record = _record_from_json(value)
            if record.review_id != key:
                raise ValueError(f"update review state key does not match review id: {key}")
            records[key] = record
        return records

    def _save_records_locked(self, records: dict[str, UpdateReviewRecord]) -> None:
        data = {
            "version": STATE_VERSION,
            "reviews": {key: asdict(value) for key, value in sorted(records.items())},
        }
        _atomic_write_text(self.state_path, json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    @contextmanager
    def _locked(self):
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)

    @contextmanager
    def _processing_locked(self):
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        with self.process_lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                yield
            finally:
                unlock_file(handle)


def render_review_markdown(record: UpdateReviewRecord, *, summary: str, diff: str) -> str:
    clean_diff = diff.rstrip()
    if _text_sha256(clean_diff) != record.original_diff_sha256:
        raise ValueError("update review diff does not match its recorded hash")
    metadata = json.dumps(
        {
            "version": STATE_VERSION,
            "review_id": record.review_id,
            "base_commit": record.base_commit,
            "update_commit": record.update_commit,
            "write_surface": record.write_surface,
            "created_at": record.created_at,
            "original_diff_sha256": record.original_diff_sha256,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    clean_summary = summary.strip() or "No update summary was provided."
    fence = _markdown_fence(clean_diff)
    return (
        "# RightMemory Update Review\n\n"
        f"{METADATA_PREFIX}{metadata}{METADATA_SUFFIX}\n\n"
        f"Update commit: `{record.update_commit}`\n\n"
        "## What changed\n\n"
        f"{clean_summary}\n\n"
        "## Original diff\n\n"
        f"{fence}diff\n{clean_diff}\n{fence}\n\n"
        "## Human review\n\n"
        "Leave this area blank when no correction is needed. Otherwise, write one overall comment.\n\n"
        f"{COMMENT_START}\n\n{COMMENT_END}\n"
    )


def parse_review_markdown(text: str) -> ParsedUpdateReview:
    metadata_start = text.find(METADATA_PREFIX)
    if metadata_start < 0:
        raise ValueError("update review is missing metadata")
    metadata_end = text.find(METADATA_SUFFIX, metadata_start + len(METADATA_PREFIX))
    if metadata_end < 0:
        raise ValueError("update review metadata is not closed")
    raw_metadata = text[metadata_start + len(METADATA_PREFIX) : metadata_end]
    try:
        metadata = json.loads(raw_metadata)
    except json.JSONDecodeError as exc:
        raise ValueError("update review metadata is not valid JSON") from exc
    if not isinstance(metadata, dict) or metadata.get("version") != STATE_VERSION:
        raise ValueError("update review metadata has unsupported format")

    if text.count(COMMENT_START) != 1 or text.count(COMMENT_END) != 1:
        raise ValueError("update review must contain exactly one human comment area")
    comment_start = text.find(COMMENT_START) + len(COMMENT_START)
    comment_end = text.find(COMMENT_END, comment_start)
    if comment_end < comment_start:
        raise ValueError("update review human comment area is malformed")

    original_diff = _extract_original_diff(text)
    original_diff_sha256 = _required_sha256(metadata, "original_diff_sha256")
    if _text_sha256(original_diff) != original_diff_sha256:
        raise ValueError("update review original diff was modified")

    processed_comment_sha256 = _embedded_processed_comment_sha256(text)

    return ParsedUpdateReview(
        review_id=_review_id(_required_metadata_string(metadata, "review_id")),
        base_commit=_single_line(_required_metadata_string(metadata, "base_commit"), "base_commit"),
        update_commit=_single_line(_required_metadata_string(metadata, "update_commit"), "update_commit"),
        write_surface=_single_line(_required_metadata_string(metadata, "write_surface"), "write_surface"),
        created_at=_parsed_metadata_time(metadata),
        comment=text[comment_start:comment_end],
        original_diff=original_diff,
        original_diff_sha256=original_diff_sha256,
        processed_comment_sha256=processed_comment_sha256,
    )


def _extract_original_diff(text: str) -> str:
    heading = "## Original diff\n\n"
    start = text.find(heading)
    if start < 0:
        raise ValueError("update review is missing its original diff")
    fence_start = start + len(heading)
    first_newline = text.find("\n", fence_start)
    if first_newline < 0:
        raise ValueError("update review original diff fence is malformed")
    opening = text[fence_start:first_newline]
    match = re.fullmatch(r"(`{3,}|~{3,})diff", opening)
    if match is None:
        raise ValueError("update review original diff fence is malformed")
    marker = match.group(1)
    closing = f"\n{marker}\n\n## Human review"
    end = text.find(closing, first_newline + 1)
    if end < 0:
        raise ValueError("update review original diff fence is not closed")
    return text[first_newline + 1 : end]


def _embedded_processed_comment_sha256(text: str) -> str | None:
    matches = re.findall(
        re.escape(PROCESSED_COMMENT_PREFIX) + r"([0-9a-f]{64})" + re.escape(PROCESSED_COMMENT_SUFFIX),
        text,
    )
    if len(matches) > 1:
        raise ValueError("update review contains multiple processed-comment markers")
    return matches[0] if matches else None


def normalize_review_comment(comment: str) -> str:
    return comment.replace("\r\n", "\n").replace("\r", "\n").strip()


def review_comment_sha256(comment: str) -> str:
    normalized = normalize_review_comment(comment)
    if not normalized:
        raise ValueError("cannot hash a blank review comment")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def validate_corrections_markdown(text: str) -> list[str]:
    """Validate the bounded updater-only correction collection shape."""
    errors: list[str] = []
    entries: list[tuple[str, int, list[tuple[str, int]]]] = []
    current: tuple[str, int, list[tuple[str, int]]] | None = None
    fence_char: str | None = None
    fence_length = 0

    lines = text.splitlines()
    for line_number, line in enumerate(lines, start=1):
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if fence is not None:
            marker = fence.group(1)
            if fence_char is None:
                fence_char = marker[0]
                fence_length = len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_length:
                fence_char = None
                fence_length = 0
            continue
        if fence_char is not None:
            continue

        heading = re.match(r"^ {0,3}(#{2,3})[ \t]+(.+?)[ \t]*#*[ \t]*$", line)
        if heading is None:
            continue
        level = len(heading.group(1))
        title = heading.group(2).strip()
        if level == 2:
            current = (title, line_number, [])
            entries.append(current)
            continue
        if current is None:
            errors.append(f"line {line_number}: correction section `{title}` appears before any `##` entry")
            continue
        current[2].append((title, line_number))

    if len(entries) > 15:
        errors.append(f"corrections.md contains {len(entries)} entries; at most 15 are allowed")

    expected = list(_CORRECTION_SECTIONS)
    for entry_index, (title, line_number, sections) in enumerate(entries):
        names = [name for name, _section_line in sections]
        unexpected = [(name, section_line) for name, section_line in sections if name not in _CORRECTION_SECTIONS]
        for name, section_line in unexpected:
            errors.append(f"line {section_line}: correction entry `{title}` has unexpected `### {name}` section")
        for name in expected:
            count = names.count(name)
            if count == 0:
                errors.append(f"line {line_number}: correction entry `{title}` is missing `### {name}`")
            elif count > 1:
                errors.append(f"line {line_number}: correction entry `{title}` repeats `### {name}`")
        recognized = [name for name in names if name in _CORRECTION_SECTIONS]
        if all(names.count(name) == 1 for name in expected) and recognized != expected:
            errors.append(
                f"line {line_number}: correction entry `{title}` sections must be ordered as "
                "Background, Proposed edit, Accepted edit"
            )
        entry_end = entries[entry_index + 1][1] - 1 if entry_index + 1 < len(entries) else len(lines)
        ordered_sections = sorted(sections, key=lambda item: item[1])
        for section_index, (name, section_line) in enumerate(ordered_sections):
            if name not in _CORRECTION_SECTIONS or names.count(name) != 1:
                continue
            section_end = (
                ordered_sections[section_index + 1][1] - 1
                if section_index + 1 < len(ordered_sections)
                else entry_end
            )
            body = "\n".join(lines[section_line:section_end]).strip()
            if not body:
                errors.append(f"line {section_line}: correction entry `{title}` has empty `### {name}` content")
    return errors


def _validate_outcome(outcome: UpdateReviewOutcome) -> None:
    if not isinstance(outcome, UpdateReviewOutcome):
        raise TypeError("update review callback must return UpdateReviewOutcome")
    if outcome.status not in _OUTCOME_STATUSES:
        raise ValueError(f"update review outcome status must be one of: {', '.join(sorted(_OUTCOME_STATUSES))}")
    if outcome.status == "needs_input" and not (outcome.message or "").strip():
        raise ValueError("needs-input outcome requires a message")


def _record_from_parsed(parsed: ParsedUpdateReview) -> UpdateReviewRecord:
    return UpdateReviewRecord(
        review_id=parsed.review_id,
        base_commit=parsed.base_commit,
        update_commit=parsed.update_commit,
        write_surface=parsed.write_surface,
        created_at=parsed.created_at,
        original_diff_sha256=parsed.original_diff_sha256,
        processed_comment_sha256=parsed.processed_comment_sha256,
    )


def _record_from_json(data: dict[str, Any]) -> UpdateReviewRecord:
    return UpdateReviewRecord(
        review_id=_review_id(_required_string(data, "review_id")),
        base_commit=_single_line(_required_string(data, "base_commit"), "base_commit"),
        update_commit=_single_line(_required_string(data, "update_commit"), "update_commit"),
        write_surface=_single_line(_required_string(data, "write_surface"), "write_surface"),
        created_at=_parsed_time_value(data, "created_at"),
        original_diff_sha256=_sha256(_required_string(data, "original_diff_sha256")),
        processed_comment_sha256=_optional_string(data, "processed_comment_sha256"),
        inflight_comment_sha256=_optional_string(data, "inflight_comment_sha256"),
        status=_required_string(data, "status"),
        status_message=_optional_string(data, "status_message"),
    )


def _assert_same_review(parsed: ParsedUpdateReview, record: UpdateReviewRecord) -> None:
    pairs = (
        ("review_id", parsed.review_id, record.review_id),
        ("base_commit", parsed.base_commit, record.base_commit),
        ("update_commit", parsed.update_commit, record.update_commit),
        ("write_surface", parsed.write_surface, record.write_surface),
        ("original_diff_sha256", parsed.original_diff_sha256, record.original_diff_sha256),
    )
    for field, actual, expected in pairs:
        if actual != expected:
            raise ValueError(f"existing update review has different {field}: {actual}")


def _review_id(value: str) -> str:
    clean = str(value).strip()
    if not _REVIEW_ID_RE.fullmatch(clean) or clean in {".", ".."}:
        raise ValueError("review_id must use 1-160 letters, digits, dots, underscores, or hyphens")
    return clean


def _single_line(value: str, field: str) -> str:
    clean = str(value).strip()
    if not clean or "\n" in clean or "\r" in clean or "\x00" in clean:
        raise ValueError(f"{field} must be a non-empty single line")
    return clean


def _required_metadata_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"update review metadata must contain string field `{key}`")
    return value


def _required_sha256(data: dict[str, Any], key: str) -> str:
    return _sha256(_required_metadata_string(data, key))


def _sha256(value: str) -> str:
    clean = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise ValueError("SHA-256 values must contain exactly 64 lowercase hexadecimal characters")
    return clean


def _text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parsed_metadata_time(data: dict[str, Any]) -> str:
    return _parsed_time_value(data, "created_at", label="update review metadata")


def _parsed_time_value(data: dict[str, Any], key: str, *, label: str = "update review state") -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"{label} must contain string field `{key}`")
    _parse_time(value, key)
    return value


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _required_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"update review state record must contain string field `{key}`")
    return value


def _required_string_allow_empty(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str):
        raise ValueError(f"update review record must contain string field `{key}`")
    return value


def _optional_string(data: dict[str, Any], key: str) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"update review state record field `{key}` must be a string or null")
    return value


def _markdown_fence(text: str) -> str:
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise
    _fsync_directory(path.parent)

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import subprocess
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .platform import lock_file, unlock_file
from .session import _ensure_runtime_gitignore, _fsync_directory
from .update_coordination import is_update_coordination_path


DEFAULT_BLANK_REVIEW_LIMIT = 50
DEFAULT_BLANK_REVIEW_EXPIRY_DAYS = 30
REVIEW_FORMAT_VERSION = 4
UPDATE_REVIEWS_DIRECTORY = "update_reviews"
OPERATION_TRAILER = "RightMemory-Operation"

COMMENT_START = "<!-- rightmemory-update-review-comment:start -->"
COMMENT_END = "<!-- rightmemory-update-review-comment:end -->"
READY_START = "<!-- rightmemory-update-review-ready:start -->"
READY_END = "<!-- rightmemory-update-review-ready:end -->"
QUESTION_START = "<!-- rightmemory-update-review-question:start -->"
QUESTION_END = "<!-- rightmemory-update-review-question:end -->"
QUESTION_OPERATION_PREFIX = "<!-- rightmemory-update-review-question-operation:"
QUESTION_OPERATION_SUFFIX = "-->"
METADATA_PREFIX = "<!-- rightmemory-update-review:"
METADATA_SUFFIX = "-->"
READY_LABEL = "Ready for correction"
HUMAN_HEADING = "## Human review\n\n"
QUESTION_HEADING = "## Corrector question\n\n"

_REVIEW_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,159}$")
_READY_RE = re.compile(
    rf"^- \[([ xX])\] {re.escape(READY_LABEL)}[ \t]*$",
    re.MULTILINE,
)
_CORRECTION_SECTIONS = ("Background", "Proposed edit", "Accepted edit")
_OUTCOME_STATUSES = {"resolved", "needs_input", "submitted"}


class UpdateReviewSourceChanged(RuntimeError):
    """Raised when a queued correction no longer targets the tracked review revision."""


class UpdateExecutionLock:
    """Serialize Update and update-corrector turns over shared semantic state."""

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
    origin_operation_id: str
    base_commit: str
    write_surface: str
    created_at: str
    summary: str


@dataclass(frozen=True)
class ParsedUpdateReview:
    review_id: str
    origin_operation_id: str
    base_commit: str
    write_surface: str
    created_at: str
    summary: str
    ready: bool
    comment: str
    question: str
    question_operation_id: str | None


@dataclass(frozen=True)
class UpdateReviewRequest:
    review_id: str
    document_path: Path
    origin_operation_id: str
    base_commit: str
    write_surface: str
    document: str
    comment: str
    comment_sha256: str
    operation_id: str
    previous_question: str | None = None


@dataclass(frozen=True)
class VerifiedUpdateReview:
    review_id: str
    origin_operation_id: str
    base_commit: str
    creation_commit: str
    write_surface: str
    summary: str
    document_commit: str
    document_blob_oid: str
    question: str
    question_operation_id: str | None
    diff: str
    changed_paths: tuple[str, ...]


@dataclass(frozen=True)
class UpdateReviewOutcome:
    status: str
    message: str | None = None
    correction_commit: str | None = None

    @classmethod
    def resolved(
        cls,
        *,
        correction_commit: str | None = None,
        message: str | None = None,
    ) -> UpdateReviewOutcome:
        return cls("resolved", message=message, correction_commit=correction_commit)

    @classmethod
    def needs_input(cls, message: str) -> UpdateReviewOutcome:
        clean_message = message.strip()
        if not clean_message:
            raise ValueError("needs-input outcome requires a message")
        return cls("needs_input", message=clean_message)

    @classmethod
    def submitted(cls, message: str | None = None) -> UpdateReviewOutcome:
        return cls("submitted", message=message)


@dataclass(frozen=True)
class UpdateReviewProcessResult:
    processed: int = 0
    resolved: int = 0
    needs_input: int = 0
    submitted: int = 0
    failed: int = 0
    blank: int = 0
    not_ready: int = 0
    changed: int = 0
    malformed: int = 0
    pruned_blank: int = 0
    errors: tuple[str, ...] = ()


class UpdateReviewStore:
    """Use tracked Markdown as the review UI and submission state."""

    def __init__(
        self,
        memory_root: Path,
        *,
        blank_review_limit: int = DEFAULT_BLANK_REVIEW_LIMIT,
        blank_review_expiry_days: int = DEFAULT_BLANK_REVIEW_EXPIRY_DAYS,
    ):
        if (
            isinstance(blank_review_limit, bool)
            or not isinstance(blank_review_limit, int)
            or blank_review_limit < 1
        ):
            raise ValueError("blank_review_limit must be a positive integer")
        if (
            isinstance(blank_review_expiry_days, bool)
            or not isinstance(blank_review_expiry_days, int)
            or blank_review_expiry_days < 1
        ):
            raise ValueError("blank_review_expiry_days must be a positive integer")
        self.memory_root = Path(memory_root).resolve()
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.memory_root / UPDATE_REVIEWS_DIRECTORY
        self.reviews_root = self.root
        self.process_lock_path = self.runtime_root / "update-review" / "process.lock"
        self.blank_review_limit = blank_review_limit
        self.blank_review_expiry_days = blank_review_expiry_days

    def create_review(
        self,
        *,
        origin_operation_id: str,
        base_commit: str,
        write_surface: str,
        summary: str,
        diff: str,
        review_id: str | None = None,
        created_at: str | None = None,
    ) -> UpdateReviewRecord:
        clean_operation_id = _single_line(origin_operation_id, "origin_operation_id")
        record = UpdateReviewRecord(
            review_id=_review_id(review_id or review_id_for_operation(clean_operation_id)),
            origin_operation_id=clean_operation_id,
            base_commit=_single_line(base_commit, "base_commit"),
            write_surface=_single_line(write_surface, "write_surface"),
            created_at=created_at or datetime.now(UTC).isoformat(),
            summary=summary.strip() or "No update summary was provided.",
        )
        _parse_time(record.created_at, "created_at")
        path = self.review_path(record.review_id)
        try:
            existing = parse_review_markdown(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            existing = None
        if existing is not None:
            _assert_same_review(existing, record)
            return _record_from_parsed(existing)

        self.reviews_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(path, render_review_markdown(record, diff=diff))
        return record

    def review_path(self, review_id: str) -> Path:
        return self.reviews_root / f"{_review_id(review_id)}.md"

    def list_records(self) -> list[UpdateReviewRecord]:
        records: list[UpdateReviewRecord] = []
        if not self.reviews_root.exists():
            return records
        for path in sorted(self.reviews_root.glob("*.md")):
            try:
                parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if path == self.review_path(parsed.review_id):
                records.append(_record_from_parsed(parsed))
        return sorted(records, key=lambda item: (item.created_at, item.review_id))

    def settle_tracked(
        self,
        review_id: str,
        outcome: UpdateReviewOutcome,
        *,
        operation_id: str,
        expected_commit: str,
        expected_blob_oid: str,
    ) -> Path:
        """Apply one fenced correction outcome inside a Git transaction worktree."""
        _validate_outcome(outcome)
        if outcome.status == "submitted":
            raise ValueError("a submitted review cannot settle tracked state")
        path = self.review_path(review_id)
        expected_revision = _git_oid(expected_commit)
        expected_blob = _git_oid(expected_blob_oid)
        try:
            document = path.read_text(encoding="utf-8")
        except FileNotFoundError as exc:
            raise UpdateReviewSourceChanged(
                "queued update review no longer exists"
            ) from exc
        if (
            _git_path_commit(self.memory_root, path) != expected_revision
            or _git_blob_oid(self.memory_root, path) != expected_blob
        ):
            raise UpdateReviewSourceChanged(
                "queued update review no longer matches its submitted revision"
            )
        parsed = parse_review_markdown(document)
        if parsed.review_id != review_id:
            raise ValueError("tracked review identity does not match its path")
        if outcome.status == "resolved":
            path.unlink()
            _fsync_directory(path.parent)
            return path
        updated = _review_with_question(
            document,
            outcome.message or "",
            operation_id,
        )
        _atomic_write_text(path, updated)
        return path

    def prune_blank_reviews(self, *, now: datetime | None = None) -> tuple[str, ...]:
        with self._processing_locked():
            return self._prune_blank_reviews_locked(now=now)

    def _prune_blank_reviews_locked(self, *, now: datetime | None = None) -> tuple[str, ...]:
        if now is not None and now.tzinfo is None:
            raise ValueError("blank review pruning time must include a timezone")
        current = (now or datetime.now(UTC)).astimezone(UTC)
        blank: list[tuple[UpdateReviewRecord, str]] = []
        if self.reviews_root.exists():
            for path in sorted(self.reviews_root.glob("*.md")):
                try:
                    document = path.read_text(encoding="utf-8")
                    parsed = parse_review_markdown(document)
                except (OSError, ValueError):
                    continue
                if path != self.review_path(parsed.review_id):
                    continue
                if (
                    not parsed.ready
                    and not normalize_review_comment(parsed.comment)
                    and not parsed.question.strip()
                ):
                    blank.append((_record_from_parsed(parsed), document))
        blank.sort(key=lambda item: (item[0].created_at, item[0].review_id))
        expiry_seconds = self.blank_review_expiry_days * 24 * 60 * 60
        expired = [
            item
            for item in blank
            if (current - _parse_time(item[0].created_at, "created_at")).total_seconds()
            >= expiry_seconds
        ]
        expired_ids = {record.review_id for record, _document in expired}
        retained = [item for item in blank if item[0].review_id not in expired_ids]
        excess = max(0, len(retained) - self.blank_review_limit)
        selected = [*expired, *retained[:excess]]
        pruned: list[str] = []
        for record, document in selected:
            if _delete_document_if_unchanged(self.review_path(record.review_id), document):
                pruned.append(record.review_id)
        return tuple(pruned)

    def process_ready(
        self,
        run_correction: Callable[[UpdateReviewRequest], UpdateReviewOutcome],
        *,
        limit: int = 1,
    ) -> UpdateReviewProcessResult:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._processing_locked() as process_handle:
            return self._process_ready_locked(
                run_correction,
                limit=limit,
                process_handle=process_handle,
            )

    def _process_ready_locked(
        self,
        run_correction: Callable[[UpdateReviewRequest], UpdateReviewOutcome],
        *,
        limit: int,
        process_handle: Any,
    ) -> UpdateReviewProcessResult:
        counts = {
            "processed": 0,
            "resolved": 0,
            "needs_input": 0,
            "submitted": 0,
            "failed": 0,
            "blank": 0,
            "not_ready": 0,
            "changed": 0,
            "malformed": 0,
            "pruned_blank": 0,
        }
        errors: list[str] = []
        if self.reviews_root.exists():
            paths = sorted(self.reviews_root.glob("*.md"))
            cursor = _read_process_cursor(process_handle)
            if cursor is not None:
                split = next(
                    (index for index, path in enumerate(paths) if path.stem > cursor),
                    len(paths),
                )
                paths = paths[split:] + paths[:split]
            for path in paths:
                if counts["processed"] >= limit:
                    break
                try:
                    document = path.read_text(encoding="utf-8")
                    parsed = parse_review_markdown(document)
                    if path != self.review_path(parsed.review_id):
                        raise ValueError("review filename does not match its review id")
                except (OSError, ValueError) as exc:
                    counts["malformed"] += 1
                    errors.append(f"{path.name}: {type(exc).__name__}: {exc}")
                    continue

                comment = normalize_review_comment(parsed.comment)
                if not parsed.ready:
                    if comment:
                        counts["not_ready"] += 1
                    else:
                        counts["blank"] += 1
                    continue
                if not comment:
                    counts["blank"] += 1
                    continue

                comment_sha256 = review_comment_sha256(comment)
                operation_id = correction_operation_id(parsed.review_id, comment_sha256)
                request = UpdateReviewRequest(
                    review_id=parsed.review_id,
                    document_path=path,
                    origin_operation_id=parsed.origin_operation_id,
                    base_commit=parsed.base_commit,
                    write_surface=parsed.write_surface,
                    document=document,
                    comment=comment,
                    comment_sha256=comment_sha256,
                    operation_id=operation_id,
                    previous_question=(
                        parsed.question
                        if parsed.question
                        and parsed.question_operation_id != operation_id
                        else None
                    ),
                )
                counts["processed"] += 1
                try:
                    _write_process_cursor(process_handle, parsed.review_id)
                except Exception as exc:
                    counts["failed"] += 1
                    errors.append(f"{parsed.review_id}: {type(exc).__name__}: {exc}")
                    break
                try:
                    outcome = run_correction(request)
                    _validate_outcome(outcome)
                except Exception as exc:
                    counts["failed"] += 1
                    errors.append(f"{parsed.review_id}: {type(exc).__name__}: {exc}")
                    continue

                try:
                    if outcome.status == "submitted":
                        if self._restore_tracked_if_unchanged(request):
                            counts["submitted"] += 1
                        else:
                            counts["changed"] += 1
                        continue
                    if outcome.status == "resolved":
                        if self._delete_if_unchanged(request):
                            counts["resolved"] += 1
                        else:
                            counts["changed"] += 1
                        continue
                    if self._write_question_if_unchanged(request, outcome.message or ""):
                        counts["needs_input"] += 1
                    else:
                        counts["changed"] += 1
                except Exception as exc:
                    counts["failed"] += 1
                    errors.append(f"{parsed.review_id}: {type(exc).__name__}: {exc}")

        return UpdateReviewProcessResult(
            **counts,
            errors=tuple(errors),
        )

    def _delete_if_unchanged(self, request: UpdateReviewRequest) -> bool:
        return _delete_document_if_unchanged(request.document_path, request.document)

    def _restore_tracked_if_unchanged(self, request: UpdateReviewRequest) -> bool:
        tracked = _git_show_file(
            self.memory_root,
            "HEAD",
            request.document_path.relative_to(self.memory_root).as_posix(),
        )
        if tracked is None:
            raise RuntimeError("submitted update review has no tracked source document")
        return _replace_document_if_unchanged(
            request.document_path,
            request.document,
            tracked,
        )

    def _write_question_if_unchanged(
        self,
        request: UpdateReviewRequest,
        message: str,
    ) -> bool:
        updated = _review_with_question(
            request.document,
            message,
            request.operation_id,
        )
        claim = _claim_document_if_unchanged(request.document_path, request.document)
        if claim is None:
            return False
        try:
            published = _write_text_if_absent(request.document_path, updated)
        except Exception:
            _restore_claim_if_vacant(claim, request.document_path)
            raise
        claim.unlink(missing_ok=True)
        _fsync_directory(request.document_path.parent)
        return published

    @contextmanager
    def _processing_locked(self):
        _ensure_runtime_gitignore(self.runtime_root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.process_lock_path.parent.mkdir(parents=True, exist_ok=True)
        with self.process_lock_path.open("a+", encoding="utf-8") as handle:
            lock_file(handle)
            try:
                _recover_claimed_documents(self.reviews_root)
                yield handle
            finally:
                unlock_file(handle)


def _review_with_question(current: str, message: str, operation_id: str) -> str:
    # A tracked clarification contains no submitted draft, so the next edit is fresh.
    human_start, question_start = _review_section_bounds(current)
    human_section = current[human_start:question_start]
    ready_area = _marked_area(human_section, READY_START, READY_END, "Ready control")
    ready_area, replacements = _READY_RE.subn(
        f"- [ ] {READY_LABEL}",
        ready_area,
        count=1,
    )
    if replacements != 1:
        raise ValueError("submitted review no longer has one Ready checkbox")
    human_section = _replace_marked_area(
        human_section,
        READY_START,
        READY_END,
        ready_area,
    )
    human_section = _replace_marked_area(
        human_section,
        COMMENT_START,
        COMMENT_END,
        "",
    )
    updated = current[:human_start] + human_section + current[question_start:]
    _human_start, question_start = _review_section_bounds(updated)
    question_section = updated[question_start:]
    question = _question_markdown(message, operation_id)
    question_section = _replace_marked_area(
        question_section,
        QUESTION_START,
        QUESTION_END,
        question,
    )
    return updated[:question_start] + question_section

def render_review_markdown(record: UpdateReviewRecord, *, diff: str) -> str:
    metadata = json.dumps(
        {
            "version": REVIEW_FORMAT_VERSION,
            "review_id": record.review_id,
            "origin_operation_id": record.origin_operation_id,
            "base_commit": record.base_commit,
            "write_surface": record.write_surface,
            "created_at": record.created_at,
            "summary": record.summary,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    clean_diff = diff.rstrip()
    fence = _markdown_fence(clean_diff)
    return (
        "# RightMemory Update Review\n\n"
        f"{METADATA_PREFIX}{metadata}{METADATA_SUFFIX}\n\n"
        f"Update operation: `{record.origin_operation_id}`\n\n"
        "## What changed\n\n"
        f"{record.summary}\n\n"
        "## Original diff\n\n"
        "This copy is for reading only. Correction re-verifies the creating Git operation and diff.\n\n"
        f"{fence}diff\n{clean_diff}\n{fence}\n\n"
        f"{HUMAN_HEADING}"
        "Write one overall correction comment between the markers, then check Ready.\n\n"
        f"{READY_START}\n\n- [ ] {READY_LABEL}\n\n{READY_END}\n\n"
        f"{COMMENT_START}\n\n{COMMENT_END}\n\n"
        f"{QUESTION_HEADING}"
        f"{QUESTION_START}\n\n{QUESTION_END}\n"
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
    if not isinstance(metadata, dict) or metadata.get("version") != REVIEW_FORMAT_VERSION:
        raise ValueError("update review metadata has unsupported format")

    human_start, question_start = _review_section_bounds(text)
    human_section = text[human_start:question_start]
    question_section = text[question_start:]
    ready_area = _marked_area(human_section, READY_START, READY_END, "Ready control")
    ready_matches = list(_READY_RE.finditer(ready_area))
    if len(ready_matches) != 1:
        raise ValueError("update review must contain exactly one Ready checkbox")
    comment = _marked_area(human_section, COMMENT_START, COMMENT_END, "human comment")
    raw_question = _marked_area(
        question_section,
        QUESTION_START,
        QUESTION_END,
        "corrector question",
    )
    question, question_operation_id = _parse_question_markdown(raw_question)
    return ParsedUpdateReview(
        review_id=_review_id(_metadata_string(metadata, "review_id")),
        origin_operation_id=_single_line(
            _metadata_string(metadata, "origin_operation_id"),
            "origin_operation_id",
        ),
        base_commit=_single_line(_metadata_string(metadata, "base_commit"), "base_commit"),
        write_surface=_single_line(
            _metadata_string(metadata, "write_surface"),
            "write_surface",
        ),
        created_at=_metadata_time(metadata),
        summary=_metadata_string(metadata, "summary").strip(),
        ready=ready_matches[0].group(1).lower() == "x",
        comment=comment,
        question=question,
        question_operation_id=question_operation_id,
    )


def normalize_review_comment(comment: str) -> str:
    return comment.replace("\r\n", "\n").replace("\r", "\n").strip()


def review_comment_sha256(comment: str) -> str:
    normalized = normalize_review_comment(comment)
    if not normalized:
        raise ValueError("cannot hash a blank review comment")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def tracked_review_blob_oid(root: Path, review_id: str) -> str | None:
    """Return the exact tracked review revision used to fence queue evidence."""
    memory_root = Path(root).resolve()
    clean_review_id = _review_id(review_id)
    relative = f"{UPDATE_REVIEWS_DIRECTORY}/{clean_review_id}.md"
    tracked = _git_show_file(memory_root, "HEAD", relative)
    if tracked is None:
        return None
    return _git_oid(_git_stdout(memory_root, "rev-parse", f"HEAD:{relative}"))


def tracked_review_commit(root: Path, review_id: str) -> str | None:
    """Return the commit that introduced the currently tracked review revision."""
    memory_root = Path(root).resolve()
    clean_review_id = _review_id(review_id)
    relative = f"{UPDATE_REVIEWS_DIRECTORY}/{clean_review_id}.md"
    if _git_show_file(memory_root, "HEAD", relative) is None:
        return None
    return _git_oid(
        _git_stdout(memory_root, "log", "-1", "--format=%H", "HEAD", "--", relative)
    )


def review_id_for_operation(operation_id: str) -> str:
    clean = _single_line(operation_id, "operation_id")
    return f"review-{hashlib.sha256(clean.encode('utf-8')).hexdigest()}"


def correction_operation_id(review_id: str, comment_sha256: str) -> str:
    clean_review_id = _review_id(review_id)
    clean_comment_hash = _sha256(comment_sha256)
    revision = hashlib.sha256(
        f"{clean_review_id}\n{clean_comment_hash}".encode("utf-8")
    ).hexdigest()
    return f"update-review-correction-{revision}"


def validate_update_reviews(root: Path) -> list[str]:
    """Validate the complete tracked review directory without trusting edited display text."""
    memory_root = Path(root).resolve()
    reviews_root = memory_root / UPDATE_REVIEWS_DIRECTORY
    if not reviews_root.exists() and not reviews_root.is_symlink():
        return []
    if reviews_root.is_symlink() or not reviews_root.is_dir():
        return [f"{UPDATE_REVIEWS_DIRECTORY}: must be a directory"]
    errors: list[str] = []
    for path in sorted(reviews_root.iterdir(), key=lambda item: item.name):
        relative = path.relative_to(memory_root).as_posix()
        if path.is_symlink() or not path.is_file():
            errors.append(f"{relative}: must be a regular file")
            continue
        if path.suffix != ".md":
            errors.append(f"{relative}: unexpected tracked review path")
            continue
        try:
            parsed = parse_review_markdown(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(f"{relative}: {exc}")
            continue
        if path.name != f"{parsed.review_id}.md":
            errors.append(f"{relative}: filename does not match review id")
        elif parsed.review_id != review_id_for_operation(parsed.origin_operation_id):
            errors.append(f"{relative}: review id does not match origin operation")
    return errors


def verify_update_review(root: Path, review_id: str) -> VerifiedUpdateReview:
    """Recover authoritative Update context from synchronized Git history."""
    memory_root = Path(root).resolve()
    clean_review_id = _review_id(review_id)
    relative = f"{UPDATE_REVIEWS_DIRECTORY}/{clean_review_id}.md"
    tracked = _git_show_file(memory_root, "HEAD", relative)
    if tracked is None:
        raise FileNotFoundError(f"tracked update review does not exist: {relative}")
    current = parse_review_markdown(tracked)
    if current.review_id != clean_review_id:
        raise ValueError("tracked update review identity does not match its path")
    if current.ready or normalize_review_comment(current.comment):
        raise ValueError(
            "tracked update review contains local-only human submission state"
        )

    log = _git_stdout(memory_root, "log", "--format=%H", "--diff-filter=A", "--", relative)
    creation_commits = tuple(line.strip() for line in log.splitlines() if line.strip())
    if len(creation_commits) != 1:
        raise ValueError("tracked update review must have exactly one creating commit")
    creation_commit = creation_commits[0]
    original_text = _git_show_file(memory_root, creation_commit, relative)
    if original_text is None:
        raise ValueError("creating commit does not contain its update review")
    original = parse_review_markdown(original_text)
    if original.review_id != clean_review_id:
        raise ValueError("creating commit review identity does not match its path")
    if original.review_id != review_id_for_operation(original.origin_operation_id):
        raise ValueError("update review identity does not match its origin operation")
    for field in (
        "review_id",
        "origin_operation_id",
        "base_commit",
        "write_surface",
        "created_at",
        "summary",
    ):
        if getattr(current, field) != getattr(original, field):
            raise ValueError(f"tracked update review changed immutable {field}")

    parents = _git_stdout(memory_root, "rev-list", "--parents", "-n", "1", creation_commit).split()
    if len(parents) != 2:
        raise ValueError("update review creating commit must have exactly one parent")
    if not _git_is_ancestor(memory_root, original.base_commit, creation_commit):
        raise ValueError("update review base is not an ancestor of its creating commit")
    parent_commit = parents[1]
    intervening = tuple(
        path
        for path in _git_changed_paths(memory_root, original.base_commit, parent_commit)
        if not is_update_coordination_path(path)
    )
    if intervening:
        raise ValueError(
            "update review base-to-parent interval contains unrelated changes: "
            + ", ".join(intervening)
        )
    message = _git_stdout(memory_root, "show", "-s", "--format=%B", creation_commit)
    trailer = f"{OPERATION_TRAILER}: {original.origin_operation_id}"
    if trailer not in (line.strip() for line in message.splitlines()):
        raise ValueError("update review creating commit has no matching operation trailer")

    changed_paths = _git_rightmemory_paths(
        memory_root,
        original.base_commit,
        creation_commit,
    )
    if not changed_paths:
        raise ValueError("update review creating commit has no Memory or Pursuit change")
    write_surface = _write_surface(changed_paths)
    if original.write_surface != write_surface:
        raise ValueError("update review write surface does not match its Git diff")
    return VerifiedUpdateReview(
        review_id=clean_review_id,
        origin_operation_id=original.origin_operation_id,
        base_commit=original.base_commit,
        creation_commit=creation_commit,
        write_surface=write_surface,
        summary=original.summary,
        document_commit=_git_oid(
            _git_stdout(memory_root, "log", "-1", "--format=%H", "HEAD", "--", relative)
        ),
        document_blob_oid=_git_oid(
            _git_stdout(memory_root, "rev-parse", f"HEAD:{relative}")
        ),
        question=current.question,
        question_operation_id=current.question_operation_id,
        diff=_git_rightmemory_diff(memory_root, original.base_commit, creation_commit),
        changed_paths=changed_paths,
    )


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
            errors.append(
                f"line {line_number}: correction section `{title}` appears before any `##` entry"
            )
            continue
        current[2].append((title, line_number))

    if len(entries) > 15:
        errors.append(f"corrections.md contains {len(entries)} entries; at most 15 are allowed")

    expected = list(_CORRECTION_SECTIONS)
    for entry_index, (title, line_number, sections) in enumerate(entries):
        names = [name for name, _section_line in sections]
        unexpected = [
            (name, section_line)
            for name, section_line in sections
            if name not in _CORRECTION_SECTIONS
        ]
        for name, section_line in unexpected:
            errors.append(
                f"line {section_line}: correction entry `{title}` has unexpected `### {name}` section"
            )
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
                errors.append(
                    f"line {section_line}: correction entry `{title}` has empty `### {name}` content"
                )
    return errors


def _validate_outcome(outcome: UpdateReviewOutcome) -> None:
    if not isinstance(outcome, UpdateReviewOutcome):
        raise TypeError("update review callback must return UpdateReviewOutcome")
    if outcome.status not in _OUTCOME_STATUSES:
        raise ValueError(
            "update review outcome status must be one of: "
            + ", ".join(sorted(_OUTCOME_STATUSES))
        )
    if outcome.status == "needs_input" and not (outcome.message or "").strip():
        raise ValueError("needs-input outcome requires a message")


def _record_from_parsed(parsed: ParsedUpdateReview) -> UpdateReviewRecord:
    return UpdateReviewRecord(
        review_id=parsed.review_id,
        origin_operation_id=parsed.origin_operation_id,
        base_commit=parsed.base_commit,
        write_surface=parsed.write_surface,
        created_at=parsed.created_at,
        summary=parsed.summary,
    )


def _assert_same_review(parsed: ParsedUpdateReview, record: UpdateReviewRecord) -> None:
    for field in (
        "review_id",
        "origin_operation_id",
        "base_commit",
        "write_surface",
        "summary",
    ):
        if getattr(parsed, field) != getattr(record, field):
            raise ValueError(f"existing update review has different {field}")


def _review_id(value: str) -> str:
    clean = str(value).strip()
    if not _REVIEW_ID_RE.fullmatch(clean) or clean in {".", ".."}:
        raise ValueError("review_id must use 1-160 letters, digits, dots, underscores, or hyphens")
    return clean


def _single_line(value: str, field: str) -> str:
    clean = str(value).strip()
    if not clean or any(character in clean for character in "\n\r\0"):
        raise ValueError(f"{field} must be a non-empty single line")
    return clean


def _metadata_string(data: dict[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"update review metadata must contain string field `{key}`")
    return value


def _metadata_time(data: dict[str, Any]) -> str:
    value = _metadata_string(data, "created_at")
    _parse_time(value, "created_at")
    return value


def _parse_time(value: str, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _sha256(value: str) -> str:
    clean = str(value).strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", clean):
        raise ValueError("SHA-256 values must contain exactly 64 lowercase hexadecimal characters")
    return clean


def _git_oid(value: str) -> str:
    clean = str(value).strip().lower()
    if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", clean):
        raise ValueError("Git object IDs must contain 40 or 64 hexadecimal characters")
    return clean


def _git_blob_oid(root: Path, path: Path) -> str:
    memory_root = Path(root).resolve()
    relative = Path(path).resolve().relative_to(memory_root).as_posix()
    return _git_oid(_git_stdout(memory_root, "hash-object", "--", relative))


def _git_path_commit(root: Path, path: Path) -> str:
    memory_root = Path(root).resolve()
    relative = Path(path).resolve().relative_to(memory_root).as_posix()
    return _git_oid(
        _git_stdout(memory_root, "log", "-1", "--format=%H", "HEAD", "--", relative)
    )


def _marked_area(text: str, start: str, end: str, label: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"update review must contain exactly one {label} area")
    area_start = text.find(start) + len(start)
    area_end = text.find(end, area_start)
    if area_end < area_start:
        raise ValueError(f"update review {label} area is malformed")
    return text[area_start:area_end]


def _review_section_bounds(text: str) -> tuple[int, int]:
    ready_marker = text.rfind(READY_START)
    comment_marker = text.rfind(COMMENT_START)
    question_marker = text.rfind(QUESTION_START)
    human_start = text.rfind(HUMAN_HEADING, 0, ready_marker)
    question_start = text.rfind(
        QUESTION_HEADING,
        comment_marker + len(COMMENT_START),
        question_marker,
    )
    if not (
        0 <= human_start < ready_marker < comment_marker < question_start < question_marker
    ):
        raise ValueError("update review human and corrector sections are malformed")
    return human_start, question_start


def _replace_marked_area(text: str, start: str, end: str, value: str) -> str:
    _marked_area(text, start, end, "marked section")
    area_start = text.find(start) + len(start)
    area_end = text.find(end, area_start)
    return text[:area_start] + f"\n\n{value.strip()}\n\n" + text[area_end:]


def _question_markdown(message: str, operation_id: str) -> str:
    clean = message.strip()
    if not clean:
        raise ValueError("corrector question must not be blank")
    escaped = html.escape(clean, quote=False)
    return (
        f"{QUESTION_OPERATION_PREFIX}{_single_line(operation_id, 'operation_id')}"
        f"{QUESTION_OPERATION_SUFFIX}\n"
        "> **Corrector question:** "
        + escaped.replace("\n", "\n> ")
    )


def _parse_question_markdown(value: str) -> tuple[str, str | None]:
    clean = value.strip()
    if not clean:
        return "", None
    operation_id: str | None = None
    if clean.startswith(QUESTION_OPERATION_PREFIX):
        marker_end = clean.find(QUESTION_OPERATION_SUFFIX, len(QUESTION_OPERATION_PREFIX))
        if marker_end < 0:
            raise ValueError("corrector question operation marker is not closed")
        operation_id = _single_line(
            clean[len(QUESTION_OPERATION_PREFIX) : marker_end],
            "question_operation_id",
        )
        clean = clean[marker_end + len(QUESTION_OPERATION_SUFFIX) :].strip()
    prefix = "> **Corrector question:** "
    if clean.startswith(prefix):
        clean = clean[len(prefix) :]
        clean = "\n".join(
            line[2:] if line.startswith("> ") else line
            for line in clean.splitlines()
        )
    return html.unescape(clean).strip(), operation_id


def _markdown_fence(text: str) -> str:
    longest = 0
    for match in re.finditer(r"`+", text):
        longest = max(longest, len(match.group(0)))
    return "`" * max(3, longest + 1)


def _read_process_cursor(handle: Any) -> str | None:
    handle.seek(0)
    value = handle.read().strip()
    if not value:
        return None
    try:
        return _review_id(value)
    except ValueError:
        return None


def _write_process_cursor(handle: Any, review_id: str) -> None:
    handle.seek(0)
    handle.truncate()
    handle.write(f"{_review_id(review_id)}\n")
    handle.flush()
    os.fsync(handle.fileno())


def _recover_claimed_documents(reviews_root: Path) -> None:
    if not reviews_root.exists():
        return
    pattern = re.compile(
        r"^\.(?P<name>[A-Za-z0-9][A-Za-z0-9_.-]{0,159}\.md)\.\d+\.[0-9a-f]{32}\.cas$"
    )
    for claim in sorted(reviews_root.glob(".*.cas")):
        match = pattern.fullmatch(claim.name)
        if match is None:
            continue
        target = reviews_root / match.group("name")
        _restore_claim_if_vacant(claim, target)


def _delete_document_if_unchanged(path: Path, expected: str) -> bool:
    claim = _claim_document_if_unchanged(path, expected)
    if claim is None:
        return False
    try:
        claim.unlink()
    except Exception:
        _restore_claim_if_vacant(claim, path)
        raise
    _fsync_directory(path.parent)
    return not path.exists()


def _replace_document_if_unchanged(path: Path, expected: str, replacement: str) -> bool:
    claim = _claim_document_if_unchanged(path, expected)
    if claim is None:
        return False
    try:
        published = _write_text_if_absent(path, replacement)
    except Exception:
        _restore_claim_if_vacant(claim, path)
        raise
    if not published:
        _restore_claim_if_vacant(claim, path)
        return False
    claim.unlink(missing_ok=True)
    _fsync_directory(path.parent)
    return True


def _git_show_file(root: Path, revision: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="strict",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode == 0:
        return result.stdout
    if result.returncode in {1, 128}:
        return None
    raise RuntimeError(f"git show failed: {result.stderr.strip()}")


def _git_stdout(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        encoding="utf-8",
        errors="replace",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.rstrip()


def _git_is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode in {0, 1}:
        return result.returncode == 0
    raise RuntimeError(f"git merge-base failed: {result.stderr.strip()}")


def _git_changed_paths(root: Path, base: str, commit: str) -> tuple[str, ...]:
    if base == commit:
        return ()
    output = _git_stdout(root, "diff", "--name-only", "-z", base, commit)
    return tuple(sorted(path for path in output.split("\0") if path))


def _git_rightmemory_paths(root: Path, base: str, commit: str) -> tuple[str, ...]:
    output = _git_stdout(
        root,
        "diff",
        "--name-only",
        "-z",
        base,
        commit,
        "--",
        "MEMORY.md",
        ":(glob)MEMORY_*.md",
        "PURSUITS.md",
        ":(glob)PURSUIT_*.md",
    )
    return tuple(sorted(path for path in output.split("\0") if path))


def _git_rightmemory_diff(root: Path, base: str, commit: str) -> str:
    return _git_stdout(
        root,
        "diff",
        base,
        commit,
        "--",
        "MEMORY.md",
        ":(glob)MEMORY_*.md",
        "PURSUITS.md",
        ":(glob)PURSUIT_*.md",
    )


def _write_surface(paths: tuple[str, ...]) -> str:
    changed_memory = any(path == "MEMORY.md" or path.startswith("MEMORY_") for path in paths)
    changed_pursuit = any(path == "PURSUITS.md" or path.startswith("PURSUIT_") for path in paths)
    if changed_memory and changed_pursuit:
        return "Memory + Pursuit"
    if changed_memory:
        return "Memory"
    if changed_pursuit:
        return "Pursuit"
    return "RightMemory"


def _claim_document_if_unchanged(path: Path, expected: str) -> Path | None:
    if not _supports_atomic_hardlink_publication(path):
        return None
    claim = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.cas")
    try:
        os.replace(path, claim)
    except FileNotFoundError:
        return None
    try:
        mode = claim.stat(follow_symlinks=False).st_mode
        actual = claim.read_text(encoding="utf-8") if stat.S_ISREG(mode) else None
    except Exception:
        _restore_claim_if_vacant(claim, path)
        raise
    if actual != expected:
        _restore_claim_if_vacant(claim, path)
        return None
    return claim


def _supports_atomic_hardlink_publication(path: Path) -> bool:
    probe = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.link-probe")
    try:
        os.link(path, probe, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RuntimeError(
            "exact review finalization requires hard-link support in the review directory"
        ) from exc
    finally:
        probe.unlink(missing_ok=True)
    return True


def _restore_claim_if_vacant(claim: Path, path: Path) -> None:
    try:
        os.link(claim, path, follow_symlinks=False)
    except FileExistsError:
        pass
    except OSError as exc:
        if not path.exists():
            raise RuntimeError(f"could not restore claimed review document: {path}") from exc
    claim.unlink(missing_ok=True)
    _fsync_directory(path.parent)


def _write_text_if_absent(path: Path, text: str) -> bool:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.publish")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path, follow_symlinks=False)
        except FileExistsError:
            return False
        except OSError as exc:
            raise RuntimeError(
                "exact review publication requires hard-link support in the review directory"
            ) from exc
        _fsync_directory(path.parent)
        return True
    finally:
        temporary.unlink(missing_ok=True)


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

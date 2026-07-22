from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.update_review import (
    COMMENT_END,
    COMMENT_START,
    READY_END,
    READY_LABEL,
    READY_START,
    UpdateExecutionLock,
    UpdateReviewOutcome,
    UpdateReviewStore,
    _claim_document_if_unchanged,
    _delete_document_if_unchanged,
    correction_operation_id,
    parse_review_markdown,
    review_comment_sha256,
    validate_corrections_markdown,
    verify_update_review,
)


class UpdateReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _store(
        self,
        *,
        blank_review_limit: int = 50,
        blank_review_expiry_days: int = 30,
    ) -> UpdateReviewStore:
        return UpdateReviewStore(
            self.root,
            blank_review_limit=blank_review_limit,
            blank_review_expiry_days=blank_review_expiry_days,
        )

    def _create(
        self,
        store: UpdateReviewStore,
        review_id: str = "abc123",
        *,
        created_at: str | None = None,
    ):
        return store.create_review(
            review_id=review_id,
            origin_operation_id=f"update-operation-{review_id}",
            base_commit=f"base-{review_id}",
            write_surface="Memory + Pursuit",
            summary="Kept the stable location and removed snapshot detail.",
            diff="- old\n+ new\n``` nested fence",
            created_at=created_at,
        )

    def _edit_review(
        self,
        store: UpdateReviewStore,
        review_id: str,
        *,
        comment: str | None = None,
        ready: bool | None = None,
    ) -> None:
        path = store.review_path(review_id)
        text = path.read_text(encoding="utf-8")
        if comment is not None:
            start = text.index(COMMENT_START) + len(COMMENT_START)
            end = text.index(COMMENT_END, start)
            text = text[:start] + f"\n\n{comment}\n\n" + text[end:]
        if ready is not None:
            old = f"- [{'x' if not ready else ' '}] {READY_LABEL}"
            new = f"- [{'x' if ready else ' '}] {READY_LABEL}"
            start = text.index(READY_START) + len(READY_START)
            end = text.index(READY_END, start)
            control = text[start:end]
            self.assertIn(old, control)
            text = text[:start] + control.replace(old, new, 1) + text[end:]
        path.write_text(text, encoding="utf-8")

    def test_create_review_renders_markdown_owned_comment_and_ready_control(self):
        store = self._store()
        record = self._create(store)

        text = store.review_path(record.review_id).read_text(encoding="utf-8")
        parsed = parse_review_markdown(text)

        self.assertEqual(text.count(COMMENT_START), 1)
        self.assertEqual(text.count(COMMENT_END), 1)
        self.assertIn(f"- [ ] {READY_LABEL}", text)
        self.assertIn("````diff", text)
        self.assertEqual(parsed.origin_operation_id, "update-operation-abc123")
        self.assertEqual(parsed.summary, "Kept the stable location and removed snapshot detail.")
        self.assertFalse(parsed.ready)
        self.assertEqual(parsed.comment.strip(), "")
        self.assertEqual(store.root, self.root / "update_reviews")

    def test_create_review_is_idempotent_without_overwriting_human_text(self):
        store = self._store()
        first = self._create(store)
        self._edit_review(store, first.review_id, comment="Remove the transient count.")

        second = store.create_review(
            review_id=first.review_id,
            origin_operation_id=first.origin_operation_id,
            base_commit=first.base_commit,
            write_surface=first.write_surface,
            summary=first.summary,
            diff="a different display diff",
        )

        self.assertEqual(second, first)
        parsed = parse_review_markdown(store.review_path(first.review_id).read_text(encoding="utf-8"))
        self.assertEqual(parsed.comment.strip(), "Remove the transient count.")

    def test_displayed_diff_is_editable_and_never_enters_the_request(self):
        store = self._store()
        self._create(store)
        path = store.review_path("abc123")
        path.write_text(
            path.read_text(encoding="utf-8").replace("+ new", "+ human-edited display"),
            encoding="utf-8",
        )
        self._edit_review(store, "abc123", comment="Keep the stable path.", ready=True)
        seen = []

        result = store.process_ready(
            lambda request: seen.append(request) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(result.resolved, 1)
        self.assertEqual(len(seen), 1)
        self.assertFalse(hasattr(seen[0], "original_diff"))

    def test_ready_control_is_scoped_away_from_matching_lines_in_diff_and_comment(self):
        store = self._store()
        store.create_review(
            review_id="abc123",
            origin_operation_id="update-operation-abc123",
            base_commit="base-abc123",
            write_surface="Memory",
            summary="Added a checkbox example.",
            diff=f"+ - [ ] {READY_LABEL}",
        )
        self._edit_review(
            store,
            "abc123",
            comment=f"The text may mention this line:\n- [x] {READY_LABEL}",
            ready=True,
        )

        result = store.process_ready(lambda _request: UpdateReviewOutcome.resolved())

        self.assertEqual(result.resolved, 1)

    def test_comment_without_ready_is_not_submitted(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Remove the snapshot value.")
        calls = []

        result = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(result.processed, 0)
        self.assertEqual(result.not_ready, 1)
        self.assertEqual(calls, [])

    def test_ready_submission_is_processed_immediately_without_mtime_heuristic(self):
        store = self._store()
        self._create(store)
        self._edit_review(
            store,
            "abc123",
            comment="Remove the snapshot value.",
            ready=True,
        )

        result = store.process_ready(lambda _request: UpdateReviewOutcome.resolved())

        self.assertEqual(result.processed, 1)
        self.assertEqual(result.resolved, 1)
        self.assertFalse(store.review_path("abc123").exists())

    def test_free_form_comment_may_contain_generated_section_headings(self):
        store = self._store()
        self._create(store)
        comment = (
            "Keep these headings as part of my note:\n\n"
            "## Human review\n\n"
            "This is quoted context.\n\n"
            "## Corrector question\n\n"
            "This is also quoted context."
        )
        self._edit_review(store, "abc123", comment=comment, ready=True)
        seen = []

        result = store.process_ready(
            lambda request: seen.append(request.comment) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(result.resolved, 1)
        self.assertEqual(seen, [comment])

    def test_needs_input_unchecks_ready_and_writes_question_into_same_document(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Keep the durable path.", ready=True)
        calls = []

        first = store.process_ready(
            lambda request: calls.append(request)
            or UpdateReviewOutcome.needs_input("Which path should remain?")
        )
        second = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved()
        )

        parsed = parse_review_markdown(store.review_path("abc123").read_text(encoding="utf-8"))
        self.assertEqual(first.needs_input, 1)
        self.assertEqual(second.processed, 0)
        self.assertEqual(second.blank, 1)
        self.assertFalse(parsed.ready)
        self.assertEqual(parsed.comment.strip(), "")
        self.assertIn("Which path should remain?", parsed.question)
        self.assertEqual(len(calls), 1)

        self._edit_review(
            store,
            "abc123",
            comment="Use `/stable/path`.",
            ready=True,
        )
        follow_up = []
        third = store.process_ready(
            lambda request: follow_up.append(request) or UpdateReviewOutcome.resolved()
        )
        self.assertEqual(third.resolved, 1)
        self.assertEqual(follow_up[0].previous_question, "Which path should remain?")

    def test_corrector_question_cannot_inject_review_control_markers(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Clarify this.", ready=True)

        result = store.process_ready(
            lambda _request: UpdateReviewOutcome.needs_input(
                f"Could this include {COMMENT_END} literally?"
            )
        )

        text = store.review_path("abc123").read_text(encoding="utf-8")
        parsed = parse_review_markdown(text)
        self.assertEqual(result.needs_input, 1)
        self.assertEqual(text.count(COMMENT_END), 1)
        self.assertIn(COMMENT_END, parsed.question)
        self.assertIn("&lt;!--", text)

    def test_question_write_failure_is_reported_and_leaves_ready_document_for_retry(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Clarify this.", ready=True)

        with patch(
            "rightmemory.update_review._write_text_if_absent",
            side_effect=OSError("disk full"),
        ):
            result = store.process_ready(
                lambda _request: UpdateReviewOutcome.needs_input("Which value?")
            )

        parsed = parse_review_markdown(store.review_path("abc123").read_text(encoding="utf-8"))
        self.assertEqual(result.failed, 1)
        self.assertTrue(parsed.ready)
        self.assertEqual(parsed.question.strip(), "")

    def test_callback_failure_leaves_ready_document_untouched_and_retries_same_revision(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Apply this correction.", ready=True)
        path = store.review_path("abc123")
        original = path.read_text(encoding="utf-8")
        operation_ids = []

        def fail(request):
            operation_ids.append(request.operation_id)
            raise RuntimeError("executor unavailable")

        first = store.process_ready(fail)
        second = store.process_ready(fail)

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.failed, 1)
        self.assertEqual(operation_ids[0], operation_ids[1])
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_cursor_write_failure_leaves_ready_document_untouched(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Apply this correction.", ready=True)
        path = store.review_path("abc123")
        original = path.read_text(encoding="utf-8")

        with patch(
            "rightmemory.update_review._write_process_cursor",
            side_effect=OSError("disk full"),
        ):
            result = store.process_ready(
                lambda _request: self.fail("correction must not run without a durable cursor")
            )

        self.assertEqual(result.failed, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_one_failed_review_does_not_starve_the_next(self):
        store = self._store()
        for review_id in ("a", "b"):
            self._create(store, review_id)
            self._edit_review(store, review_id, comment=f"Correct {review_id}.", ready=True)
        calls = []

        def run(request):
            calls.append(request.review_id)
            if request.review_id == "a":
                raise RuntimeError("provider unavailable")
            return UpdateReviewOutcome.resolved()

        first = store.process_ready(run)
        second = self._store().process_ready(run)

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.resolved, 1)
        self.assertEqual(calls, ["a", "b"])

    def test_fairness_cursor_advances_past_a_deleted_review(self):
        store = self._store()
        for review_id in ("a", "b", "c"):
            self._create(store, review_id)
            self._edit_review(store, review_id, comment=f"Correct {review_id}.", ready=True)
        calls = []

        def run(request):
            calls.append(request.review_id)
            if request.review_id == "a":
                raise RuntimeError("provider unavailable")
            return UpdateReviewOutcome.resolved()

        first = store.process_ready(run)
        second = self._store().process_ready(run)
        third = self._store().process_ready(run)

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.resolved, 1)
        self.assertEqual(third.resolved, 1)
        self.assertEqual(calls, ["a", "b", "c"])

    def test_operation_id_depends_only_on_review_and_normalized_comment(self):
        first_hash = review_comment_sha256("  keep this\r\npath  ")
        second_hash = review_comment_sha256("keep this\npath")

        self.assertEqual(first_hash, second_hash)
        self.assertEqual(
            correction_operation_id("abc123", first_hash),
            correction_operation_id("abc123", second_hash),
        )
        self.assertNotEqual(
            correction_operation_id("abc123", first_hash),
            correction_operation_id("other", first_hash),
        )

    def test_resolved_callback_does_not_delete_a_newer_document_revision(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="First correction", ready=True)

        def resolve_while_user_edits(_request):
            self._edit_review(store, "abc123", comment="Second correction")
            return UpdateReviewOutcome.resolved()

        first = store.process_ready(resolve_while_user_edits)
        seen = []
        second = store.process_ready(
            lambda request: seen.append(request.comment) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(first.changed, 1)
        self.assertEqual(first.resolved, 0)
        self.assertEqual(second.resolved, 1)
        self.assertEqual(seen, ["Second correction"])

    def test_resolved_finalization_preserves_edit_saved_during_atomic_claim(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="First correction", ready=True)
        path = store.review_path("abc123")
        newer = path.read_text(encoding="utf-8").replace("First correction", "Newer correction")
        real_replace = os.replace

        def replace_then_save(source, destination):
            real_replace(source, destination)
            if Path(source) == path and str(destination).endswith(".cas"):
                path.write_text(newer, encoding="utf-8")

        with patch("rightmemory.update_review.os.replace", side_effect=replace_then_save):
            result = store.process_ready(lambda _request: UpdateReviewOutcome.resolved())

        self.assertEqual(result.changed, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), newer)

    def test_needs_input_callback_does_not_overwrite_a_newer_document_revision(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="First correction", ready=True)

        def question_while_user_edits(_request):
            self._edit_review(store, "abc123", comment="Newer correction")
            return UpdateReviewOutcome.needs_input("Which value?")

        result = store.process_ready(question_while_user_edits)
        parsed = parse_review_markdown(store.review_path("abc123").read_text(encoding="utf-8"))

        self.assertEqual(result.changed, 1)
        self.assertEqual(result.needs_input, 0)
        self.assertEqual(parsed.comment.strip(), "Newer correction")
        self.assertEqual(parsed.question.strip(), "")
        self.assertTrue(parsed.ready)

    def test_needs_input_finalization_preserves_edit_saved_during_atomic_claim(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="First correction", ready=True)
        path = store.review_path("abc123")
        newer = path.read_text(encoding="utf-8").replace("First correction", "Newer correction")
        real_replace = os.replace

        def replace_then_save(source, destination):
            real_replace(source, destination)
            if Path(source) == path and str(destination).endswith(".cas"):
                path.write_text(newer, encoding="utf-8")

        with patch("rightmemory.update_review.os.replace", side_effect=replace_then_save):
            result = store.process_ready(
                lambda _request: UpdateReviewOutcome.needs_input("Which value?")
            )

        self.assertEqual(result.changed, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), newer)

    def test_next_scan_recovers_a_review_claim_left_by_process_interruption(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Apply this correction", ready=True)
        path = store.review_path("abc123")
        document = path.read_text(encoding="utf-8")
        claim = _claim_document_if_unchanged(path, document)

        self.assertIsNotNone(claim)
        self.assertFalse(path.exists())
        seen = []
        result = store.process_ready(
            lambda request: seen.append(request.comment) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(result.resolved, 1)
        self.assertEqual(seen, ["Apply this correction"])
        self.assertFalse(path.exists())
        self.assertFalse(claim.exists())

    def test_missing_hardlink_support_leaves_ready_document_untouched(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Apply this correction", ready=True)
        path = store.review_path("abc123")
        original = path.read_text(encoding="utf-8")

        with patch("rightmemory.update_review.os.link", side_effect=OSError("unsupported")):
            result = store.process_ready(lambda _request: UpdateReviewOutcome.resolved())

        self.assertEqual(result.failed, 1)
        self.assertEqual(path.read_text(encoding="utf-8"), original)

    def test_process_lock_serializes_concurrent_scans(self):
        store = self._store()
        self._create(store)
        self._edit_review(store, "abc123", comment="Apply this correction", ready=True)
        entered = threading.Event()
        release = threading.Event()
        second_finished = threading.Event()
        results = []

        def blocking_callback(_request):
            entered.set()
            self.assertTrue(release.wait(5))
            return UpdateReviewOutcome.resolved()

        first = threading.Thread(target=lambda: results.append(store.process_ready(blocking_callback)))
        second = threading.Thread(
            target=lambda: (
                results.append(store.process_ready(lambda _request: UpdateReviewOutcome.resolved())),
                second_finished.set(),
            )
        )
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        self.assertFalse(second_finished.wait(0.1))
        release.set()
        first.join(5)
        second.join(5)

        self.assertEqual(sum(result.resolved for result in results), 1)
        self.assertEqual(sum(result.failed for result in results), 0)

    def test_review_creation_does_not_invert_scanner_and_update_execution_locks(self):
        store = self._store()
        self._create(store, "ready")
        self._edit_review(store, "ready", comment="Apply this correction", ready=True)
        update_locked = threading.Event()
        scanner_waiting = threading.Event()
        errors = []

        def normal_update():
            try:
                with UpdateExecutionLock(self.root):
                    update_locked.set()
                    self.assertTrue(scanner_waiting.wait(5))
                    self._create(store, "new-update")
            except Exception as exc:
                errors.append(exc)

        def scan():
            try:
                def correct(_request):
                    scanner_waiting.set()
                    with UpdateExecutionLock(self.root):
                        return UpdateReviewOutcome.resolved()

                store.process_ready(correct)
            except Exception as exc:
                errors.append(exc)

        update_thread = threading.Thread(target=normal_update, daemon=True)
        scan_thread = threading.Thread(target=scan, daemon=True)
        update_thread.start()
        self.assertTrue(update_locked.wait(5))
        scan_thread.start()
        update_thread.join(5)
        scan_thread.join(5)

        self.assertFalse(update_thread.is_alive(), "normal Update deadlocked with review scan")
        self.assertFalse(scan_thread.is_alive(), "review scan deadlocked with normal Update")
        self.assertEqual(errors, [])
        self.assertTrue(store.review_path("new-update").is_file())

    def test_blank_review_pruning_preserves_comments_and_corrector_questions(self):
        store = self._store(blank_review_limit=2, blank_review_expiry_days=30)
        origin = datetime.now(UTC)
        self._create(store, "one", created_at=origin.isoformat())
        self._create(store, "two", created_at=(origin + timedelta(seconds=1)).isoformat())
        self._edit_review(store, "one", comment="Keep this review open.")
        self._create(store, "three", created_at=(origin + timedelta(seconds=2)).isoformat())
        self._create(store, "four", created_at=(origin + timedelta(seconds=3)).isoformat())

        self._edit_review(store, "three", comment="Clarify this.", ready=True)
        store.process_ready(lambda _request: UpdateReviewOutcome.needs_input("Which value?"))
        self._edit_review(store, "three", comment="")
        pruned = store.prune_blank_reviews(now=origin + timedelta(days=31))

        self.assertFalse(store.review_path("two").exists())
        self.assertTrue(store.review_path("one").exists())
        self.assertTrue(store.review_path("three").exists())
        self.assertIn("four", pruned)

    def test_blank_pruning_rechecks_exact_document_before_removal(self):
        store = self._store(blank_review_expiry_days=30)
        origin = datetime.now(UTC)
        self._create(store, "blank", created_at=origin.isoformat())
        path = store.review_path("blank")
        def edit_then_delete(candidate, expected):
            self._edit_review(store, "blank", comment="New human comment.")
            return _delete_document_if_unchanged(candidate, expected)

        with patch(
            "rightmemory.update_review._delete_document_if_unchanged",
            side_effect=edit_then_delete,
        ):
            pruned = store.prune_blank_reviews(now=origin + timedelta(days=31))

        self.assertEqual(pruned, ())
        self.assertIn("New human comment", path.read_text(encoding="utf-8"))

    def test_review_inbox_has_no_state_or_pending_sidecars(self):
        store = self._store()
        record = self._create(store)

        self.assertEqual(store.list_records(), [record])
        self.assertFalse((store.root / "state.json").exists())
        self.assertFalse((store.root / "pending").exists())

    def test_execution_lock_uses_shared_update_runtime_path(self):
        lock = UpdateExecutionLock(self.root)
        expected = self.root / ".runtime" / "update" / "execution.lock"

        with lock:
            self.assertEqual(lock.lock_path, expected)
            self.assertTrue(expected.is_file())


class UpdateReviewGitVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Memory\n", encoding="utf-8")
        (self.root / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.root / "PURSUIT_RULES.md").write_text("# Rules\n", encoding="utf-8")
        self._git("add", ".")
        self._git("commit", "-m", "initial memory")
        self.base = self._git("rev-parse", "HEAD")

    def test_verification_rejects_semantic_change_between_base_and_creation_parent(self):
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n- unrelated semantic change\n",
            encoding="utf-8",
        )
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "unrelated update")
        review_id = self._commit_review("target update")

        with self.assertRaisesRegex(ValueError, "base-to-parent interval"):
            verify_update_review(self.root, review_id)

    def test_verification_allows_coordination_change_between_base_and_creation_parent(self):
        lease = self.root / "update_queue" / "lease.json"
        lease.parent.mkdir()
        lease.write_text("{}\n", encoding="utf-8")
        self._git("add", str(lease.relative_to(self.root)))
        self._git("commit", "-m", "coordination-only change")
        review_id = self._commit_review("target update")

        verified = verify_update_review(self.root, review_id)

        self.assertEqual(verified.review_id, review_id)
        self.assertEqual(verified.changed_paths, ("MEMORY.md",))

    def test_verification_rejects_committed_human_submission_state(self):
        review_id = self._commit_review("target update")
        store = UpdateReviewStore(self.root)
        path = store.review_path(review_id)
        text = path.read_text(encoding="utf-8")
        text = text.replace(
            f"- [ ] {READY_LABEL}",
            f"- [x] {READY_LABEL}",
            1,
        ).replace(COMMENT_START, COMMENT_START + "\n\nUse the stable value.", 1)
        path.write_text(text, encoding="utf-8")
        self._git("add", str(path.relative_to(self.root)))
        self._git("commit", "-m", "commit review draft")

        with self.assertRaisesRegex(ValueError, "local-only human submission state"):
            verify_update_review(self.root, review_id)

    def _commit_review(self, label: str) -> str:
        operation_id = f"operation-{label.replace(' ', '-')}"
        (self.root / "MEMORY.md").write_text(
            (self.root / "MEMORY.md").read_text(encoding="utf-8")
            + f"\n- {label}\n",
            encoding="utf-8",
        )
        store = UpdateReviewStore(self.root)
        record = store.create_review(
            origin_operation_id=operation_id,
            base_commit=self.base,
            write_surface="Memory",
            summary=label,
            diff=self._git("diff", "--", "MEMORY.md"),
        )
        self._git("add", "MEMORY.md", str(store.review_path(record.review_id).relative_to(self.root)))
        self._git(
            "commit",
            "-m",
            f"{label}\n\nRightMemory-Operation: {operation_id}",
        )
        return record.review_id

    def _git(self, *args: str) -> str:
        result = subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        )
        return result.stdout.strip()


class CorrectionsMarkdownValidationTests(unittest.TestCase):
    def test_valid_collection_and_headings_inside_fences(self):
        text = """\
# RightMemory Update Corrections

## Stable paths

### Background

The updater included snapshot values.

### Proposed edit

```md
## This is evidence, not another entry
### Background
```

### Accepted edit

Keep only the stable path.
"""

        self.assertEqual(validate_corrections_markdown(text), [])

    def test_reports_missing_duplicate_unexpected_and_out_of_order_sections(self):
        text = """\
# RightMemory Update Corrections

## Bad entry

### Accepted edit

x

### Background

y

### Background

z

### Lesson

extra
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("missing `### Proposed edit`" in error for error in errors))
        self.assertTrue(any("repeats `### Background`" in error for error in errors))
        self.assertTrue(any("unexpected `### Lesson`" in error for error in errors))

    def test_rejects_more_than_fifteen_entries(self):
        entry = """\
## Entry {number}

### Background
a
### Proposed edit
b
### Accepted edit
c
"""
        text = "# RightMemory Update Corrections\n\n" + "\n".join(
            entry.format(number=index) for index in range(1, 17)
        )

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("contains 16 entries" in error for error in errors))

    def test_rejects_empty_required_section_content(self):
        text = """\
## Empty proposal

### Background
context

### Proposed edit

### Accepted edit
accepted
"""

        errors = validate_corrections_markdown(text)

        self.assertTrue(any("empty `### Proposed edit` content" in error for error in errors))


if __name__ == "__main__":
    unittest.main()

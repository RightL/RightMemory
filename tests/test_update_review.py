from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rightmemory.update_review import (
    COMMENT_END,
    COMMENT_START,
    UpdateExecutionLock,
    UpdateReviewOutcome,
    UpdateReviewStore,
    parse_review_markdown,
    validate_corrections_markdown,
)


class UpdateReviewStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def _store(
        self,
        *,
        stable_seconds: int = 0,
        blank_review_limit: int = 50,
        blank_review_expiry_days: int = 30,
    ) -> UpdateReviewStore:
        return UpdateReviewStore(
            self.root,
            stable_seconds=stable_seconds,
            blank_review_limit=blank_review_limit,
            blank_review_expiry_days=blank_review_expiry_days,
        )

    def _create(self, store: UpdateReviewStore, review_id: str = "abc123", *, created_at: str | None = None):
        return store.create_review(
            review_id=review_id,
            base_commit=f"base-{review_id}",
            update_commit=f"commit-{review_id}",
            write_surface="full",
            summary="Kept the stable location and removed snapshot detail.",
            diff="- old\n+ new\n``` nested fence",
            created_at=created_at,
        )

    def _write_comment(self, store: UpdateReviewStore, review_id: str, comment: str) -> None:
        path = store.review_path(review_id)
        text = path.read_text(encoding="utf-8")
        start = text.index(COMMENT_START) + len(COMMENT_START)
        end = text.index(COMMENT_END, start)
        updated = text[:start] + f"\n\n{comment}\n\n" + text[end:]
        path.write_text(updated, encoding="utf-8")

    def test_create_review_renders_one_free_form_comment_area_and_parseable_metadata(self):
        store = self._store()
        record = self._create(store)

        path = store.review_path(record.review_id)
        text = path.read_text(encoding="utf-8")
        parsed = parse_review_markdown(text)

        self.assertEqual(text.count(COMMENT_START), 1)
        self.assertEqual(text.count(COMMENT_END), 1)
        self.assertIn("## Human review", text)
        self.assertIn("````diff", text)
        self.assertEqual(parsed.review_id, "abc123")
        self.assertEqual(parsed.base_commit, "base-abc123")
        self.assertEqual(parsed.update_commit, "commit-abc123")
        self.assertEqual(parsed.write_surface, "full")
        self.assertEqual(parsed.comment.strip(), "")
        self.assertEqual(parsed.original_diff, "- old\n+ new\n``` nested fence")
        self.assertTrue((self.root / ".runtime" / ".gitignore").is_file())

    def test_create_review_is_idempotent_and_never_overwrites_human_comment(self):
        store = self._store()
        first = self._create(store)
        self._write_comment(store, first.review_id, "Remove the transient count.")

        second = store.create_review(
            review_id=first.review_id,
            base_commit=first.base_commit,
            update_commit=first.update_commit,
            write_surface=first.write_surface,
            summary="A different retry summary",
            diff="- old\n+ new\n``` nested fence",
        )

        self.assertEqual(second, first)
        parsed = parse_review_markdown(store.review_path(first.review_id).read_text(encoding="utf-8"))
        self.assertEqual(parsed.comment.strip(), "Remove the transient count.")

    def test_create_review_rejects_a_different_diff_for_the_same_update(self):
        store = self._store()
        first = self._create(store)

        with self.assertRaisesRegex(ValueError, "different original_diff_sha256"):
            store.create_review(
                review_id=first.review_id,
                base_commit=first.base_commit,
                update_commit=first.update_commit,
                write_surface=first.write_surface,
                summary="retry",
                diff="different",
            )

    def test_pending_review_obligation_materializes_and_clears(self):
        store = self._store()
        review_id = store.queue_review(
            review_id="queued",
            base_commit="base-queued",
            update_commit="commit-queued",
            write_surface="Memory + Pursuit",
            summary="Unified update",
            diff="- before\n+ after",
            created_at="2026-07-17T00:00:00+00:00",
        )

        self.assertTrue(store.pending_path(review_id).is_file())
        self.assertEqual(store.materialize_pending(), 1)
        self.assertFalse(store.pending_path(review_id).exists())
        parsed = parse_review_markdown(store.review_path(review_id).read_text(encoding="utf-8"))
        self.assertEqual(parsed.original_diff, "- before\n+ after")

    def test_parse_rejects_an_edited_original_diff(self):
        store = self._store()
        record = self._create(store)
        path = store.review_path(record.review_id)
        path.write_text(path.read_text(encoding="utf-8").replace("+ new", "+ tampered"), encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "original diff was modified"):
            parse_review_markdown(path.read_text(encoding="utf-8"))

    def test_stable_nonempty_comment_is_processed_once_and_needs_input_waits_for_change(self):
        store = self._store()
        self._create(store)
        self._write_comment(store, "abc123", "Please keep only the stable path.")
        calls = []

        first = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.needs_input("Which path should remain?")
        )
        second = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(first.processed, 1)
        self.assertEqual(first.needs_input, 1)
        self.assertEqual(second.processed, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].original_diff, "- old\n+ new\n``` nested fence")
        review_text = store.review_path("abc123").read_text(encoding="utf-8")
        self.assertIn("**Needs input.** Which path should remain?", review_text)

        self._write_comment(store, "abc123", "Keep `/stable/path`; remove the counts.")
        third = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved(correction_commit="fix123")
        )

        self.assertEqual(third.processed, 1)
        self.assertEqual(third.resolved, 1)
        self.assertEqual(len(calls), 2)
        self.assertFalse(store.review_path("abc123").exists())
        self.assertEqual(store.list_records(), [])

    def test_needs_input_marker_survives_state_reconstruction(self):
        store = self._store()
        self._create(store)
        self._write_comment(store, "abc123", "Keep the durable location.")
        calls = []

        first = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.needs_input("Which location?")
        )
        store.state_path.unlink()
        second = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(first.needs_input, 1)
        self.assertEqual(second.processed, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(calls), 1)
        review_text = store.review_path("abc123").read_text(encoding="utf-8")
        self.assertIn("**Needs input.** Which location?", review_text)

    def test_recently_modified_comment_waits_until_stable(self):
        store = self._store(stable_seconds=60)
        self._create(store)
        self._write_comment(store, "abc123", "Remove the snapshot value.")
        path = store.review_path("abc123")
        mtime = path.stat().st_mtime
        calls = []

        waiting = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved(),
            now=mtime + 59,
        )
        ready = store.process_ready(
            lambda request: calls.append(request) or UpdateReviewOutcome.resolved(),
            now=mtime + 60,
        )

        self.assertEqual(waiting.unstable, 1)
        self.assertEqual(waiting.processed, 0)
        self.assertEqual(ready.resolved, 1)
        self.assertEqual(len(calls), 1)

    def test_resolved_comment_does_not_delete_a_newer_nonempty_revision(self):
        store = self._store()
        self._create(store)
        self._write_comment(store, "abc123", "First correction")

        def resolve_while_user_edits(request):
            self._write_comment(store, request.review_id, "Second correction")
            return UpdateReviewOutcome.resolved()

        first = store.process_ready(resolve_while_user_edits)
        self.assertEqual(first.resolved, 0)
        self.assertTrue(store.review_path("abc123").exists())

        seen = []
        second = store.process_ready(
            lambda request: seen.append(request.comment) or UpdateReviewOutcome.resolved()
        )

        self.assertEqual(second.resolved, 1)
        self.assertEqual(seen, ["Second correction"])

    def test_concurrent_scans_do_not_invalidate_an_active_correction(self):
        store = self._store()
        self._create(store)
        self._write_comment(store, "abc123", "Apply this correction")
        entered = threading.Event()
        release = threading.Event()
        second_finished = threading.Event()
        results = []

        def blocking_callback(request):
            self.assertIn("Apply this correction", request.document)
            entered.set()
            self.assertTrue(release.wait(5))
            return UpdateReviewOutcome.resolved()

        first = threading.Thread(target=lambda: results.append(store.process_ready(blocking_callback)))
        second = threading.Thread(
            target=lambda: (results.append(store.process_ready(lambda _request: UpdateReviewOutcome.resolved())), second_finished.set())
        )
        first.start()
        self.assertTrue(entered.wait(5))
        second.start()
        self.assertFalse(second_finished.wait(0.1))
        release.set()
        first.join(5)
        second.join(5)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(sum(result.resolved for result in results), 1)
        self.assertEqual(sum(result.failed for result in results), 0)

    def test_callback_failure_is_reported_once_until_comment_changes(self):
        store = self._store()
        self._create(store)
        self._write_comment(store, "abc123", "Apply this correction")
        calls = []

        def fail(request):
            calls.append(request)
            raise RuntimeError("executor unavailable")

        first = store.process_ready(fail)
        second = store.process_ready(fail)

        self.assertEqual(first.failed, 1)
        self.assertEqual(second.processed, 0)
        self.assertEqual(second.unchanged, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Edit the human review comment to retry", store.review_path("abc123").read_text(encoding="utf-8"))

    def test_blank_reviews_are_retained_up_to_cap_without_pruning_commented_review(self):
        store = self._store(blank_review_limit=2)
        origin = datetime.now(UTC)
        self._create(store, "one", created_at=origin.isoformat())
        self._create(store, "two", created_at=(origin + timedelta(seconds=1)).isoformat())
        self._write_comment(store, "one", "Keep this review open.")
        self._create(store, "three", created_at=(origin + timedelta(seconds=2)).isoformat())
        self._create(store, "four", created_at=(origin + timedelta(seconds=3)).isoformat())

        self.assertTrue(store.review_path("one").exists())
        self.assertFalse(store.review_path("two").exists())
        self.assertTrue(store.review_path("three").exists())
        self.assertTrue(store.review_path("four").exists())
        self.assertEqual([record.review_id for record in store.list_records()], ["one", "three", "four"])

    def test_untouched_blank_review_expires_but_needs_input_review_does_not(self):
        store = self._store(blank_review_expiry_days=30)
        origin = datetime.now(UTC)
        self._create(store, "blank", created_at=origin.isoformat())
        self._create(store, "needs-input", created_at=(origin + timedelta(seconds=1)).isoformat())
        self._write_comment(store, "needs-input", "Clarify this edit.")
        store.process_ready(lambda _request: UpdateReviewOutcome.needs_input("Which value should remain?"))
        self._write_comment(store, "needs-input", "")

        pruned = store.prune_blank_reviews(now=origin + timedelta(days=31))

        self.assertEqual(pruned, ("blank",))
        self.assertFalse(store.review_path("blank").exists())
        self.assertTrue(store.review_path("needs-input").exists())

    def test_sidecar_can_be_rebuilt_from_review_metadata(self):
        store = self._store()
        record = self._create(store)
        store.state_path.unlink()

        records = store.list_records()

        self.assertEqual(records, [record])

    def test_execution_lock_uses_update_runtime_path(self):
        lock = UpdateExecutionLock(self.root)
        expected = self.root / ".runtime" / "update" / "execution.lock"

        with lock:
            self.assertEqual(lock.lock_path, expected)
            self.assertTrue(expected.is_file())


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

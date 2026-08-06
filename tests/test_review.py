import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import ReviewConfig, ReviewSourceConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.review import (
    REVIEW_NO_CANDIDATE,
    ReviewDeliveryReceipt,
    ReviewScanResult,
    ReviewScanner,
    ReviewStateStore,
)
from rightmemory.transcripts.codex import parse_session as parse_codex_session
from rightmemory.transcripts.claude import parse_session as parse_claude_session


class TranscriptParserTests(unittest.TestCase):
    def test_codex_parser_normalizes_completed_turns(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "rollout.jsonl"
            self._write_jsonl(
                path,
                [
                    {"type": "session_meta", "timestamp": "t0", "payload": {"id": "s1", "cwd": "/repo"}},
                    {"type": "event_msg", "timestamp": "t1", "payload": {"type": "user_message", "message": "hello"}},
                    {"type": "event_msg", "timestamp": "t2", "payload": {"type": "agent_message", "message": "hi"}},
                    {"type": "event_msg", "timestamp": "t3", "payload": {"type": "task_complete"}},
                ],
            )

            session = parse_codex_session(path)

        self.assertIsNotNone(session)
        self.assertEqual(session.source, "codex")
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.project, "/repo")
        self.assertEqual([(turn.user, turn.assistant) for turn in session.turns], [("hello", "hi")])

    def test_claude_parser_omits_tool_blocks(self):
        with tempfile.TemporaryDirectory() as tempdir:
            path = Path(tempdir) / "session.jsonl"
            self._write_jsonl(
                path,
                [
                    {
                        "type": "user",
                        "sessionId": "s1",
                        "cwd": "/repo",
                        "timestamp": "t1",
                        "message": {"role": "user", "content": "hello"},
                    },
                    {
                        "type": "assistant",
                        "sessionId": "s1",
                        "cwd": "/repo",
                        "timestamp": "t2",
                        "message": {
                            "role": "assistant",
                            "stop_reason": "end_turn",
                            "content": [
                                {"type": "text", "text": "hi"},
                                {"type": "tool_use", "name": "bash", "input": {}},
                            ],
                        },
                    },
                ],
            )

            session = parse_claude_session(path)

        self.assertIsNotNone(session)
        self.assertEqual(session.source, "claude")
        self.assertEqual(session.session_id, "s1")
        self.assertEqual(session.project, "/repo")
        self.assertEqual(session.turns[0].assistant, "hi")

    def _write_jsonl(self, path: Path, rows: list[dict]) -> None:
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


class ReviewScannerTests(unittest.TestCase):
    def test_delivery_recovery_uses_uid_when_concurrent_submit_takes_display_id(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            scanner = ReviewScanner(
                ReviewConfig(memory_root=root, sources=[]),
                lambda _session_id, _message: REVIEW_NO_CANDIDATE,
            )
            receipt = ReviewDeliveryReceipt(
                batch_id="review-batch",
                candidate="review candidate",
                candidate_id=1,
                candidate_uid="a" * 32,
                reviewed_at="2026-07-21T00:00:00+00:00",
                sessions=(),
                reviewed_count=1,
                skipped_duplicate_count=0,
            )
            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                scanner.update_store.submit(
                    receipt.batch_id,
                    "concurrent candidate",
                    candidate_uid="b" * 32,
                )
                scanner.update_store.submit(
                    receipt.batch_id,
                    receipt.candidate,
                    candidate_uid=receipt.candidate_uid,
                )
                scanner._resume_delivery(receipt)
            state = scanner.update_store.read(receipt.batch_id)

        self.assertEqual([job.id for job in state.pending], [1, 2])
        self.assertEqual(
            [job.candidate_uid for job in state.pending],
            ["b" * 32, receipt.candidate_uid],
        )

    def test_delivery_recovery_submits_after_concurrent_display_id_was_processed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            scanner = ReviewScanner(
                ReviewConfig(memory_root=root, sources=[]),
                lambda _session_id, _message: REVIEW_NO_CANDIDATE,
            )
            receipt = ReviewDeliveryReceipt(
                batch_id="review-batch",
                candidate="review candidate",
                candidate_id=1,
                candidate_uid="a" * 32,
                reviewed_at="2026-07-21T00:00:00+00:00",
                sessions=(),
                reviewed_count=1,
                skipped_duplicate_count=0,
            )
            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                scanner.update_store.submit(
                    receipt.batch_id,
                    "concurrent candidate",
                    candidate_uid="b" * 32,
                )
            scanner.update_store.run_pending_batches(
                lambda _session_id, _message: "updated",
                target_batch_candidates=1,
                max_wait_seconds=0,
            )

            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                scanner._resume_delivery(receipt)
            state = scanner.update_store.read(receipt.batch_id)

        self.assertEqual([job.id for job in state.pending], [2])
        self.assertEqual(state.pending[0].candidate_uid, receipt.candidate_uid)
        self.assertEqual(
            state.accepted_candidate_uids,
            ["b" * 32, receipt.candidate_uid],
        )

    def test_scan_reviews_idle_session_and_updates_state(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1"), ("u2", "a2")])
            old = 1_000
            transcript.touch()
            now = old + 10_000
            self._set_mtime(transcript, old)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append((session_id, message)) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=now)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(len(calls), 1)
        only_state = next(iter(state.sessions.values()))
        self.assertEqual(only_state.session_id, "s1")
        self.assertEqual(only_state.source, "codex")

    def test_scan_submits_candidate_before_saving_review_state(self):
        submitted = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)

            def submit_candidate(session_id: str, candidate: str) -> None:
                self.assertEqual(ReviewStateStore(root).load().sessions, {})
                submitted.append((session_id, candidate))

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: "# Transcript review candidates\n\n- source: codex:s1",
                submit_candidate=submit_candidate,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(len(submitted), 1)
        self.assertTrue(submitted[0][0].startswith("review-batch-"))
        self.assertIn("codex:s1", submitted[0][1])
        self.assertEqual(len(state.sessions), 1)

    def test_scan_submission_failure_leaves_session_unreviewed(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)

            def fail_submission(session_id: str, candidate: str) -> None:
                raise RuntimeError("queue failed")

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: "candidate",
                submit_candidate=fail_submission,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(result.retried, 0)
        self.assertEqual(state.sessions, {})

    def test_scan_retries_worker_launch_without_duplicating_durable_candidate(self):
        reviewer_calls = []
        outputs = iter(("first candidate wording", "different retry wording"))

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append(session_id)
            return next(outputs)

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )

            with patch.object(
                scanner.update_store,
                "_start_worker_if_needed",
                side_effect=[OSError("no worker"), None],
            ) as start_worker:
                first = scanner.scan_once(now=10_000)
                delivery_path = next((root / ".runtime" / "review" / "deliveries").glob("*.json"))
                delivery = json.loads(delivery_path.read_text(encoding="utf-8"))
                second = scanner.scan_once(now=10_000)
            queued = next((root / ".runtime" / "async" / "update").glob("*.json"))
            queue_state = json.loads(queued.read_text(encoding="utf-8"))
            review_state = ReviewStateStore(root).load()

        self.assertEqual(first.failed, 1)
        self.assertEqual(first.reviewed, 0)
        self.assertEqual(second.reviewed, 1)
        self.assertEqual(start_worker.call_count, 2)
        self.assertEqual(len(reviewer_calls), 1)
        self.assertEqual([item["message"] for item in queue_state["pending"]], ["first candidate wording"])
        self.assertEqual(delivery["version"], 2)
        self.assertRegex(delivery["candidate_uid"], r"^[0-9a-f]{32}$")
        self.assertEqual(queue_state["pending"][0]["candidate_uid"], delivery["candidate_uid"])
        self.assertEqual(len(review_state.sessions), 1)

    def test_scan_recovers_receipt_when_submission_failed_before_queue_write(self):
        reviewer_calls = []

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append(session_id)
            return "candidate"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )
            original_submit = scanner.update_store.submit
            submit_calls = 0
            submitted_uids = []

            def flaky_submit(session_id: str, candidate: str, *, candidate_uid: str | None = None):
                nonlocal submit_calls
                submit_calls += 1
                submitted_uids.append(candidate_uid)
                if submit_calls == 1:
                    raise OSError("queue write failed")
                return original_submit(session_id, candidate, candidate_uid=candidate_uid)

            with (
                patch.object(scanner.update_store, "submit", side_effect=flaky_submit),
                patch.object(scanner.update_store, "_start_worker_if_needed"),
            ):
                first = scanner.scan_once(now=10_000)
                second = scanner.scan_once(now=10_000)
            update_state = scanner.update_store.read(reviewer_calls[0])
            review_state = ReviewStateStore(root).load()

        self.assertEqual(first.failed, 1)
        self.assertEqual(first.reviewed, 0)
        self.assertEqual(second.reviewed, 1)
        self.assertEqual(submit_calls, 2)
        self.assertEqual(len(set(submitted_uids)), 1)
        self.assertRegex(submitted_uids[0] or "", r"^[0-9a-f]{32}$")
        self.assertEqual(len(reviewer_calls), 1)
        self.assertEqual([job.message for job in update_state.pending], ["candidate"])
        self.assertEqual(update_state.pending[0].candidate_uid, submitted_uids[0])
        self.assertEqual(len(review_state.sessions), 1)

    def test_scan_recovers_delivery_after_state_save_failure_and_completed_update(self):
        reviewer_calls = []

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append(session_id)
            return "candidate"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )

            with (
                patch.object(scanner.update_store, "_start_worker_if_needed"),
                patch.object(scanner.state_store, "mark_reviewed", side_effect=OSError("state write failed")),
            ):
                with self.assertRaisesRegex(OSError, "state write failed"):
                    scanner.scan_once(now=10_000)

            worker = scanner.update_store.run_pending_batches(
                lambda session_id, message: "updated",
                target_batch_candidates=1,
                max_wait_seconds=0,
            )
            recovered = scanner.scan_once(now=10_000)
            update_state = scanner.update_store.read(reviewer_calls[0])
            review_state = ReviewStateStore(root).load()

        self.assertEqual(worker.status, "succeeded")
        self.assertEqual(worker.processed, 1)
        self.assertEqual(recovered.reviewed, 1)
        self.assertEqual(len(reviewer_calls), 1)
        self.assertEqual(update_state.status, "succeeded")
        self.assertEqual(update_state.next_id, 2)
        self.assertEqual(len(review_state.sessions), 1)

    def test_clearing_review_state_reruns_reviewer_for_changed_transcript(self):
        reviewer_calls = []

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append(session_id)
            return f"candidate {len(reviewer_calls)}"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )

            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                first = scanner.scan_once(now=10_000)
                delivery_root = root / ".runtime" / "review" / "deliveries"
                self.assertEqual(list(delivery_root.glob("*.json")), [])
                scanner.state_store.path.unlink()
                self._write_codex(
                    transcript,
                    turns=[("remember this", "understood"), ("also remember this", "noted")],
                )
                self._set_mtime(transcript, 2_000)
                second = scanner.scan_once(now=10_000)
            review_state = ReviewStateStore(root).load()

        self.assertEqual(first.reviewed, 1)
        self.assertEqual(second.reviewed, 1)
        self.assertEqual(len(reviewer_calls), 2)
        self.assertNotEqual(reviewer_calls[0], reviewer_calls[1])
        self.assertEqual(len(review_state.sessions), 1)

    def test_clearing_review_state_reruns_reviewer_despite_old_async_history(self):
        reviewer_calls = []

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append(session_id)
            return f"candidate {len(reviewer_calls)}"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("remember this", "understood")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )

            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                first = scanner.scan_once(now=10_000)
                scanner.state_store.path.unlink()
                second = scanner.scan_once(now=10_000)
            update_state = scanner.update_store.read(reviewer_calls[0])

        self.assertEqual(first.reviewed, 1)
        self.assertEqual(second.reviewed, 1)
        self.assertEqual(len(reviewer_calls), 2)
        self.assertEqual(reviewer_calls[0], reviewer_calls[1])
        self.assertEqual(update_state.next_id, 3)
        self.assertEqual([job.message for job in update_state.pending], ["candidate 1", "candidate 2"])

    def test_delivery_receipt_recovers_exact_batch_before_new_eligible_session(self):
        reviewer_calls = []

        def review(session_id: str, message: str) -> str:
            reviewer_calls.append((session_id, message))
            return "candidate"

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first_transcript = source / "01-first.jsonl"
            second_transcript = source / "02-second.jsonl"
            self._write_codex(first_transcript, turns=[("first", "a1")], session_id="s1")
            self._write_codex(second_transcript, turns=[("second", "a2")], session_id="s2")
            self._set_mtime(first_transcript, 1_000)
            self._set_mtime(second_transcript, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    batch_size=2,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                review,
            )

            with patch.object(scanner.update_store, "_start_worker_if_needed"):
                with patch.object(scanner.state_store, "mark_reviewed", side_effect=OSError("state write failed")):
                    with self.assertRaisesRegex(OSError, "state write failed"):
                        scanner.scan_once(now=10_000)

                earlier_transcript = source / "00-earlier.jsonl"
                self._write_codex(earlier_transcript, turns=[("earlier", "a0")], session_id="s0")
                self._set_mtime(earlier_transcript, 500)
                recovered = scanner.scan_once(now=10_000)
                recovered_state = ReviewStateStore(root).load()
                remaining = scanner.scan_once(now=10_000)
                final_state = ReviewStateStore(root).load()

        self.assertEqual(recovered.reviewed, 2)
        self.assertEqual(len(reviewer_calls), 2)
        self.assertEqual(set(recovered_state.sessions), {"codex:s1", "codex:s2"})
        self.assertEqual(remaining.reviewed, 1)
        self.assertEqual(set(final_state.sessions), {"codex:s0", "codex:s1", "codex:s2"})

    def test_scan_full_batch_gate_waits_before_reviewing(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            self._write_codex(first, turns=[("first", "a1")], session_id="s1")
            self._write_codex(second, turns=[("second", "a2")], session_id="s2")
            self._set_mtime(first, 1_000)
            self._set_mtime(second, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000, require_full_batch=True)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.waiting_for_batch, 2)
        self.assertEqual(calls, [])
        self.assertEqual(state.sessions, {})

    def test_scan_reviews_time_adjacent_batch_per_call(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "z-first.jsonl"
            second = source / "a-second.jsonl"
            third = source / "m-third.jsonl"
            fourth = source / "b-fourth.jsonl"
            self._write_codex(first, turns=[("first", "a1")], session_id="s1")
            self._write_codex(second, turns=[("second", "a2")], session_id="s2")
            self._write_codex(third, turns=[("third", "a3")], session_id="s3")
            self._write_codex(fourth, turns=[("fourth", "a4")], session_id="s4")
            self._set_mtime(second, 1_000)
            self._set_mtime(first, 2_000)
            self._set_mtime(fourth, 3_000)
            self._set_mtime(third, 4_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            first_result = scanner.scan_once(now=10_000)
            second_result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(first_result.reviewed, 3)
        self.assertEqual(second_result.reviewed, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(state.sessions), 4)

    def test_scan_respects_configured_batch_size(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            self._write_codex(first, turns=[("first", "a1")], session_id="s1")
            self._write_codex(second, turns=[("second", "a2")], session_id="s2")
            self._set_mtime(first, 1_000)
            self._set_mtime(second, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    batch_size=1,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(state.sessions), 1)

    def test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("codex:long", state.sessions)
        self.assertIn("codex:short", state.sessions)

    def test_scan_does_not_mark_duplicate_alias_when_reviewer_fails(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)

            def fail(session_id: str, message: str) -> str:
                calls.append(message)
                raise RuntimeError("review failed")

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                fail,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(result.failed, 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(state.sessions, {})

    def test_scan_result_format_includes_skipped_duplicate(self):
        result = ReviewScanResult(reviewed=1, skipped_duplicate=2)

        formatted = result.format()

        self.assertIn("reviewed: 1", formatted)
        self.assertIn("skipped_duplicate: 2", formatted)

    def test_scan_skips_reviewed_session_even_when_file_changes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1")])
            self._set_mtime(transcript, 1_000)
            config = ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            )
            scanner = ReviewScanner(config, lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE)
            scanner.scan_once(now=10_000)

            self._write_codex(transcript, turns=[("u1", "a1"), ("u2", "a2")])
            self._set_mtime(transcript, 20_000)
            result = scanner.scan_once(now=30_000)

        self.assertEqual(result.skipped_reviewed, 1)
        self.assertEqual(len(calls), 1)

    def test_scan_retries_once_then_stops_after_reviewer_failure(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            failed_transcript = source / "01-fail.jsonl"
            reviewed_transcript = source / "02-review.jsonl"
            self._write_codex(failed_transcript, turns=[("fail", "a1")], session_id="s1")
            self._write_codex(reviewed_transcript, turns=[("review", "a2")], session_id="s2")
            self._set_mtime(failed_transcript, 1_000)
            self._set_mtime(reviewed_transcript, 1_000)

            def fail(session_id: str, message: str) -> str:
                calls.append(message)
                raise RuntimeError("review failed")

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                fail,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.retried, 1)
        self.assertEqual(result.reviewed, 0)
        self.assertEqual(len(calls), 2)
        self.assertEqual(len(state.sessions), 0)

    def test_scan_skips_sessions_older_than_since_days(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "old.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1")])
            now = 40 * 24 * 60 * 60
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    since_days=30,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=now)

        self.assertEqual(result.skipped_old, 1)
        self.assertEqual(calls, [])

    def test_scan_skips_duplicate_session_id_from_different_file(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            self._write_codex(first, turns=[("first", "a1")], session_id="s1")
            self._write_codex(second, turns=[("second", "a2")], session_id="s1")
            self._set_mtime(first, 1_000)
            self._set_mtime(second, 1_000)
            config = ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            )
            scanner = ReviewScanner(config, lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE)
            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_reviewed, 1)
        self.assertEqual(len(calls), 1)

    def test_scan_allows_mixed_provider_batch(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_source = root / "codex"
            claude_root = root / "claude"
            claude_project = claude_root / "-repo"
            codex_source.mkdir()
            claude_project.mkdir(parents=True)
            codex_transcript = codex_source / "codex.jsonl"
            claude_transcript = claude_project / "claude.jsonl"
            self._write_codex(codex_transcript, turns=[("codex user", "a1")], session_id="s1")
            self._write_claude(claude_transcript, turns=[("claude user", "a2")], session_id="c1")
            self._set_mtime(codex_transcript, 1_000)
            self._set_mtime(claude_transcript, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[
                        ReviewSourceConfig(kind="codex", path=codex_source),
                        ReviewSourceConfig(kind="claude", path=claude_root),
                    ],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 2)
        self.assertEqual(len(calls), 1)

    def test_scan_prefix_dedupe_is_provider_local(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            codex_source = root / "codex"
            claude_root = root / "claude"
            claude_project = claude_root / "-repo"
            codex_source.mkdir()
            claude_project.mkdir(parents=True)
            codex_transcript = codex_source / "codex.jsonl"
            claude_transcript = claude_project / "claude.jsonl"
            self._write_codex(codex_transcript, turns=[("shared", "answer")], session_id="codex-short")
            self._write_claude(
                claude_transcript,
                turns=[("shared", "answer"), ("claude extra", "answer")],
                session_id="claude-long",
            )
            self._set_mtime(codex_transcript, 1_000)
            self._set_mtime(claude_transcript, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[
                        ReviewSourceConfig(kind="codex", path=codex_source),
                        ReviewSourceConfig(kind="claude", path=claude_root),
                    ],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 2)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(len(calls), 1)

    def test_scan_full_batch_gate_uses_representative_count_after_dedupe(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            short = source / "01-short.jsonl"
            long = source / "02-long.jsonl"
            self._write_codex(short, turns=[("u1", "a1")], session_id="short")
            self._write_codex(long, turns=[("u1", "a1"), ("u2", "a2")], session_id="long")
            self._set_mtime(short, 1_000)
            self._set_mtime(long, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    batch_size=2,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000, require_full_batch=True)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 0)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(result.waiting_for_batch, 1)
        self.assertEqual(calls, [])
        self.assertEqual(state.sessions, {})

    def test_scan_skips_session_from_existing_state_entry(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1")], session_id="s1")
            self._set_mtime(transcript, 1_000)
            state_path = root / ".runtime" / "review" / "state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(
                json.dumps(
                    {
                        "sessions": {
                            "codex:/old/path/session.jsonl": {
                                "session_id": "s1",
                                "source": "codex",
                                "last_reviewed_at": "2026-05-17T00:00:00+00:00",
                            }
                        }
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.skipped_reviewed, 1)
        self.assertEqual(calls, [])

    def test_scan_skips_internal_provider_session(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            ProviderSessionStore(root, "retrieve").save(
                ProviderSessionRecord(
                    provider="codex",
                    provider_session_id="s1",
                    role="retrieve",
                    rightmemory_session_id="agent-1",
                    created_at="2026-05-18T00:00:00+00:00",
                    updated_at="2026-05-18T00:00:00+00:00",
                )
            )
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("internal", "work")], session_id="s1")
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.skipped_internal, 1)
        self.assertEqual(result.reviewed, 0)
        self.assertEqual(calls, [])
        self.assertEqual(state.sessions, {})

    def test_scan_skips_active_session(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1")])
            self._set_mtime(transcript, 9_900)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or REVIEW_NO_CANDIDATE,
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.skipped_idle, 1)
        self.assertEqual(calls, [])

    def _write_codex(self, path: Path, turns: list[tuple[str, str]], session_id: str = "s1") -> None:
        rows = [{"type": "session_meta", "timestamp": "t0", "payload": {"id": session_id, "cwd": "/repo"}}]
        for index, (user, assistant) in enumerate(turns, start=1):
            rows.extend(
                [
                    {
                        "type": "event_msg",
                        "timestamp": f"t{index}.1",
                        "payload": {"type": "user_message", "message": user},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": f"t{index}.2",
                        "payload": {"type": "agent_message", "message": assistant},
                    },
                    {
                        "type": "event_msg",
                        "timestamp": f"t{index}.3",
                        "payload": {"type": "task_complete"},
                    },
                ]
            )
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def _write_claude(self, path: Path, turns: list[tuple[str, str]], session_id: str = "c1") -> None:
        rows = []
        for index, (user, assistant) in enumerate(turns, start=1):
            rows.extend(
                [
                    {
                        "type": "user",
                        "sessionId": session_id,
                        "cwd": "/repo",
                        "timestamp": f"t{index}.1",
                        "message": {"role": "user", "content": user},
                    },
                    {
                        "type": "assistant",
                        "sessionId": session_id,
                        "cwd": "/repo",
                        "timestamp": f"t{index}.2",
                        "message": {"role": "assistant", "stop_reason": "end_turn", "content": assistant},
                    },
                ]
            )
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")

    def _set_mtime(self, path: Path, mtime: float) -> None:
        import os

        os.utime(path, (mtime, mtime))


if __name__ == "__main__":
    unittest.main()

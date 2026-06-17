import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.config import ReviewConfig, ReviewSourceConfig
from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore
from rightmemory.review import ReviewScanResult, ReviewScanner, ReviewStateStore
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
                lambda session_id, message: calls.append((session_id, message)) or "ok",
            )

            result = scanner.scan_once(now=now)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn("Normalized transcript batch JSON", calls[0][1])
        self.assertIn('"sessions"', calls[0][1])
        self.assertIn('"batch_id"', calls[0][1])
        self.assertNotIn("already_reviewed_turns", calls[0][1])
        self.assertNotIn('"i"', calls[0][1])
        self.assertIn('"user": "u2"', calls[0][1])
        only_state = next(iter(state.sessions.values()))
        self.assertEqual(only_state.session_id, "s1")
        self.assertEqual(only_state.source, "codex")

    def test_scan_success_callback_runs_after_state_save_with_session_count(self):
        callback_calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1")])
            self._set_mtime(transcript, 1_000)

            def on_review_success(count: int) -> None:
                saved = ReviewStateStore(root).load()
                callback_calls.append((count, len(saved.sessions)))

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: "ok",
                on_review_success=on_review_success,
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(callback_calls, [(1, 1)])

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
                lambda session_id, message: calls.append(message) or "ok",
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
                lambda session_id, message: calls.append(message) or "ok",
            )

            first_result = scanner.scan_once(now=10_000)
            second_result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(first_result.reviewed, 3)
        self.assertEqual(second_result.reviewed, 1)
        self.assertEqual(len(calls), 2)
        first_message = calls[0]
        self.assertIn('"user": "second"', first_message)
        self.assertIn('"user": "first"', first_message)
        self.assertIn('"user": "fourth"', first_message)
        self.assertNotIn('"user": "third"', first_message)
        self.assertLess(first_message.index('"user": "second"'), first_message.index('"user": "first"'))
        self.assertLess(first_message.index('"user": "first"'), first_message.index('"user": "fourth"'))
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
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"user": "first"', calls[0])
        self.assertNotIn('"user": "second"', calls[0])
        self.assertEqual(len(state.sessions), 1)

    def test_scan_reviews_longest_prefix_duplicate_and_marks_alias_reviewed(self):
        calls = []
        callback_calls = []
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

            def on_review_success(count: int) -> None:
                saved = ReviewStateStore(root).load()
                callback_calls.append((count, len(saved.sessions)))

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
                on_review_success=on_review_success,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "long"', calls[0])
        self.assertNotIn('"session_id": "short"', calls[0])
        self.assertIn('"user": "u2"', calls[0])
        self.assertIn("codex:long", state.sessions)
        self.assertIn("codex:short", state.sessions)
        self.assertEqual(callback_calls, [(1, 2)])

    def test_scan_exact_duplicate_keeps_newest_representative(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            older = source / "01-older.jsonl"
            newer = source / "02-newer.jsonl"
            turns = [("u1", "a1"), ("u2", "a2")]
            self._write_codex(older, turns=turns, session_id="older")
            self._write_codex(newer, turns=turns, session_id="newer")
            self._set_mtime(older, 1_000)
            self._set_mtime(newer, 2_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "newer"', calls[0])
        self.assertNotIn('"session_id": "older"', calls[0])
        self.assertIn("codex:older", state.sessions)
        self.assertIn("codex:newer", state.sessions)

    def test_scan_exact_duplicate_same_mtime_uses_path_tiebreaker(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            turns = [("u1", "a1")]
            self._write_codex(first, turns=turns, session_id="first")
            self._write_codex(second, turns=turns, session_id="second")
            self._set_mtime(first, 1_000)
            self._set_mtime(second, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_duplicate, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"session_id": "first"', calls[0])
        self.assertNotIn('"session_id": "second"', calls[0])

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
        self.assertIn('"session_id": "long"', calls[0])
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
            scanner = ReviewScanner(config, lambda session_id, message: calls.append(message) or "ok")
            scanner.scan_once(now=10_000)

            self._write_codex(transcript, turns=[("u1", "a1"), ("u2", "a2")])
            self._set_mtime(transcript, 20_000)
            result = scanner.scan_once(now=30_000)

        self.assertEqual(result.skipped_reviewed, 1)
        self.assertEqual(len(calls), 1)

    def test_scan_retries_once_then_stops_after_reviewer_failure(self):
        calls = []
        callbacks = []
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
                if '"user": "fail"' in message:
                    raise RuntimeError("review failed")
                return "ok"

            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                fail,
                on_review_success=callbacks.append,
            )

            result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(result.failed, 1)
        self.assertEqual(result.retried, 1)
        self.assertEqual(result.reviewed, 0)
        self.assertEqual(len(calls), 2)
        self.assertIn('"user": "fail"', calls[0])
        self.assertIn('"user": "review"', calls[0])
        self.assertEqual(len(state.sessions), 0)
        self.assertEqual(callbacks, [])

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
                lambda session_id, message: calls.append(message) or "ok",
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
            scanner = ReviewScanner(config, lambda session_id, message: calls.append(message) or "ok")
            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 1)
        self.assertEqual(result.skipped_reviewed, 1)
        self.assertEqual(len(calls), 1)
        self.assertIn('"user": "first"', calls[0])

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
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 2)
        self.assertEqual(len(calls), 1)
        self.assertIn('"source": "codex"', calls[0])
        self.assertIn('"source": "claude"', calls[0])
        self.assertIn('"session_id": "s1"', calls[0])
        self.assertIn('"session_id": "c1"', calls[0])

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
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=10_000)

        self.assertEqual(result.reviewed, 2)
        self.assertEqual(result.skipped_duplicate, 0)
        self.assertEqual(len(calls), 1)
        self.assertIn('"source": "codex"', calls[0])
        self.assertIn('"source": "claude"', calls[0])

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
                lambda session_id, message: calls.append(message) or "ok",
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
                lambda session_id, message: calls.append(message) or "ok",
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
                lambda session_id, message: calls.append(message) or "ok",
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
                lambda session_id, message: calls.append(message) or "ok",
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

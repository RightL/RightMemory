import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.config import ReviewConfig, ReviewSourceConfig
from rightmemory.review import ReviewScanner, ReviewStateStore
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
        self.assertEqual([(turn.i, turn.user, turn.assistant) for turn in session.turns], [(1, "hello", "hi")])

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
        self.assertIn('"already_reviewed_turns": 0', calls[0][1])
        self.assertIn('"user": "u2"', calls[0][1])
        only_state = next(iter(state.sessions.values()))
        self.assertEqual(only_state.last_reviewed_turn, 2)

    def test_scan_reviews_one_eligible_session_per_call(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            first = source / "01-first.jsonl"
            second = source / "02-second.jsonl"
            self._write_codex(first, turns=[("first", "a1")])
            self._write_codex(second, turns=[("second", "a2")])
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

            first_result = scanner.scan_once(now=10_000)
            second_result = scanner.scan_once(now=10_000)
            state = ReviewStateStore(root).load()

        self.assertEqual(first_result.reviewed, 1)
        self.assertEqual(second_result.reviewed, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn('"user": "first"', calls[0])
        self.assertIn('"user": "second"', calls[1])
        self.assertEqual(len(state.sessions), 2)

    def test_scan_uses_whole_session_but_marks_already_reviewed_turns(self):
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
            scanner.scan_once(now=30_000)

        self.assertEqual(len(calls), 2)
        self.assertIn('"already_reviewed_turns": 1', calls[1])
        self.assertIn('"user": "u1"', calls[1])
        self.assertIn('"user": "u2"', calls[1])

    def test_scan_retries_once_then_stops_after_reviewer_failure(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            failed_transcript = source / "01-fail.jsonl"
            reviewed_transcript = source / "02-review.jsonl"
            self._write_codex(failed_transcript, turns=[("fail", "a1")])
            self._write_codex(reviewed_transcript, turns=[("review", "a2")])
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
                lambda session_id, message: calls.append(message) or "ok",
            )

            result = scanner.scan_once(now=now)

        self.assertEqual(result.skipped_old, 1)
        self.assertEqual(calls, [])

    def test_scan_resets_cursor_when_reviewed_prefix_changes(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("u1", "a1"), ("u2", "a2")])
            self._set_mtime(transcript, 1_000)
            config = ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            )
            scanner = ReviewScanner(config, lambda session_id, message: calls.append(message) or "ok")
            scanner.scan_once(now=10_000)

            self._write_codex(transcript, turns=[("u1", "changed"), ("u2", "a2"), ("u3", "a3")])
            self._set_mtime(transcript, 20_000)
            result = scanner.scan_once(now=30_000)

        self.assertEqual(result.reset_changed, 1)
        self.assertEqual(len(calls), 2)
        self.assertIn('"already_reviewed_turns": 0', calls[1])
        self.assertIn('"assistant": "changed"', calls[1])

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

    def _write_codex(self, path: Path, turns: list[tuple[str, str]]) -> None:
        rows = [{"type": "session_meta", "timestamp": "t0", "payload": {"id": "s1", "cwd": "/repo"}}]
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

    def _set_mtime(self, path: Path, mtime: float) -> None:
        import os

        os.utime(path, (mtime, mtime))


if __name__ == "__main__":
    unittest.main()

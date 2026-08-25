from __future__ import annotations

import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
from types import SimpleNamespace

from rightmemory.pursuit_tasks import (
    apply_reconciliation,
    link_task,
    list_tasks,
    plan_task,
    propose_reconciliation,
    run_task,
)
from rightmemory.pursuit_workspace import (
    PursuitEditor,
    PursuitRevisionConflict,
    apply_operations,
    preview_operations,
    redo,
    undo,
)


class FakeCodexRunner:
    def __init__(self):
        self.calls = []
        self.closed = False

    def run_turn(self, **kwargs):
        self.calls.append(kwargs)
        callback = kwargs.get("on_thread_started")
        if callback:
            callback("thread-created")
        return SimpleNamespace(provider_session_id="thread-created", text="Implemented and verified the task.")

    def close(self):
        self.closed = True


class _PursuitFixture:
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Memory\n\n## Retrieval Context {#retrieval-context}\n\nUse deterministic graph context.\n",
            encoding="utf-8",
        )
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n"
            "## Focus\n\n"
            "- `rightmemory`\n\n"
            "## RightMemory Product {#rightmemory} → [rel:retrieval-context]\n\n"
            "Make RightMemory a coherent long-term agent memory system.\n\n"
            "**State:** The core graph is stable.\n\n"
            "### Faster Retrieval {#retrieval-speed}\n\n"
            "Reduce retrieval latency without losing relevant context.\n\n"
            "**Next:**\n"
            "- `do` Benchmark the current selector.\n",
            encoding="utf-8",
        )


class PursuitWorkspaceTests(_PursuitFixture, unittest.TestCase):
    def test_snapshot_exposes_tree_and_fields(self):
        snapshot = PursuitEditor(self.root).snapshot()
        by_id = {node["id"]: node for node in snapshot["nodes"]}

        self.assertEqual(snapshot["roots"], ["rightmemory"])
        self.assertEqual(snapshot["focus_ids"], ["rightmemory"])
        self.assertEqual(by_id["retrieval-speed"]["parent_id"], "rightmemory")
        self.assertEqual(by_id["retrieval-speed"]["next"][0]["kind"], "do")
        self.assertEqual(by_id["rightmemory"]["edges"], [{"type": "rel", "target": "retrieval-context"}])

    def test_preview_apply_move_and_unicode_round_trip(self):
        revision = PursuitEditor(self.root).revision()
        operations = [
            {
                "op": "create",
                "id": "pursuit-map",
                "title": "可编辑 Pursuit Map",
                "parent_id": "rightmemory",
                "objective": "用树状思维导图管理长期意图。",
                "next": ["do: 完成结构化编辑器"],
            },
            {
                "op": "update",
                "id": "retrieval-speed",
                "state": "基准已经准备好。",
                "next": [{"kind": "do", "text": "比较延迟与召回率"}],
            },
            {"op": "set_focus", "ids": ["pursuit-map", "retrieval-speed"]},
            {"op": "move", "id": "retrieval-speed", "parent_id": "pursuit-map", "index": 0},
        ]

        preview = preview_operations(self.root, operations, expected_revision=revision)
        self.assertIn("可编辑 Pursuit Map", preview.diff)
        self.assertEqual(preview.snapshot["focus_ids"], ["pursuit-map", "retrieval-speed"])

        result = apply_operations(self.root, operations, expected_revision=revision)
        by_id = {node["id"]: node for node in result.snapshot["nodes"]}
        self.assertEqual(by_id["retrieval-speed"]["parent_id"], "pursuit-map")
        self.assertIn("基准已经准备好", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_split_and_inline_f_backing(self):
        apply_operations(self.root, [{"op": "split_file", "id": "rightmemory"}])
        backing = self.root / "PURSUIT_rightmemory.md"
        self.assertTrue(backing.is_file())
        self.assertIn("{F#rightmemory}", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))
        self.assertIn("retrieval-speed", backing.read_text(encoding="utf-8"))

        apply_operations(self.root, [{"op": "inline_file", "id": "rightmemory"}])
        self.assertFalse(backing.exists())
        text = (self.root / "PURSUITS.md").read_text(encoding="utf-8")
        self.assertIn("{#rightmemory}", text)
        self.assertIn("retrieval-speed", text)

    def test_structural_text_injection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "must not introduce Markdown headings"):
            preview_operations(
                self.root,
                [
                    {
                        "op": "update",
                        "id": "retrieval-speed",
                        "state": "Valid state.\n\n## Injected {#injected}",
                    }
                ],
            )

    def test_crlf_style_is_preserved(self):
        text = (self.root / "PURSUITS.md").read_text(encoding="utf-8")
        with (self.root / "PURSUITS.md").open("w", encoding="utf-8", newline="") as handle:
            handle.write(text.replace("\n", "\r\n"))
        apply_operations(self.root, [{"op": "park", "id": "retrieval-speed"}])
        data = (self.root / "PURSUITS.md").read_bytes()
        self.assertIn(b"\r\n", data)
        self.assertNotIn(b"\n", data.replace(b"\r\n", b""))

    def test_stale_revision_is_rejected(self):
        revision = PursuitEditor(self.root).revision()
        apply_operations(self.root, [{"op": "park", "id": "retrieval-speed"}])
        with self.assertRaises(PursuitRevisionConflict):
            apply_operations(
                self.root,
                [{"op": "unpark", "id": "retrieval-speed"}],
                expected_revision=revision,
            )

    def test_undo_and_redo_restore_exact_state(self):
        original = (self.root / "PURSUITS.md").read_text(encoding="utf-8")
        apply_operations(self.root, [{"op": "park", "id": "retrieval-speed"}])
        parked = (self.root / "PURSUITS.md").read_text(encoding="utf-8")
        self.assertIn("**Status:** parked", parked)

        undo(self.root)
        self.assertEqual((self.root / "PURSUITS.md").read_text(encoding="utf-8"), original)
        redo(self.root)
        self.assertEqual((self.root / "PURSUITS.md").read_text(encoding="utf-8"), parked)


class PursuitTaskTests(_PursuitFixture, unittest.TestCase):
    def test_link_is_idempotent_and_plan_avoids_duplicate_live_task(self):
        first = link_task(
            self.root,
            pursuit_ids=["retrieval-speed"],
            provider="codex",
            thread_id="thread-1",
            title="Benchmark retrieval",
            project=str(self.root),
        )
        second = link_task(
            self.root,
            pursuit_ids=["rightmemory"],
            provider="codex",
            thread_id="thread-1",
            title="Benchmark retrieval",
            project=str(self.root),
        )
        self.assertEqual(first.task_id, second.task_id)
        self.assertEqual(set(second.pursuit_ids), {"retrieval-speed", "rightmemory"})

        planned = plan_task(
            self.root,
            pursuit_id="retrieval-speed",
            action="Benchmark selector latency and recall",
            project=str(self.root),
        )
        duplicate = plan_task(
            self.root,
            pursuit_id="retrieval-speed",
            action="Benchmark selector latency and recall",
            project=str(self.root),
        )
        self.assertEqual(planned.task_id, duplicate.task_id)
        self.assertIn("Relevant durable Memory", planned.prompt)
        self.assertIn("retrieval-context", planned.prompt)

    def test_linked_pursuit_cannot_be_deleted(self):
        link_task(
            self.root,
            pursuit_ids=["retrieval-speed"],
            provider="codex",
            thread_id="thread-delete",
            title="Linked task",
        )
        with self.assertRaisesRegex(ValueError, "unlink Pursuit"):
            preview_operations(
                self.root,
                [{"op": "delete", "id": "retrieval-speed"}],
            )

    def test_run_records_real_thread_and_result(self):
        task = plan_task(
            self.root,
            pursuit_id="retrieval-speed",
            action="Implement the benchmark",
            project=str(self.root),
        )
        runner = FakeCodexRunner()
        completed = run_task(self.root, task.task_id, runner=runner)

        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.thread_id, "thread-created")
        self.assertIn("Implemented and verified", completed.result)
        self.assertEqual(runner.calls[0]["cwd"], self.root.resolve())
        with self.assertRaisesRegex(ValueError, "only a planned task"):
            run_task(self.root, task.task_id, runner=runner)

    def test_reconciliation_rolls_back_if_registry_write_fails(self):
        task = link_task(
            self.root,
            pursuit_ids=["retrieval-speed"],
            provider="codex",
            thread_id="thread-rollback",
            title="Benchmark retrieval",
            status="completed",
        )
        before = (self.root / "PURSUITS.md").read_bytes()
        reconciliation = propose_reconciliation(
            self.root,
            task_id=task.task_id,
            summary="Update state.",
            operations=[{"op": "park", "id": "retrieval-speed"}],
        )
        original_save = __import__("rightmemory.pursuit_tasks", fromlist=["_save_registry"])._save_registry
        calls = 0

        def fail_after_pursuit(root, registry):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise OSError("simulated registry failure")
            return original_save(root, registry)

        with patch("rightmemory.pursuit_tasks._save_registry", side_effect=fail_after_pursuit):
            with self.assertRaisesRegex(OSError, "simulated registry failure"):
                apply_reconciliation(self.root, reconciliation.reconciliation_id)
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), before)

    def test_reconciliation_is_revision_bound_and_applies(self):
        task = link_task(
            self.root,
            pursuit_ids=["retrieval-speed"],
            provider="codex",
            thread_id="thread-2",
            title="Benchmark retrieval",
            status="completed",
        )
        revision = PursuitEditor(self.root).revision()
        reconciliation = propose_reconciliation(
            self.root,
            task_id=task.task_id,
            summary="The benchmark settled the next production step.",
            expected_revision=revision,
            operations=[
                {
                    "op": "update",
                    "id": "retrieval-speed",
                    "state": "The benchmark passed the target recall gate.",
                    "next": ["do: Integrate the selector"],
                }
            ],
        )

        outcome = apply_reconciliation(self.root, reconciliation.reconciliation_id)
        self.assertEqual(outcome["reconciliation"]["status"], "applied")
        node = next(node for node in PursuitEditor(self.root).snapshot()["nodes"] if node["id"] == "retrieval-speed")
        self.assertIn("passed", node["state"])
        self.assertEqual(list_tasks(self.root, "retrieval-speed")[0].task_id, task.task_id)


if __name__ == "__main__":
    unittest.main()

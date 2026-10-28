import unittest
from pathlib import Path

from rightmemory.isolated_write import IsolatedWriteSupervisor
from rightmemory.semantic_operation import SemanticOperationStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class IsolatedWriteUpdateFinalizationTests(IsolatedWriteTestBase):
    def test_external_finalizer_prepares_without_landing_or_generic_recovery(self):
        calls = []

        def callback(worktree: Path) -> str:
            calls.append("model")
            self._append_memory(worktree, "- `two` externally fenced → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: externally fenced", cwd=worktree)
            return "prepared"

        supervisor = IsolatedWriteSupervisor(self.root, "update")
        prepared = supervisor.run(
            callback,
            operation_id="update-external-1",
            operation_input={"message": "remember"},
            external_finalizer="curation",
        )
        supervisor.recover_prepared()
        resumed = supervisor.run(
            lambda _worktree: self.fail("prepared external work must not rerun"),
            operation_id="update-external-1",
            operation_input={"message": "remember"},
            external_finalizer="curation",
        )

        self.assertTrue(prepared.prepared)
        self.assertEqual(prepared.commits_landed, 0)
        self.assertEqual(resumed.candidate_commit, prepared.candidate_commit)
        self.assertEqual(calls, ["model"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_external_finalizer_completes_only_after_published_commit_is_active(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` externally published → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: externally published", cwd=worktree)
            return "published"

        supervisor = IsolatedWriteSupervisor(self.root, "update")
        prepared = supervisor.run(
            callback,
            operation_id="update-external-2",
            operation_input={"message": "remember"},
            external_finalizer="curation",
        )
        self._git("merge", "--ff-only", prepared.candidate_commit)

        completed = supervisor.complete_external(
            "update-external-2",
            external_finalizer="curation",
            landed_commit=prepared.candidate_commit,
        )

        self.assertEqual(completed.commits_landed, 1)
        self.assertFalse(completed.prepared)
        self.assertIn("externally published", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_external_finalizer_can_restart_a_stale_prepared_result(self):
        supervisor = IsolatedWriteSupervisor(self.root, "update")

        def first(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` stale result → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: stale result", cwd=worktree)
            return "stale"

        supervisor.run(
            first,
            operation_id="update-external-3",
            operation_input={"message": "remember"},
            external_finalizer="curation",
        )
        supervisor.restart_external(
            "update-external-3",
            external_finalizer="curation",
            reason="semantic base changed",
        )

        second = supervisor.run(
            lambda _worktree: "fresh no-change",
            operation_id="update-external-3",
            operation_input={"message": "remember"},
            external_finalizer="curation",
        )

        self.assertTrue(second.prepared)
        self.assertIsNone(second.candidate_commit)
        self.assertEqual(second.output, "fresh no-change")

    def test_external_lease_token_can_change_without_changing_operation_identity(self):
        supervisor = IsolatedWriteSupervisor(self.root, "update")
        supervisor.run(
            lambda _worktree: "first lease",
            operation_id="update-external-token",
            operation_input={"message": "remember"},
            external_finalizer="curation:" + "1" * 32,
        )
        supervisor.restart_external(
            "update-external-token",
            external_finalizer="curation:" + "1" * 32,
            reason="lease changed",
        )

        prepared = supervisor.run(
            lambda _worktree: "second lease",
            operation_id="update-external-token",
            operation_input={"message": "remember"},
            external_finalizer="curation:" + "2" * 32,
        )

        self.assertEqual(prepared.output, "second lease")
        self.assertEqual(
            SemanticOperationStore(self.root)
            .read("update-external-token")
            .outcome.metadata["external_finalizer"],
            "curation:" + "2" * 32,
        )

    def test_external_result_can_be_superseded_by_another_lease_owner(self):
        supervisor = IsolatedWriteSupervisor(self.root, "update")
        finalizer = "curation:" + "1" * 32
        prepared = supervisor.run(
            lambda _worktree: "stale local result",
            operation_id="update-external-superseded",
            operation_input={"message": "remember"},
            external_finalizer=finalizer,
        )

        supervisor.supersede_external(
            "update-external-superseded",
            external_finalizer=finalizer,
            landed_commit=prepared.start_commit,
        )

        record = SemanticOperationStore(self.root).read("update-external-superseded")
        self.assertEqual(record.phase, "no_change")
        self.assertEqual(record.effects, ())
        self.assertTrue(record.outcome.metadata["superseded"])

    def test_update_rejects_cross_domain_transaction_without_partial_landing(self):
        self._add_fixed_agent_correction_collections()
        before_head = self._git("rev-parse", "HEAD")
        before = {
            name: (self.root / name).read_bytes()
            for name in ("MEMORY.md", "MEMORY_agent-corrections-writing.md")
        }

        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `durable` durable result → []\n")
            correction = worktree / "MEMORY_agent-corrections-writing.md"
            correction.write_text(
                correction.read_text(encoding="utf-8")
                + "\n### Keep the useful context\n\nPreserve the durable result.\n",
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", correction.name, cwd=worktree)
            self._git("commit", "-m", "rightmemory: unified update", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)
        self.assertEqual(self._git("rev-parse", "HEAD"), before_head)
        self.assertEqual({name: (self.root / name).read_bytes() for name in before}, before)

    def test_sync_reconciler_preserves_structured_corrections_over_updater_ceiling(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                self._corrections_markdown(11),
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: preserve correction union", cwd=worktree)
            return "repaired"

        result = IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

        self.assertEqual(result.commits_landed, 1)
        text = (self.root / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Entry "), 11)

    def test_correction_overflow_does_not_block_unrelated_normal_update(self):
        (self.root / "corrections.md").write_text(
            self._corrections_markdown(11),
            encoding="utf-8",
        )
        self._git("add", "corrections.md")
        self._git("commit", "-m", "sync: preserve unresolved correction union")

        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `later` unrelated durable state → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: unrelated update", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertIn("later", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "corrections.md").read_text(encoding="utf-8").count("## Entry "),
            11,
        )

if __name__ == "__main__":
    unittest.main()

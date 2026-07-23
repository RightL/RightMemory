import unittest
from pathlib import Path

from rightmemory.isolated_write import IsolatedWriteSupervisor
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.update_corrector import UpdateCorrectionResult
from rightmemory.update_review import UpdateReviewStore, verify_update_review
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
            external_finalizer="update-queue",
        )
        supervisor.recover_prepared()
        resumed = supervisor.run(
            lambda _worktree: self.fail("prepared external work must not rerun"),
            operation_id="update-external-1",
            operation_input={"message": "remember"},
            external_finalizer="update-queue",
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
            external_finalizer="update-queue",
        )
        self._git("merge", "--ff-only", prepared.candidate_commit)

        completed = supervisor.complete_external(
            "update-external-2",
            external_finalizer="update-queue",
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
            external_finalizer="update-queue",
        )
        supervisor.restart_external(
            "update-external-3",
            external_finalizer="update-queue",
            reason="semantic base changed",
        )

        second = supervisor.run(
            lambda _worktree: "fresh no-change",
            operation_id="update-external-3",
            operation_input={"message": "remember"},
            external_finalizer="update-queue",
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
            external_finalizer="update-queue:" + "1" * 32,
        )
        supervisor.restart_external(
            "update-external-token",
            external_finalizer="update-queue:" + "1" * 32,
            reason="lease changed",
        )

        prepared = supervisor.run(
            lambda _worktree: "second lease",
            operation_id="update-external-token",
            operation_input={"message": "remember"},
            external_finalizer="update-queue:" + "2" * 32,
        )

        self.assertEqual(prepared.output, "second lease")
        self.assertEqual(
            SemanticOperationStore(self.root)
            .read("update-external-token")
            .outcome.metadata["external_finalizer"],
            "update-queue:" + "2" * 32,
        )

    def test_external_result_can_be_superseded_by_another_lease_owner(self):
        supervisor = IsolatedWriteSupervisor(self.root, "update")
        finalizer = "update-queue:" + "1" * 32
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

    def test_update_lands_its_runtime_managed_review_in_the_same_commit(self):
        operation_id = "update-operation-with-review"

        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `two` reviewed update → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: reviewed update", cwd=worktree)
            return "Added one durable memory."

        def prepare(worktree, base, candidate, paths, output):
            store = UpdateReviewStore(worktree)
            record = store.create_review(
                origin_operation_id=operation_id,
                base_commit=base,
                write_surface="Memory",
                summary=output,
                diff=self._git("diff", base, candidate, "--", "MEMORY.md", cwd=worktree),
            )
            return (store.review_path(record.review_id).relative_to(worktree).as_posix(),)

        result = IsolatedWriteSupervisor(self.root, "update").run(
            callback,
            operation_id=operation_id,
            operation_input={"message": "remember"},
            prepare_managed_artifacts=prepare,
        )

        verified = verify_update_review(self.root, next((self.root / "update_reviews").glob("*.md")).stem)
        changed = self._git("show", "--format=", "--name-only", "HEAD").splitlines()
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.changed_paths, ("MEMORY.md",))
        self.assertIn("MEMORY.md", changed)
        self.assertIn(f"update_reviews/{verified.review_id}.md", changed)
        self.assertEqual(verified.origin_operation_id, operation_id)

    def test_update_lands_memory_and_pursuit_as_one_transaction(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `durable` durable result → []\n")
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Continue {#continue} \u2192 [dep:durable]\n",
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: unified update", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.start_commit, self._git("rev-parse", f"{result.landed_commit}^"))
        self.assertEqual(result.changed_paths, ("MEMORY.md", "PURSUITS.md"))
        self.assertIn("continue", (self.root / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_update_corrector_lands_state_and_feedback_in_one_commit(self):
        def callback(worktree: Path) -> UpdateCorrectionResult:
            self._append_memory(worktree, "- `corrected` accepted state → []\n")
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n\n"
                "## Keep accepted scope\n\n"
                "### Background\n\nThe updater broadened the edit.\n\n"
                "### Proposed edit\n\nRewrite unrelated state.\n\n"
                "### Accepted edit\n\nChange only the reviewed state.\n",
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: apply reviewed correction", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        result = IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(result.changed_paths, ("MEMORY.md", "corrections.md"))
        self.assertTrue((self.root / "corrections.md").is_file())

    def test_update_corrector_preserves_fixed_agent_correction_references(self):
        self._add_fixed_agent_correction_collections()

        def callback(worktree: Path) -> UpdateCorrectionResult:
            memory_path = worktree / "MEMORY.md"
            memory = memory_path.read_text(encoding="utf-8").replace(
                "\n### Agent corrections",
                "\n- `corrected` accepted state → []\n\n### Agent corrections",
            )
            memory_path.write_text(memory, encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: preserve fixed corrections", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        result = IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(result.commits_landed, 1)
        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("{M#agent-corrections-writing}", memory)
        self.assertIn("{M#agent-corrections-design}", memory)

    def test_update_corrector_accepts_needs_input_without_a_commit(self):
        result = IsolatedWriteSupervisor(self.root, "update-corrector").run(
            lambda _worktree: UpdateCorrectionResult(
                status="needs_input",
                message="Which scope should change?",
            )
        )

        self.assertEqual(result.commits_landed, 0)
        self.assertEqual(result.landed_commit, self.initial_head)

    def test_update_corrector_accepts_json_after_visible_thinking(self):
        result = IsolatedWriteSupervisor(self.root, "update-corrector").run(
            lambda _worktree: (
                "<think>checking</think>"
                '{"status":"needs_input","message":"Which scope should change?"}'
            )
        )

        self.assertEqual(result.commits_landed, 0)

    def test_sync_reconciler_preserves_structured_corrections_over_updater_ceiling(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                self._corrections_markdown(16),
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: preserve correction union", cwd=worktree)
            return "repaired"

        result = IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

        self.assertEqual(result.commits_landed, 1)
        text = (self.root / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Entry "), 16)

    def test_correction_overflow_does_not_block_unrelated_normal_update(self):
        (self.root / "corrections.md").write_text(
            self._corrections_markdown(16),
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
            16,
        )

    def test_correction_overflow_does_not_block_unrelated_update_correction(self):
        (self.root / "corrections.md").write_text(
            self._corrections_markdown(16),
            encoding="utf-8",
        )
        self._git("add", "corrections.md")
        self._git("commit", "-m", "sync: preserve unresolved correction union")

        def callback(worktree: Path) -> UpdateCorrectionResult:
            self._append_memory(worktree, "- `corrected` unrelated state repair → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: unrelated correction", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        result = IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertIn("corrected", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(
            (self.root / "corrections.md").read_text(encoding="utf-8").count("## Entry "),
            16,
        )


if __name__ == "__main__":
    unittest.main()

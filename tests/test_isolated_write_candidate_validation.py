import unittest
from pathlib import Path

from rightmemory.isolated_write import (
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
)
from rightmemory.update_corrector import UpdateCorrectionResult
from tests.isolated_write_test_base import IsolatedWriteTestBase


class IsolatedWriteCandidateValidationTests(IsolatedWriteTestBase):
    def test_update_model_cannot_author_a_review_file(self):
        def callback(worktree: Path) -> str:
            review = worktree / "update_reviews" / f"review-{'a' * 64}.md"
            review.parent.mkdir()
            review.write_text("model-authored review\n", encoding="utf-8")
            self._git("add", "-f", str(review.relative_to(worktree)), cwd=worktree)
            self._git("commit", "-m", "memory: invalid review", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            IsolatedWriteSupervisor(self.root, "update").run(
                callback,
                operation_id="update-model-review",
                operation_input={"message": "remember"},
            )

    def test_non_memory_temp_commit_does_not_land(self):
        def callback(worktree: Path) -> None:
            (worktree / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")
            self._git("add", "rightmemory.toml", cwd=worktree)
            self._git("commit", "-m", "memory: bad config update", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: rightmemory\\.toml"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "rightmemory.toml").exists())
        self._assert_isolated_cleanup()

    def test_deleted_memory_md_does_not_land(self):
        def callback(worktree: Path) -> None:
            self._git("rm", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: delete root memory", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY\\.md.*regular file"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertTrue((self.root / "MEMORY.md").is_file())
        self._assert_isolated_cleanup()

    def test_intermediate_deleted_memory_md_does_not_land(self):
        def callback(worktree: Path) -> None:
            original = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            self._git("rm", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: delete root memory", cwd=worktree)
            (worktree / "MEMORY.md").write_text(original + "- `two` restored memory → []\n", encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "memory: restore root memory", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY\\.md.*regular file"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertNotIn("restored memory", (self.root / "MEMORY.md").read_text(encoding="utf-8"))
        self._assert_isolated_cleanup()

    def test_allowed_memory_symlink_does_not_land(self):
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to("MEMORY.md")
        except OSError as exc:
            self.skipTest(f"symlink creation is not available: {exc}")
        finally:
            probe.unlink(missing_ok=True)

        def callback(worktree: Path) -> None:
            (worktree / "MEMORY_secret.md").symlink_to("MEMORY.md")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: add symlink detail", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY_secret\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "MEMORY_secret.md").exists())
        self._assert_isolated_cleanup()

    def test_intermediate_allowed_memory_symlink_does_not_land(self):
        probe = self.root / "symlink-probe"
        try:
            probe.symlink_to("MEMORY.md")
        except OSError as exc:
            self.skipTest(f"symlink creation is not available: {exc}")
        finally:
            probe.unlink(missing_ok=True)

        def callback(worktree: Path) -> None:
            secret = worktree / "MEMORY_secret.md"
            secret.symlink_to("MEMORY.md")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: add symlink detail", cwd=worktree)
            secret.unlink()
            secret.write_text("# Secret\n\n- `secret` restored regular memory → []\n", encoding="utf-8")
            self._git("add", "MEMORY_secret.md", cwd=worktree)
            self._git("commit", "-m", "memory: replace symlink detail", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "MEMORY_secret\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertFalse((self.root / "MEMORY_secret.md").exists())
        self._assert_isolated_cleanup()

    def test_insight_commit_lands_when_active_memory_is_invalid(self):
        self._append_memory(self.root, "- `one` duplicate active memory → []\n")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: preexisting invalid active memory")

        def callback(worktree: Path) -> str:
            insight = worktree / "insight_logs" / "2026-05-30-143012.md"
            insight.parent.mkdir()
            insight.write_text("# Insight\n\nUseful reflection.\n", encoding="utf-8")
            self._git("add", "insight_logs/2026-05-30-143012.md", cwd=worktree)
            self._git("commit", "-m", "insight: reflect on memory shape", cwd=worktree)
            return "insight"

        result = IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertEqual(result.output, "insight")
        self.assertTrue((self.root / "insight_logs" / "2026-05-30-143012.md").is_file())
        self.assertEqual(self._git("log", "-1", "--format=%s"), "insight: reflect on memory shape")

    def test_insight_commit_rejects_memory_edit(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` invalid insight memory edit → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "insight: invalid memory edit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-insight paths: MEMORY\.md"):
            IsolatedWriteSupervisor(self.root, "insight").run(callback)

        self.assertNotIn("invalid insight", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_update_rejects_multiple_commits(self):
        def callback(worktree: Path) -> str:
            self._append_memory(worktree, "- `first` first state → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: first", cwd=worktree)
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Continue {#continue}\n",
                encoding="utf-8",
            )
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: second", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "at most one commit"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_normal_update_rejects_corrections_file(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: invalid normal correction", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: corrections\\.md"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

    def test_dreamer_rejects_pursuit_write(self):
        def callback(worktree: Path) -> None:
            (worktree / "PURSUITS.md").write_text(
                "# Pursuits\n\n## Changed {#changed}\n",
                encoding="utf-8",
            )
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "dreamer: invalid pursuit edit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: PURSUITS\\.md"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

    def test_dreamer_rejects_fixed_correction_collection_write(self):
        def callback(worktree: Path) -> None:
            path = worktree / "MEMORY_agent-corrections-writing.md"
            path.write_text("# Curated writing corrections\n", encoding="utf-8")
            self._git("add", path.name, cwd=worktree)
            self._git("commit", "-m", "dreamer: invalid correction curation", cwd=worktree)

        with self.assertRaisesRegex(
            RuntimeError,
            "non-memory paths: MEMORY_agent-corrections-writing\\.md",
        ):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

    def test_update_corrector_rejects_removing_a_fixed_agent_correction_reference(self):
        self._add_fixed_agent_correction_collections()
        protected_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> UpdateCorrectionResult:
            memory = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            memory = memory.replace(
                "#### Writing corrections {M#agent-corrections-writing}\n",
                "",
            )
            (worktree / "MEMORY.md").write_text(memory, encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: remove fixed corrections", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        with self.assertRaisesRegex(
            RuntimeError,
            "cannot alter fixed M# collection `agent-corrections-writing`",
        ):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), protected_head)

    def test_update_corrector_rejects_retargeting_a_fixed_agent_correction_reference(self):
        self._add_fixed_agent_correction_collections()
        protected_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> UpdateCorrectionResult:
            memory = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            memory = memory.replace(
                "{M#agent-corrections-design}",
                "{M#other-design-corrections}",
            )
            (worktree / "MEMORY.md").write_text(memory, encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: retarget fixed corrections", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        with self.assertRaisesRegex(
            RuntimeError,
            "cannot alter fixed M# collection `agent-corrections-design`",
        ):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), protected_head)

    def test_update_corrector_rejects_renaming_a_fixed_agent_correction_collection(self):
        self._add_fixed_agent_correction_collections()
        protected_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> UpdateCorrectionResult:
            memory = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            memory = memory.replace(
                "#### Writing corrections {M#agent-corrections-writing}",
                "#### Preferred prose {M#agent-corrections-writing}",
            )
            (worktree / "MEMORY.md").write_text(memory, encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: rename fixed corrections", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        with self.assertRaisesRegex(
            RuntimeError,
            "cannot alter fixed M# collection `agent-corrections-writing`",
        ):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), protected_head)

    def test_update_corrector_rejects_feedback_only_commit(self):
        def callback(worktree: Path) -> UpdateCorrectionResult:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: feedback only", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        with self.assertRaisesRegex(RuntimeError, "corrections.md-only"):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

    def test_update_corrector_enforces_updater_correction_ceiling(self):
        def callback(worktree: Path) -> UpdateCorrectionResult:
            self._append_memory(worktree, "- `corrected` accepted state → []\n")
            (worktree / "corrections.md").write_text(
                self._corrections_markdown(16),
                encoding="utf-8",
            )
            self._git("add", "MEMORY.md", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: oversized correction curation", cwd=worktree)
            return UpdateCorrectionResult(status="applied", message="corrected")

        with self.assertRaisesRegex(RuntimeError, "at most 15 are allowed"):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

    def test_update_corrector_rejects_commit_for_needs_input_result(self):
        def callback(worktree: Path) -> UpdateCorrectionResult:
            self._append_memory(worktree, "- `speculative` must not land → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: speculative correction", cwd=worktree)
            return UpdateCorrectionResult(
                status="needs_input",
                message="Which scope should change?",
            )

        with self.assertRaisesRegex(RuntimeError, "needs_input.*must not create a commit"):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(callback)

        self.assertNotIn("speculative", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

    def test_update_corrector_rejects_applied_result_without_a_commit(self):
        with self.assertRaisesRegex(RuntimeError, "applied.*exactly one state commit"):
            IsolatedWriteSupervisor(self.root, "update-corrector").run(
                lambda _worktree: UpdateCorrectionResult(
                    status="applied",
                    message="corrected",
                )
            )

    def test_narrow_memory_role_preserves_pursuit_cross_tree_reference(self):
        (self.root / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Continue {#continue} \u2192 [dep:one]\n",
            encoding="utf-8",
        )
        self._git("add", "PURSUITS.md")
        self._git("commit", "-m", "pursuit: depend on memory")
        current_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> None:
            memory = (worktree / "MEMORY.md").read_text(encoding="utf-8")
            (worktree / "MEMORY.md").write_text(memory.replace("`one`", "`renamed`"), encoding="utf-8")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "dreamer: break pursuit edge", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "dangling edge `dep:one`"):
            IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

        self.assertEqual(self._git("rev-parse", "HEAD"), current_head)

    def test_dirty_pursuit_rules_or_corrections_blocks_narrow_writer(self):
        for name in ("PURSUITS.md", "PURSUIT_RULES.md", "corrections.md"):
            with self.subTest(name=name):
                path = self.root / name
                existed = path.exists()
                original = path.read_text(encoding="utf-8") if existed else None
                path.write_text("local synchronized state\n", encoding="utf-8")

                with self.assertRaises(MainMemoryDirtyError) as caught:
                    IsolatedWriteSupervisor(self.root, "dreamer").run(lambda _worktree: None)

                self.assertEqual(caught.exception.paths, (name,))
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_reviewer_cannot_land_graph_edits(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `reviewed` invalid reviewer edit → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "reviewer: invalid graph write", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: MEMORY\\.md"):
            IsolatedWriteSupervisor(self.root, "reviewer").run(callback)

    def test_sync_reconciler_still_rejects_malformed_correction_entries(self):
        def callback(worktree: Path) -> str:
            (worktree / "corrections.md").write_text(
                "# RightMemory Update Corrections\n\n"
                "## Incomplete\n\n### Background\n\nOnly one section.\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: malformed correction", cwd=worktree)
            return "repaired"

        with self.assertRaisesRegex(RuntimeError, "missing `### Proposed edit`"):
            IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)


if __name__ == "__main__":
    unittest.main()

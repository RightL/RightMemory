import unittest
from pathlib import Path

from rightmemory.isolated_write import (
    IsolatedWriteSupervisor,
    MainMemoryDirtyError,
)
from rightmemory.update_queue import UpdateCandidate
from rightmemory.update_record import UpdateRecord, UpdateRecordStore
from tests.isolated_write_test_base import IsolatedWriteTestBase


class IsolatedWriteCandidateValidationTests(IsolatedWriteTestBase):
    def test_update_model_cannot_author_a_candidate_record(self):
        candidate = UpdateCandidate(
            uid="a" * 32,
            session_id="agent-session",
            display_id=1,
            message="runtime-owned evidence",
            submitted_at="2026-07-27T12:00:00+00:00",
        )
        record = UpdateRecord.from_candidates((candidate,))

        def callback(worktree: Path) -> str:
            path = UpdateRecordStore(worktree).write(record)
            self._git("add", "-f", path.relative_to(worktree).as_posix(), cwd=worktree)
            self._git("commit", "-m", "update: model-authored record", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths"):
            IsolatedWriteSupervisor(self.root, "update").run(
                callback,
                operation_id=record.operation_id,
                operation_input={"message": candidate.message},
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

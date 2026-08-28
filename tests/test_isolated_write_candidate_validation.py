import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

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
                "# RightMemory Edit Corrections\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "rightmemory: invalid normal correction", cwd=worktree)
            return "updated"

        with self.assertRaisesRegex(RuntimeError, "non-memory paths: corrections\\.md"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)

    def test_update_rejects_pursuit_root_and_backing_commits_from_git(self):
        originals = {
            "PURSUITS.md": "# Pursuits\n\n## Branch {F#branch}\n",
            "PURSUIT_branch.md": "# Existing direction {#direction}\n",
        }
        for path, content in originals.items():
            (self.root / path).write_text(content, encoding="utf-8")
        self._git("add", *originals)
        self._git("commit", "-m", "pursuit: existing human map")
        start_head = self._git("rev-parse", "HEAD")
        start_memory = (self.root / "MEMORY.md").read_bytes()

        for path in originals:
            with self.subTest(path=path):
                def callback(worktree: Path) -> None:
                    content = originals[path].replace("Branch", "Changed branch").replace("Existing", "Changed")
                    (worktree / path).write_text(content, encoding="utf-8")
                    self._append_memory(worktree, "- `two` otherwise valid memory → []\n")
                    self._git("add", path, "MEMORY.md", cwd=worktree)
                    self._git("commit", "-m", "update: forbidden pursuit edit", cwd=worktree)

                with self.assertRaisesRegex(RuntimeError, "non-memory paths: PURSUIT"):
                    IsolatedWriteSupervisor(self.root, "update").run(callback)
                self.assertEqual(self._git("rev-parse", "HEAD"), start_head)
                self.assertEqual(self._git("status", "--short"), "")
                self.assertEqual((self.root / "MEMORY.md").read_bytes(), start_memory)
                self.assertEqual((self.root / path).read_text(encoding="utf-8"), originals[path])
                self._assert_isolated_cleanup()

    def test_update_rejects_pursuit_rename_into_memory(self):
        (self.root / "PURSUIT_branch.md").write_text("# Direction {#direction}\n", encoding="utf-8")
        self._git("add", "PURSUIT_branch.md")
        self._git("commit", "-m", "pursuit: existing detail")
        start_head = self._git("rev-parse", "HEAD")

        def callback(worktree: Path) -> None:
            self._git("mv", "PURSUIT_branch.md", "MEMORY_branch.md", cwd=worktree)
            self._git("commit", "-m", "update: rename pursuit into memory", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-memory paths: PURSUIT_branch\.md"):
            IsolatedWriteSupervisor(self.root, "update").run(callback)
        self.assertEqual(self._git("rev-parse", "HEAD"), start_head)
        self.assertTrue((self.root / "PURSUIT_branch.md").is_file())
        self.assertFalse((self.root / "MEMORY_branch.md").exists())
        self._assert_isolated_cleanup()

    def test_update_cannot_hide_pursuit_write_before_operation_squash(self):
        original_pursuits = (self.root / "PURSUITS.md").read_bytes()

        def callback(worktree: Path) -> None:
            (worktree / "PURSUITS.md").write_text("# Pursuits\n\n## Changed {#changed}\n", encoding="utf-8")
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "update: forbidden intermediate pursuit", cwd=worktree)
            (worktree / "PURSUITS.md").write_bytes(original_pursuits)
            self._append_memory(worktree, "- `two` otherwise valid final memory → []\n")
            self._git("add", "MEMORY.md", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "update: restore pursuit", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-memory paths: PURSUITS\.md"):
            IsolatedWriteSupervisor(self.root, "update").run(
                callback,
                operation_id="update-hidden-pursuit-write",
                operation_input={"message": "change memory"},
            )
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual((self.root / "PURSUITS.md").read_bytes(), original_pursuits)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_update_rechecks_old_prepared_pursuit_candidate_before_publication(self):
        def callback(worktree: Path) -> None:
            (worktree / "PURSUITS.md").write_text("# Pursuits\n\n## Changed {#changed}\n", encoding="utf-8")
            self._git("add", "PURSUITS.md", cwd=worktree)
            self._git("commit", "-m", "update: candidate prepared under old policy", cwd=worktree)

        with (
            patch.object(IsolatedWriteSupervisor, "_is_role_write_path", return_value=True),
            patch.object(IsolatedWriteSupervisor, "_land_operation_commit", side_effect=RuntimeError("interrupted before publish")),
            self.assertRaisesRegex(RuntimeError, "interrupted before publish"),
        ):
            IsolatedWriteSupervisor(self.root, "update").run(
                callback,
                operation_id="update-prepared-under-old-policy",
                operation_input={"message": "candidate from old runtime"},
            )

        with self.assertRaisesRegex(RuntimeError, r"non-memory paths: PURSUITS\.md"):
            IsolatedWriteSupervisor(self.root, "update").recover_prepared()
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self.assertEqual(self._git("status", "--short"), "")
        self.assertEqual((self.root / "PURSUITS.md").read_text(encoding="utf-8"), "# Pursuits\n")

    def test_update_lands_memory_graph_and_evidence_changes(self):
        def callback(worktree: Path) -> None:
            self._append_memory(
                worktree,
                "\n## Detail {F#detail}\n\n## Notes {M#notes}\n\n## Review {S#review}\n",
            )
            (worktree / "MEMORY_detail.md").write_text("# Detail fact {#detail-fact}\n", encoding="utf-8")
            (worktree / "MEMORY_notes.md").write_text("Free-form evidence.\n", encoding="utf-8")
            (worktree / "MEMORY_SKILL_review.md").write_text("Review the evidence.\n", encoding="utf-8")
            self._git("add", "MEMORY.md", "MEMORY_detail.md", "MEMORY_notes.md", "MEMORY_SKILL_review.md", cwd=worktree)
            self._git("commit", "-m", "update: add durable memory", cwd=worktree)

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(
            set(result.changed_paths),
            {"MEMORY.md", "MEMORY_detail.md", "MEMORY_notes.md", "MEMORY_SKILL_review.md"},
        )
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_sync_reconciler_lands_pursuit_root_and_backing_changes(self):
        def callback(worktree: Path) -> None:
            (worktree / "PURSUITS.md").write_text("# Pursuits\n\n## Shared direction {F#shared}\n", encoding="utf-8")
            (worktree / "PURSUIT_shared.md").write_text("# Shared child {#shared-child}\n", encoding="utf-8")
            self._git("add", "PURSUITS.md", "PURSUIT_shared.md", cwd=worktree)
            self._git("commit", "-m", "sync: reconcile human pursuit map", cwd=worktree)

        result = IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(set(result.changed_paths), {"PURSUITS.md", "PURSUIT_shared.md"})
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

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

    def test_update_lands_fixed_agent_correction_collection_write(self):
        self._add_fixed_agent_correction_collections()

        def callback(worktree: Path) -> str:
            path = worktree / "MEMORY_agent-corrections-writing.md"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\n### Keep the concrete contrast\n\nPreserve the redirected result.\n",
                encoding="utf-8",
            )
            self._git("add", path.name, cwd=worktree)
            self._git("commit", "-m", "update: curate expression correction", cwd=worktree)
            return "updated"

        result = IsolatedWriteSupervisor(self.root, "update").run(callback)

        self.assertEqual(result.changed_paths, ("MEMORY_agent-corrections-writing.md",))
        self.assertIn(
            "Keep the concrete contrast",
            (self.root / "MEMORY_agent-corrections-writing.md").read_text(encoding="utf-8"),
        )

    def test_sync_reconciler_lands_fixed_agent_correction_collection_write(self):
        self._add_fixed_agent_correction_collections()

        def callback(worktree: Path) -> str:
            path = worktree / "MEMORY_agent-corrections-design.md"
            path.write_text(
                path.read_text(encoding="utf-8")
                + "\n### Preserve the settled workflow\n\nKeep the resulting process concrete.\n",
                encoding="utf-8",
            )
            self._git("add", path.name, cwd=worktree)
            self._git("commit", "-m", "sync: reconcile substance correction", cwd=worktree)
            return "repaired"

        result = IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

        self.assertEqual(result.changed_paths, ("MEMORY_agent-corrections-design.md",))
        self.assertIn(
            "Preserve the settled workflow",
            (self.root / "MEMORY_agent-corrections-design.md").read_text(encoding="utf-8"),
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

        for role in ("dreamer", "update"):
            with self.subTest(role=role), self.assertRaisesRegex(RuntimeError, "dangling edge `dep:one`"):
                IsolatedWriteSupervisor(self.root, role).run(callback)
            self.assertEqual(self._git("rev-parse", "HEAD"), current_head)

    def test_dirty_pursuit_rules_or_corrections_blocks_narrow_writer(self):
        for name in (
            "PURSUITS.md",
            "corrections.md",
        ):
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
                "# RightMemory Edit Corrections\n\n"
                "## Incomplete\n\n### Candidate\n\nOnly one section.\n",
                encoding="utf-8",
            )
            self._git("add", "corrections.md", cwd=worktree)
            self._git("commit", "-m", "sync: malformed correction", cwd=worktree)
            return "repaired"

        with self.assertRaisesRegex(RuntimeError, "missing `### Proposed edit`"):
            IsolatedWriteSupervisor(self.root, "sync-reconciler").run(callback)

    def test_pursuit_map_lands_only_exact_memory_edge_repairs(self):
        _original, repaired = self._add_pursuit_repair_graph()

        result = IsolatedWriteSupervisor(self.root, "pursuit-map").run(
            lambda worktree: self._commit_pursuit_graph(worktree, repaired, "pursuit: remove direction")
        )

        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(set(result.changed_paths), set(repaired))
        self.assertEqual(self._git("status", "--short"), "")
        for path, content in repaired.items():
            if content is None:
                self.assertFalse((self.root / path).exists())
            else:
                self.assertEqual((self.root / path).read_bytes(), content)
        self._assert_isolated_cleanup()

    def test_pursuit_map_rejects_semantic_or_extra_memory_changes_during_delete(self):
        originals, repaired = self._add_pursuit_repair_graph()
        start_head = self._git("rev-parse", "HEAD")
        variants = {
            "prose": repaired["MEMORY.md"].replace(b"Keep prose mentioning removed.", b"Curated prose."),
            "surviving edge": repaired["MEMORY.md"].replace(b"[ doc:kept]", b"[]"),
            "line endings": repaired["MEMORY.md"].replace(b"\r\n", b"\n"),
        }
        for label, memory in variants.items():
            with self.subTest(change=label):
                candidate = {**repaired, "MEMORY.md": memory}
                with self.assertRaisesRegex(RuntimeError, "only to repair references"):
                    IsolatedWriteSupervisor(self.root, "pursuit-map").run(
                        lambda worktree: self._commit_pursuit_graph(worktree, candidate, "pursuit: invalid memory change")
                    )
                self.assertEqual(self._git("rev-parse", "HEAD"), start_head)
                self.assertEqual(self._git("status", "--short"), "")
                for path, content in originals.items():
                    self.assertEqual((self.root / path).read_bytes(), content)
                self._assert_isolated_cleanup()

    def test_pursuit_map_rejects_memory_changes_without_deleted_pursuit_ids(self):
        def callback(worktree: Path) -> None:
            self._append_memory(worktree, "- `two` unauthorized durable fact → []\n")
            self._git("add", "MEMORY.md", cwd=worktree)
            self._git("commit", "-m", "pursuit: unauthorized memory curation", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, "only to repair references"):
            IsolatedWriteSupervisor(self.root, "pursuit-map").run(callback)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self._assert_isolated_cleanup()

    def test_pursuit_map_cannot_modify_fixed_correction_collections(self):
        def callback(worktree: Path) -> None:
            path = worktree / "MEMORY_agent-corrections-writing.md"
            path.write_text("# Curated writing corrections\n", encoding="utf-8")
            self._git("add", path.name, cwd=worktree)
            self._git("commit", "-m", "pursuit: unauthorized corrections", cwd=worktree)

        with self.assertRaisesRegex(RuntimeError, r"non-memory paths: MEMORY_agent-corrections-writing\.md"):
            IsolatedWriteSupervisor(self.root, "pursuit-map").run(callback)
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)
        self._assert_isolated_cleanup()

    def test_pursuit_map_restores_exact_verified_deletion_and_can_delete_again(self):
        originals, repaired = self._add_pursuit_repair_graph()
        deletion = IsolatedWriteSupervisor(self.root, "pursuit-map").run(
            lambda worktree: self._commit_pursuit_graph(worktree, repaired, "pursuit: remove direction")
        )

        with self.assertRaisesRegex(RuntimeError, "only to repair references"):
            IsolatedWriteSupervisor(self.root, "pursuit-map").run(
                lambda worktree: self._commit_pursuit_graph(worktree, originals, "pursuit: unverified restoration")
            )
        self.assertEqual(self._git("rev-parse", "HEAD"), deletion.landed_commit)

        restoration = IsolatedWriteSupervisor(
            self.root, "pursuit-map", pursuit_restore_commit=deletion.landed_commit
        ).run(lambda worktree: self._commit_pursuit_graph(worktree, originals, "pursuit: undo deletion"))
        self.assertEqual(restoration.commits_landed, 1)
        for path, content in originals.items():
            self.assertEqual((self.root / path).read_bytes(), content)

        redo = IsolatedWriteSupervisor(
            self.root, "pursuit-map", pursuit_restore_commit=restoration.landed_commit
        ).run(lambda worktree: self._commit_pursuit_graph(worktree, repaired, "pursuit: redo deletion"))
        self.assertEqual(redo.commits_landed, 1)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_pursuit_map_restore_rejects_memory_curation_hidden_in_source(self):
        originals, repaired = self._add_pursuit_repair_graph()
        unsafe_source = {
            **repaired,
            "MEMORY.md": repaired["MEMORY.md"].replace(b"Keep prose mentioning removed.", b"Unrelated curation."),
        }
        source = self._commit_pursuit_graph(self.root, unsafe_source, "sync: unrelated memory and pursuit changes")

        with self.assertRaisesRegex(RuntimeError, "only to repair references"):
            IsolatedWriteSupervisor(self.root, "pursuit-map", pursuit_restore_commit=source).run(
                lambda worktree: self._commit_pursuit_graph(worktree, originals, "pursuit: invalid reverse curation")
            )
        self.assertEqual(self._git("rev-parse", "HEAD"), source)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_pursuit_map_restore_cannot_overwrite_later_memory_change(self):
        originals, repaired = self._add_pursuit_repair_graph()
        deletion = IsolatedWriteSupervisor(self.root, "pursuit-map").run(
            lambda worktree: self._commit_pursuit_graph(worktree, repaired, "pursuit: remove direction")
        )
        later_memory = repaired["MEMORY.md"] + b"\r\nLater durable context.\r\n"
        current_head = self._commit_pursuit_graph(
            self.root, {"MEMORY.md": later_memory}, "update: preserve later context"
        )

        with self.assertRaisesRegex(RuntimeError, "only to repair references"):
            IsolatedWriteSupervisor(
                self.root, "pursuit-map", pursuit_restore_commit=deletion.landed_commit
            ).run(lambda worktree: self._commit_pursuit_graph(worktree, originals, "pursuit: invalid stale inverse"))
        self.assertEqual(self._git("rev-parse", "HEAD"), current_head)
        self.assertEqual((self.root / "MEMORY.md").read_bytes(), later_memory)
        self.assertEqual(self._git("status", "--short"), "")
        self._assert_isolated_cleanup()

    def test_update_cannot_request_pursuit_reference_restoration(self):
        with self.assertRaises(ValueError):
            IsolatedWriteSupervisor(self.root, "update", pursuit_restore_commit=self.initial_head)

    def test_pursuit_snapshot_batches_exact_immutable_blobs(self):
        self._git("config", "core.autocrlf", "false")
        repeated = "Notes with Chinese 中文.\r\n\r\nMore context.\r\n".encode("utf-8")
        originals = {
            "MEMORY.md": b"# Domain {#domain}\n\n## Notes {M#notes}\n\n## Copy {M#copy}\n\n## Empty {M#empty}\n",
            "PURSUITS.md": b"# Pursuits\n",
            "MEMORY_notes.md": repeated,
            "MEMORY_copy.md": repeated,
            "MEMORY_empty.md": b"",
        }
        commit = self._commit_pursuit_graph(self.root, originals, "memory: snapshot binary boundaries")
        (self.root / "MEMORY_notes.md").write_bytes(b"Uncommitted replacement.\n")

        with patch("rightmemory.isolated_write.subprocess.run", wraps=subprocess.run) as run_git:
            snapshot = IsolatedWriteSupervisor(self.root, "pursuit-map")._commit_graph_snapshot(self.root, commit)

        self.assertEqual(snapshot.files, originals)
        self.assertEqual(snapshot.graph.errors, [])
        blob_calls = [call for call in run_git.call_args_list if call.args[0][:2] == ["git", "cat-file"]]
        self.assertEqual(len(blob_calls), 1)

    def _add_pursuit_repair_graph(self):
        self._git("config", "core.autocrlf", "false")
        original = {
            "MEMORY.md": (
                b"# Domain {#domain} -> [dep:removed, doc:kept]\r\n\r\n"
                b"Keep prose mentioning removed.\r\n\r\n"
                b"```md\r\n- `example` unchanged example -> [dep:removed]\r\n```\r\n\r\n"
                b"- `one` initial memory -> [dep:removed-child]\r\n\r\n"
                b"## Detail {F#detail}\r\n"
            ),
            "MEMORY_detail.md": b"# Evidence {#evidence} -> [doc:removed-child, dep:kept]\n",
            "PURSUITS.md": (
                b"# Pursuits\n\n## Focus\n\n- `removed`\n\n"
                b"## Removed {F#removed}\n\n## Kept {#kept}\n"
            ),
            "PURSUIT_removed.md": b"# Removed child {#removed-child}\n",
        }
        self._commit_pursuit_graph(self.root, original, "pursuit: seed cross-root references")
        repaired = {
            "MEMORY.md": original["MEMORY.md"].replace(b"[dep:removed, doc:kept]", b"[ doc:kept]").replace(
                b"[dep:removed-child]", b"[]"
            ),
            "MEMORY_detail.md": b"# Evidence {#evidence} -> [ dep:kept]\n",
            "PURSUITS.md": b"# Pursuits\n\n## Focus\n\n## Kept {#kept}\n",
            "PURSUIT_removed.md": None,
        }
        return original, repaired

    def _commit_pursuit_graph(self, worktree: Path, files: dict[str, bytes | None], subject: str) -> str:
        for path, content in files.items():
            if content is None:
                (worktree / path).unlink(missing_ok=True)
            else:
                (worktree / path).write_bytes(content)
        self._git("add", "-A", "--", *files, cwd=worktree)
        self._git("commit", "-m", subject, cwd=worktree)
        return self._git("rev-parse", "HEAD", cwd=worktree)


if __name__ == "__main__":
    unittest.main()

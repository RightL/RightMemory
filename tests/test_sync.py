import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import SyncConfig
from rightmemory.semantic_operation import SemanticOperationStore
from rightmemory.sync import GIT_TIMEOUT_SECONDS, SyncManager, SyncResult


class SyncManagerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.remote = self.root / "remote.git"
        self.device = self.root / "device"
        self.other = self.root / "other"
        self._git(self.root, "init", "--bare", str(self.remote))
        self._git(self.root, "clone", str(self.remote), str(self.device))
        self._git(self.root, "clone", str(self.remote), str(self.other))
        for repo in (self.device, self.other):
            self._git(repo, "config", "user.email", "test@example.com")
            self._git(repo, "config", "user.name", "Test User")
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` first → []\n", encoding="utf-8")
        (self.device / "PURSUITS.md").write_text("# Pursuits\n", encoding="utf-8")
        (self.device / "PURSUIT_RULES.md").write_text("# Pursuit Rules\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md", "PURSUITS.md", "PURSUIT_RULES.md")
        self._git(self.device, "commit", "-m", "initial memory")
        self._git(self.device, "push", "-u", "origin", "HEAD:main")
        self._git(self.device, "branch", "--set-upstream-to", "origin/main")
        self._git(self.other, "fetch", "origin")
        self._git(self.other, "checkout", "-B", "main", "origin/main")
        self._git(self.other, "branch", "--set-upstream-to", "origin/main")

    def test_preflight_disabled(self):
        result = SyncManager(SyncConfig(memory_root=self.device, enabled=False)).preflight()

        self.assertEqual(result.status, "disabled")
        self.assertIn("disabled", result.message)

    def test_sync_paths_include_shared_view_registry_and_provider_definitions(self):
        from rightmemory.sync import MEMORY_SYNC_PATHS, _is_sync_path

        self.assertIn("shared_views.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shares.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/view.md", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/retriever.md", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/recipe.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/question.toml", MEMORY_SYNC_PATHS)
        self.assertIn("PURSUITS.md", MEMORY_SYNC_PATHS)
        self.assertIn("PURSUIT_*.md", MEMORY_SYNC_PATHS)
        self.assertIn("PURSUIT_RULES.md", MEMORY_SYNC_PATHS)
        self.assertIn("corrections.md", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/candidates/*.json", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/recovery/*.json", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/lease.json", MEMORY_SYNC_PATHS)
        self.assertTrue(_is_sync_path(f"update_queue/candidates/{'a' * 32}.json"))
        self.assertTrue(_is_sync_path(f"update_queue/recovery/update-batch-{'b' * 64}.json"))
        self.assertTrue(_is_sync_path("update_queue/lease.json"))
        self.assertFalse(_is_sync_path("update_queue/candidates/not-a-uuid.json"))
        self.assertFalse(_is_sync_path(f"update_queue/candidates/{'A' * 32}.json"))
        self.assertFalse(_is_sync_path(f"update_queue/recovery/{'b' * 64}.json"))
        self.assertFalse(_is_sync_path("update_queue/extra.json"))

    def test_preflight_rejects_memory_root_nested_in_outer_git_repo(self):
        outer_remote = self.root / "outer.git"
        outer = self.root / "outer"
        peer = self.root / "outer-peer"
        self._git(self.root, "init", "--bare", str(outer_remote))
        self._git(self.root, "clone", str(outer_remote), str(outer))
        self._git(outer, "config", "user.email", "test@example.com")
        self._git(outer, "config", "user.name", "Test User")
        nested = outer / "memory"
        nested.mkdir()
        (nested / "MEMORY.md").write_text("# Domain\n\n- `one` nested memory → []\n", encoding="utf-8")
        self._git(outer, "add", "memory/MEMORY.md")
        self._git(outer, "commit", "-m", "initial outer memory")
        self._git(outer, "push", "-u", "origin", "HEAD:main")
        self._git(outer, "branch", "--set-upstream-to", "origin/main")
        outer_head = self._git(outer, "rev-parse", "HEAD")

        self._git(self.root, "clone", str(outer_remote), str(peer))
        self._git(peer, "config", "user.email", "test@example.com")
        self._git(peer, "config", "user.name", "Test User")
        self._git(peer, "checkout", "-B", "main", "origin/main")
        self._git(peer, "branch", "--set-upstream-to", "origin/main")
        (peer / "memory" / "MEMORY.md").write_text(
            "# Domain\n\n- `one` nested memory → []\n- `two` remote outer change → []\n",
            encoding="utf-8",
        )
        self._git(peer, "add", "memory/MEMORY.md")
        self._git(peer, "commit", "-m", "remote outer memory")
        self._git(peer, "push")

        result = SyncManager(SyncConfig(memory_root=nested, enabled=True)).preflight()

        self.assertEqual(result.status, "unconfigured")
        self.assertEqual(self._git(outer, "rev-parse", "HEAD"), outer_head)
        self.assertNotIn("remote outer change", (nested / "MEMORY.md").read_text(encoding="utf-8"))

    def test_preflight_fast_forwards_clean_repo(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertIn("two", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_preflight_reports_dirty_memory_without_merging(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote only → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        memory = (self.device / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("local dirty", memory)
        self.assertNotIn("remote only", memory)

    def test_preflight_reports_each_new_synchronized_state_path_as_dirty(self):
        for name in ("PURSUITS.md", "PURSUIT_RULES.md", "corrections.md"):
            with self.subTest(name=name):
                path = self.device / name
                existed = path.exists()
                original = path.read_text(encoding="utf-8") if existed else None
                path.write_text("local synchronized state\n", encoding="utf-8")

                result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

                self.assertEqual(result.status, "dirty")
                self.assertEqual(result.files, [name])
                if original is None:
                    path.unlink()
                else:
                    path.write_text(original, encoding="utf-8")

    def test_clean_git_merge_with_duplicate_cross_tree_id_reports_semantic_conflict(self):
        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Duplicate {#one}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "pursuit: add duplicate id")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "conflict")
        self.assertIn("duplicate id `one`", result.message)

    def test_sync_transports_structured_corrections_over_updater_ceiling(self):
        (self.other / "corrections.md").write_text(
            self._corrections_markdown(16),
            encoding="utf-8",
        )
        self._git(self.other, "add", "corrections.md")
        self._git(self.other, "commit", "-m", "sync: transport correction union")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        text = (self.device / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Entry "), 16)

    def test_sync_reconciler_prompt_preserves_distinct_corrections_without_ranking(self):
        prompt_path = Path(__file__).parents[1] / "rightmemory" / "prompts" / "sync-reconciler.md"
        prompt = prompt_path.read_text(encoding="utf-8").casefold()

        self.assertIn("non-identical complete entries", prompt)
        self.assertIn("exactly duplicated", prompt)
        self.assertIn("do not rank", prompt)
        self.assertIn("may exceed that ceiling", prompt)
        self.assertIn("unresolved updater-owned semantic maintenance", prompt)

    def test_preflight_reports_dirty_shared_view_registry_and_ignores_runtime_shared_views(self):
        runtime_cache = self.device / ".runtime" / "shared_views" / "cache" / "alice-auth-api.txt"
        runtime_cache.parent.mkdir(parents=True)
        runtime_cache.write_text("runtime cache\n", encoding="utf-8")
        registry = self.device / "shared_views.toml"
        registry.write_text(
            '[connections.alice-auth-api]\ntype = "file"\nref = "rightmemory://mf/current"\n',
            encoding="utf-8",
        )

        dirty = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()
        registry.unlink()
        clean = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(dirty.status, "dirty")
        self.assertEqual(dirty.files, ["shared_views.toml"])
        self.assertEqual(clean.status, "synced")

    def test_preflight_reports_dirty_provider_shared_view_source_and_ignores_dist(self):
        view_dir = self.device / "shared_views" / "alice-auth-api"
        view_dir.mkdir(parents=True)
        (view_dir / "view.md").write_text("# Alice Auth API\n", encoding="utf-8")
        dist = view_dir / "dist"
        dist.mkdir()
        (dist / "MEMORY.md").write_text("# Generated shared surface\n", encoding="utf-8")

        dirty = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()
        (view_dir / "view.md").unlink()
        clean = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(dirty.status, "dirty")
        self.assertEqual(dirty.files, ["shared_views/alice-auth-api/view.md"])
        self.assertEqual(clean.status, "synced")

    def test_push_merges_remote_change_and_reports_conflict(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        start_memory = (self.device / "MEMORY.md").read_bytes()

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), start_memory)
        self.assertNotIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_conflict_can_be_resolved_committed_and_pushed(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote durable fact → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local durable fact → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        active_head = self._git(self.device, "rev-parse", "HEAD")

        def repair(candidate, result, operation_id):
            self.assertNotEqual(candidate, self.device)
            self.assertEqual(result.status, "conflict")
            self.assertTrue(operation_id.startswith("sync-repair-"))
            self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), active_head)
            self.assertNotIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
            (candidate / "MEMORY.md").write_text(
                "# Domain\n\n"
                "- `one-remote` remote durable fact → []\n"
                "- `one-local` local durable fact → []\n",
                encoding="utf-8",
            )
            self._git(candidate, "add", "MEMORY.md")
            self._git(candidate, "commit", "-m", "memory: resolve staged sync conflict")
            return "staged conflict repaired"

        pushed = manager.push(repair=repair)

        self.assertEqual(pushed.status, "pushed")
        self._git(self.other, "pull", "--ff-only")
        text = (self.other / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("one-remote", text)
        self.assertIn("one-local", text)

    def test_push_reports_dirty_memory_without_pushing(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` committed local → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` committed local → []\n- `three` dirty local → []\n",
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertNotIn("committed local", remote_memory)
        self.assertNotIn("dirty local", remote_memory)

    def test_push_refuses_committed_paths_outside_synchronized_state(self):
        remote_head = self._git(self.other, "rev-parse", "origin/main")
        (self.device / "rightmemory.toml").write_text(
            '[retrieve.model]\nmodel_id = "private/provider"\n',
            encoding="utf-8",
        )
        self._git(self.device, "add", "rightmemory.toml")
        self._git(self.device, "commit", "-m", "local machine config")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, ["rightmemory.toml"])
        self.assertIn("outside the synchronized state", result.message)
        self._git(self.other, "fetch", "origin")
        self.assertEqual(self._git(self.other, "rev-parse", "origin/main"), remote_head)

    def test_push_reports_dirty_insight_log(self):
        insight = self.device / "insight_logs" / "2026-05-30-143012.md"
        insight.parent.mkdir()
        insight.write_text("# Insight\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["insight_logs/2026-05-30-143012.md"])

    def test_push_ignores_untracked_retired_dream_log(self):
        dream = self.device / "dream_logs" / "2026-05-30.md"
        dream.parent.mkdir()
        dream.write_text("# Dream\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "pushed")

    def test_push_uses_upstream_even_when_local_branch_name_differs(self):
        self._git(self.device, "checkout", "-B", "memory-device", "origin/main")
        self._git(self.device, "branch", "--set-upstream-to", "origin/main")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` local branch diff → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local branch memory")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "pushed")
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertIn("local branch diff", remote_memory)

    def test_state_records_successful_pull(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        result = manager.preflight()

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "synced")
        self.assertIn("last_successful_pull_at", state)
        parsed = datetime.fromisoformat(state["last_successful_pull_at"])
        self.assertEqual(parsed.tzinfo, UTC)

    def test_preflight_dirty_records_state(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(result.status, "dirty")
        self.assertEqual(state["last_status"], "dirty")
        self.assertEqual(state["last_files"], ["MEMORY.md"])

    def test_git_runs_noninteractive_with_timeout(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        completed = subprocess.CompletedProcess(["git", "status"], 0, "", "")

        with patch("rightmemory.sync.subprocess.run", return_value=completed) as run:
            result = manager._git("status")

        self.assertIs(result, completed)
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["env"]["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(kwargs["env"]["GIT_ASKPASS"], "true")
        self.assertEqual(kwargs["timeout"], GIT_TIMEOUT_SECONDS)

    def test_background_pull_skips_fresh_state(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24))
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "fresh")

    def test_background_pull_fetches_remote_change_even_when_state_is_fresh(self):
        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Remote work {#remote-work}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "pursuit: remote change")
        self._git(self.other, "push")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(
            SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)
        ).background_pull()

        self.assertEqual(result.status, "synced")
        self.assertIn("remote-work", (self.device / "PURSUITS.md").read_text(encoding="utf-8"))

    def test_background_pull_pushes_ahead_commits_even_when_pull_state_fresh(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` local committed → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "pushed")
        self._git(self.other, "fetch", "origin")
        remote_memory = self._git(self.other, "show", "origin/main:MEMORY.md")
        self.assertIn("local committed", remote_memory)

    def test_background_pull_reports_conflict_even_when_pull_state_fresh(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")
        self._git(self.device, "fetch", "origin")
        merge = subprocess.run(
            ["git", "merge", "--no-edit", "origin/main"],
            cwd=self.device,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.assertNotEqual(merge.returncode, 0, merge.stdout + merge.stderr)

        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_background_pull_reports_dirty_even_when_pull_state_fresh(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` dirty → []\n", encoding="utf-8")
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": datetime.now(UTC).isoformat()}),
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24)).background_pull()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])

    def test_background_pull_runs_when_stale(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True, stale_pull_after_hours=24))
        state_path = self.device / ".runtime" / "sync" / "state.json"
        state_path.parent.mkdir(parents=True)
        state_path.write_text(
            json.dumps({"last_successful_pull_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat()}),
            encoding="utf-8",
        )

        result = manager.background_pull()

        self.assertEqual(result.status, "synced")

    def test_repair_message_describes_dirty_and_conflict_states(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))

        dirty = manager.repair_message(SyncResult("dirty", "dirty memory", ["MEMORY.md"]))
        conflict = manager.repair_message(SyncResult("conflict", "memory sync conflict", ["MEMORY.md"]))

        self.assertIn("inspect and repair dirty memory state", dirty)
        self.assertIn("staged incoming candidate", conflict)
        self.assertIn("active memory root is unchanged", conflict)

    def test_clean_divergent_merge_lands_exact_candidate_commit(self):
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `local` local fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory")
        local_tip = self._git(self.device, "rev-parse", "HEAD")

        (self.other / "PURSUITS.md").write_text(
            "# Pursuits\n\n## Remote work {#remote-work}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md")
        self._git(self.other, "commit", "-m", "remote pursuit")
        remote_tip = self._git(self.other, "rev-parse", "HEAD")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        landed = self._git(self.device, "rev-parse", "HEAD")
        self.assertNotEqual(landed, local_tip)
        self.assertEqual(self._git(self.device, "show", "-s", "--format=%P", landed).split(), [local_tip, remote_tip])
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_remote_non_sync_path_is_rejected_before_candidate_publication(self):
        start_head = self._git(self.device, "rev-parse", "HEAD")
        (self.other / "rightmemory.toml").write_text("[sync]\nenabled = true\n", encoding="utf-8")
        self._git(self.other, "add", "rightmemory.toml")
        self._git(self.other, "commit", "-m", "remote config")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, ["rightmemory.toml"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertFalse((self.device / "rightmemory.toml").exists())

    def test_remote_queue_path_with_invalid_identity_is_rejected(self):
        start_head = self._git(self.device, "rev-parse", "HEAD")
        path = self.other / "update_queue" / "candidates" / "not-a-uuid.json"
        path.parent.mkdir(parents=True)
        path.write_text("{}\n", encoding="utf-8")
        self._git(self.other, "add", "-f", str(path.relative_to(self.other)))
        self._git(self.other, "commit", "-m", "queue: add invalid candidate path")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, ["update_queue/candidates/not-a-uuid.json"])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)

    def test_malformed_remote_queue_fails_closed_without_model_repair(self):
        start_head = self._git(self.device, "rev-parse", "HEAD")
        relative = f"update_queue/candidates/{'a' * 32}.json"
        path = self.other / relative
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._git(self.other, "add", "-f", relative)
        self._git(self.other, "commit", "-m", "queue: add malformed candidate")
        self._git(self.other, "push")
        repair_calls = []

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
            repair=lambda *args: repair_calls.append(args) or "repaired"
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("invalid JSON", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertFalse((self.device / relative).exists())

    def test_queue_conflict_fails_closed_without_model_repair(self):
        relative = "update_queue/lease.json"
        for root, owner in ((self.device, "local"), (self.other, "remote")):
            path = root / relative
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"owner": owner}) + "\n", encoding="utf-8")
            self._git(root, "add", "-f", relative)
            self._git(root, "commit", "-m", f"queue: {owner} lease")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        local_bytes = (self.device / relative).read_bytes()
        self._git(self.other, "push")
        repair_calls = []

        with patch("rightmemory.sync.validate_update_queue", return_value=[]):
            result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
                repair=lambda *args: repair_calls.append(args) or "repaired"
            )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("coordination conflict", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / relative).read_bytes(), local_bytes)

    def test_malformed_queue_with_memory_conflict_never_reaches_model_repair(self):
        relative = f"update_queue/candidates/{'a' * 32}.json"
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote durable fact → []\n",
            encoding="utf-8",
        )
        path = self.other / relative
        path.parent.mkdir(parents=True)
        path.write_text("{not json\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "add", "-f", relative)
        self._git(self.other, "commit", "-m", "remote memory and malformed queue")
        self._git(self.other, "push")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local memory edit")
        start_head = self._git(self.device, "rev-parse", "HEAD")
        repair_calls = []

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(
            repair=lambda *args: repair_calls.append(args) or "repaired"
        )

        self.assertEqual(result.status, "error")
        self.assertEqual(result.files, [relative])
        self.assertIn("invalid JSON", result.message)
        self.assertEqual(repair_calls, [])
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)

    def test_repair_exception_leaves_active_checkout_unchanged(self):
        self._create_remote_local_conflict()
        start_head = self._git(self.device, "rev-parse", "HEAD")
        start_bytes = (self.device / "MEMORY.md").read_bytes()

        def fail(candidate, result, operation_id):
            self.assertIn("<<<<<<<", (candidate / "MEMORY.md").read_text(encoding="utf-8"))
            raise RuntimeError("model unavailable")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull(repair=fail)

        self.assertEqual(result.status, "error")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), start_head)
        self.assertEqual((self.device / "MEMORY.md").read_bytes(), start_bytes)
        self.assertEqual(self._git(self.device, "status", "--porcelain"), "")

    def test_running_repair_commit_is_adopted_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        original_prepare = SemanticOperationStore.prepare_outcome
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash before prepared receipt")
            return original_prepare(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "prepare_outcome", fail_once):
            first = manager.pull(repair=repair)
        second = manager.pull(
            repair=lambda *_args: self.fail("durable candidate commit must prevent a second model run")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(len(calls), 1)
        self.assertIn("one-remote", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_prepared_repair_resumes_publication_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        with patch.object(manager, "_publish_candidate", side_effect=RuntimeError("crash before publish")):
            first = manager.pull(repair=repair)
        second = manager.pull(
            repair=lambda *_args: self.fail("prepared repair must not run the model again")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(len(calls), 1)

    def test_publication_crash_is_recognized_without_rerunning_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def repair(candidate, result, operation_id):
            calls.append(operation_id)
            self._commit_candidate_resolution(candidate)
            return "repaired"

        original_complete = SemanticOperationStore.complete_commit
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash after fast-forward")
            return original_complete(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "complete_commit", fail_once):
            first = manager.pull(repair=repair)
        self._assert_no_sync_candidates()
        published_head = self._git(self.device, "rev-parse", "HEAD")
        second = manager.pull(
            repair=lambda *_args: self.fail("published repair must not run the model again")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "synced")
        self.assertEqual(self._git(self.device, "rev-parse", "HEAD"), published_head)
        self.assertEqual(len(calls), 1)
        self._assert_no_sync_candidates()

    def test_durable_no_change_conflict_does_not_rerun_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def no_change(candidate, result, operation_id):
            calls.append(operation_id)
            return "no safe repair"

        first = manager.pull(repair=no_change)
        second = manager.pull(repair=no_change)

        self.assertEqual(first.status, "conflict")
        self.assertEqual(second.status, "conflict")
        self.assertEqual(len(calls), 1)
        record = SemanticOperationStore(self.device).read(calls[0])
        self.assertIsNotNone(record)
        self.assertEqual(record.phase, "no_change")

    def test_no_change_completion_crash_cleans_candidate_and_does_not_rerun_model(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        calls = []

        def no_change(candidate, result, operation_id):
            calls.append(operation_id)
            return "no safe repair"

        original_complete = SemanticOperationStore.complete_no_change
        failed = False

        def fail_once(store, *args, **kwargs):
            nonlocal failed
            if not failed:
                failed = True
                raise RuntimeError("crash after no-change preparation")
            return original_complete(store, *args, **kwargs)

        with patch.object(SemanticOperationStore, "complete_no_change", fail_once):
            first = manager.pull(repair=no_change)
        self._assert_no_sync_candidates()
        second = manager.pull(
            repair=lambda *_args: self.fail("prepared no-change must not rerun the model")
        )

        self.assertEqual(first.status, "error")
        self.assertEqual(second.status, "conflict")
        self.assertEqual(len(calls), 1)
        self._assert_no_sync_candidates()

    def test_recorded_candidate_identity_uses_operation_digest_for_lease(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        digest = "b" * 64
        operation_id = f"sync-repair-{digest}"
        branch = f"rightmemory-sync-{digest}"
        relative = f".runtime/worktrees/sync-{digest}"
        path = self.device / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        self._git(self.device, "worktree", "add", "-b", branch, str(path), "HEAD")
        record = SemanticOperationStore(self.device).begin(
            operation_id,
            {
                "kind": "sync-repair",
                "role": "sync-reconciler",
                "candidate_branch": branch,
                "candidate_worktree": relative,
            },
        )

        candidate = manager._open_recorded_candidate(record)

        self.assertEqual(candidate.lease.identifier, digest)
        candidate.lease.release()
        manager._cleanup_recorded_candidate(record, require_removed=True)
        self.assertFalse(path.exists())
        self.assertEqual(self._git(self.device, "branch", "--list", branch), "")

    def test_corrupt_recorded_routing_cannot_remove_another_worktree(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        other_branch = "rightmemory-sync-other-live-operation"
        other_relative = ".runtime/worktrees/sync-other-live-operation"
        other_path = self.device / other_relative
        other_path.parent.mkdir(parents=True, exist_ok=True)
        self._git(self.device, "worktree", "add", "-b", other_branch, str(other_path), "HEAD")
        def cleanup_other_worktree():
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(other_path)],
                cwd=self.device,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            subprocess.run(
                ["git", "branch", "-D", other_branch],
                cwd=self.device,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        self.addCleanup(cleanup_other_worktree)
        record = SemanticOperationStore(self.device).begin(
            f"sync-repair-{'c' * 64}",
            {
                "kind": "sync-repair",
                "role": "sync-reconciler",
                "candidate_branch": other_branch,
                "candidate_worktree": other_relative,
            },
        )

        with self.assertRaisesRegex(RuntimeError, "does not match its operation id"):
            manager._cleanup_recorded_candidate(record, require_removed=True)

        self.assertTrue(other_path.is_dir())
        self.assertIn(other_branch, self._git(self.device, "branch", "--list", other_branch))

    def test_active_head_race_blocks_publication_without_overwrite(self):
        self._create_remote_local_conflict()
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))

        def repair(candidate, result, operation_id):
            self._commit_candidate_resolution(candidate)
            (self.device / "MEMORY.md").write_text(
                "# Domain\n\n- `external` independently committed → []\n",
                encoding="utf-8",
            )
            self._git(self.device, "add", "MEMORY.md")
            self._git(self.device, "commit", "-m", "external active change")
            return "repaired"

        result = manager.pull(repair=repair)

        self.assertEqual(result.status, "error")
        self.assertIn("external", (self.device / "MEMORY.md").read_text(encoding="utf-8"))
        self.assertNotIn("one-remote", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def _create_remote_local_conflict(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` remote durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")
        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

    def _commit_candidate_resolution(self, candidate):
        (candidate / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one-remote` remote durable fact → []\n"
            "- `one-local` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(candidate, "add", "MEMORY.md")
        self._git(candidate, "commit", "-m", "memory: resolve staged sync conflict")

    def _assert_no_sync_candidates(self):
        self.assertEqual(self._git(self.device, "branch", "--list", "rightmemory-sync-*"), "")
        worktrees = self._git(self.device, "worktree", "list", "--porcelain")
        self.assertNotIn("/.runtime/worktrees/sync-", worktrees)
        lease_root = self.device / ".runtime" / "worktree-leases"
        leases = sorted(path.name for path in lease_root.glob("sync-*.json")) if lease_root.is_dir() else []
        self.assertEqual(leases, [])

    def _git(self, cwd: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr)
        return process.stdout.strip()

    def _corrections_markdown(self, count: int) -> str:
        entries = []
        for index in range(count):
            entries.append(
                f"## Entry {index}\n\n"
                "### Background\n\nBackground.\n\n"
                "### Proposed edit\n\nProposed.\n\n"
                "### Accepted edit\n\nAccepted.\n"
            )
        return "# RightMemory Update Corrections\n\n" + "\n".join(entries)

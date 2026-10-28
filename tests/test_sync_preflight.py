from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.sync import SyncManager
from rightmemory.learning import list_learning_candidates, record_learning_processed, submit_learning
from tests.sync_test_base import SyncTestBase


class SyncPreflightTests(SyncTestBase):
    def test_outgoing_paths_include_files_created_then_deleted(self):
        start = self._git(self.device, "rev-parse", "HEAD")
        (self.device / "transient.txt").write_text("temporary change\n", encoding="utf-8")
        self._git(self.device, "add", "transient.txt")
        self._git(self.device, "commit", "-m", "add transient file")
        self._git(self.device, "rm", "transient.txt")
        self._git(self.device, "commit", "-m", "remove transient file")
        end = self._git(self.device, "rev-parse", "HEAD")
        self.assertEqual(self._git(self.device, "diff", "--name-only", start, end), "")
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        self.assertEqual(manager._outgoing_paths(start, end), ["transient.txt"])

    def test_preflight_disabled(self):
        result = SyncManager(SyncConfig(memory_root=self.device, enabled=False)).pull()

        self.assertEqual(result.status, "disabled")
        self.assertIn("disabled", result.message)

    def test_sync_paths_include_shared_view_registry_and_provider_definitions(self):
        from rightmemory.sync import MEMORY_SYNC_PATHS, _is_sync_path

        self.assertIn(".gitignore", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shares.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/view.md", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/retriever.md", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/recipe.toml", MEMORY_SYNC_PATHS)
        self.assertIn("shared_views/*/question.toml", MEMORY_SYNC_PATHS)
        self.assertIn("PURSUITS.md", MEMORY_SYNC_PATHS)
        self.assertIn("PURSUIT_*.md", MEMORY_SYNC_PATHS)
        self.assertIn("corrections.md", MEMORY_SYNC_PATHS)
        self.assertIn("learning/**", MEMORY_SYNC_PATHS)
        self.assertTrue(_is_sync_path(f"learning/candidates/{'a' * 64}.json"))
        self.assertTrue(_is_sync_path(f"learning/processed/memory/{'b' * 64}.json"))
        self.assertTrue(_is_sync_path(f"learning/withdrawn/{'c' * 64}.json"))
        self.assertTrue(_is_sync_path("learning/claims/memory.json"))
        self.assertFalse(_is_sync_path("learning/candidates/not-an-identity.json"))
        self.assertFalse(_is_sync_path("learning/claims/unknown.json"))

    def test_preflight_accepts_incoming_root_gitignore_change(self):
        gitignore = "*\n!MEMORY.md\n!PURSUITS.md\n"
        (self.other / ".gitignore").write_text(gitignore, encoding="utf-8")
        self._git(self.other, "add", "-f", ".gitignore")
        self._git(self.other, "commit", "-m", "sync: update managed gitignore")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        self.assertEqual((self.device / ".gitignore").read_text(encoding="utf-8"), gitignore)

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
        (nested / "MEMORY.md").write_text(
            "# Domain\n\n- `one` nested memory → []\n",
            encoding="utf-8",
        )
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

        result = SyncManager(SyncConfig(memory_root=nested, enabled=True)).pull()

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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        self.assertIn("two", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_preflight_fast_forwards_pursuit_detail(self):
        pursuit_detail = "# Pursuit Detail\n\nRemote pursuit update.\n"
        (self.other / "PURSUIT_remote.md").write_text(pursuit_detail, encoding="utf-8")
        pursuits = (self.other / "PURSUITS.md").read_text(encoding="utf-8")
        (self.other / "PURSUITS.md").write_text(
            pursuits + "\n## Remote {F#remote}\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "PURSUITS.md", "PURSUIT_remote.md")
        self._git(self.other, "commit", "-m", "sync: update pursuit detail")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        self.assertEqual(
            (self.device / "PURSUIT_remote.md").read_text(encoding="utf-8"),
            pursuit_detail,
        )

    def test_preflight_transports_shared_evidence_and_independent_completion(self):
        candidate = submit_learning(self.other, "agent-session", "durable candidate evidence")
        record_learning_processed(self.other, "memory", [candidate.candidate_id])
        self._git(self.other, "add", "-f", "learning")
        self._git(self.other, "commit", "-m", "learning: process memory evidence")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        self.assertEqual(list_learning_candidates(self.device, domain="memory"), ())
        self.assertEqual(list_learning_candidates(self.device, domain="guidance")[0].candidate_id,
                         candidate.candidate_id)

    def test_preflight_reports_dirty_memory_without_merging(self):
        (self.other / "MEMORY.md").write_text(
            "# Domain\n\n- `one` first → []\n- `two` remote only → []\n",
            encoding="utf-8",
        )
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n- `one` local dirty → []\n",
            encoding="utf-8",
        )

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])
        memory = (self.device / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("local dirty", memory)
        self.assertNotIn("remote only", memory)

    def test_preflight_reports_each_new_synchronized_state_path_as_dirty(self):
        for name in (
            "PURSUITS.md",
            "corrections.md",
        ):
            with self.subTest(name=name):
                path = self.device / name
                existed = path.exists()
                original = path.read_text(encoding="utf-8") if existed else None
                path.write_text("local synchronized state\n", encoding="utf-8")

                result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "conflict")
        self.assertIn("duplicate id `one`", result.message)

    def test_sync_transports_structured_corrections_over_updater_ceiling(self):
        (self.other / "corrections.md").write_text(
            self._corrections_markdown(11),
            encoding="utf-8",
        )
        self._git(self.other, "add", "corrections.md")
        self._git(self.other, "commit", "-m", "sync: transport correction union")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(result.status, "synced")
        text = (self.device / "corrections.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("## Entry "), 11)

    def test_preflight_reports_dirty_shared_view_registry_and_ignores_runtime_shared_views(self):
        runtime_cache = self.device / ".runtime" / "shared_views" / "cache" / "alice-auth-api.txt"
        runtime_cache.parent.mkdir(parents=True)
        runtime_cache.write_text("runtime cache\n", encoding="utf-8")
        registry = self.device / "shared_views.toml"
        registry.write_text(
            '[connections.alice-auth-api]\ntype = "file"\nref = "rightmemory://mf/current"\n',
            encoding="utf-8",
        )

        dirty = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()
        registry.unlink()
        clean = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

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

        dirty = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()
        (view_dir / "view.md").unlink()
        clean = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).pull()

        self.assertEqual(dirty.status, "dirty")
        self.assertEqual(dirty.files, ["shared_views/alice-auth-api/view.md"])
        self.assertEqual(clean.status, "synced")

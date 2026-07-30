from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.sync import SyncManager
from rightmemory.update_queue import UpdateCandidate
from rightmemory.update_record import UpdateRecord, UpdateRecordStore
from tests.sync_test_base import SyncTestBase


class SyncPreflightTests(SyncTestBase):
    def test_preflight_disabled(self):
        result = SyncManager(SyncConfig(memory_root=self.device, enabled=False)).preflight()

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
        self.assertNotIn("PURSUIT_RULES.md", MEMORY_SYNC_PATHS)
        self.assertNotIn("AGENT_CORRECTION_MEMORY_RULES.md", MEMORY_SYNC_PATHS)
        self.assertFalse(_is_sync_path("PURSUIT_RULES.md"))
        self.assertFalse(_is_sync_path("AGENT_CORRECTION_MEMORY_RULES.md"))
        self.assertIn("corrections.md", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/candidates/*.json", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/recovery/*.json", MEMORY_SYNC_PATHS)
        self.assertIn("update_queue/lease.json", MEMORY_SYNC_PATHS)
        self.assertIn("update_records/*.json", MEMORY_SYNC_PATHS)
        self.assertTrue(_is_sync_path(f"update_queue/candidates/{'a' * 32}.json"))
        self.assertTrue(_is_sync_path(f"update_queue/recovery/update-batch-{'b' * 64}.json"))
        self.assertTrue(_is_sync_path("update_queue/lease.json"))
        self.assertTrue(_is_sync_path(f"update_records/update-batch-{'d' * 64}.json"))
        self.assertFalse(_is_sync_path("update_queue/candidates/not-a-uuid.json"))
        self.assertFalse(_is_sync_path(f"update_queue/candidates/{'A' * 32}.json"))
        self.assertFalse(_is_sync_path(f"update_queue/recovery/{'b' * 64}.json"))
        self.assertFalse(_is_sync_path("update_queue/extra.json"))
        self.assertFalse(_is_sync_path("update_records/not-an-operation.json"))

    def test_preflight_accepts_incoming_root_gitignore_change(self):
        gitignore = "*\n!MEMORY.md\n!PURSUITS.md\n"
        (self.other / ".gitignore").write_text(gitignore, encoding="utf-8")
        self._git(self.other, "add", "-f", ".gitignore")
        self._git(self.other, "commit", "-m", "sync: update managed gitignore")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertEqual(
            (self.device / "PURSUIT_remote.md").read_text(encoding="utf-8"),
            pursuit_detail,
        )

    def test_preflight_transports_valid_retained_candidate_record(self):
        candidate = UpdateCandidate(
            uid="a" * 32,
            session_id="agent-session",
            display_id=1,
            message="durable candidate evidence",
            submitted_at="2026-07-27T12:00:00+00:00",
        )
        record = UpdateRecord.from_candidates((candidate,))
        path = UpdateRecordStore(self.other).write(record)
        self._git(self.other, "add", "-f", path.relative_to(self.other).as_posix())
        self._git(self.other, "commit", "-m", "update: retain candidate batch")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertEqual(UpdateRecordStore(self.device).read(record.operation_id), record)

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

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

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
        self.assertIn("preserve the overflow for later explicit direct maintenance", prompt)

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

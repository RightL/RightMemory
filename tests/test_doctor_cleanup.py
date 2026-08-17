import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.codex_app_server import CodexThreadDeleteResult
from rightmemory.config import AgentCliConfig, RuntimeConfig
from rightmemory.doctor import _check_codex_thread_cleanup
from rightmemory.provider_threads import ProviderThreadStore


class FakeDeleteClient:
    def __init__(self, memory_root):
        self.memory_root = memory_root

    def delete_threads(self, thread_ids):
        return [CodexThreadDeleteResult(thread_id, True) for thread_id in thread_ids]


class AgentCliDoctorCleanupTests(unittest.TestCase):
    def test_skips_cleanup_probe_when_no_role_uses_codex(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="retrieve",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="claude"),
                memory_root=root,
            )
            checks = []

            _check_codex_thread_cleanup(checks, {"retrieve": config})

        self.assertEqual(checks, [])

    def test_cleanup_probe_deletes_owned_codex_threads_through_cleanup_path(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="retrieve",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=root,
            )
            store = ProviderThreadStore(root)
            store.record_created(
                provider="codex",
                provider_session_id="doctor-thread",
                role="retrieve",
                rightmemory_session_id="doctor-session",
                policy="persistent",
                created_at="2026-08-17T00:00:00+00:00",
            )
            checks = []

            with patch("rightmemory.agent_cli_cleanup.CodexAppServerClient", FakeDeleteClient):
                _check_codex_thread_cleanup(checks, {"retrieve": config})

            remaining = store.load("codex", "doctor-thread")

        self.assertIsNone(remaining)
        self.assertEqual(len(checks), 1)
        self.assertTrue(checks[0].ok)


if __name__ == "__main__":
    unittest.main()

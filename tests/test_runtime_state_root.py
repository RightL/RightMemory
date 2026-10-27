from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from rightmemory.config import AgentCliConfig, RuntimeConfig
from rightmemory.runtime import RightMemoryRuntime


class RuntimeStateRootTests(unittest.TestCase):
    def test_runtime_state_stores_use_state_root_while_tools_use_memory_root(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            state_root = root / "state"
            memory_root.mkdir()
            config = RuntimeConfig(
                role="retrieve",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=memory_root,
                state_root=state_root,
                fresh_provider_session=False,
            )

            with patch("rightmemory.runtime.CliAgentExecutor") as executor_class:
                runtime = RightMemoryRuntime(config)
                try:
                    session_paths = runtime.sessions.paths("agent-1")
                    retrieve_context_path = runtime.retrieve_context._state_path("agent-1")
                finally:
                    runtime.cleanup()

        self.assertEqual(runtime.tools.memory_root, memory_root.resolve())
        self.assertEqual(session_paths.history, state_root / ".runtime" / "sessions" / "retrieve" / "agent-1.json")
        self.assertEqual(
            retrieve_context_path,
            state_root / ".runtime" / "retrieve_context" / "sessions" / "agent-1.json",
        )
        executor_class.assert_called_once_with(
            memory_root,
            "retrieve",
            AgentCliConfig(provider="codex"),
            codex_runner=runtime._codex_runner,
            state_root=state_root,
            fresh_provider_session=False,
            trace_event=runtime._trace,
        )


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

from rightmemory.entrypoint import main
from tests.isolated_write_test_base import IsolatedWriteTestBase


class AssistantCliTests(IsolatedWriteTestBase):
    def setUp(self):
        super().setUp()
        (self.root / "rightmemory.toml").write_text(
            "[update.async]\ntrigger_candidates = 1\ntarget_batch_candidates = 1\nmax_wait_seconds = 1\n"
        )

    def _call(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = main(["assistant", "--root", str(self.root), *args])
        return code, json.loads(output.getvalue())

    def test_cli_native_flow_binds_completion_to_active_root_from_worktree(self):
        evidence = self.root.parent / f"{self.root.name}-evidence.txt"
        evidence.write_text("Fictional project launch code is Atlas.")
        self.addCleanup(evidence.unlink, missing_ok=True)
        code, submitted = self._call("submit", "--session", "cli", "--request-id", "one", "--file", str(evidence))
        self.assertEqual(code, 0)
        self.assertTrue(submitted["update"]["ready"])
        code, task = self._call("update", "begin")
        self.assertEqual(code, 0)
        worktree = Path(task["worktree"])
        self._append_memory(worktree, "- `atlas` Fictional project launch code is Atlas. → []\n")
        self._git("add", "MEMORY.md", cwd=worktree)
        self._git("commit", "-m", "memory: CLI native edit", cwd=worktree)
        environment = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        result = subprocess.run(
            [sys.executable, "-m", "rightmemory.entrypoint", *task["complete_argv"][1:]],
            cwd=worktree, env=environment, text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        receipt = json.loads(result.stdout)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["landed_commit"], self._git("rev-parse", "HEAD"))
        self.assertIn("Atlas", (self.root / "MEMORY.md").read_text())

    def test_argument_and_core_errors_are_json_with_failure_exit(self):
        code, result = self._call("submit", "--session", "cli")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["type"], "ValueError")
        code, result = self._call("update", "complete", "--task", "../../outside")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertEqual(self._git("rev-parse", "HEAD"), self.initial_head)

    def test_validate_is_independent_and_reports_invalid_root(self):
        code, result = self._call("validate")
        self.assertEqual(code, 0)
        self.assertTrue(result["ok"])
        (self.root / "PURSUITS.md").unlink()
        code, result = self._call("validate")
        self.assertEqual(code, 1)
        self.assertFalse(result["ok"])
        self.assertTrue(result["document_errors"])

    def test_new_entry_points_do_not_import_any_model_runtime(self):
        script = r'''
import contextlib
import importlib.abc
import io
import json
import sys

class RejectModelRuntime(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        blocked = ("pydantic_ai", "rightmemory.cli", "rightmemory.runtime", "rightmemory.codex_sdk", "rightmemory.agent_cli")
        if any(fullname == name or fullname.startswith(name + ".") for name in blocked):
            raise ImportError("unexpected model/runtime dependency: " + fullname)

sys.meta_path.insert(0, RejectModelRuntime())
from rightmemory.entrypoint import main
root = sys.argv[1]
with contextlib.redirect_stdout(io.StringIO()):
    assert main(["validate", "--root", root]) == 0
for command in (["context", "--session", "reader"], ["update", "status"], ["sync", "refresh"], ["validate"]):
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        assert main(["assistant", "--root", root, *command]) == 0
    assert json.loads(captured.getvalue())["ok"]
print("ok")
'''
        result = subprocess.run(
            [sys.executable, "-c", script, str(self.root)], text=True, capture_output=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        self.assertEqual(result.stdout.strip(), "ok")

# Global Memory Sync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build automatic local-first global memory sync over Git, with runtime-assisted preflight, scheduled freshness pulls, and a narrow sync reconciler role for background conflicts.

**Architecture:** Add sync config and state, a deterministic `SyncManager` for Git transport, runtime sync-context injection for write roles, a sync push tool for write-capable roles, and a `sync-reconciler` role for scheduled conflicts. Reuse the existing watch manager for periodic sync instead of creating an unrelated daemon.

**Tech Stack:** Python 3.11, stdlib `subprocess`/`json`/`dataclasses`, Git CLI, existing `unittest` suite, existing RightMemory runtime and prompt system.

---

## File Structure

- Create `rightmemory/sync.py`: deterministic Git sync manager, sync state persistence, result formatting, and conflict message formatting.
- Create `rightmemory/prompts/sync-reconciler.md`: narrow role prompt for resolving sync conflicts.
- Create `tests/test_sync.py`: local bare-repo tests for sync state, preflight, push, and conflict detection.
- Modify `rightmemory/config.py`: add `SyncConfig`, `load_sync_config()`, `[sync]` parsing, and `sync-reconciler` as a runtime role.
- Modify `rightmemory/runtime.py`: run preflight before active write roles, inject compact sync context into the model input, and expose a `sync_push` tool to write-capable roles.
- Modify `rightmemory/prompt.py`: include `sync-reconciler` and add common sync-context guidance for write-capable roles.
- Modify `rightmemory/cli.py`: add internal `rightmemory sync watch`, load sync config for managed watches, and invoke `sync-reconciler` for scheduled conflicts.
- Modify `rightmemory/watch.py`: add `sync` as a managed watch target.
- Modify `tests/test_config.py`, `tests/test_cli.py`, and `tests/test_tools.py` where existing assumptions need updates.
- Modify `README.md` and `DESIGN_NOTES.md`: document automatic sync behavior, local `.runtime/sync` state, and the sync reconciler boundary.

## Task 1: Parse Sync Config And Register The Reconciler Role

**Files:**
- Modify: `rightmemory/config.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing config tests**

Add these tests to `tests/test_config.py` inside `ConfigTests`:

```python
    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_defaults_to_disabled(self):
        config_path = self._write_config("")

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_sync_config()

        self.assertFalse(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 24)
        self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_enabled(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true
            stale_pull_after_hours = 12
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_sync_config()

        self.assertTrue(config.enabled)
        self.assertEqual(config.stale_pull_after_hours, 12)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_sync_config_rejects_unknown_key(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true
            remote = "origin"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_sync_config()

        self.assertIn("unsupported [sync] config key(s): remote", str(caught.exception))

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_load_config_allows_sync_section_and_reconciler_role(self):
        config_path = self._write_config(
            """
            [sync]
            enabled = true

            [sync-reconciler.model]
            model_id = "openai/reconciler"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            config = load_config("sync-reconciler")

        self.assertEqual(config.role, "sync-reconciler")
        self.assertEqual(config.model_id, "openai/reconciler")
        self.assertTrue(config.sync.enabled)
```

Update the import at the top:

```python
from rightmemory.config import RuntimeConfig, load_config, load_review_config, load_sync_config
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_sync_config_defaults_to_disabled tests.test_config.ConfigTests.test_sync_config_enabled tests.test_config.ConfigTests.test_sync_config_rejects_unknown_key tests.test_config.ConfigTests.test_load_config_allows_sync_section_and_reconciler_role
```

Expected: fail because `load_sync_config`, `SyncConfig`, and the `sync-reconciler` role do not exist.

- [ ] **Step 3: Implement config parsing**

In `rightmemory/config.py`, add:

```python
DEFAULT_SYNC_STALE_PULL_HOURS = 24
ROLES = {"dreamer", "retrieve", "reviewer", "sync-reconciler", "update"}


@dataclass(frozen=True)
class SyncConfig:
    memory_root: Path = MEMORY_ROOT
    enabled: bool = False
    stale_pull_after_hours: int = DEFAULT_SYNC_STALE_PULL_HOURS
```

Add `sync` to the allowed top-level keys in `load_config()` and `load_review_config()`:

```python
_reject_unknown_keys(data, {*ROLES, "review", "debug", "sync"}, "top-level")
```

Add `sync=_sync_config(data.get("sync", {}))` to the `RuntimeConfig(...)` constructor call.

Add a field to `RuntimeConfig`:

```python
sync: SyncConfig = field(default_factory=SyncConfig)
```

Add these functions below `load_review_config()`:

```python
def load_sync_config() -> SyncConfig:
    data = _load_raw_config()
    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")
    _reject_unknown_keys(data, {*ROLES, "review", "debug", "sync"}, "top-level")
    return _sync_config(data.get("sync", {}))


def _sync_config(section: object) -> SyncConfig:
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[sync] must be a TOML table")
    _reject_unknown_keys(section, {"enabled", "stale_pull_after_hours"}, "[sync]")

    enabled = section.get("enabled", False)
    if not isinstance(enabled, bool):
        raise ValueError("[sync].enabled must be a boolean")

    stale_pull_after_hours = section.get("stale_pull_after_hours", DEFAULT_SYNC_STALE_PULL_HOURS)
    if not isinstance(stale_pull_after_hours, int) or stale_pull_after_hours < 1:
        raise ValueError("[sync].stale_pull_after_hours must be a positive integer")

    return SyncConfig(
        memory_root=MEMORY_ROOT,
        enabled=enabled,
        stale_pull_after_hours=stale_pull_after_hours,
    )
```

- [ ] **Step 4: Run focused config tests**

Run the same command from Step 2.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/config.py tests/test_config.py
git commit -m "feat: add sync config"
```

## Task 2: Add Sync State And Deterministic Git Sync Manager

**Files:**
- Create: `rightmemory/sync.py`
- Create: `tests/test_sync.py`

- [ ] **Step 1: Write failing sync manager tests**

Create `tests/test_sync.py`:

```python
import json
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rightmemory.config import SyncConfig
from rightmemory.sync import SyncManager


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
        self._git(self.device, "add", "MEMORY.md")
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

    def test_preflight_fast_forwards_clean_repo(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` first → []\n- `two` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote memory")
        self._git(self.other, "push")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "synced")
        self.assertIn("two", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_preflight_reports_dirty_memory_without_merging(self):
        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local dirty → []\n", encoding="utf-8")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).preflight()

        self.assertEqual(result.status, "dirty")
        self.assertEqual(result.files, ["MEMORY.md"])

    def test_push_merges_remote_change_and_reports_conflict(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        result = SyncManager(SyncConfig(memory_root=self.device, enabled=True)).push()

        self.assertEqual(result.status, "conflict")
        self.assertEqual(result.files, ["MEMORY.md"])
        self.assertIn("<<<<<<<", (self.device / "MEMORY.md").read_text(encoding="utf-8"))

    def test_state_records_successful_pull(self):
        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        result = manager.preflight()

        state = json.loads((self.device / ".runtime" / "sync" / "state.json").read_text(encoding="utf-8"))

        self.assertEqual(result.status, "synced")
        self.assertIn("last_successful_pull_at", state)
        parsed = datetime.fromisoformat(state["last_successful_pull_at"])
        self.assertEqual(parsed.tzinfo, UTC)

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

    def _git(self, cwd: Path, *args: str) -> str:
        process = subprocess.run(
            ["git", *args],
            cwd=cwd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if process.returncode != 0:
            raise AssertionError(process.stderr)
        return process.stdout.strip()
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_sync
```

Expected: fail because `rightmemory.sync` does not exist.

- [ ] **Step 3: Implement `rightmemory/sync.py`**

Create `rightmemory/sync.py`:

```python
from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import SyncConfig
from .session import _ensure_runtime_gitignore, _fsync_directory


MEMORY_SYNC_PATHS = ("MEMORY.md", "MEMORY_*.md", "dream_logs/*.md")


@dataclass(frozen=True)
class SyncResult:
    status: str
    message: str
    files: list[str] = field(default_factory=list)

    def context_block(self) -> str:
        files = ", ".join(self.files) if self.files else "none"
        return (
            "Runtime sync context (authoritative at turn start):\n"
            f"- status: {self.status}\n"
            f"- files: {files}\n"
            f"- message: {self.message}\n"
            "Treat this as the current sync state for the start of the turn. "
            "Use sync tools again after your own edits or after a later tool result changes sync state.\n"
        )


class SyncManager:
    def __init__(self, config: SyncConfig):
        self.config = config
        self.memory_root = config.memory_root
        self.state_path = self.memory_root / ".runtime" / "sync" / "state.json"

    def preflight(self) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")
        if not self._is_git_repo():
            return self._record_failure("unconfigured", "memory root is not a git repository")
        upstream = self._upstream()
        if upstream is None:
            return self._record_failure("unconfigured", "memory repo has no upstream branch")
        conflicts = self._conflicted_memory_files()
        if conflicts:
            return self._record_failure("conflict", "memory repo already has unresolved sync conflicts", conflicts)
        dirty = self._dirty_memory_files()
        if dirty:
            return SyncResult("dirty", "memory files have local uncommitted changes; commit them before sync", dirty)
        fetch = self._git("fetch", "--prune")
        if fetch.returncode != 0:
            return self._record_failure("offline", fetch.stderr.strip() or "git fetch failed")
        ahead, behind = self._ahead_behind(upstream)
        if behind == 0:
            return self._record_success("synced", "local memory is current")
        if ahead == 0:
            merge = self._git("merge", "--ff-only", upstream)
            if merge.returncode == 0:
                return self._record_success("synced", "fast-forwarded local memory")
            return self._record_failure("error", merge.stderr.strip() or "fast-forward failed")
        merge = self._git("merge", "--no-edit", upstream)
        if merge.returncode == 0:
            return self._record_success("synced", "merged remote memory changes")
        conflicts = self._conflicted_memory_files()
        if conflicts:
            return self._record_failure("conflict", "remote memory changes conflict with local commits", conflicts)
        return self._record_failure("error", merge.stderr.strip() or "git merge failed")

    def push(self) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")
        upstream = self._upstream()
        if upstream is None:
            return self._record_failure("unconfigured", "memory repo has no upstream branch")
        push = self._git("push")
        if push.returncode == 0:
            return self._record_success("pushed", "pushed memory changes")
        fetch = self._git("fetch", "--prune")
        if fetch.returncode != 0:
            return self._record_failure("offline", fetch.stderr.strip() or "git fetch failed after push rejection")
        merge = self._git("merge", "--no-edit", upstream)
        if merge.returncode == 0:
            retry = self._git("push")
            if retry.returncode == 0:
                return self._record_success("pushed", "merged remote changes and pushed memory")
            return self._record_failure("push-failed", retry.stderr.strip() or "git push failed")
        conflicts = self._conflicted_memory_files()
        if conflicts:
            return self._record_failure("conflict", "push rejected and merge created memory conflicts", conflicts)
        return self._record_failure("error", merge.stderr.strip() or "merge after push rejection failed")

    def background_pull(self) -> SyncResult:
        if not self.config.enabled:
            return SyncResult("disabled", "sync disabled")
        state = self._read_state()
        last_pull = state.get("last_successful_pull_at")
        if isinstance(last_pull, str):
            try:
                parsed = datetime.fromisoformat(last_pull)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            if parsed is not None:
                next_pull = parsed.astimezone(UTC) + timedelta(hours=self.config.stale_pull_after_hours)
                if next_pull > datetime.now(UTC):
                    return SyncResult("fresh", "last successful pull is still fresh")
        return self.preflight()

    def conflict_message(self, result: SyncResult) -> str:
        files = "\n".join(f"- {path}" for path in result.files) or "- none"
        return (
            "Resolve the current RightMemory sync conflict.\n\n"
            f"{result.context_block()}\n"
            "Conflicted files:\n"
            f"{files}\n\n"
            "Read the conflicted memory files, resolve the conflict markers, validate memory, commit the resolved "
            "state, then push the memory repo."
        )

    def _is_git_repo(self) -> bool:
        return self._git("rev-parse", "--is-inside-work-tree").returncode == 0

    def _upstream(self) -> str | None:
        result = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _ahead_behind(self, upstream: str) -> tuple[int, int]:
        result = self._git("rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if result.returncode != 0:
            return (0, 0)
        left, right = result.stdout.strip().split()
        return int(left), int(right)

    def _dirty_memory_files(self) -> list[str]:
        result = self._git("status", "--porcelain", "--", *MEMORY_SYNC_PATHS)
        return _status_paths(result.stdout)

    def _conflicted_memory_files(self) -> list[str]:
        result = self._git("diff", "--name-only", "--diff-filter=U", "--", *MEMORY_SYNC_PATHS)
        return sorted(line for line in result.stdout.splitlines() if line)

    def _record_success(self, status: str, message: str) -> SyncResult:
        state = self._read_state()
        state.update(
            {
                "last_status": status,
                "last_message": message,
                "last_successful_pull_at": datetime.now(UTC).isoformat(),
                "last_error_at": None,
                "conflicted_files": [],
            }
        )
        self._write_state(state)
        return SyncResult(status, message)

    def _record_failure(self, status: str, message: str, files: list[str] | None = None) -> SyncResult:
        files = files or []
        state = self._read_state()
        state.update(
            {
                "last_status": status,
                "last_message": message,
                "last_error_at": datetime.now(UTC).isoformat(),
                "conflicted_files": files,
            }
        )
        self._write_state(state)
        return SyncResult(status, message, files)

    def _read_state(self) -> dict[str, object]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _write_state(self, data: dict[str, object]) -> None:
        _ensure_runtime_gitignore(self.memory_root / ".runtime")
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.state_path)
        _fsync_directory(self.state_path.parent)

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *args],
            cwd=self.memory_root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )


def _status_paths(output: str) -> list[str]:
    paths: list[str] = []
    for line in output.splitlines():
        if not line:
            continue
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path)
    return sorted(paths)
```

- [ ] **Step 4: Run focused sync tests**

Run:

```bash
python -m unittest tests.test_sync
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/sync.py tests/test_sync.py
git commit -m "feat: add git sync manager"
```

## Task 3: Inject Runtime Sync Context Before Active Write Roles

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing runtime tests**

Add these tests inside `RuntimeTests` in `tests/test_config.py`:

```python
    def test_update_turn_includes_runtime_sync_context(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            manager_class.return_value.preflight.return_value.context_block.return_value = "Runtime sync context\n- status: synced\n"
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "remember one")

        message = runtime.agent.calls[0]["message"]
        self.assertIn("Runtime sync context", message)
        self.assertIn("remember one", message)

    def test_retrieve_turn_does_not_run_sync_preflight(self):
        config = RuntimeConfig(
            role="retrieve",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with (
            patch.dict("sys.modules", self._fake_pydantic_modules()),
            patch("rightmemory.runtime.SyncManager") as manager_class,
        ):
            runtime = RightMemoryRuntime(config)
            runtime.run_session_turn("agent-session", "find one")

        manager_class.assert_not_called()
```

Add this helper near the bottom of `tests/test_config.py`:

```python
def load_sync_config_for_test(memory_root: Path, enabled: bool):
    from rightmemory.config import SyncConfig

    return SyncConfig(memory_root=memory_root, enabled=enabled)
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_update_turn_includes_runtime_sync_context tests.test_config.RuntimeTests.test_retrieve_turn_does_not_run_sync_preflight
```

Expected: fail because runtime does not use `SyncManager`.

- [ ] **Step 3: Implement sync context injection**

In `rightmemory/runtime.py`, import:

```python
from .sync import SyncManager
```

Add near `RECOVERABLE_TOOL_ERRORS`:

```python
SYNC_PREFLIGHT_ROLES = {"dreamer", "reviewer", "update"}
```

In `RightMemoryRuntime.__init__`, add:

```python
self._sync_manager: SyncManager | None = None
```

Add methods:

```python
    def _sync(self) -> SyncManager:
        if self._sync_manager is None:
            self._sync_manager = SyncManager(self.config.sync)
        return self._sync_manager

    def _prepare_message(self, message: str) -> str:
        if self.config.role not in SYNC_PREFLIGHT_ROLES or not self.config.sync.enabled:
            return message
        result = self._sync().preflight()
        return f"{result.context_block()}\nCaller message:\n{message}"
```

In `run_turn()` and `run_session_turn()`, call:

```python
prepared_message = self._prepare_message(message)
```

Pass `prepared_message` into `self.agent.run_sync(...)`, while keeping trace field `message=message` so debug logs preserve the original caller input.

- [ ] **Step 4: Run focused runtime tests**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_update_turn_includes_runtime_sync_context tests.test_config.RuntimeTests.test_retrieve_turn_does_not_run_sync_preflight
```

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/runtime.py tests/test_config.py
git commit -m "feat: inject sync context for write roles"
```

## Task 4: Add Sync Push Tool And Prompt Guidance

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/prompts/update.md`
- Modify: `rightmemory/prompts/dreamer.md`
- Modify: `rightmemory/prompts/reviewer.md`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing prompt and tool tests**

Add these tests to `RuntimeTests` in `tests/test_config.py`:

```python
    def test_write_roles_receive_sync_push_tool_when_sync_enabled(self):
        config = RuntimeConfig(
            role="update",
            model_id="openai/test",
            memory_root=Path(self.tempdir.name),
            sync=load_sync_config_for_test(Path(self.tempdir.name), enabled=True),
        )

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = [tool.__name__ for tool in runtime.agent.tools]
        self.assertIn("sync_push", tool_names)

    def test_sync_prompt_guidance_says_preflight_is_current(self):
        instructions = build_instructions(Path("/memory"), "update")

        self.assertIn("Runtime sync context", instructions)
        self.assertIn("already performed sync preflight", instructions)
        self.assertIn("avoid repeating preflight discovery", instructions)
```

If the fake `Agent` in `_fake_pydantic_modules()` does not store `tools`, update it so the constructor assigns `self.tools = tools`.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_write_roles_receive_sync_push_tool_when_sync_enabled tests.test_config.RuntimeTests.test_sync_prompt_guidance_says_preflight_is_current
```

Expected: fail because `sync_push` and prompt guidance do not exist.

- [ ] **Step 3: Implement sync push tool**

In `rightmemory/runtime.py`, add:

```python
SYNC_TOOL_ROLES = {"dreamer", "reviewer", "sync-reconciler", "update"}
```

Add method:

```python
    def sync_push(self) -> str:
        """Push committed memory changes to the configured Git upstream."""
        result = self._sync().push()
        return result.context_block()
```

In `_agent_tools()`, after building write tools, append `self._agent_tool(self.sync_push)` when `self.config.sync.enabled` and `self.config.role in SYNC_TOOL_ROLES`.

- [ ] **Step 4: Implement common prompt guidance**

In `rightmemory/prompt.py`, add a `sync_guidance = _sync_guidance(role)` line in `build_instructions()` and include it after `Command-selected behavior`.

Add:

```python
def _sync_guidance(role: str) -> str:
    if role == "retrieve":
        return "- Retrieval uses local memory and does not perform sync preflight by default."
    if role in {"dreamer", "reviewer", "sync-reconciler", "update"}:
        return (
            "- If the caller message contains a Runtime sync context block, the runtime already performed sync "
            "preflight for this turn. Treat that block as current at turn start and avoid repeating preflight "
            "discovery. Use sync tools again after your own edits or after a later tool result changes sync state.\n"
            "- When sync is enabled and you commit memory changes, call `sync_push` after the commit. If `sync_push` "
            "reports a conflict, resolve the conflicted memory files in the same role, validate memory, commit the "
            "resolved state, and call `sync_push` again."
        )
    return ""
```

In `rightmemory/prompts/update.md`, replace:

```md
- Do not commit routine update edits after your own write unless the caller explicitly asks you to commit.
```

with:

```md
- In a local update with no runtime sync context, leave routine update edits uncommitted unless the caller asks for a commit.
- When runtime sync context is present, commit the memory edit and call `sync_push` so the global memory can update.
```

In `rightmemory/prompts/dreamer.md` and `rightmemory/prompts/reviewer.md`, add one sentence under commit guidance:

```md
- When runtime sync context is present, call `sync_push` after a successful memory commit.
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_write_roles_receive_sync_push_tool_when_sync_enabled tests.test_config.RuntimeTests.test_sync_prompt_guidance_says_preflight_is_current
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/runtime.py rightmemory/prompt.py rightmemory/prompts/update.md rightmemory/prompts/dreamer.md rightmemory/prompts/reviewer.md tests/test_config.py
git commit -m "feat: add sync push guidance"
```

## Task 5: Add Sync Reconciler Role

**Files:**
- Create: `rightmemory/prompts/sync-reconciler.md`
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing role prompt test**

Add this test to `RuntimeTests`:

```python
    def test_sync_reconciler_instructions_are_narrow(self):
        instructions = build_instructions(Path("/memory"), "sync-reconciler")

        self.assertIn("RightMemory sync conflicts", instructions)
        self.assertIn("preserve coherent durable memory from both sides", instructions)
        self.assertIn("Final replies should include resolved files", instructions)
```

- [ ] **Step 2: Run test to verify failure**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_sync_reconciler_instructions_are_narrow
```

Expected: fail because the prompt file and command guidance are missing.

- [ ] **Step 3: Implement prompt routing**

In `rightmemory/prompt.py`, update:

```python
ROLE_PROMPTS = {"dreamer", "retrieve", "reviewer", "sync-reconciler", "update"}
```

Add command guidance:

```python
    if role == "sync-reconciler":
        return (
            "- The sync watcher selected sync reconciliation behavior. Treat the caller message as the complete "
            "current sync-conflict context for this run.\n"
            "- Resolve RightMemory sync conflicts in the memory file set and finish by committing and pushing the "
            "resolved memory state."
        )
```

- [ ] **Step 4: Create `sync-reconciler` prompt**

Create `rightmemory/prompts/sync-reconciler.md`:

```md
# Sync Reconciler Role

## Scope

- You resolve RightMemory sync conflicts in the memory file set: `MEMORY.md`, sibling `MEMORY_*.md` files, and `dream_logs/*.md` when present.
- The runtime has already detected the sync state and conflicted files. Treat the runtime sync context in the caller message as current at turn start.
- Read each conflicted memory file before editing it.
- Resolve conflict markers by preserving coherent durable memory from both sides.
- Keep edits focused on the conflicted memory state and the schema supplied by the execution wrapper.

## Conflict Resolution

- Prefer a final memory shape that would make sense if it had been written freshly.
- Merge duplicate facts when the two sides describe the same durable idea.
- Preserve both facts when they are compatible but distinct.
- Keep contradictory facts visible when the conflict cannot be settled from the file context.
- Maintain heading anchors, node ids, detail-file pointers, and graph edges according to the schema.

## Validation And Sync

- After editing, run validation and fix schema errors in the touched memory files.
- Stage and commit resolved memory files with a concise sync message.
- Call `sync_push` after the commit.
- If `sync_push` reports a new conflict, resolve it in the same way, validate, commit, and call `sync_push` again.

## Final Reply

- Final replies should include resolved files, validation result, commit hash if available, push result, and any conflict that could not be settled from memory context.
```

- [ ] **Step 5: Run focused role test**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_sync_reconciler_instructions_are_narrow
```

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/prompt.py rightmemory/prompts/sync-reconciler.md tests/test_config.py
git commit -m "feat: add sync reconciler role"
```

## Task 6: Add Managed Sync Watch Target

**Files:**
- Modify: `rightmemory/watch.py`
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing watch tests**

Add to `tests/test_cli.py`:

```python
    def test_watch_start_starts_sync_when_enabled(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config():
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": True})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.subprocess.Popen", side_effect=[FakeProcess(101), FakeProcess(102), FakeProcess(103)]) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

            sync_pid = (memory_root / ".runtime" / "watch" / "sync.pid").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 3)
        self.assertEqual(sync_pid, "103\n")
        self.assertIn("sync: running pid 103", stdout.getvalue())

    def test_watch_start_skips_sync_when_disabled(self):
        stdout = io.StringIO()

        class FakeProcess:
            def __init__(self, pid):
                self.pid = pid

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)

            def fake_load_config(role):
                return type("Config", (), {"memory_root": memory_root})()

            def fake_load_sync_config():
                return type("SyncConfig", (), {"memory_root": memory_root, "enabled": False})()

            with (
                patch("rightmemory.cli.load_config", fake_load_config),
                patch("rightmemory.cli.load_sync_config", fake_load_sync_config),
                patch("rightmemory.watch.subprocess.Popen", side_effect=[FakeProcess(101), FakeProcess(102)]) as popen,
                patch("sys.stdout", stdout),
            ):
                result = main(["watch", "start"])

        self.assertEqual(result, 0)
        self.assertEqual(popen.call_count, 2)
        self.assertIn("sync: disabled", stdout.getvalue())
```

Update the existing `test_watch_start_starts_review_and_dreamer_managed_processes` to patch `rightmemory.cli.load_sync_config` with a disabled sync config and to accept a `sync: disabled` line in stdout. Update `test_watch_status_reports_stopped_without_config` to expect `sync: stopped` in output.

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_watch_start_starts_sync_when_enabled tests.test_cli.JsonRequestTests.test_watch_start_skips_sync_when_disabled tests.test_cli.JsonRequestTests.test_watch_status_reports_stopped_without_config
```

Expected: fail because `sync` is not a managed watch target.

- [ ] **Step 3: Update watch target registry**

In `rightmemory/watch.py`, change:

```python
MANAGED_WATCH_TARGETS = ("review", "dreamer", "sync")
WATCH_COMMANDS = {
    "review": ("review", "watch"),
    "dreamer": ("dreamer", "watch"),
    "sync": ("sync", "watch"),
}
```

- [ ] **Step 4: Update CLI watch start**

In `rightmemory/cli.py`, import `load_sync_config`.

Update `_watch_start()`:

```python
def _watch_start(target: str) -> int:
    for name in _watch_targets(target):
        try:
            if name == "sync":
                sync_config = load_sync_config()
                if not sync_config.enabled:
                    print("sync: disabled")
                    continue
                memory_root = sync_config.memory_root
            else:
                config = load_config(_watch_role(name))
                memory_root = config.memory_root
            status = start_managed_watch(memory_root, name, sys.executable)
            print(_format_watch_status(status))
        except Exception as exc:
            print(f"{name}: error: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
    return 0
```

Leave `_watch_role()` handling `review` and `dreamer`; it should raise for other names.

- [ ] **Step 5: Run focused watch tests**

Run the command from Step 2.

Expected: pass.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/watch.py rightmemory/cli.py tests/test_cli.py
git commit -m "feat: add sync watch target"
```

## Task 7: Implement Scheduled Sync Watch And Reconciler Invocation

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing scheduled sync tests**

Add to `tests/test_cli.py`:

```python
    def test_sync_watch_clean_pull_does_not_load_runtime(self):
        stdout = io.StringIO()
        stderr = io.StringIO()

        with tempfile.TemporaryDirectory() as tempdir:
            config = type("SyncConfig", (), {"memory_root": Path(tempdir), "enabled": True, "stale_pull_after_hours": 24})()
            result_obj = type("Result", (), {"status": "synced", "message": "local memory is current", "files": []})()

            with (
                patch("rightmemory.cli.load_sync_config", return_value=config),
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not load")),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_pull.return_value = result_obj
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        self.assertIn("rightmemory sync check", stdout.getvalue())
        self.assertIn("local memory is current", stdout.getvalue())

    def test_sync_watch_conflict_invokes_sync_reconciler(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        calls = []

        class RecordingRuntime(FakeRuntime):
            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message))
                return "resolved"

        with tempfile.TemporaryDirectory() as tempdir:
            memory_root = Path(tempdir)
            sync_config = type("SyncConfig", (), {"memory_root": memory_root, "enabled": True, "stale_pull_after_hours": 24})()
            reconciler_config = type("Config", (), {"memory_root": memory_root})()
            result_obj = type("Result", (), {"status": "conflict", "message": "conflict", "files": ["MEMORY.md"]})()

            with (
                patch("rightmemory.cli.load_sync_config", return_value=sync_config),
                patch("rightmemory.cli.load_config", return_value=reconciler_config) as load_config,
                patch("rightmemory.cli.SyncManager") as manager_class,
                patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
                patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
                patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt),
                patch("sys.stdout", stdout),
                patch("sys.stderr", stderr),
            ):
                manager_class.return_value.background_pull.return_value = result_obj
                manager_class.return_value.conflict_message.return_value = "resolve MEMORY.md"
                result = main(["sync", "watch", "--interval", "60"])

        self.assertEqual(result, 130)
        load_config.assert_called_with("sync-reconciler")
        self.assertEqual(calls, [("sync-watch", "resolve MEMORY.md")])
        self.assertIn("resolved", stdout.getvalue())
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_sync_watch_clean_pull_does_not_load_runtime tests.test_cli.JsonRequestTests.test_sync_watch_conflict_invokes_sync_reconciler
```

Expected: fail because `sync watch` is not implemented.

- [ ] **Step 3: Implement sync CLI entry**

In `rightmemory/cli.py`, import `SyncManager`.

At the top of `main()` add:

```python
if argv and argv[0] == "sync":
    return _sync_main(argv[1:])
```

Add constants:

```python
SYNC_WATCH_SESSION_ID = "sync-watch"
```

Add parser and watch function:

```python
def _sync_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    watch = subparsers.add_parser("watch", help="run automatic sync freshness checks")
    watch.add_argument("--interval", type=int, default=3600, help="seconds between freshness checks")
    args = parser.parse_args(argv)
    if args.command == "watch":
        return _sync_watch(args.interval)
    raise ValueError(f"unknown sync command: {args.command}")


def _sync_watch(interval: int) -> int:
    if interval < 1:
        raise ValueError("--interval must be a positive integer")
    sync_config = load_sync_config()
    refresh = InstallStamp(sync_config.memory_root)
    try:
        with _watch_stop_signal("sync") as stop, WatchLock(sync_config.memory_root, "sync"):
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)
                timestamp = datetime.now(UTC).isoformat()
                print(f"[{timestamp}] rightmemory sync check", flush=True)
                manager = SyncManager(sync_config)
                result = manager.background_pull()
                print(result.message, flush=True)
                if result.status == "conflict":
                    _run_sync_reconciler(sync_config.memory_root, manager, result)
                if not _sleep_with_refresh_check(interval, refresh, stop):
                    break
        print("rightmemory sync watch stopped", file=sys.stderr)
        return 0
    except KeyboardInterrupt:
        print("rightmemory sync watch stopped", file=sys.stderr)
        return 130


def _run_sync_reconciler(memory_root: Path, manager: SyncManager, result: Any) -> None:
    reconciler_config = load_config("sync-reconciler")
    runtime = RightMemoryRuntime(reconciler_config)
    try:
        print(runtime.run_session_turn(SYNC_WATCH_SESSION_ID, manager.conflict_message(result)), flush=True)
    finally:
        runtime.cleanup()
```

- [ ] **Step 4: Run focused scheduled sync tests**

Run the command from Step 2.

Expected: pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: run scheduled sync"
```

## Task 8: Verify Conflict Resolution Tooling With A Fixture

**Files:**
- Modify: `tests/test_sync.py`

- [ ] **Step 1: Add an integration-style conflict fixture test**

Add to `SyncManagerTests`:

```python
    def test_conflict_can_be_resolved_committed_and_pushed(self):
        (self.other / "MEMORY.md").write_text("# Domain\n\n- `one` remote durable fact → []\n", encoding="utf-8")
        self._git(self.other, "add", "MEMORY.md")
        self._git(self.other, "commit", "-m", "remote edit")
        self._git(self.other, "push")

        (self.device / "MEMORY.md").write_text("# Domain\n\n- `one` local durable fact → []\n", encoding="utf-8")
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "local edit")

        manager = SyncManager(SyncConfig(memory_root=self.device, enabled=True))
        conflict = manager.push()
        self.assertEqual(conflict.status, "conflict")

        (self.device / "MEMORY.md").write_text(
            "# Domain\n\n"
            "- `one-remote` remote durable fact → []\n"
            "- `one-local` local durable fact → []\n",
            encoding="utf-8",
        )
        self._git(self.device, "add", "MEMORY.md")
        self._git(self.device, "commit", "-m", "memory: resolve sync conflict")

        pushed = manager.push()

        self.assertEqual(pushed.status, "pushed")
        self._git(self.other, "pull", "--ff-only")
        text = (self.other / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("one-remote", text)
        self.assertIn("one-local", text)
```

- [ ] **Step 2: Run fixture test**

Run:

```bash
python -m unittest tests.test_sync.SyncManagerTests.test_conflict_can_be_resolved_committed_and_pushed
```

Expected: pass after Task 2 implementation.

- [ ] **Step 3: Commit**

```bash
git add tests/test_sync.py
git commit -m "test: cover sync conflict resolution path"
```

## Task 9: Documentation And Design Notes

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`

- [ ] **Step 1: Update README**

Add a section after "Scheduled Dream Cycles":

```md
### Automatic Global Sync

RightMemory can keep one memory root shared across devices through a normal private Git remote. GitHub private repos are the easiest hosted setup, and any SSH/HTTPS Git remote works once the memory repo has an upstream branch.

Enable automatic sync in `<memory-root>/rightmemory.toml`:

```toml
[sync]
enabled = true
stale_pull_after_hours = 24
```

Write-capable roles run a deterministic sync preflight before model work, receive a compact runtime sync context, and push committed memory changes after editing. Retrieval stays local by default for speed. The managed watch process includes a `sync` target that pulls when the last successful pull is stale; clean pulls do not call a model.

If a scheduled pull creates a memory conflict, RightMemory invokes the `sync-reconciler` role with the conflicted files and current sync state. Active write roles resolve conflicts they encounter during their own write.
```

- [ ] **Step 2: Update design notes**

Add:

```md
### Global memory sync

Global memory stays local-first: every device has a full memory root and Git provides the distributed transport. Runtime code handles deterministic fetch, merge, freshness checks, state recording, and push retries before asking a model to reason. Memory roles handle semantic conflict resolution because Markdown memory conflicts require schema and durability judgment, not just Git mechanics.

The sync reconciler is separate from dreamer because scheduled sync conflict repair is a narrow maintenance responsibility. It resolves conflict markers, validates memory, commits, and pushes; broad consolidation remains dreamer work.
```

- [ ] **Step 3: Run docs-adjacent test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: pass.

- [ ] **Step 4: Commit**

```bash
git add README.md DESIGN_NOTES.md
git commit -m "docs: describe automatic memory sync"
```

## Task 10: Full Verification

**Files:**
- No file edits expected unless verification exposes a bug.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: exit code 0.

- [ ] **Step 2: Run full test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: no unstaged implementation files from this plan. Pre-existing unrelated files, if any, should remain outside the implementation commits.

## Self-Review Notes

- Spec coverage: config, automatic write preflight, local retrieve behavior, scheduled freshness sync, sync reconciler role, `.runtime/sync` state, conflict handling, and local bare Git testing all have tasks.
- Scope: GitHub direct repo creation stays outside this implementation pass. The working core needs a configured Git upstream and supports GitHub through normal Git behavior.
- Prompt boundary: runtime sync context is treated as authoritative at turn start, and prompts tell agents to avoid repeating preflight discovery.

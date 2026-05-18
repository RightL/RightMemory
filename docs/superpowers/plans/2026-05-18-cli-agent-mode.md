# CLI Agent Mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the old spawned-subagent install path with a `cli-agent` mode that runs all RightMemory roles through Codex CLI or Claude Code CLI while keeping the existing `rightmemory` command surface.

**Architecture:** Keep Python responsible for orchestration: config, locks, sessions, async update batching, review scanning, watchers, sync flow, and stdout. Add a CLI-agent executor beside the existing Pydantic AI executor, backed by provider session mapping under `.runtime/`, thin prompt composition, deterministic command builders, and a user-facing doctor command.

**Tech Stack:** Python 3.11 standard library, `unittest`, existing RightMemory runtime modules, Codex CLI JSONL output, Claude Code JSON output.

---

## File Structure

- Modify `rightmemory/config.py`: add CLI-agent config dataclasses, parse `[agent_cli]` and `[<role>.agent_cli]`, and mark each role config as `standalone` or `cli-agent`.
- Modify `rightmemory/prompt.py`: keep existing standalone prompt composer, add a thin `build_cli_agent_instructions()` composer that embeds schema plus canonical role prompt without custom-tool guidance.
- Create `rightmemory/provider_sessions.py`: store provider session mappings and expose a registry query for review exclusion.
- Create `rightmemory/agent_cli.py`: build provider commands, parse provider output, run subprocesses, and implement `CliAgentExecutor`.
- Modify `rightmemory/runtime.py`: route model turns to either the existing Pydantic path or the new CLI-agent executor while preserving locks and sync behavior.
- Modify `rightmemory/review.py`: skip normalized provider sessions recorded as internal CLI-agent work.
- Create `rightmemory/doctor.py`: implement `rightmemory doctor agent-cli` checks against a temporary memory root.
- Modify `rightmemory/cli.py`: add the `doctor` command and wire runtime mode behavior.
- Modify `install.sh`: replace `subagent` with `cli-agent`, install the CLI runtime and command-backed orchestrator, and reject the old mode name.
- Rename `skills/memory-orchestrator-standalone/SKILL.md` to `skills/memory-orchestrator-cli/SKILL.md`: make the command-backed orchestrator text mode-neutral and update installer references.
- Delete `skills/memory-curator/SKILL.md` and `skills/memory-dreamer/SKILL.md` after installer/tests no longer reference them.
- Modify `README.md`, `DESIGN_NOTES.md`, and `AGENTS.md` where they describe install modes, role prompts, runtime shape, or test commands.
- Add `tests/test_agent_cli.py`: deterministic tests for command builders, output parsers, session mapping, executor subprocess calls, and doctor report formatting.
- Modify `tests/test_config.py`, `tests/test_cli.py`, `tests/test_install.py`, and `tests/test_review.py`.

---

### Task 1: Parse CLI-Agent Config

**Files:**
- Modify: `rightmemory/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Add failing config tests**

Add tests covering default provider, role override, standalone preservation, and missing provider errors:

```python
def test_agent_cli_default_config(self):
    config_path = self._write_config(
        """
        [agent_cli]
        provider = "codex"

        [retrieve.agent_cli]
        model = "gpt-5.4-mini"
        """
    )

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        config = load_config("retrieve")

    self.assertEqual(config.runtime_mode, "cli-agent")
    self.assertEqual(config.agent_cli.provider, "codex")
    self.assertEqual(config.agent_cli.model, "gpt-5.4-mini")
    self.assertIsNone(config.model_id)


def test_agent_cli_role_provider_override(self):
    config_path = self._write_config(
        """
        [agent_cli]
        provider = "codex"

        [update.agent_cli]
        provider = "claude"
        model = "sonnet"
        """
    )

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        config = load_config("update")

    self.assertEqual(config.runtime_mode, "cli-agent")
    self.assertEqual(config.agent_cli.provider, "claude")
    self.assertEqual(config.agent_cli.model, "sonnet")


def test_agent_cli_requires_provider(self):
    config_path = self._write_config(
        """
        [retrieve.agent_cli]
        model = "gpt-5.4-mini"
        """
    )

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        with self.assertRaises(ValueError) as caught:
            load_config("retrieve")

    self.assertIn("[agent_cli].provider", str(caught.exception))
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_config.PromptTests tests.test_config.ConfigTests
```

Expected: new tests fail because `RuntimeConfig` has no `runtime_mode` or `agent_cli`.

- [ ] **Step 3: Implement config dataclasses and parser**

In `rightmemory/config.py`, add:

```python
@dataclass(frozen=True)
class AgentCliConfig:
    provider: str
    model: str | None = None


@dataclass(frozen=True)
class RuntimeConfig:
    role: str
    model_id: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    model_kwargs: dict[str, Any] = field(default_factory=dict)
    memory_root: Path = MEMORY_ROOT
    max_tool_retries: int = DEFAULT_MAX_TOOL_RETRIES
    debug_trace: bool = False
    sync: SyncConfig = field(default_factory=SyncConfig)
    runtime_mode: str = "standalone"
    agent_cli: AgentCliConfig | None = None
```

Update `load_config()` so:

```python
_reject_unknown_keys(data, {*ROLES, "agent_cli", "review", "debug", "sync"}, "top-level")
role_section = data.get(role)
if not isinstance(role_section, dict):
    role_section = {}
_reject_unknown_keys(role_section, {"model", "agent_cli"}, f"[{role}]")

if "model" in role_section and "agent_cli" in role_section:
    raise ValueError(f"[{role}] must not define both model and agent_cli")

if "model" in role_section:
    return _standalone_runtime_config(role, data, role_section)

return _agent_cli_runtime_config(role, data, role_section)
```

Add helpers:

```python
def _agent_cli_runtime_config(role: str, data: dict[str, object], role_section: dict[str, object]) -> RuntimeConfig:
    global_section = data.get("agent_cli", {})
    if global_section is None:
        global_section = {}
    if not isinstance(global_section, dict):
        raise ValueError("[agent_cli] must be a TOML table")
    _reject_unknown_keys(global_section, {"provider"}, "[agent_cli]")

    role_cli = role_section.get("agent_cli", {})
    if role_cli is None:
        role_cli = {}
    if not isinstance(role_cli, dict):
        raise ValueError(f"[{role}.agent_cli] must be a TOML table")
    _reject_unknown_keys(role_cli, {"provider", "model"}, f"[{role}.agent_cli]")

    provider = role_cli.get("provider", global_section.get("provider"))
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError(f"[agent_cli].provider or [{role}.agent_cli].provider must be a non-empty string")
    provider = provider.strip().lower()
    if provider not in {"codex", "claude"}:
        raise ValueError("agent_cli provider must be one of: claude, codex")

    model = role_cli.get("model")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        raise ValueError(f"[{role}.agent_cli].model must be a non-empty string when set")

    return RuntimeConfig(
        role=role,
        memory_root=MEMORY_ROOT,
        debug_trace=_debug_trace(data.get("debug", {})),
        sync=_sync_config(data.get("sync", {})),
        runtime_mode="cli-agent",
        agent_cli=AgentCliConfig(provider=provider, model=model.strip() if isinstance(model, str) else None),
    )
```

Move current model parsing into `_standalone_runtime_config()` and keep existing error wording for `[<role>.model]` when the role is intended to use standalone mode.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m unittest tests.test_config
```

Expected: config tests pass or reveal prompt-test updates handled in Task 2.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/config.py tests/test_config.py
git commit -m "feat: parse cli agent config"
```

---

### Task 2: Add Thin CLI-Agent Prompt Composer

**Files:**
- Modify: `rightmemory/prompt.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Add failing prompt tests**

Add tests:

```python
def test_cli_agent_prompt_is_thin_and_role_specific(self):
    prompt = build_cli_agent_instructions(Path("/memory"), "retrieve")

    self.assertIn("You are RightMemory retrieve mode.", prompt)
    self.assertIn("Work in this memory root: /memory", prompt)
    self.assertIn("RightMemory schema:", prompt)
    self.assertIn("Retrieve Role", prompt)
    self.assertNotIn("edit_file(path, old_string", prompt)
    self.assertNotIn("read_command accepts", prompt)
    self.assertNotIn("pydantic", prompt.lower())


def test_cli_agent_prompt_rejects_unknown_role(self):
    with self.assertRaises(ValueError):
        build_cli_agent_instructions(Path("/memory"), "unknown")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_config.PromptTests
```

Expected: import or name failure for `build_cli_agent_instructions`.

- [ ] **Step 3: Implement composer**

In `rightmemory/prompt.py`, add:

```python
def build_cli_agent_instructions(memory_root: Path, role: str) -> str:
    if role not in ROLE_PROMPTS:
        raise ValueError(f"role must be one of: {_role_list()}")
    schema = _read_prompt_file("skills/rightmemory-schema.md")
    role_guidance = _read_prompt_file(f"prompts/{role}.md")
    return f"""You are RightMemory {role} mode.

Work in this memory root: {memory_root}

Memory store:
- MEMORY.md
- MEMORY_*.md sibling detail files
- dream_logs/

Follow the canonical role instructions below. Return a concise final reply for the caller.

RightMemory schema:
{schema}

Role instructions:
{role_guidance}
"""
```

Do not add provider tool instructions here. Keep existing `build_instructions()` unchanged for standalone.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m unittest tests.test_config.PromptTests
```

Expected: prompt tests pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/prompt.py tests/test_config.py
git commit -m "feat: add cli agent prompt composer"
```

---

### Task 3: Add Provider Session Registry And Review Exclusion

**Files:**
- Create: `rightmemory/provider_sessions.py`
- Modify: `rightmemory/review.py`
- Test: `tests/test_agent_cli.py`, `tests/test_review.py`

- [ ] **Step 1: Add failing provider session store tests**

Create `tests/test_agent_cli.py` with:

```python
import tempfile
import unittest
from pathlib import Path

from rightmemory.provider_sessions import ProviderSessionRecord, ProviderSessionStore


class ProviderSessionStoreTests(unittest.TestCase):
    def test_save_load_and_internal_lookup(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = ProviderSessionStore(root, "retrieve")
            record = ProviderSessionRecord(
                provider="codex",
                provider_session_id="thread-1",
                role="retrieve",
                rightmemory_session_id="agent-1",
                created_at="2026-05-18T00:00:00+00:00",
                updated_at="2026-05-18T00:00:00+00:00",
            )

            store.save("agent-1", record)
            loaded = store.load("agent-1")

        self.assertEqual(loaded, record)
        self.assertTrue(ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-1"))
        self.assertFalse(ProviderSessionStore.is_internal_provider_session(root, "codex", "other"))
```

- [ ] **Step 2: Add failing review skip test**

In `tests/test_review.py`, add:

```python
def test_scan_skips_internal_cli_agent_session(self):
    calls = []
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        source = root / "codex"
        source.mkdir()
        transcript = source / "session.jsonl"
        self._write_codex(transcript, turns=[("internal", "a1")], session_id="thread-1")
        self._set_mtime(transcript, 1_000)
        ProviderSessionStore(root, "retrieve").save(
            "agent-1",
            ProviderSessionRecord(
                provider="codex",
                provider_session_id="thread-1",
                role="retrieve",
                rightmemory_session_id="agent-1",
                created_at="2026-05-18T00:00:00+00:00",
                updated_at="2026-05-18T00:00:00+00:00",
            ),
        )
        scanner = ReviewScanner(
            ReviewConfig(
                memory_root=root,
                idle_seconds=3600,
                sources=[ReviewSourceConfig(kind="codex", path=source)],
            ),
            lambda session_id, message: calls.append(message) or "ok",
        )

        result = scanner.scan_once(now=10_000)

    self.assertEqual(result.skipped_internal, 1)
    self.assertEqual(calls, [])
```

Import `ProviderSessionRecord` and `ProviderSessionStore` at the top of `tests/test_review.py`.

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_agent_cli tests.test_review
```

Expected: missing module and `skipped_internal` failures.

- [ ] **Step 4: Implement provider session store**

Create `rightmemory/provider_sessions.py`:

```python
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from .session import _ensure_runtime_gitignore, _fsync_directory, _safe_session_id


@dataclass(frozen=True)
class ProviderSessionRecord:
    provider: str
    provider_session_id: str
    role: str
    rightmemory_session_id: str
    created_at: str
    updated_at: str


class ProviderSessionStore:
    def __init__(self, memory_root: Path, role: str):
        self.memory_root = memory_root
        self.role = role
        self.root = memory_root / ".runtime" / "agent_cli_sessions" / role

    def path(self, rightmemory_session_id: str) -> Path:
        return self.root / f"{_safe_session_id(rightmemory_session_id)}.json"

    def load(self, rightmemory_session_id: str) -> ProviderSessionRecord | None:
        path = self.path(rightmemory_session_id)
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return ProviderSessionRecord(
            provider=str(data["provider"]),
            provider_session_id=str(data["provider_session_id"]),
            role=str(data["role"]),
            rightmemory_session_id=str(data["rightmemory_session_id"]),
            created_at=str(data["created_at"]),
            updated_at=str(data["updated_at"]),
        )

    def save(self, rightmemory_session_id: str, record: ProviderSessionRecord) -> None:
        runtime_root = self.memory_root / ".runtime"
        _ensure_runtime_gitignore(runtime_root)
        path = self.path(rightmemory_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        content = json.dumps(asdict(record), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        _fsync_directory(path.parent)

    @staticmethod
    def is_internal_provider_session(memory_root: Path, provider: str, provider_session_id: str) -> bool:
        root = memory_root / ".runtime" / "agent_cli_sessions"
        if not root.exists():
            return False
        for path in root.glob("*/*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if data.get("provider") == provider and data.get("provider_session_id") == provider_session_id:
                return True
        return False
```

- [ ] **Step 5: Implement review scanner skip**

In `rightmemory/review.py`, import the store:

```python
from .provider_sessions import ProviderSessionStore
```

Add `skipped_internal: int = 0` to `ReviewScanResult` and include it in `format()`.

In `scan_once()`, initialize `skipped_internal` in `counts`, and after normalization but before `state_key`:

```python
if ProviderSessionStore.is_internal_provider_session(
    self.config.memory_root,
    normalized.source,
    normalized.session_id,
):
    counts["skipped_internal"] += 1
    continue
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m unittest tests.test_agent_cli tests.test_review
```

Expected: tests pass. If existing format assertions fail, update them to include `skipped_internal: 0`.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/provider_sessions.py rightmemory/review.py tests/test_agent_cli.py tests/test_review.py
git commit -m "feat: track cli agent provider sessions"
```

---

### Task 4: Build CLI-Agent Commands And Parsers

**Files:**
- Create/modify: `rightmemory/agent_cli.py`
- Test: `tests/test_agent_cli.py`

- [ ] **Step 1: Add failing command builder and parser tests**

In `tests/test_agent_cli.py`, add:

```python
from rightmemory.agent_cli import (
    build_claude_command,
    build_codex_command,
    parse_claude_output,
    parse_codex_output,
)
from rightmemory.config import AgentCliConfig


class AgentCliCommandTests(unittest.TestCase):
    def test_build_codex_first_and_resume_commands(self):
        config = AgentCliConfig(provider="codex", model="gpt-5.4-mini")

        first = build_codex_command(Path("/memory"), "retrieve", config, "prompt", None)
        resumed = build_codex_command(Path("/memory"), "retrieve", config, "prompt", "thread-1")

        self.assertEqual(first[:4], ["codex", "exec", "--json", "--cd"])
        self.assertIn("/memory", first)
        self.assertIn("--sandbox", first)
        self.assertIn("read-only", first)
        self.assertIn("--model", first)
        self.assertEqual(resumed[:4], ["codex", "exec", "resume", "--json"])
        self.assertIn("thread-1", resumed)

    def test_build_claude_first_and_resume_commands(self):
        config = AgentCliConfig(provider="claude", model="sonnet")

        first = build_claude_command("retrieve", config, "prompt", "uuid-1", False)
        resumed = build_claude_command("retrieve", config, "prompt", "uuid-1", True)

        self.assertEqual(first[:3], ["claude", "-p", "--output-format"])
        self.assertIn("--session-id", first)
        self.assertIn("uuid-1", first)
        self.assertIn("--model", first)
        self.assertIn("--resume", resumed)

    def test_parse_codex_jsonl_output(self):
        output = (
            '{"type":"thread.started","thread_id":"thread-1"}\\n'
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}\\n'
        )

        parsed = parse_codex_output(output)

        self.assertEqual(parsed.provider_session_id, "thread-1")
        self.assertEqual(parsed.text, "done")

    def test_parse_claude_json_output(self):
        parsed = parse_claude_output('{"type":"result","session_id":"uuid-1","result":"done"}')

        self.assertEqual(parsed.provider_session_id, "uuid-1")
        self.assertEqual(parsed.text, "done")
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_agent_cli
```

Expected: missing functions.

- [ ] **Step 3: Implement command builders and parsers**

Create `rightmemory/agent_cli.py`:

```python
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import AgentCliConfig


WRITE_ROLES = {"dreamer", "reviewer", "sync-reconciler", "update"}


@dataclass(frozen=True)
class AgentCliResult:
    provider_session_id: str
    text: str


def build_codex_command(
    memory_root: Path,
    role: str,
    config: AgentCliConfig,
    prompt: str,
    provider_session_id: str | None,
) -> list[str]:
    sandbox = "workspace-write" if role in WRITE_ROLES else "read-only"
    if provider_session_id:
        command = ["codex", "exec", "resume", "--json", "--skip-git-repo-check"]
        if config.model:
            command.extend(["--model", config.model])
        command.extend([provider_session_id, prompt])
        return command
    command = [
        "codex",
        "exec",
        "--json",
        "--cd",
        str(memory_root),
        "--skip-git-repo-check",
        "--sandbox",
        sandbox,
    ]
    if config.model:
        command.extend(["--model", config.model])
    command.append(prompt)
    return command


def build_claude_command(
    role: str,
    config: AgentCliConfig,
    prompt: str,
    provider_session_id: str,
    resume: bool,
) -> list[str]:
    command = ["claude", "-p", "--output-format", "json"]
    if config.model:
        command.extend(["--model", config.model])
    if resume:
        command.extend(["--resume", provider_session_id])
    else:
        command.extend(["--session-id", provider_session_id])
    command.append(prompt)
    return command


def parse_codex_output(stdout: str) -> AgentCliResult:
    thread_id = ""
    text = ""
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "thread.started":
            thread_id = str(obj.get("thread_id") or thread_id)
        item = obj.get("item")
        if isinstance(item, dict) and item.get("type") == "agent_message":
            text = str(item.get("text") or text)
    if not thread_id:
        raise RuntimeError("Codex output did not include thread_id")
    if not text:
        raise RuntimeError("Codex output did not include final agent message")
    return AgentCliResult(provider_session_id=thread_id, text=text)


def parse_claude_output(stdout: str) -> AgentCliResult:
    data = json.loads(stdout)
    session_id = str(data.get("session_id") or "")
    text = str(data.get("result") or "")
    if not session_id:
        raise RuntimeError("Claude output did not include session_id")
    if not text:
        raise RuntimeError("Claude output did not include result")
    return AgentCliResult(provider_session_id=session_id, text=text)
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
python -m unittest tests.test_agent_cli
```

Expected: command and parser tests pass.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/agent_cli.py tests/test_agent_cli.py
git commit -m "feat: build cli agent provider commands"
```

---

### Task 5: Integrate CLI-Agent Executor With Runtime

**Files:**
- Modify: `rightmemory/agent_cli.py`
- Modify: `rightmemory/runtime.py`
- Test: `tests/test_agent_cli.py`, `tests/test_cli.py`, `tests/test_config.py`

- [ ] **Step 1: Add failing executor test**

In `tests/test_agent_cli.py`, add:

```python
from unittest.mock import patch

from rightmemory.agent_cli import CliAgentExecutor
from rightmemory.config import RuntimeConfig


class CliAgentExecutorTests(unittest.TestCase):
    def test_codex_executor_creates_and_resumes_provider_session(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="retrieve",
                memory_root=root,
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex", model=None),
            )
            executor = CliAgentExecutor(config)
            outputs = [
                '{"type":"thread.started","thread_id":"thread-1"}\\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}\\n',
                '{"type":"thread.started","thread_id":"thread-1"}\\n'
                '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}\\n',
            ]

            def fake_run(command, **kwargs):
                self.assertEqual(kwargs["cwd"], root)
                return type("Completed", (), {"stdout": outputs.pop(0), "stderr": "", "returncode": 0})()

            with patch("rightmemory.agent_cli.subprocess.run", fake_run):
                first = executor.run_session_turn("agent-1", "hello")
                second = executor.run_session_turn("agent-1", "again")

        self.assertEqual(first, "first")
        self.assertEqual(second, "second")
        self.assertTrue(ProviderSessionStore.is_internal_provider_session(root, "codex", "thread-1"))
```

- [ ] **Step 2: Add failing runtime selection test**

In `tests/test_cli.py`, add:

```python
def test_main_runs_cli_agent_runtime(self):
    stdout = io.StringIO()
    config = type("Config", (), {"runtime_mode": "cli-agent", "role": "retrieve"})()

    class FakeCliRuntime:
        def __init__(self, config):
            self.config = config
        def run_session_turn(self, session_id, message):
            return f"cli {session_id}: {message}"
        def cleanup(self):
            pass

    with (
        patch("rightmemory.cli.load_config", return_value=config),
        patch("rightmemory.cli.RightMemoryRuntime", FakeCliRuntime),
        patch("sys.stdout", stdout),
    ):
        result = main(["retrieve", "--session", "agent-1", "hello"])

    self.assertEqual(result, 0)
    self.assertEqual(stdout.getvalue().strip(), "cli agent-1: hello")
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_agent_cli tests.test_cli
```

Expected: missing `CliAgentExecutor`.

- [ ] **Step 4: Implement `CliAgentExecutor`**

In `rightmemory/agent_cli.py`, add:

```python
import subprocess
import uuid
from datetime import UTC, datetime

from .prompt import build_cli_agent_instructions
from .provider_sessions import ProviderSessionRecord, ProviderSessionStore


class CliAgentExecutor:
    def __init__(self, config):
        if config.agent_cli is None:
            raise ValueError("cli-agent runtime requires agent_cli config")
        self.config = config
        self.agent_cli = config.agent_cli
        self.sessions = ProviderSessionStore(config.memory_root, config.role)

    def run_turn(self, message: str) -> str:
        return self.run_session_turn("chat", message)

    def run_session_turn(self, session_id: str, message: str) -> str:
        record = self.sessions.load(session_id)
        prompt = _turn_prompt(self.config.memory_root, self.config.role, message)
        if self.agent_cli.provider == "codex":
            result = self._run_codex(prompt, record)
        elif self.agent_cli.provider == "claude":
            result = self._run_claude(session_id, prompt, record)
        else:
            raise ValueError(f"unsupported cli-agent provider: {self.agent_cli.provider}")
        now = datetime.now(UTC).isoformat()
        created_at = record.created_at if record else now
        self.sessions.save(
            session_id,
            ProviderSessionRecord(
                provider=self.agent_cli.provider,
                provider_session_id=result.provider_session_id,
                role=self.config.role,
                rightmemory_session_id=session_id,
                created_at=created_at,
                updated_at=now,
            ),
        )
        return result.text

    def _run_codex(self, prompt: str, record: ProviderSessionRecord | None) -> AgentCliResult:
        command = build_codex_command(
            self.config.memory_root,
            self.config.role,
            self.agent_cli,
            prompt,
            record.provider_session_id if record else None,
        )
        completed = subprocess.run(command, cwd=self.config.memory_root, text=True, capture_output=True)
        _raise_for_failed_cli(completed, "codex")
        return parse_codex_output(completed.stdout)

    def _run_claude(self, session_id: str, prompt: str, record: ProviderSessionRecord | None) -> AgentCliResult:
        provider_session_id = record.provider_session_id if record else _stable_uuid(self.config.role, session_id)
        command = build_claude_command(self.config.role, self.agent_cli, prompt, provider_session_id, record is not None)
        completed = subprocess.run(command, cwd=self.config.memory_root, text=True, capture_output=True)
        _raise_for_failed_cli(completed, "claude")
        return parse_claude_output(completed.stdout)


def _turn_prompt(memory_root: Path, role: str, message: str) -> str:
    return build_cli_agent_instructions(memory_root, role) + "\n\nCaller message:\n" + message


def _stable_uuid(role: str, session_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rightmemory:{role}:{session_id}"))


def _raise_for_failed_cli(completed, provider: str) -> None:
    if completed.returncode == 0:
        return
    detail = (completed.stderr or completed.stdout or "").strip()
    raise RuntimeError(f"{provider} CLI failed with exit code {completed.returncode}: {detail}")
```

- [ ] **Step 5: Route runtime to CLI-agent executor**

In `rightmemory/runtime.py`, import `CliAgentExecutor`. In `__init__`, create `self.cli_executor` when `config.runtime_mode == "cli-agent"` and skip Pydantic agent construction:

```python
if self.config.runtime_mode == "cli-agent":
    self.executor = CliAgentExecutor(config)
    self.agent = None
else:
    self.executor = None
    self.agent = self._build_agent()
```

In `run_turn()` and `_run_session_model()`, branch:

```python
if self.executor is not None:
    return self.executor.run_turn(message)
```

and:

```python
if self.executor is not None:
    return self.executor.run_session_turn(session_id, message)
```

Keep existing Pydantic behavior unchanged when `self.executor is None`.

In traces, replace direct `self.config.model_id` access with:

```python
model_id=self.config.model_id or getattr(self.config.agent_cli, "model", None)
```

Update `build_model()` to reject configs without `model_id`:

```python
if not config.model_id:
    raise ValueError("standalone runtime requires model_id")
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m unittest tests.test_agent_cli tests.test_cli tests.test_config
```

Expected: tests pass after updating any test fixtures that instantiate `RuntimeConfig` with positional arguments.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/agent_cli.py rightmemory/runtime.py tests/test_agent_cli.py tests/test_cli.py tests/test_config.py
git commit -m "feat: run roles through cli agent executor"
```

---

### Task 6: Add `rightmemory doctor agent-cli`

**Files:**
- Create: `rightmemory/doctor.py`
- Modify: `rightmemory/cli.py`
- Test: `tests/test_cli.py`, `tests/test_agent_cli.py`

- [ ] **Step 1: Add failing CLI test**

In `tests/test_cli.py`, add:

```python
def test_doctor_agent_cli_command(self):
    stdout = io.StringIO()

    with (
        patch("rightmemory.cli.run_agent_cli_doctor", return_value="agent-cli doctor: ok\n") as doctor,
        patch("sys.stdout", stdout),
    ):
        result = main(["doctor", "agent-cli"])

    self.assertEqual(result, 0)
    self.assertEqual(stdout.getvalue(), "agent-cli doctor: ok\n")
    doctor.assert_called_once()
```

- [ ] **Step 2: Add failing doctor report test**

In `tests/test_agent_cli.py`, add a unit around report formatting:

```python
from rightmemory.doctor import DoctorCheck, format_doctor_report


class AgentCliDoctorTests(unittest.TestCase):
    def test_format_doctor_report(self):
        report = format_doctor_report(
            [
                DoctorCheck("codex exists", True, "codex 0.130.0"),
                DoctorCheck("resume history", False, "token was not preserved"),
            ]
        )

        self.assertIn("[ok] codex exists - codex 0.130.0", report)
        self.assertIn("[fail] resume history - token was not preserved", report)
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_cli tests.test_agent_cli
```

Expected: missing doctor module/function.

- [ ] **Step 4: Implement doctor module skeleton**

Create `rightmemory/doctor.py`:

```python
from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .config import ROLES, load_config
from .runtime import RightMemoryRuntime


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    passed: bool
    detail: str


def format_doctor_report(checks: list[DoctorCheck]) -> str:
    lines = []
    for check in checks:
        marker = "ok" if check.passed else "fail"
        lines.append(f"[{marker}] {check.name} - {check.detail}")
    return "\n".join(lines) + "\n"


def run_agent_cli_doctor() -> str:
    checks: list[DoctorCheck] = []
    configs = []
    for role in sorted(ROLES):
        try:
            config = load_config(role)
        except Exception as exc:
            checks.append(DoctorCheck(f"{role} config", False, f"{type(exc).__name__}: {exc}"))
            continue
        if config.runtime_mode != "cli-agent" or config.agent_cli is None:
            checks.append(DoctorCheck(f"{role} config", False, "role is not configured for cli-agent"))
            continue
        configs.append(config)
        exists = shutil.which(config.agent_cli.provider) is not None
        checks.append(DoctorCheck(f"{role} {config.agent_cli.provider} CLI", exists, "found" if exists else "not found"))

    if not configs:
        return format_doctor_report(checks)

    with tempfile.TemporaryDirectory(prefix="rightmemory-doctor-") as tempdir:
        temp_root = Path(tempdir)
        _seed_doctor_memory(temp_root)
        for config in configs:
            checks.extend(_run_role_checks(config, temp_root))
    return format_doctor_report(checks)
```

Add helpers with concrete checks:

```python
def _seed_doctor_memory(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "MEMORY.md").write_text(
        "# Doctor Memory {#doctor-memory}\n\n- `doctor-token` RM_DOCTOR_MEMORY_TOKEN → []\n",
        encoding="utf-8",
    )
    (root / "dream_logs").mkdir(exist_ok=True)


def _run_role_checks(config, temp_root: Path) -> list[DoctorCheck]:
    checks: list[DoctorCheck] = []
    test_config = config.__class__(
        role=config.role,
        memory_root=temp_root,
        runtime_mode=config.runtime_mode,
        agent_cli=config.agent_cli,
        sync=config.sync,
        debug_trace=False,
    )
    try:
        runtime = RightMemoryRuntime(test_config)
        first = runtime.run_session_turn("doctor-history", "Reply exactly: RM_DOCTOR_HISTORY_TOKEN")
        second = runtime.run_session_turn(
            "doctor-history",
            "What exact token did I ask you to reply with in the previous user message? Reply with that token and nothing else.",
        )
    except Exception as exc:
        checks.append(DoctorCheck(f"{config.role} resume history", False, f"{type(exc).__name__}: {exc}"))
    else:
        checks.append(
            DoctorCheck(
                f"{config.role} resume history",
                "RM_DOCTOR_HISTORY_TOKEN" in second,
                second.strip(),
            )
        )
    finally:
        cleanup = locals().get("runtime")
        if cleanup is not None:
            cleanup.cleanup()
    return checks
```

Then extend `_run_role_checks()` in the same task or the next small commit to include memory read/write/git checks:

```python
if config.role == "retrieve":
    output = runtime.run_session_turn("doctor-read", "Find the doctor memory token.")
    checks.append(DoctorCheck("retrieve reads memory", "RM_DOCTOR_MEMORY_TOKEN" in output, output.strip()))
elif config.role == "update":
    output = runtime.run_session_turn("doctor-write", "Add a compact memory node that says RM_DOCTOR_WRITE_TOKEN was verified.")
    memory = (temp_root / "MEMORY.md").read_text(encoding="utf-8")
    checks.append(DoctorCheck("update writes memory", "RM_DOCTOR_WRITE_TOKEN" in memory, output.strip()))
```

Keep the first implementation useful and bounded. If live CLI behavior reveals provider-specific friction, improve doctor checks in a follow-up.

- [ ] **Step 5: Wire CLI command**

In `rightmemory/cli.py`, import:

```python
from .doctor import run_agent_cli_doctor
```

At the top of `main()`:

```python
if argv and argv[0] == "doctor":
    return _doctor_main(argv[1:])
```

Add:

```python
def _doctor_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory doctor")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("agent-cli", help="verify local Codex/Claude Code CLI-agent setup")
    args = parser.parse_args(argv)
    if args.command == "agent-cli":
        print(run_agent_cli_doctor(), end="")
        return 0
    raise ValueError(f"unknown doctor command: {args.command}")
```

- [ ] **Step 6: Run focused tests**

Run:

```bash
python -m unittest tests.test_cli tests.test_agent_cli
```

Expected: doctor unit tests pass. Live doctor behavior is checked manually after implementation with `rightmemory doctor agent-cli`.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/doctor.py rightmemory/cli.py tests/test_cli.py tests/test_agent_cli.py
git commit -m "feat: add cli agent doctor"
```

---

### Task 7: Replace Install Mode And Orchestrator Wording

**Files:**
- Modify: `install.sh`
- Rename: `skills/memory-orchestrator-standalone/SKILL.md` -> `skills/memory-orchestrator-cli/SKILL.md`
- Delete: `skills/memory-curator/SKILL.md`
- Delete: `skills/memory-dreamer/SKILL.md`
- Test: `tests/test_install.py`

- [ ] **Step 1: Add failing install tests**

In `tests/test_install.py`, update the explicit subagent helper to use `cli-agent`, and add:

```python
def test_subagent_mode_is_rejected(self):
    with tempfile.TemporaryDirectory() as tempdir:
        result = subprocess.run(
            ["bash", "install.sh", "--mode", "subagent", str(Path(tempdir) / "memory"), str(Path(tempdir) / "skills")],
            cwd=REPO_ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    self.assertNotEqual(result.returncode, 0)
    self.assertIn("Use --mode cli-agent", result.stderr)


def test_cli_agent_install_uses_command_orchestrator_without_role_skills(self):
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        memory_root = root / "memory"
        skills_target = root / "skills"

        result = self._install(memory_root, skills_target)

    self.assertIn("MODE         = cli-agent", result.stdout)
    self.assertTrue((skills_target / "memory-orchestrator" / "SKILL.md").exists())
    self.assertFalse((skills_target / "memory-curator").exists())
    self.assertFalse((skills_target / "memory-dreamer").exists())
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_install
```

Expected: `cli-agent` is not accepted yet.

- [ ] **Step 3: Update installer modes**

In `install.sh`:

- Change usage text to `./install.sh [--mode cli-agent|standalone]`.
- Set valid modes to `cli-agent|standalone`.
- Reject `subagent` explicitly:

```bash
case "$MODE" in
  subagent)
    echo "Invalid --mode: subagent. Use --mode cli-agent." >&2
    usage
    exit 1
    ;;
  cli-agent|standalone)
    ;;
  *)
    echo "Invalid --mode: $MODE" >&2
    usage
    exit 1
    ;;
esac
```

- For `cli-agent`, call `install_standalone_runtime_layout` because the same Python CLI runtime is needed.
- Install the command-backed `memory-orchestrator`.
- Remove `memory-curator` and `memory-dreamer` from the target when they identify as RightMemory-owned.

- [ ] **Step 4: Rename and update orchestrator text**

Rename the command-backed orchestrator skill:

```bash
git mv skills/memory-orchestrator-standalone/SKILL.md skills/memory-orchestrator-cli/SKILL.md
```

Update the skill text so it does not say "standalone" as the execution model. Keep the command instructions:

```md
# Memory Orchestrator

## Access Rules

- The main agent should not access any `{{MEMORY_ROOT}}/MEMORY*.md` file by direct reads or writes. Memory access goes through the installed `rightmemory` command.
- Pick one stable session id for this agent conversation and reuse it for retrieve and update calls.
```

Keep the retrieve/update command sections, but remove wording that assumes Pydantic AI internals.

- [ ] **Step 5: Remove generated role skill sources**

Delete source skill files that are no longer installed:

```bash
git rm skills/memory-curator/SKILL.md skills/memory-dreamer/SKILL.md
```

If deleting the directories leaves empty folders, Git will stop tracking them naturally.

- [ ] **Step 6: Run install tests**

Run:

```bash
python -m unittest tests.test_install
```

Expected: install tests pass.

- [ ] **Step 7: Commit**

```bash
git add install.sh skills/memory-orchestrator-cli/SKILL.md tests/test_install.py
git commit -m "feat: replace subagent install with cli agent mode"
```

---

### Task 8: Update Docs And Run Full Verification

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`
- Modify: `AGENTS.md`
- Modify: tests as needed for changed mode names and `skipped_internal` output

- [ ] **Step 1: Update README mode docs**

Replace install mode table with:

```md
| Mode | Use When | What Gets Installed |
| --- | --- | --- |
| `standalone` | You want RightMemory's Pydantic AI runtime with custom tools. | A command-backed `memory-orchestrator` skill plus the `rightmemory` CLI. |
| `cli-agent` | You want RightMemory to delegate role execution to Codex CLI or Claude Code CLI. | A command-backed `memory-orchestrator` skill plus the `rightmemory` CLI. |
```

Add CLI-agent config:

```toml
[agent_cli]
provider = "codex"

[retrieve.agent_cli]
model = "gpt-5.4-mini"

[update.agent_cli]
provider = "claude"
model = "sonnet"
```

Document:

```bash
rightmemory doctor agent-cli
```

- [ ] **Step 2: Update design and agent notes**

In `DESIGN_NOTES.md`, replace the standalone/subagent split note with a note that command-backed orchestration is installed for both modes, while the runtime executor differs.

In `AGENTS.md`, update project shape and installer notes so future agents do not look for generated curator/dreamer skills.

- [ ] **Step 3: Run syntax checks**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: no output.

- [ ] **Step 4: Run full unit suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 5: Run installer smoke test**

Run:

```bash
tmp_root="$(mktemp -d)"
conda run -n rightmemory ./install.sh --mode cli-agent "$tmp_root/memory" "$tmp_root/skills"
```

Expected:

- output includes `MODE         = cli-agent`;
- `$tmp_root/skills/memory-orchestrator/SKILL.md` exists;
- no `$tmp_root/skills/memory-curator` or `$tmp_root/skills/memory-dreamer` exists;
- `$tmp_root/memory/.runtime/install.stamp` exists.

- [ ] **Step 6: Optional live provider check**

If local provider auth is available and the user wants the live check, run:

```bash
rightmemory doctor agent-cli
```

Expected: compact pass/fail report. A failure here means local CLI/auth/permission setup needs adjustment; it should not block deterministic unit tests.

- [ ] **Step 7: Commit**

```bash
git add README.md DESIGN_NOTES.md AGENTS.md tests rightmemory
git commit -m "docs: document cli agent mode"
```

---

## Self-Review Notes

- Spec coverage: config, runtime executor, session mapping, transcript review exclusion, prompt thinness, doctor checks, installer migration, docs, and tests are each covered by a task.
- The plan keeps live Codex/Claude calls inside `rightmemory doctor agent-cli`; unit tests use command construction and subprocess fakes.
- The plan preserves current standalone behavior by routing it through the existing Pydantic path rather than rewriting provider/tool logic during the CLI-agent work.

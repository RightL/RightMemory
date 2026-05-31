# Insight Role Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a first-class automatic `insight` role that writes timestamped reflective logs while Dreamer returns to memory consolidation without report files.

**Architecture:** Add Insight as a normal RightMemory role with its own prompt, config, trigger state, watcher, status section, sync surface, and narrow write policy. Keep scheduled Dreamer and Insight operation cycle-driven by adding runtime cycle entry points while preserving manual operator hints. Use role-aware write validation so memory-editing roles write active memory and Insight writes `insight_logs/*.md`.

**Tech Stack:** Python standard library, `unittest`, Git CLI, existing RightMemory runtime/tool/watch/sync abstractions, Markdown prompt files, Bash installer.

---

## File Structure

- Modify `rightmemory/config.py` for the `insight` role, default watch config, and config parsing.
- Create `rightmemory/insight_trigger.py` for Insight trigger state. It should mirror Dreamer trigger behavior while using `.runtime/insight/trigger-state.json` and `last_successful_insight_at`.
- Create `rightmemory/prompts/insight.md` for the new role prompt.
- Modify `rightmemory/prompts/dreamer.md` with focused report-removal edits.
- Modify `rightmemory/prompt.py` so prompt assembly knows Insight, excludes `dream_logs/`, and describes operator hints for Dreamer and Insight.
- Modify `rightmemory/tools.py` to make file write and commit path validation role-aware.
- Modify `rightmemory/runtime.py` to construct role-aware tools, expose `run_cycle`, avoid `validate_memory` for Insight, and include Insight in automatic isolated/sync behavior.
- Modify `rightmemory/isolated_write.py` to validate commits through a role-aware write policy.
- Modify `rightmemory/sync.py` so dirty/conflict checks include active memory plus Insight logs, and exclude retired artifacts.
- Modify `rightmemory/session.py` so generated `.gitignore` reflects the current artifact model.
- Modify `rightmemory/agent_cli.py` so Insight is a write role and CLI prompts can receive cycle hints.
- Modify `rightmemory/cli.py` to add `rightmemory insight`, `rightmemory insight watch`, trigger incrementing, and scheduled cycle calls.
- Modify `rightmemory/watch.py` so managed watch includes Insight.
- Modify `rightmemory/status.py` so the dashboard reports Insight trigger progress.
- Modify `rightmemory/doctor.py` so doctor seeds the current artifact layout.
- Modify `install.sh` so fresh installs and reinstalls use the current allowlist and create `insight_logs/`.
- Modify `README.md`, `DESIGN_NOTES.md`, and `AGENTS.md` so they describe the final Dreamer/Insight role model coherently.
- Update tests in `tests/test_config.py`, `tests/test_cli.py`, `tests/test_tools.py`, `tests/test_isolated_write.py`, `tests/test_sync.py`, `tests/test_status.py`, `tests/test_agent_cli.py`, and `tests/test_install.py`.

## Task 1: Config And Insight Trigger State

**Files:**
- Modify: `rightmemory/config.py`
- Create: `rightmemory/insight_trigger.py`
- Test: `tests/test_config.py`
- Test: `tests/test_insight_trigger.py`

- [ ] **Step 1: Write failing config tests**

Add imports in `tests/test_config.py`:

```python
from rightmemory.config import (
    load_async_update_config,
    load_config,
    load_dreamer_watch_config,
    load_insight_watch_config,
    load_pruner_config,
    load_review_config,
    load_sync_config,
)
```

Add these tests to `ConfigTests` near the Dreamer watch tests:

```python
@patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
def test_insight_watch_config_defaults(self):
    config_path = self._write_config("")

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        config = load_insight_watch_config()

    self.assertEqual(config.memory_root, Path("/home/example/.rightmemory"))
    self.assertEqual(config.trigger_points, 150.0)
    self.assertEqual(config.update_candidate_points, 1.0)
    self.assertEqual(config.review_session_points, 1.5)
    self.assertEqual(config.check_interval_seconds, 3000)

@patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
def test_insight_watch_config_parses_custom_values(self):
    config_path = self._write_config(
        """
        [insight.model]
        model_id = "openai/insight"

        [insight.watch]
        trigger_points = 225
        update_candidate_points = 2.5
        review_session_points = 4
        check_interval_seconds = 600
        """
    )

    with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
        config = load_insight_watch_config()
        runtime_config = load_config("insight")

    self.assertEqual(config.trigger_points, 225.0)
    self.assertEqual(config.update_candidate_points, 2.5)
    self.assertEqual(config.review_session_points, 4.0)
    self.assertEqual(config.check_interval_seconds, 600)
    self.assertEqual(runtime_config.model_id, "openai/insight")

@patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
def test_insight_watch_config_rejects_invalid_values(self):
    cases = [
        ("trigger_points = 0", "[insight.watch].trigger_points must be a positive number"),
        ("update_candidate_points = -1", "[insight.watch].update_candidate_points must be a positive number"),
        ("review_session_points = true", "[insight.watch].review_session_points must be a positive number"),
        ("check_interval_seconds = 1.5", "[insight.watch].check_interval_seconds must be a positive integer"),
        ("unknown = 1", "unsupported [insight.watch] config key(s): unknown"),
    ]

    for watch_config, message in cases:
        with self.subTest(watch_config=watch_config):
            config_path = self._write_config(
                f"""
                [insight.watch]
                {watch_config}
                """
            )

            with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
                with self.assertRaises(ValueError) as caught:
                    load_insight_watch_config()

            self.assertIn(message, str(caught.exception))
```

- [ ] **Step 2: Write failing trigger tests**

Create `tests/test_insight_trigger.py`:

```python
import json
import tempfile
import unittest
from pathlib import Path

from rightmemory.insight_trigger import InsightTriggerStore


class InsightTriggerStoreTests(unittest.TestCase):
    def test_increment_and_consume_use_insight_runtime_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)

            state = store.increment(12.5)
            consumed = store.consume_if_available(10.0)
            after = store.read()

        self.assertEqual(state.points, 12.5)
        self.assertTrue(consumed)
        self.assertEqual(after.points, 2.5)
        self.assertIsNotNone(after.last_successful_insight_at)
        self.assertTrue((root / ".runtime" / "insight" / "trigger-state.json").exists())

    def test_consume_below_threshold_preserves_points(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            store = InsightTriggerStore(root)
            store.increment(3.0)

            consumed = store.consume_if_available(5.0)
            after = store.read()

        self.assertFalse(consumed)
        self.assertEqual(after.points, 3.0)
        self.assertIsNone(after.last_successful_insight_at)

    def test_corrupt_state_is_recovered_under_insight_directory(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "insight" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text("{not json", encoding="utf-8")

            state = InsightTriggerStore(root).read()
            backups = list(state_path.parent.glob("trigger-state.corrupt-*.json"))

        self.assertEqual(state.points, 0.0)
        self.assertIsNotNone(state.last_recovery_at)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), "{not json")

    def test_rejects_invalid_state_values(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state_path = root / ".runtime" / "insight" / "trigger-state.json"
            state_path.parent.mkdir(parents=True)
            state_path.write_text(json.dumps({"points": -1}), encoding="utf-8")

            state = InsightTriggerStore(root).read()

        self.assertEqual(state.points, 0.0)
        self.assertIsNotNone(state.last_recovery_at)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_insight_watch_config_defaults tests.test_insight_trigger
```

Expected: import failure for `load_insight_watch_config` and `rightmemory.insight_trigger`.

- [ ] **Step 4: Implement config**

In `rightmemory/config.py`, add Insight defaults and dataclass near Dreamer watch config:

```python
DEFAULT_INSIGHT_TRIGGER_POINTS = 150.0
DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS = 1.0
DEFAULT_INSIGHT_REVIEW_SESSION_POINTS = 1.5
DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS = 3000


@dataclass(frozen=True)
class InsightWatchConfig:
    memory_root: Path = MEMORY_ROOT
    trigger_points: float = DEFAULT_INSIGHT_TRIGGER_POINTS
    update_candidate_points: float = DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS
    review_session_points: float = DEFAULT_INSIGHT_REVIEW_SESSION_POINTS
    check_interval_seconds: int = DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS
```

Update role sets:

```python
ROLES = {"dreamer", "historian", "insight", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"}
MODEL_FALLBACK_ROLES = ("update", "dreamer", "insight", "reviewer", "pruner", "sync-reconciler", "historian")
```

Add parser:

```python
def load_insight_watch_config() -> InsightWatchConfig:
    data = _load_raw_config()

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("insight", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[insight] must be a TOML table")
    _reject_unknown_keys(section, {"model", "agent_cli", "watch"}, "[insight]")

    watch = section.get("watch", {})
    if watch is None:
        watch = {}
    if not isinstance(watch, dict):
        raise ValueError("[insight.watch] must be a TOML table")
    _reject_unknown_keys(
        watch,
        {"trigger_points", "update_candidate_points", "review_session_points", "check_interval_seconds"},
        "[insight.watch]",
    )

    return InsightWatchConfig(
        memory_root=MEMORY_ROOT,
        trigger_points=_positive_number(
            watch,
            "trigger_points",
            DEFAULT_INSIGHT_TRIGGER_POINTS,
            "[insight.watch]",
        ),
        update_candidate_points=_positive_number(
            watch,
            "update_candidate_points",
            DEFAULT_INSIGHT_UPDATE_CANDIDATE_POINTS,
            "[insight.watch]",
        ),
        review_session_points=_positive_number(
            watch,
            "review_session_points",
            DEFAULT_INSIGHT_REVIEW_SESSION_POINTS,
            "[insight.watch]",
        ),
        check_interval_seconds=_positive_integer(
            watch,
            "check_interval_seconds",
            DEFAULT_INSIGHT_CHECK_INTERVAL_SECONDS,
            "[insight.watch]",
        ),
    )
```

Update `_allowed_role_keys`:

```python
if role in {"dreamer", "insight"}:
    allowed.add("watch")
```

- [ ] **Step 5: Implement Insight trigger store**

Create `rightmemory/insight_trigger.py` by adapting `dreamer_trigger.py` with these public names and state field:

```python
@dataclass(frozen=True)
class InsightTriggerState:
    points: float = 0.0
    updated_at: str | None = None
    last_successful_insight_at: str | None = None
    last_recovery_at: str | None = None


class InsightTriggerStore:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root)
        self.runtime_root = self.memory_root / ".runtime"
        self.root = self.runtime_root / "insight"
        self.state_path = self.root / "trigger-state.json"
        self.lock_path = self.root / "trigger-state.lock"
```

In `consume_if_available`, write `last_successful_insight_at=now`. In error messages, use `insight trigger state`.

- [ ] **Step 6: Run config and trigger tests**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_insight_watch_config_defaults tests.test_config.ConfigTests.test_insight_watch_config_parses_custom_values tests.test_config.ConfigTests.test_insight_watch_config_rejects_invalid_values tests.test_insight_trigger
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/config.py rightmemory/insight_trigger.py tests/test_config.py tests/test_insight_trigger.py
git commit -m "feat: add insight watch config"
```

## Task 2: Prompts And Role Assembly

**Files:**
- Create: `rightmemory/prompts/insight.md`
- Modify: `rightmemory/prompts/dreamer.md`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/agent_cli.py`
- Test: `tests/test_config.py`
- Test: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing prompt tests**

In `tests/test_config.py`, update prompt role loops to include `insight`:

```python
for role in ("dreamer", "historian", "insight", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"):
    prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), role)
```

and:

```python
for role in ("dreamer", "historian", "insight", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"):
    prompt = build_instructions(Path("/home/example/.rightmemory"), role)
```

Add tests to `PromptTests`:

```python
def test_dreamer_prompt_no_longer_mentions_dream_logs(self):
    prompt = build_instructions(Path("/memory"), "dreamer")

    self.assertNotIn("dream_logs", prompt)
    self.assertNotIn("dream report", prompt.lower())
    self.assertIn("# Open Context Questions", prompt)

def test_insight_prompt_uses_insight_logs_and_excludes_memory_validation(self):
    prompt = build_instructions(Path("/memory"), "insight")

    self.assertIn("Insight Role", prompt)
    self.assertIn("insight_logs/", prompt)
    self.assertIn("operator hint", prompt)
    self.assertNotIn("validate_memory", prompt)
    self.assertNotIn("dream_logs", prompt)
```

- [ ] **Step 2: Write failing agent CLI role tests**

In `tests/test_agent_cli.py`, add this to `AgentCliCommandTests`:

```python
def test_build_codex_uses_workspace_write_for_insight(self):
    command = build_codex_command(
        Path("/memory/root"),
        "insight",
        AgentCliConfig(provider="codex"),
        "prompt",
        None,
    )

    self.assertIn("workspace-write", command)
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_config.PromptTests.test_insight_prompt_uses_insight_logs_and_excludes_memory_validation tests.test_config.PromptTests.test_dreamer_prompt_no_longer_mentions_dream_logs tests.test_agent_cli.AgentCliCommandTests.test_build_codex_uses_workspace_write_for_insight
```

Expected: failures because the role prompt and role sets are incomplete.

- [ ] **Step 4: Add Insight prompt**

Create `rightmemory/prompts/insight.md`:

```markdown
# Insight Role

## Sources And Scope

- The source of truth is the memory root: `MEMORY.md`, relevant sibling
  `MEMORY_*.md` files, and prior `insight_logs/*.md`.
- Read `MEMORY.md` first. Use relevant detail files and prior Insight logs when
  they help you see patterns across memory.
- Treat any caller message as an optional operator hint. It can focus the cycle,
  but it is not the source of truth.
- Do not read or infer project directories outside the memory root.
- Do not edit active memory files.

## Reflection Work

Write an Insight log when you find a useful pattern, risk, strategy,
recommendation, reflection, next-step idea, or project-improvement idea. These
are examples of useful reflection, not required headings.

Prefer coherent prose over a report template. Make the artifact useful to a
future agent or user who wants to understand the broader shape of the memory,
not a list of routine cleanup work.

If there is no meaningful reflection, do not create a log. Return a concise
no-op.

## Artifact And Commit

- Create `insight_logs/`, then create one timestamped log at
  `insight_logs/YYYY-MM-DD-HHMMSS.md` for a useful run.
- Do not append to existing logs.
- Commit the new Insight log with an `insight: reflect on memory shape` style subject.
- Do not create empty commits.

## Final Reply

Return the log path and commit hash when you created an artifact. For a no-op,
say that no meaningful insight was found for this cycle and that no file was
written.
```

- [ ] **Step 5: Edit Dreamer prompt surgically**

In `rightmemory/prompts/dreamer.md`, replace this line:

```markdown
- Deep restructures should improve the memory tree or graph even when they require broad edits. Keep each restructuring coherent, explain the rationale in the dream report, and surface uncertain cases instead of guessing.
```

with:

```markdown
- Deep restructures should improve the memory tree or graph even when they require broad edits. Keep each restructuring coherent and surface durable uncertain cases through memory instead of guessing.
```

Replace:

```markdown
- Never auto-resolve contradictions. When two nodes disagree about the same entity, keep both and surface the conflict in the dream report for the user to settle.
```

with:

```markdown
- Never auto-resolve contradictions. When two nodes disagree about the same entity, keep both and add or refine a compact `# Open Context Questions` item when user judgment is needed.
```

Replace the `## Report And Commit` section with:

```markdown
## Commit

- Commit changes after editing. Stage touched `MEMORY*.md` files; do not commit unrelated files. If the working directory is not yet a git repo, initialize it first.
- Dreaming must be idempotent. If the file is already in good shape, skip the commit and return a concise no-op.
```

Replace the final reply section with:

```markdown
## Final Reply

- Final replies should include the number of light fixes applied, deep restructures applied, durable open questions surfaced or refined, and the resulting commit hash or `no commit`.
```

- [ ] **Step 6: Update prompt assembly**

In `rightmemory/prompt.py`, add `insight` to `ROLE_PROMPTS`.

Change CLI memory store text to:

```python
Memory store:
- MEMORY.md
- MEMORY_*.md
- insight_logs/
```

Change standalone workspace rule to mention the current role-owned paths:

```python
- Use memory-store-relative paths such as `MEMORY.md`, `MEMORY_*.md`, and `insight_logs/*.md` when they are allowed for the selected role.
```

Change memory source of truth text to:

```python
Memory source of truth:
- The root file is MEMORY.md.
- Optional detail files are named MEMORY_<slug>.md.
- Insight logs are stored under insight_logs/.
- MEMORY.md is normal memory, not a routing-only index.
```

Add command guidance:

```python
if role == "insight":
    return (
        "- The `rightmemory insight` command selected insight behavior. Run one reflection cycle for the memory store.\n"
        "- Treat the caller message as an optional operator hint, not as the ordinary source of truth."
    )
```

Change Dreamer command guidance to:

```python
if role == "dreamer":
    return (
        "- The `rightmemory dreamer` command selected dreamer consolidation behavior. Run one consolidation cycle for the memory store.\n"
        "- Treat the caller message as an optional operator hint, not as the ordinary source of truth."
    )
```

Update `_tool_guidance` so Insight receives its own text:

```python
if role == "insight":
    return (
        "- Use the provided tools for memory-root reads, Insight log creation or refinement, git inspection, and committing Insight logs.\n"
        "- Commit tools are scoped to `insight_logs/*.md`; keep active memory and unrelated files out of Insight commits.\n"
        "- Do not run memory validation; Insight does not edit the memory graph."
    )
```

- [ ] **Step 7: Update CLI-agent role sets**

In `rightmemory/agent_cli.py`, change:

```python
WRITE_ROLES = {"dreamer", "insight", "pruner", "reviewer", "sync-reconciler", "update"}
```

- [ ] **Step 8: Run prompt and agent CLI tests**

Run:

```bash
python -m unittest tests.test_config.PromptTests tests.test_agent_cli.AgentCliCommandTests.test_build_codex_uses_workspace_write_for_insight
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add rightmemory/prompts/insight.md rightmemory/prompts/dreamer.md rightmemory/prompt.py rightmemory/agent_cli.py tests/test_config.py tests/test_agent_cli.py
git commit -m "feat: add insight role prompt"
```

## Task 3: Role-Aware Tool And Isolated Write Boundaries

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/isolated_write.py`
- Test: `tests/test_tools.py`
- Test: `tests/test_isolated_write.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing tool tests**

In `tests/test_tools.py`, add:

```python
def test_insight_tools_commit_only_insight_logs(self):
    self._git("init")
    self._git("config", "user.email", "test@example.com")
    self._git("config", "user.name", "Test User")
    (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
    (self.root / "insight_logs").mkdir()
    (self.root / "insight_logs" / "2026-05-30-143012.md").write_text("# Insight\n", encoding="utf-8")
    tools = MemoryTools(self.root, role="insight")

    result = tools.git_add(["insight_logs/2026-05-30-143012.md"])

    self.assertEqual(result, "staged: insight_logs/2026-05-30-143012.md")
    with self.assertRaisesRegex(ValueError, "insight_logs/\\*.md"):
        tools.git_add(["MEMORY.md"])

def test_dreamer_tools_reject_dream_logs(self):
    self._git("init")
    (self.root / "dream_logs").mkdir()
    (self.root / "dream_logs" / "2026-05-30.md").write_text("# Dream\n", encoding="utf-8")
    tools = MemoryTools(self.root, role="dreamer")

    with self.assertRaisesRegex(ValueError, "MEMORY.md or MEMORY_\\*.md"):
        tools.git_add(["dream_logs/2026-05-30.md"])

def test_insight_create_file_rejects_active_memory(self):
    tools = MemoryTools(self.root, role="insight")

    with self.assertRaisesRegex(ValueError, "can only write insight_logs/\\*.md"):
        tools.create_file("MEMORY_new.md", "# New\n")
```

Replace the existing `test_git_add_accepts_memory_files_and_dream_logs` with:

```python
def test_git_add_accepts_active_memory_files(self):
    self._git("init")
    self._git("config", "user.email", "test@example.com")
    self._git("config", "user.name", "Test User")
    (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
    (self.root / "MEMORY_detail.md").write_text("# Detail\n", encoding="utf-8")
    (self.root / "rightmemory.toml").write_text("[update]\n", encoding="utf-8")

    result = self.tools.git_add(["MEMORY.md", "MEMORY_detail.md"])

    self.assertEqual(result, "staged: MEMORY.md, MEMORY_detail.md")
    status = self.tools.git_status()
    self.assertIn("A  MEMORY.md", status)
    self.assertIn("A  MEMORY_detail.md", status)
    self.assertIn("?? rightmemory.toml", status)

    with self.assertRaises(ValueError):
        self.tools.git_add(["rightmemory.toml"])
```

Replace the existing `test_git_discard_reverts_allowed_tracked_changes` with:

```python
def test_git_discard_reverts_allowed_tracked_memory_changes(self):
    self._git("init")
    self._git("config", "user.email", "test@example.com")
    self._git("config", "user.name", "Test User")
    memory = self.root / "MEMORY.md"
    detail = self.root / "MEMORY_detail.md"
    memory.write_text("# Domain\n", encoding="utf-8")
    detail.write_text("# Detail\n", encoding="utf-8")
    self._git("add", "MEMORY.md", "MEMORY_detail.md")
    self._git("commit", "-m", "initial memory")
    memory.write_text("# Broken\n", encoding="utf-8")
    detail.write_text("# Broken\n", encoding="utf-8")

    result = self.tools.git_discard(["MEMORY.md", "MEMORY_detail.md"])

    self.assertEqual(result, "discarded: MEMORY.md, MEMORY_detail.md")
    self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
    self.assertEqual(detail.read_text(encoding="utf-8"), "# Detail\n")
    self.assertEqual(self.tools.git_status(), "")
```

- [ ] **Step 2: Write failing isolated write tests**

In `tests/test_isolated_write.py`, add:

```python
def test_insight_commit_lands_insight_log(self):
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
        self._append_memory(worktree, "- `two` invalid insight memory edit\n")
        self._git("add", "MEMORY.md", cwd=worktree)
        self._git("commit", "-m", "insight: invalid memory edit", cwd=worktree)

    with self.assertRaisesRegex(RuntimeError, "non-insight paths: MEMORY\\.md"):
        IsolatedWriteSupervisor(self.root, "insight").run(callback)

    self.assertNotIn("invalid insight", (self.root / "MEMORY.md").read_text(encoding="utf-8"))

def test_untracked_insight_log_blocks_insight_before_callback(self):
    called = False
    insight = self.root / "insight_logs" / "2026-05-30-143012.md"
    insight.parent.mkdir()
    insight.write_text("# Insight\n", encoding="utf-8")

    def callback(_worktree: Path) -> None:
        nonlocal called
        called = True

    with self.assertRaises(MainMemoryDirtyError) as caught:
        IsolatedWriteSupervisor(self.root, "insight").run(callback)

    self.assertEqual(caught.exception.paths, ("insight_logs/2026-05-30-143012.md",))
    self.assertFalse(called)
```

Update the current `test_untracked_main_dream_log_blocks_before_callback` to assert retired artifacts no longer block Dreamer:

```python
def test_untracked_main_dream_log_does_not_block_dreamer(self):
    called = False
    dream_log = self.root / "dream_logs" / "2026-05-22.md"
    dream_log.parent.mkdir()
    dream_log.write_text("# Dream\n", encoding="utf-8")

    def callback(_worktree: Path) -> None:
        nonlocal called
        called = True

    result = IsolatedWriteSupervisor(self.root, "dreamer").run(callback)

    self.assertIsNone(result.output)
    self.assertTrue(called)
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_insight_tools_commit_only_insight_logs tests.test_tools.MemoryToolsTests.test_dreamer_tools_reject_dream_logs tests.test_tools.MemoryToolsTests.test_insight_create_file_rejects_active_memory tests.test_isolated_write.IsolatedWriteSupervisorTests.test_insight_commit_lands_insight_log
```

Expected: failures because `MemoryTools` and isolated writes are not role-aware.

- [ ] **Step 4: Add role-aware path helpers in tools**

In `rightmemory/tools.py`, add:

```python
INSIGHT_LOG_FILE_RE = re.compile(r"^insight_logs/[A-Za-z0-9_.-]+\.md$")

ACTIVE_MEMORY_ROLES = {"dreamer", "pruner", "reviewer", "sync-reconciler", "update"}
INSIGHT_ROLES = {"insight"}
```

Change constructor:

```python
def __init__(self, memory_root: Path, role: str | None = None):
    self.memory_root = memory_root.resolve()
    self.role = role
    self._read_signatures: dict[Path, str] = {}
```

Add helpers:

```python
def _write_policy_label(self) -> str:
    if self.role in INSIGHT_ROLES:
        return "insight_logs/*.md"
    return "MEMORY.md or MEMORY_*.md"

def _is_allowed_write_path(self, relative_path: str) -> bool:
    if self.role in INSIGHT_ROLES:
        return bool(INSIGHT_LOG_FILE_RE.fullmatch(relative_path))
    return relative_path == "MEMORY.md" or bool(MEMORY_DETAIL_FILE_RE.fullmatch(relative_path))

def _allowed_write_path(self, path: str) -> str:
    resolved = self._resolve_path(path)
    relative_path = resolved.relative_to(self.memory_root).as_posix()
    if self._is_allowed_write_path(relative_path):
        return relative_path
    raise ValueError(f"can only write {self._write_policy_label()}: {relative_path}")
```

Call `_allowed_write_path(path)` at the start of `edit_file`, `create_file`, `delete_file`, and for both old and new paths in `rename_file`.

Replace `_allowed_commit_path` with role-aware logic:

```python
def _allowed_commit_path(self, path: str) -> str:
    resolved = self._resolve_path(path)
    relative_path = resolved.relative_to(self.memory_root).as_posix()
    if self._is_allowed_write_path(relative_path):
        return relative_path
    raise ValueError(f"can only stage, commit, or discard {self._write_policy_label()}: {relative_path}")
```

- [ ] **Step 5: Update runtime tool construction**

In `rightmemory/runtime.py`, construct tools with the role:

```python
self.tools = MemoryTools(config.memory_root, role=config.role)
```

In `_agent_tools`, remove `validate_memory` from base read tools and add it back for non-Insight roles:

```python
read_tools = [
    self._agent_tool(self.tools.glob),
    self._agent_tool(self.tools.grep),
    self._agent_tool(self.tools.read),
    self._agent_tool(self.tools.read_command),
    self._agent_tool(self.tools.outline_file),
]
if self.config.role != "insight":
    read_tools.append(self._agent_tool(self.tools.validate_memory))
```

- [ ] **Step 6: Update isolated write validation**

In `rightmemory/isolated_write.py`, replace `MEMORY_WRITE_PATHS` with:

```python
ACTIVE_MEMORY_WRITE_PATHS = ("MEMORY.md", "MEMORY_*.md")
INSIGHT_WRITE_PATHS = ("insight_logs/*.md",)
```

Change `_dirty_memory_files`:

```python
def _dirty_memory_files(self) -> list[str]:
    status = self._git_stdout(self.memory_root, "status", "--porcelain", "--", *self._write_paths())
    return _porcelain_paths(status)

def _write_paths(self) -> tuple[str, ...]:
    if self.role == "insight":
        return INSIGHT_WRITE_PATHS
    return ACTIVE_MEMORY_WRITE_PATHS
```

Change invalid path reporting:

```python
invalid_paths = {path for path in changed_paths if not self._is_role_write_path(path)}
if invalid_paths:
    paths = ", ".join(sorted(invalid_paths))
    label = "non-insight paths" if self.role == "insight" else "non-memory paths"
    raise RuntimeError(f"isolated commit touches {label}: {paths}")
```

Add:

```python
def _is_role_write_path(self, path: str) -> bool:
    if self.role == "insight":
        return bool(INSIGHT_LOG_FILE_RE.fullmatch(path))
    return path == "MEMORY.md" or bool(MEMORY_DETAIL_FILE_RE.fullmatch(path))
```

Import `INSIGHT_LOG_FILE_RE` from `rightmemory.tools`.

In `_validate_commit_tree`, require `MEMORY.md` for active memory roles and skip that requirement for Insight:

```python
if self.role != "insight":
    self._validate_regular_memory_path(worktree, commit, "MEMORY.md", required=True)
for path in sorted(changed_paths - {"MEMORY.md"}):
    self._validate_regular_memory_path(worktree, commit, path, required=False)
```

Run `MemoryTools(worktree, role=self.role).validate_memory()` only when `self.role != "insight"`.

- [ ] **Step 7: Update runtime tests for Insight tool list**

In `tests/test_config.py`, add to `RuntimeTests`:

```python
def test_insight_role_tools_exclude_memory_validation(self):
    config = RuntimeConfig(
        role="insight",
        model_id="openai/test",
        memory_root=Path(self.tempdir.name),
        state_root=Path(self.tempdir.name) / "state",
    )

    with patch.dict("sys.modules", self._fake_pydantic_modules()):
        runtime = RightMemoryRuntime(config)

    tool_names = [tool.__name__ for tool in runtime.agent.tools]
    self.assertIn("create_file", tool_names)
    self.assertIn("git_commit", tool_names)
    self.assertNotIn("validate_memory", tool_names)
```

- [ ] **Step 8: Run tool and isolated write tests**

Run:

```bash
python -m unittest tests.test_tools tests.test_isolated_write tests.test_config.RuntimeTests.test_insight_role_tools_exclude_memory_validation
```

Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add rightmemory/tools.py rightmemory/runtime.py rightmemory/isolated_write.py tests/test_tools.py tests/test_isolated_write.py tests/test_config.py
git commit -m "feat: enforce insight write boundaries"
```

## Task 4: Runtime Cycle Entry Points

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/agent_cli.py`
- Modify: `rightmemory/cli.py`
- Test: `tests/test_config.py`
- Test: `tests/test_agent_cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing runtime cycle test**

In `tests/test_config.py`, add to `RuntimeTests`:

```python
def test_run_cycle_passes_operator_hint_message(self):
    config = RuntimeConfig(
        role="insight",
        model_id="openai/test",
        memory_root=Path(self.tempdir.name),
        state_root=Path(self.tempdir.name) / "state",
    )

    with patch.dict("sys.modules", self._fake_pydantic_modules()):
        runtime = RightMemoryRuntime(config)
        result = runtime.run_cycle("insight-watch", operator_hint="focus on risks")

    self.assertEqual(result, "reply 1")
    sent = runtime.agent.calls[0]["message"]
    self.assertIn("<rightmemory_cycle>", sent)
    self.assertIn("role: insight", sent)
    self.assertIn("operator_hint: focus on risks", sent)
```

- [ ] **Step 2: Write failing CLI-agent prompt test**

In `tests/test_agent_cli.py`, add to `CliAgentExecutorTests`:

```python
def test_cli_agent_cycle_prompt_marks_operator_hint(self):
    prompts = []

    def fake_build_codex_command(memory_root, role, config, prompt, provider_session_id):
        prompts.append(prompt)
        return ["codex", "exec"]

    with (
        patch("rightmemory.agent_cli.build_codex_command", fake_build_codex_command),
        patch("rightmemory.agent_cli._run_cli", return_value='{"type":"thread.started","thread_id":"t1"}\n{"item":{"type":"agent_message","text":"done"}}\n'),
    ):
        executor = CliAgentExecutor(Path("/memory"), "insight", AgentCliConfig(provider="codex"))
        executor.run_session_turn("insight-watch", "<rightmemory_cycle>\nrole: insight\noperator_hint: none\n</rightmemory_cycle>")

    self.assertIn("Caller message:", prompts[0])
    self.assertIn("<rightmemory_cycle>", prompts[0])
    self.assertIn("operator_hint: none", prompts[0])
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_run_cycle_passes_operator_hint_message
```

Expected: `RightMemoryRuntime` has no `run_cycle`.

- [ ] **Step 4: Implement cycle entry point**

In `rightmemory/runtime.py`, add:

```python
CYCLE_ROLES = {"dreamer", "insight"}
```

Add public method:

```python
def run_cycle(self, session_id: str, operator_hint: str | None = None) -> str:
    if self.config.role not in CYCLE_ROLES:
        raise ValueError("run_cycle requires dreamer or insight role")
    hint = (operator_hint or "none").strip() or "none"
    message = "\n".join(
        (
            "<rightmemory_cycle>",
            f"role: {self.config.role}",
            f"operator_hint: {hint}",
            "</rightmemory_cycle>",
        )
    )
    return self.run_session_turn(session_id, message)
```

This keeps the model input text-based while making scheduled callers use a cycle API instead of a synthetic user-like sentence.

- [ ] **Step 5: Update CLI manual hint handling**

In `rightmemory/cli.py`, keep normal `rightmemory insight --session <session-id> "<hint>"` routed through `_session_turn`. Scheduled watchers will call `runtime.run_cycle(session_id)`.

- [ ] **Step 6: Run cycle tests**

Run:

```bash
python -m unittest tests.test_config.RuntimeTests.test_run_cycle_passes_operator_hint_message tests.test_agent_cli.CliAgentExecutorTests.test_cli_agent_cycle_prompt_marks_operator_hint
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/runtime.py rightmemory/cli.py rightmemory/agent_cli.py tests/test_config.py tests/test_agent_cli.py
git commit -m "feat: add role cycle entry point"
```

## Task 5: CLI Insight Watch And Trigger Increments

**Files:**
- Modify: `rightmemory/cli.py`
- Test: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI watch tests**

Update imports in `tests/test_cli.py`:

```python
from rightmemory.cli import _daemon_stdio_json, _dreamer_watch_once, _handle_json_request, _insight_watch_once, main
from rightmemory.config import DreamerWatchConfig, InsightWatchConfig
from rightmemory.insight_trigger import InsightTriggerStore
```

Add helper:

```python
def _insight_watch_config(
    memory_root: Path | None = None,
    trigger_points: float = 150.0,
    update_candidate_points: float = 1.0,
    review_session_points: float = 1.5,
    check_interval_seconds: int = 3000,
):
    return InsightWatchConfig(
        memory_root=Path("/unused") if memory_root is None else memory_root,
        trigger_points=trigger_points,
        update_candidate_points=update_candidate_points,
        review_session_points=review_session_points,
        check_interval_seconds=check_interval_seconds,
    )
```

Add tests near Dreamer watch tests:

```python
def test_insight_watch_once_runs_and_consumes_threshold_on_success(self):
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = []

    with tempfile.TemporaryDirectory() as tempdir:
        memory_root = Path(tempdir)
        InsightTriggerStore(memory_root).increment(155.0)
        watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0)

        def run_cycle(session_id: str) -> str:
            calls.append(session_id)
            return f"session {session_id}: insight"

        with patch("sys.stdout", stdout), patch("sys.stderr", stderr):
            result = _insight_watch_once(watch_config, "insight-watch", run_cycle)
        trigger = InsightTriggerStore(memory_root).read()

    self.assertEqual(result, "succeeded")
    self.assertEqual(calls, ["insight-watch"])
    self.assertEqual(trigger.points, 5.0)
    self.assertIsNotNone(trigger.last_successful_insight_at)
    self.assertIn("rightmemory insight cycle", stdout.getvalue())
    self.assertIn("session insight-watch: insight", stdout.getvalue())
    self.assertEqual(stderr.getvalue(), "")

def test_insight_watch_cli_uses_cycle_entry_point(self):
    stdout = io.StringIO()
    stderr = io.StringIO()
    calls = []

    class RecordingRuntime(FakeRuntime):
        def run_cycle(self, session_id: str, operator_hint=None) -> str:
            calls.append((session_id, operator_hint))
            return f"cycle {session_id}: {operator_hint}"

    with tempfile.TemporaryDirectory() as tempdir:
        memory_root = Path(tempdir)
        InsightTriggerStore(memory_root).increment(151.0)
        runtime_config = type("Config", (), {"memory_root": memory_root})()
        watch_config = _insight_watch_config(memory_root=memory_root, trigger_points=150.0, check_interval_seconds=9)
        with (
            patch("rightmemory.cli.load_insight_watch_config", return_value=watch_config),
            patch("rightmemory.cli.load_config", return_value=runtime_config),
            patch("rightmemory.cli.RightMemoryRuntime", RecordingRuntime),
            patch("rightmemory.cli.WATCH_REFRESH_POLL_SECONDS", 999999),
            patch("rightmemory.cli.time.sleep", side_effect=KeyboardInterrupt) as sleep,
            patch("sys.stdout", stdout),
            patch("sys.stderr", stderr),
        ):
            result = main(["insight", "watch"])
        trigger = InsightTriggerStore(memory_root).read()

    self.assertEqual(result, 130)
    self.assertEqual(calls, [("insight-watch", None)])
    self.assertEqual(trigger.points, 1.0)
    sleep.assert_called_once_with(9)
    self.assertIn("rightmemory insight cycle", stdout.getvalue())
    self.assertIn("rightmemory insight watch stopped", stderr.getvalue())
```

Update Dreamer watch test `test_dreamer_watch_cli_uses_trigger_config_and_runs_when_points_are_available` to expect `run_cycle` instead of `run_session_turn`:

```python
class RecordingRuntime(FakeRuntime):
    def run_cycle(self, session_id: str, operator_hint=None) -> str:
        calls.append((session_id, operator_hint))
        return f"cycle {session_id}: {operator_hint}"
```

Expected call:

```python
self.assertEqual(calls, [("dreamer-watch", None)])
```

- [ ] **Step 2: Write failing trigger increment tests**

Add:

```python
def test_review_scan_increments_dreamer_and_insight_triggers(self):
    class FakeScanner:
        def __init__(self, config, run_reviewer, *, on_review_success=None):
            self.on_review_success = on_review_success

        def scan_once(self, *, require_full_batch=False):
            self.on_review_success(2)
            return FakeReviewResult("reviewed: 2", reviewed=2)

    with tempfile.TemporaryDirectory() as tempdir:
        memory_root = Path(tempdir)
        config = type("Config", (), {"memory_root": memory_root})()
        with (
            patch("rightmemory.cli.load_config", return_value=config),
            patch("rightmemory.cli.load_review_config", return_value=object()),
            patch("rightmemory.cli.load_dreamer_watch_config", return_value=_dreamer_watch_config(memory_root=memory_root, review_session_points=1.5)),
            patch("rightmemory.cli.load_insight_watch_config", return_value=_insight_watch_config(memory_root=memory_root, review_session_points=1.5)),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("rightmemory.cli.ReviewScanner", FakeScanner),
        ):
            result = main(["review", "scan", "--once"])

    self.assertEqual(result, 0)
    self.assertEqual(DreamerTriggerStore(memory_root).read().points, 3.0)
    self.assertEqual(InsightTriggerStore(memory_root).read().points, 3.0)
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_handle_json_request tests.test_cli
```

Expected: failures for missing `_insight_watch_once`, `load_insight_watch_config` import wiring, and cycle entry usage.

- [ ] **Step 4: Implement CLI insight watch**

In `rightmemory/cli.py`, import:

```python
load_insight_watch_config,
InsightTriggerStore,
```

Add constants:

```python
DEFAULT_INSIGHT_WATCH_RETRY_SECONDS = 60
INSIGHT_WATCH_SESSION_ID = "insight-watch"
_INSIGHT_WATCH_SKIPPED = "skipped"
_INSIGHT_WATCH_SUCCEEDED = "succeeded"
_INSIGHT_WATCH_FAILED = "failed"
```

Route role `watch`:

```python
if remaining and remaining[0] == "watch":
    if args.role == "dreamer":
        if _is_help_request(remaining[1:]):
            _dreamer_watch_parser().parse_args(remaining[1:])
            return 0
        watch_args = _dreamer_watch_parser().parse_args(remaining[1:])
        return _dreamer_watch(watch_args.interval, watch_args.session)
    if args.role == "insight":
        if _is_help_request(remaining[1:]):
            _insight_watch_parser().parse_args(remaining[1:])
            return 0
        watch_args = _insight_watch_parser().parse_args(remaining[1:])
        return _insight_watch(watch_args.interval, watch_args.session)
    raise ValueError("watch is supported for dreamer and insight roles")
```

Add parser:

```python
def _insight_watch_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rightmemory insight watch")
    parser.add_argument("--interval", type=int, default=None, help="override trigger check interval in seconds")
    parser.add_argument(
        "--session",
        default=INSIGHT_WATCH_SESSION_ID,
        help="persist insight message history under this session id",
    )
    return parser
```

Add cycle helpers:

```python
def _run_dream_cycle(session_id: str, dreamer_config: Any | None = None) -> str:
    config = dreamer_config if dreamer_config is not None else load_config("dreamer")
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_cycle(session_id)
    finally:
        runtime.cleanup()


def _run_insight_cycle(session_id: str, insight_config: Any | None = None) -> str:
    config = insight_config if insight_config is not None else load_config("insight")
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_cycle(session_id)
    finally:
        runtime.cleanup()
```

Add `_insight_watch_once`:

```python
def _insight_watch_once(watch_config: Any, session_id: str, run_cycle: Callable[[str], str]) -> str:
    store = InsightTriggerStore(watch_config.memory_root)
    state = store.read()
    if state.points < watch_config.trigger_points:
        return _INSIGHT_WATCH_SKIPPED

    timestamp = datetime.now(UTC).isoformat()
    print(f"[{timestamp}] rightmemory insight cycle", flush=True)
    try:
        output = run_cycle(session_id)
    except Exception as exc:
        print(f"rightmemory insight cycle failed: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return _INSIGHT_WATCH_FAILED

    print(output, flush=True)
    store.consume_if_available(watch_config.trigger_points)
    return _INSIGHT_WATCH_SUCCEEDED
```

Add `_insight_watch`:

```python
def _insight_watch(interval: int | None, session_id: str) -> int:
    if interval is not None and interval < 1:
        raise ValueError("--interval must be a positive integer")
    watch_config = load_insight_watch_config()
    if interval is not None:
        watch_config = replace(watch_config, check_interval_seconds=interval)
    insight_config = load_config("insight")
    refresh = InstallStamp(watch_config.memory_root)
    consecutive_failures = 0
    exit_code = 0
    try:
        with _watch_stop_signal("insight") as stop, WatchLock(watch_config.memory_root, "insight"):
            next_config: Any | None = insight_config
            while not stop.requested:
                _reexec_if_install_changed(refresh, stop)

                def run_cycle(current_session_id: str) -> str:
                    nonlocal next_config
                    if next_config is None:
                        return _run_insight_cycle(current_session_id)
                    output = _run_insight_cycle(current_session_id, next_config)
                    next_config = None
                    return output

                status = _insight_watch_once(watch_config, session_id, run_cycle)
                _reexec_if_install_changed(refresh, stop)
                if status == _INSIGHT_WATCH_SKIPPED:
                    consecutive_failures = 0
                    if not _sleep_with_refresh_check(watch_config.check_interval_seconds, refresh, stop):
                        break
                elif status == _INSIGHT_WATCH_FAILED:
                    consecutive_failures += 1
                    if _watch_failure_limit_reached("insight", consecutive_failures):
                        exit_code = 1
                        break
                    retry_seconds = min(watch_config.check_interval_seconds, DEFAULT_INSIGHT_WATCH_RETRY_SECONDS)
                    if not _sleep_with_refresh_check(retry_seconds, refresh, stop):
                        break
                else:
                    consecutive_failures = 0
        print("rightmemory insight watch stopped", file=sys.stderr)
        return exit_code
    except KeyboardInterrupt:
        print("rightmemory insight watch stopped", file=sys.stderr)
        return 130
```

- [ ] **Step 5: Increment both triggers after update and review**

Add helper:

```python
def _combined_trigger_incrementer(*incrementers: Callable[[int], None]) -> Callable[[int], None]:
    def increment(count: int) -> None:
        for item in incrementers:
            item(count)
    return increment
```

Rename `_dreamer_trigger_incrementer` to a role-neutral helper or add:

```python
def _insight_trigger_incrementer(memory_root: Path, points_per_item: float) -> Callable[[int], None]:
    store = InsightTriggerStore(memory_root)

    def increment(count: int) -> None:
        try:
            store.increment(count * points_per_item)
        except OSError as exc:
            print(
                f"Warning: could not update insight trigger state: {type(exc).__name__}: {exc}",
                file=sys.stderr,
                flush=True,
            )

    return increment
```

In `_run_review_scan_with_config`, load both configs and pass:

```python
on_review_success=_combined_trigger_incrementer(
    _dreamer_trigger_incrementer(reviewer_config.memory_root, dreamer_watch_config.review_session_points),
    _insight_trigger_incrementer(reviewer_config.memory_root, insight_watch_config.review_session_points),
),
```

In `_async_worker`, use the same combined pattern with update candidate points.

- [ ] **Step 6: Run CLI tests**

Run:

```bash
python -m unittest tests.test_cli
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: add insight watch cycles"
```

## Task 6: Managed Watch And Status Dashboard

**Files:**
- Modify: `rightmemory/watch.py`
- Modify: `rightmemory/status.py`
- Test: `tests/test_cli.py`
- Test: `tests/test_status.py`

- [ ] **Step 1: Write failing managed watch tests**

In `tests/test_cli.py`, update `test_watch_start_starts_review_dreamer_and_pruner_managed_processes` to expect four processes and roles:

```python
side_effect=[FakeProcess(101), FakeProcess(102), FakeProcess(103), FakeProcess(104)]
```

Read `insight.pid`:

```python
insight_pid = (memory_root / ".runtime" / "watch" / "insight.pid").read_text(encoding="utf-8")
```

Expected assertions:

```python
self.assertEqual(roles, ["reviewer", "dreamer", "pruner", "insight"])
self.assertEqual(popen.call_count, 4)
self.assertEqual(insight_pid, "104\n")
self.assertIn("insight: running pid 104", stdout.getvalue())
```

- [ ] **Step 2: Write failing status tests**

In `tests/test_status.py`, import `collect_insight_section` after it exists. Add:

```python
def test_collect_insight_section_reports_trigger_progress(self):
    state = type(
        "InsightState",
        (),
        {
            "points": 88.0,
            "updated_at": "2026-05-30T08:00:00+00:00",
            "last_successful_insight_at": "2026-05-29T08:00:00+00:00",
            "last_recovery_at": None,
        },
    )()
    config = type("InsightConfig", (), {"trigger_points": 150.0, "check_interval_seconds": 3000})()

    section = collect_insight_section(
        Path("/memory/root"),
        trigger_reader=lambda memory_root: state,
        config_loader=lambda: config,
    )

    self.assertEqual(section.name, "insight")
    self.assertEqual(section.state, "trigger progress")
    self.assertIn("trigger: 88.0/150.0 points", section.detail)
    self.assertIn("check interval: 3000 seconds", section.detail)
```

Add a dashboard formatting assertion to the existing status formatting test:

```python
self.assertIn("Insight", formatted)
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_cli tests.test_status.StatusDashboardTests.test_collect_insight_section_reports_trigger_progress
```

Expected: failures because `insight` is not a managed target and status has no Insight section.

- [ ] **Step 4: Update managed watch**

In `rightmemory/watch.py`:

```python
MANAGED_WATCH_TARGETS = ("review", "dreamer", "pruner", "insight", "sync")
WATCH_COMMANDS = {
    "review": ("review", "watch"),
    "dreamer": ("dreamer", "watch"),
    "pruner": ("prune", "watch"),
    "insight": ("insight", "watch"),
    "sync": ("sync", "watch"),
}
WATCH_CLEANUP_ROLES = {
    "review": "reviewer",
    "dreamer": "dreamer",
    "pruner": "pruner",
    "insight": "insight",
}
```

In `rightmemory/cli.py`, update watch-start docs/messages if tests assert exact output.

- [ ] **Step 5: Add Insight status section**

In `rightmemory/status.py`, import `load_insight_watch_config` and create `_InsightTriggerSnapshot`:

```python
@dataclass(frozen=True)
class _InsightTriggerSnapshot:
    points: float = 0.0
    updated_at: str | None = None
    last_successful_insight_at: str | None = None
    last_recovery_at: str | None = None
```

Add `_read_insight_trigger_snapshot`:

```python
def _read_insight_trigger_snapshot(memory_root: Path) -> _InsightTriggerSnapshot:
    path = Path(memory_root) / ".runtime" / "insight" / "trigger-state.json"
    if not path.exists():
        return _InsightTriggerSnapshot()
    data = _read_json(path)
    points = data.get("points", 0.0)
    if isinstance(points, bool) or not isinstance(points, (int, float)):
        raise ValueError("insight trigger points must be a number")
    points = float(points)
    if not math.isfinite(points) or points < 0:
        raise ValueError("insight trigger points must be a nonnegative finite number")
    return _InsightTriggerSnapshot(
        points=points,
        updated_at=_optional_iso_datetime_str(data.get("updated_at"), "updated_at"),
        last_successful_insight_at=_optional_iso_datetime_str(
            data.get("last_successful_insight_at"),
            "last_successful_insight_at",
        ),
        last_recovery_at=_optional_iso_datetime_str(data.get("last_recovery_at"), "last_recovery_at"),
    )
```

Add:

```python
def collect_insight_section(
    memory_root: Path,
    *,
    trigger_reader: Callable[[Path], object] | None = None,
    config_loader: Callable[[], object] = load_insight_watch_config,
) -> SectionStatus:
    if trigger_reader is None:
        trigger_reader = _read_insight_trigger_snapshot
    try:
        state = trigger_reader(memory_root)
        config = config_loader()
        detail = (
            f"trigger: {getattr(state, 'points')}/{getattr(config, 'trigger_points')} points\n"
            f"check interval: {getattr(config, 'check_interval_seconds')} seconds"
        )
        updated_at = getattr(state, "updated_at", None)
        if updated_at:
            detail += f"\nupdated: {updated_at}"
        last = getattr(state, "last_successful_insight_at", None)
        return SectionStatus(name="insight", state="trigger progress", detail=detail, last=last)
    except Exception as exc:
        return SectionStatus(
            name="insight",
            state=f"error: {type(exc).__name__}: {exc}",
            issue=f"insight trigger error: {type(exc).__name__}: {exc}",
        )
```

Extend `DashboardStatus` with `insight: SectionStatus | None = None`, add `insight_collector` to `collect_status`, and format:

```python
if status.insight is not None:
    lines.append("")
    lines.append("Insight")
    lines.extend(_format_section(status.insight))
```

- [ ] **Step 6: Run watch and status tests**

Run:

```bash
python -m unittest tests.test_cli tests.test_status
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/watch.py rightmemory/status.py tests/test_cli.py tests/test_status.py
git commit -m "feat: surface insight watch status"
```

## Task 7: Sync, Install, Doctor, And Repository Layout

**Files:**
- Modify: `rightmemory/sync.py`
- Modify: `rightmemory/session.py`
- Modify: `rightmemory/doctor.py`
- Modify: `install.sh`
- Test: `tests/test_sync.py`
- Test: `tests/test_config.py`
- Test: `tests/test_install.py`
- Test: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing sync tests**

In `tests/test_sync.py`, add:

```python
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
```

- [ ] **Step 2: Update gitignore tests**

In `tests/test_config.py`, update `test_write_role_creates_memory_lock_and_gitignore` expected `.gitignore`:

```python
"*\n!MEMORY.md\n!MEMORY_*.md\n!insight_logs/\n!insight_logs/*.md\n"
```

In `tests/test_install.py`, update assertions that mention `dream_logs/` to expect `insight_logs/`.

Add an install test that existing `.gitignore` is refreshed:

```python
def test_install_refreshes_memory_gitignore_to_current_allowlist(self):
    memory_root = Path(self.tempdir.name) / "memory"
    skills_root = Path(self.tempdir.name) / "skills"
    memory_root.mkdir()
    (memory_root / ".gitignore").write_text("*\n!MEMORY.md\n!dream_logs/\n!dream_logs/*.md\n", encoding="utf-8")

    result = self._install(memory_root, skills_root)

    self.assertEqual(result.returncode, 0, result.stderr)
    self.assertEqual(
        (memory_root / ".gitignore").read_text(encoding="utf-8"),
        "*\n!MEMORY.md\n!MEMORY_*.md\n!insight_logs/\n!insight_logs/*.md\n",
    )
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_sync.SyncManagerTests.test_push_reports_dirty_insight_log tests.test_sync.SyncManagerTests.test_push_ignores_untracked_retired_dream_log tests.test_config.RuntimeTests.test_write_role_creates_memory_lock_and_gitignore
```

Expected: failures because sync and gitignore still use `dream_logs/`.

- [ ] **Step 4: Update sync paths**

In `rightmemory/sync.py`:

```python
MEMORY_SYNC_PATHS = ("MEMORY.md", "MEMORY_*.md", "insight_logs/*.md")
```

- [ ] **Step 5: Update generated memory gitignore**

In `rightmemory/session.py`:

```python
b"*\n!MEMORY.md\n!MEMORY_*.md\n!insight_logs/\n!insight_logs/*.md\n"
```

- [ ] **Step 6: Update doctor seed root**

In `rightmemory/doctor.py`, change:

```python
(memory_root / "insight_logs").mkdir()
```

Remove the `dream_logs` directory creation.

- [ ] **Step 7: Update installer**

In `install.sh`, update help text from `dream_logs/` to `insight_logs/`.

Change initial baseline file globs:

```bash
files=(MEMORY.md MEMORY_*.md insight_logs/*.md)
```

Change cached diff pathspecs:

```bash
git diff --cached --name-only -- MEMORY.md 'MEMORY_*.md' 'insight_logs/*.md'
```

Create current artifact directory:

```bash
mkdir -p "$MEMORY_ROOT/insight_logs"
```

Replace the `.gitignore` block with unconditional refresh:

```bash
cat > "$MEMORY_ROOT/.gitignore" <<'EOF'
*
!MEMORY.md
!MEMORY_*.md
!insight_logs/
!insight_logs/*.md
EOF
echo "  [refresh] $MEMORY_ROOT/.gitignore  (memory allowlist)"
```

Update final text:

```bash
echo "your existing MEMORY.md, MEMORY_*.md, and insight_logs/ are preserved."
```

- [ ] **Step 8: Update doctor and agent CLI tests**

Run:

```bash
python -m unittest tests.test_agent_cli.AgentCliDoctorTests tests.test_install tests.test_sync tests.test_config.RuntimeTests.test_write_role_creates_memory_lock_and_gitignore
```

Expected: PASS after updating exact test expectations.

- [ ] **Step 9: Commit**

```bash
git add rightmemory/sync.py rightmemory/session.py rightmemory/doctor.py install.sh tests/test_sync.py tests/test_config.py tests/test_install.py tests/test_agent_cli.py
git commit -m "feat: track insight logs in memory layout"
```

## Task 8: Documentation And Project Instructions

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update README as final model prose**

Replace top-level file model bullets with wording like:

```markdown
- **Readable files:** memory lives in `MEMORY.md`, optional `MEMORY_<slug>.md` detail files, and `insight_logs/` reflection artifacts.
```

Update architecture diagram:

```text
  +--> rightmemory dreamer          memory consolidation and commits
  |
  +--> rightmemory insight          reflective Insight logs
```

Update command examples:

```bash
rightmemory insight --session <agent-session-id> "optional focus hint"
rightmemory insight watch
rightmemory insight chat
```

Update everyday use so Dreamer consolidates memory and Insight writes reflection artifacts. Keep the prose as the current model; avoid describing a historical transition.

Update file layout:

```text
~/.rightmemory/
├── .git/
├── MEMORY.md
├── MEMORY_<slug>.md
└── insight_logs/
```

- [ ] **Step 2: Update DESIGN_NOTES**

Revise `Command-backed roles` to include Insight:

```markdown
`insight` reflects over active memory and prior Insight logs, producing durable essay artifacts without editing active memory.
```

Revise `Standalone commit boundary`:

```markdown
Standalone commit tools are role-aware: memory-editing roles commit active memory files, while Insight commits `insight_logs/*.md`.
```

Add a short `Insight logs` section:

```markdown
### Insight logs

Insight logs are a reflective artifact stream inside the memory repo. They are useful when memory activity reveals broader patterns, strategy, risks, recommendations, or next-step ideas that should be preserved without turning them into active memory facts. Insight reads active memory and prior Insight logs, writes timestamped essays, and leaves memory edits to the memory-editing roles.
```

- [ ] **Step 3: Update AGENTS.md**

Update project shape and memory runtime rules to use `insight_logs/` in the current artifact list. Keep Dreamer described as consolidation and Insight as reflection. Remove operational instructions that say runtime memory commits may touch `dream_logs/*.md`; replace with role-aware wording:

```markdown
Runtime memory commits for memory-editing roles are limited to `MEMORY.md` and `MEMORY_*.md`; Insight commits are limited to `insight_logs/*.md`.
```

- [ ] **Step 4: Search for stale public wording**

Run:

```bash
rg -n "dream_logs|dream report|Dream cycles write reports|consolidation, dream logs" README.md DESIGN_NOTES.md AGENTS.md
```

Expected: no matches in `README.md`, `DESIGN_NOTES.md`, or `AGENTS.md`.

- [ ] **Step 5: Commit**

```bash
git add README.md DESIGN_NOTES.md AGENTS.md
git commit -m "docs: describe insight role model"
```

## Task 9: Final Integration And Verification

**Files:**
- No planned file changes; this task verifies the integrated branch and captures exact-output test corrections discovered by the full suite.

- [ ] **Step 1: Run focused role search**

Run:

```bash
rg -n "dream_logs|dream report|DREAMER_WATCH_MESSAGE|Run a scheduled dream cycle|MEMORY_SYNC_PATHS|WRITE_ROLES|AUTOMATIC_WRITE_ROLES|MANAGED_WATCH_TARGETS|ROLE_PROMPTS|ROLES" rightmemory tests install.sh README.md DESIGN_NOTES.md AGENTS.md
```

Expected:

- No `DREAMER_WATCH_MESSAGE`.
- No product prompt or doc requires `dream_logs/`.
- Remaining `dream_logs` matches, if any, are confined to tests that assert retired artifacts are ignored or preserved.

- [ ] **Step 2: Run syntax checks**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: exit code 0.

- [ ] **Step 3: Run full test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 4: Run install smoke test manually**

Run:

```bash
tmp_root="$(mktemp -d)"
./install.sh "$tmp_root/memory" "$tmp_root/skills"
test -d "$tmp_root/memory/insight_logs"
test ! -d "$tmp_root/memory/dream_logs"
cat "$tmp_root/memory/.gitignore"
```

Expected `.gitignore` content:

```text
*
!MEMORY.md
!MEMORY_*.md
!insight_logs/
!insight_logs/*.md
```

- [ ] **Step 5: Inspect Git status**

Run:

```bash
git status --short
```

Expected: clean.

- [ ] **Step 6: Final commit if verification changed test expectations**

If Step 3 required additional exact-output test updates, commit them:

```bash
git add rightmemory tests install.sh README.md DESIGN_NOTES.md AGENTS.md
git commit -m "test: align insight role integration"
```

If Step 3 passed without more edits, skip this commit step.

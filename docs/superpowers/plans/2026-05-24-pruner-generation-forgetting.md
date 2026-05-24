# Pruner Generation Forgetting Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add commit-generation pruning plus explicit historical retrieval for pruned RightMemory entries.

**Architecture:** Add two roles: `pruner` for active-surface forgetting and `historian` for read-only archaeology. Deterministic runtime code decides whether a prune generation is due and builds the pruner turn context; role prompts own semantic judgment and Markdown edits. Git remains the generation boundary and archaeology ledger through `prune:` commits.

**Tech Stack:** Python 3.11 standard library, `unittest`, existing RightMemory runtime/prompt/tool patterns, Git CLI.

---

## Scope Check

This plan keeps pruner and historian in one implementation because historian depends on the `prune:` ledger that pruner writes. The feature remains testable without provider transcripts, external indexes, or update/reviewer changes.

## File Structure

- Create `rightmemory/prune.py`: deterministic generation due checks, prune ledger parsing, and pruner caller-message rendering.
- Create `rightmemory/prompts/pruner.md`: role prompt for deleting unchanged active memory, writing `prune:` commits, and advancing revival grace.
- Create `rightmemory/prompts/historian.md`: role prompt for explicit historical retrieval from `prune:` commits and Git snapshots.
- Create `tests/test_prune.py`: generation threshold, boundary selection, ledger parsing, and pruner message tests.
- Modify `rightmemory/config.py`: add `pruner` and `historian` roles plus `[pruner]` lifecycle config.
- Modify `rightmemory/prompt.py`: register new prompt files and role command/tool guidance.
- Modify `rightmemory/runtime.py`: expose pruner as an isolated write role; expose historian as read-only with Git history tools.
- Modify `rightmemory/tools.py`: add safe Git history read tools and allow controlled empty `prune:` commits.
- Modify `rightmemory/isolated_write.py`: allow isolated empty `prune:` commits to land.
- Modify `rightmemory/agent_cli.py`: map `historian` to read-only CLI permissions and `pruner` to write-capable permissions.
- Modify `rightmemory/cli.py`: add top-level `rightmemory prune` and `rightmemory history` commands.
- Modify `tests/test_config.py`: role registration, prompt assembly, runtime tools, isolation, and config tests.
- Modify `tests/test_tools.py`: safe history tools and empty prune checkpoint commit tests.
- Modify `tests/test_isolated_write.py`: isolated empty prune commit landing test.
- Modify `tests/test_cli.py`: `prune` and `history` command routing tests.
- Modify `README.md` and `DESIGN_NOTES.md`: document active forgetting, `prune:` ledger, and historian retrieval.

## Task 1: Register Roles And Pruner Config

**Files:**
- Modify: `rightmemory/config.py`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/agent_cli.py`
- Create: `rightmemory/prompts/pruner.md`
- Create: `rightmemory/prompts/historian.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_agent_cli.py`

- [ ] **Step 1: Write failing config and prompt tests**

Add tests near the existing config and prompt tests in `tests/test_config.py`:

```python
from rightmemory.config import load_pruner_config


    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_defaults(self):
        config_path = self._write_config(
            """
            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            runtime_config = load_config("pruner")
            pruner_config = load_pruner_config()

        self.assertEqual(runtime_config.role, "pruner")
        self.assertEqual(runtime_config.model_id, "openai/pruner")
        self.assertEqual(pruner_config.generation_commits, 70)
        self.assertEqual(pruner_config.revival_grace_checkpoints, 2)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_accepts_generation_values(self):
        config_path = self._write_config(
            """
            [pruner]
            generation_commits = 12
            revival_grace_checkpoints = 3

            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            runtime_config = load_config("pruner")
            pruner_config = load_pruner_config()

        self.assertEqual(runtime_config.role, "pruner")
        self.assertEqual(pruner_config.generation_commits, 12)
        self.assertEqual(pruner_config.revival_grace_checkpoints, 3)

    @patch("rightmemory.config.MEMORY_ROOT", Path("/home/example/.rightmemory"))
    def test_pruner_config_rejects_bool_generation_commits(self):
        config_path = self._write_config(
            """
            [pruner]
            generation_commits = true

            [pruner.model]
            model_id = "openai/pruner"
            """
        )

        with patch("rightmemory.config.CONFIG_PATH", config_path), patch("pathlib.Path.exists", return_value=True):
            with self.assertRaises(ValueError) as caught:
                load_pruner_config()

        self.assertIn("[pruner].generation_commits must be a positive integer", str(caught.exception))

    def test_standalone_prompts_assemble_for_pruner_and_historian(self):
        for role in ("pruner", "historian"):
            prompt = build_instructions(Path("/home/example/.rightmemory"), role)

            self.assertIn("RightMemory Schema", prompt)
            self.assertIn(f"{role.title()} Role", prompt)
            self.assertIn("Command-selected behavior", prompt)
            self.assertNotIn("{{MEMORY_ROOT}}", prompt)
            self.assertNotIn("{{SKILLS_ROOT}}", prompt)
```

Add CLI-agent role mapping tests in `tests/test_agent_cli.py` near the sandbox tests:

```python
    def test_codex_uses_workspace_write_for_pruner(self):
        command = build_codex_command(Path("/memory/root"), "pruner", AgentCliConfig(provider="codex"), "prompt", None)

        self.assertIn("--sandbox", command)
        self.assertIn("workspace-write", command)

    def test_codex_uses_read_only_for_historian(self):
        command = build_codex_command(Path("/memory/root"), "historian", AgentCliConfig(provider="codex"), "prompt", None)

        self.assertIn("--sandbox", command)
        self.assertIn("read-only", command)
```

- [ ] **Step 2: Run tests to verify role registration is missing**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_config tests.test_agent_cli
```

Expected: FAIL with import or role validation errors for `load_pruner_config`, `pruner`, and `historian`.

- [ ] **Step 3: Add role constants and pruner config loader**

Modify `rightmemory/config.py`:

```python
ROLES = {"dreamer", "historian", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"}
DEFAULT_PRUNER_GENERATION_COMMITS = 70
DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS = 2


@dataclass(frozen=True)
class PrunerConfig:
    memory_root: Path = MEMORY_ROOT
    generation_commits: int = DEFAULT_PRUNER_GENERATION_COMMITS
    revival_grace_checkpoints: int = DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS
```

Update role-key validation in `load_config`:

```python
    allowed_role_keys = {"model", "agent_cli"}
    if role == "dreamer":
        allowed_role_keys.add("watch")
    if role == "pruner":
        allowed_role_keys.update({"generation_commits", "revival_grace_checkpoints"})
```

Add `load_pruner_config()` after `load_dreamer_watch_config()`:

```python
def load_pruner_config() -> PrunerConfig:
    data = _load_raw_config()

    if not MEMORY_ROOT.exists():
        raise FileNotFoundError(f"RightMemory memory root does not exist: {MEMORY_ROOT}")

    _reject_unknown_keys(data, _top_level_keys(), "top-level")
    section = data.get("pruner", {})
    if section is None:
        section = {}
    if not isinstance(section, dict):
        raise ValueError("[pruner] must be a TOML table")
    _reject_unknown_keys(
        section,
        {"model", "agent_cli", "generation_commits", "revival_grace_checkpoints"},
        "[pruner]",
    )

    return PrunerConfig(
        memory_root=MEMORY_ROOT,
        generation_commits=_positive_integer(
            section,
            "generation_commits",
            DEFAULT_PRUNER_GENERATION_COMMITS,
            "[pruner]",
        ),
        revival_grace_checkpoints=_positive_integer(
            section,
            "revival_grace_checkpoints",
            DEFAULT_PRUNER_REVIVAL_GRACE_CHECKPOINTS,
            "[pruner]",
        ),
    )
```

- [ ] **Step 4: Register prompts and command guidance**

Modify `rightmemory/prompt.py`:

```python
ROLE_PROMPTS = {"dreamer", "historian", "pruner", "retrieve", "reviewer", "sync-reconciler", "update"}
```

Add command guidance branches:

```python
    if role == "historian":
        return (
            "- The `rightmemory history` command selected historical retrieval. Treat every caller message as a "
            "read-only archaeology request over pruned memory and Git history.\n"
            "- Return historical matches as historical/pruned memory, not active memory. Do not edit memory files."
        )
    if role == "pruner":
        return (
            "- The `rightmemory prune` command selected active-memory pruning. Treat the caller message as the "
            "current prune generation context.\n"
            "- Edit memory files only when the supplied generation context says pruning is due."
        )
```

Add tool guidance branches:

```python
    if role == "historian":
        return (
            "- Use the provided read-only tools for `read`, `grep`, `glob`, restricted `read_command`, outline, "
            "validation, `git_log`, and `git_show_file`.\n"
            "- Use `git_log` to inspect `prune:` commit ledgers and `git_show_file` to recover memory snapshots."
        )
    if role == "pruner":
        return (
            "- Use the provided tools for memory reads, exact file edits, file lifecycle changes, validation, "
            "git inspection, `git_log`, `git_show_file`, staging, and commits.\n"
            "- Use `git_commit(..., allow_empty=true)` only for `prune: checkpoint` commits that advance the prune ledger."
        )
```

- [ ] **Step 5: Add prompt files**

Create `rightmemory/prompts/pruner.md`:

```markdown
# Pruner Role

## Sources And Scope

- The caller message supplies the prune generation context. It includes the boundary commit, current head, configured generation size, previous prune ledger, and any revival grace carried forward.
- The source of truth for active memory is `MEMORY.md` plus sibling `MEMORY_*.md` files.
- Git history is the generation boundary and archaeology ledger. Do not create separate prune log files or runtime indexes.
- Do not inspect provider transcripts.

## Pruning Behavior

- If the caller message says pruning is not due, do not edit files or commit.
- When pruning is due, compare the supplied boundary snapshot with current memory.
- Remove active memory that crossed the generation without semantic change, except items covered by revival grace.
- Preserve current-generation new or changed memory.
- If a current item matches a prior `Removed` ledger entry, treat it as revived and advance grace according to the supplied ledger.
- If a revived item has spent its configured grace and remains unchanged, it can be removed.
- Skip uncertain semantic matches and record the uncertainty in the commit body.

## Edit And Commit Rules

- Keep the Markdown tree coherent. Remove dangling edges caused by deletion or skip the deletion.
- Run validation before committing.
- Use subject `prune: expired active memory` when memory files change.
- Use subject `prune: checkpoint` when the generation is due but no memory file change is needed.
- The commit body should include `Boundary`, `Generation commits`, `Removed`, `Revival grace`, and `Skipped` sections when they apply.
- Use `git_commit(..., allow_empty=true)` for an empty `prune: checkpoint`.

## Final Reply

- Report whether pruning was due.
- List removed ids, grace ids advanced, skipped ids, and the resulting commit hash or `no commit`.
```

Create `rightmemory/prompts/historian.md`:

```markdown
# Historian Role

## Sources And Scope

- Historical retrieval is explicit archaeology over pruned memory.
- Ordinary active memory retrieval belongs to `rightmemory retrieve`; this role searches Git history and `prune:` ledgers.
- Do not edit memory files or write commits.

## Retrieval Flow

- Search `prune:` commit subjects and bodies for ids, heading paths, topics, summaries, and query terms.
- Inspect matching prune commit bodies to identify removed entries and source files.
- Use Git snapshots such as `<prune-commit>^:<path>` through `git_show_file` to recover the original addressable line and nearby heading context from before removal.
- Search beyond prune commit bodies when the user names a specific id, file path, or phrase and prune ledgers are not enough.

## Output

- Label returned matches as historical/pruned memory, not active memory.
- Include the prune commit, source file, heading path when available, and recovered addressable line.
- Do not rewrite recovered memory in your own words when returning an addressable line.
- If no historical match is strong, say so and include weak candidates only when they may help.
- When returning historical matches, end with: `If this historical memory is useful again, send an update to reactivate it in current memory.`
```

- [ ] **Step 6: Update CLI-agent role permissions**

Modify `rightmemory/agent_cli.py`:

```python
READ_ROLES = {"historian", "retrieve"}
WRITE_ROLES = {"dreamer", "pruner", "reviewer", "sync-reconciler", "update"}
```

- [ ] **Step 7: Run role/config tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_config tests.test_agent_cli
```

Expected: PASS for the tests added in this task, with possible unrelated failures handled in later tasks if they involve missing tools.

- [ ] **Step 8: Commit**

```bash
git add rightmemory/config.py rightmemory/prompt.py rightmemory/agent_cli.py rightmemory/prompts/pruner.md rightmemory/prompts/historian.md tests/test_config.py tests/test_agent_cli.py
git commit -m "feat: register pruner and historian roles"
```

## Task 2: Safe Git History Tools And Empty Prune Commits

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/isolated_write.py`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_isolated_write.py`

- [ ] **Step 1: Write failing MemoryTools tests**

Add tests to `tests/test_tools.py`:

```python
    def test_git_log_filters_prune_commits(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: initial")
        self._git("commit", "--allow-empty", "-m", "prune: checkpoint", "-m", "Removed:\n- `old` path: # Domain")

        result = self.tools.git_log(grep="^prune:", max_count=5)

        self.assertIn("prune: checkpoint", result)
        self.assertIn("Removed:", result)
        self.assertNotIn("memory: initial", result)

    def test_git_show_file_reads_historical_memory_file(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n\n- `old` original → []\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: initial")
        first = self._git("rev-parse", "HEAD")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "prune: expired active memory")

        result = self.tools.git_show_file(first, "MEMORY.md")

        self.assertIn("- `old` original", result)

    def test_git_show_file_rejects_non_memory_path(self):
        self._git("init")

        with self.assertRaisesRegex(ValueError, "historical path must be MEMORY.md or MEMORY_*.md"):
            self.tools.git_show_file("HEAD", "rightmemory.toml")

    def test_git_commit_accepts_empty_prune_checkpoint(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: initial")

        result = self.tools.git_commit("prune: checkpoint", body="Boundary: HEAD", allow_empty=True)

        self.assertIn("committed", result)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")

    def test_git_commit_rejects_empty_non_prune_commit(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: initial")

        with self.assertRaisesRegex(ValueError, "empty commits are limited to prune: subjects"):
            self.tools.git_commit("memory: empty", allow_empty=True)
```

Add runtime tool exposure tests to `tests/test_config.py`:

```python
    def test_pruner_runtime_exposes_write_and_history_tools(self):
        config = RuntimeConfig(role="pruner", model_id="openai/test")

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.kwargs["tools"]}
        self.assertIn("git_log", tool_names)
        self.assertIn("git_show_file", tool_names)
        self.assertIn("git_commit", tool_names)
        self.assertIn("edit_file", tool_names)

    def test_historian_runtime_is_read_only_with_history_tools(self):
        config = RuntimeConfig(role="historian", model_id="openai/test")

        with patch.dict("sys.modules", self._fake_pydantic_modules()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime.agent.kwargs["tools"]}
        self.assertIn("git_log", tool_names)
        self.assertIn("git_show_file", tool_names)
        self.assertIn("read", tool_names)
        self.assertNotIn("edit_file", tool_names)
        self.assertNotIn("git_commit", tool_names)
```

Add an isolated write test to `tests/test_isolated_write.py`:

```python
    def test_empty_prune_checkpoint_commit_lands(self):
        def callback(worktree: Path) -> str:
            self._git("commit", "--allow-empty", "-m", "prune: checkpoint", "-m", "Boundary: HEAD", cwd=worktree)
            return "checkpoint"

        result = IsolatedWriteSupervisor(self.root, "pruner").run(callback)

        self.assertEqual(result.output, "checkpoint")
        self.assertEqual(result.commits_landed, 1)
        self.assertEqual(self._git("log", "-1", "--format=%s"), "prune: checkpoint")
        self.assertEqual(self._git("status", "--short"), "")
```

- [ ] **Step 2: Run tests to verify tools are missing**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_tools tests.test_config tests.test_isolated_write
```

Expected: FAIL for missing `git_log`, `git_show_file`, `allow_empty`, pruner isolation, and historian/pruner tools.

- [ ] **Step 3: Add safe history tools**

Modify `rightmemory/tools.py`:

```python
GIT_REVISION_RE = re.compile(r"^[A-Za-z0-9_.^~/-]+$")


    def git_log(self, grep: str = "^prune:", max_count: int = 20) -> str:
        """Return Git commit subjects and bodies matching a grep pattern."""
        grep = grep.strip()
        if not grep:
            raise ValueError("grep must not be empty")
        if "\x00" in grep or "\n" in grep:
            raise ValueError("grep must not contain NUL bytes or newlines")
        self._validate_positive("max_count", max_count)
        max_count = min(max_count, 200)
        command = [
            "git",
            "log",
            f"--max-count={max_count}",
            "--extended-regexp",
            f"--grep={grep}",
            "--format=commit %H%nsubject %s%n%B%n---",
        ]
        output = self._run_git(command)
        return self._cap_command_output(output) if output else "no matches"

    def git_show_file(self, revision: str, path: str, max_lines: int = FULL_READ_LINE_LIMIT) -> str:
        """Read a memory file from a Git revision."""
        revision = revision.strip()
        if not revision or not GIT_REVISION_RE.fullmatch(revision):
            raise ValueError("revision must be a simple Git revision")
        relative_path = self._historical_memory_path(path)
        self._validate_positive("max_lines", max_lines)
        output = self._run_git(["git", "show", f"{revision}:{relative_path}"])
        lines = output.splitlines()
        selected = lines[:max_lines]
        rendered = "\n".join(f"{line_number}: {line}" for line_number, line in enumerate(selected, start=1))
        if len(lines) > max_lines:
            rendered += f"\n[truncated: showing 1-{max_lines} of {len(lines)} lines]"
        return rendered

    def _historical_memory_path(self, path: str) -> str:
        raw = Path(path)
        if raw.is_absolute() or ".." in raw.parts or len(raw.parts) != 1:
            raise ValueError("historical path must be MEMORY.md or MEMORY_*.md")
        name = raw.as_posix()
        if name != "MEMORY.md" and MEMORY_DETAIL_FILE_RE.match(name) is None:
            raise ValueError("historical path must be MEMORY.md or MEMORY_*.md")
        return name
```

- [ ] **Step 4: Add controlled empty commit support**

Modify `MemoryTools.git_commit` signature and body:

```python
    def git_commit(self, message: str, body: str | None = None, allow_empty: bool = False) -> str:
        """Commit staged memory files and dream logs under the RightMemory root."""
        message = self._validate_commit_subject(message)
        body = self._validate_commit_body(body)
        if allow_empty and not message.startswith("prune:"):
            raise ValueError("empty commits are limited to prune: subjects")
        staged = self._run_git(["git", "diff", "--cached", "--name-only", "--no-renames", "--"])
        staged_files = [line for line in staged.splitlines() if line]
        if not staged_files and not allow_empty:
            raise ValueError("no staged changes to commit")
        for path in staged_files:
            self._allowed_commit_path(path)

        command = ["git", "commit"]
        if allow_empty:
            command.append("--allow-empty")
        command.extend(["-m", message])
        if body is not None:
            command.extend(["-m", body])
        self._run_git(command)
```

Keep the existing return formatting after the command.

- [ ] **Step 5: Expose history tools by role**

Modify `rightmemory/runtime.py`:

```python
AUTOMATIC_WRITE_ROLES = {"dreamer", "pruner", "reviewer", "update"}
HISTORY_READ_ROLES = {"historian", "pruner"}
```

Update `_agent_tools()`:

```python
        read_tools = [
            self._agent_tool(self.tools.glob),
            self._agent_tool(self.tools.grep),
            self._agent_tool(self.tools.read),
            self._agent_tool(self.tools.read_command),
            self._agent_tool(self.tools.outline_file),
            self._agent_tool(self.tools.validate_memory),
        ]
        if self.config.role in HISTORY_READ_ROLES:
            read_tools.extend(
                [
                    self._agent_tool(self.tools.git_log),
                    self._agent_tool(self.tools.git_show_file),
                ]
            )
        if self.config.role in {"historian", "retrieve"}:
            return read_tools
```

- [ ] **Step 6: Allow isolated empty commits to land**

Modify `_land_commits` in `rightmemory/isolated_write.py`:

```python
    def _land_commits(self, commits: list[str]) -> None:
        result = self._run_git(self.memory_root, "cherry-pick", "--allow-empty", *commits, check=False)
        if result.returncode == 0:
            return
        self._run_git(self.memory_root, "cherry-pick", "--abort", check=False)
        raise RuntimeError(_git_error_message(result))
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_tools tests.test_config tests.test_isolated_write
```

Expected: PASS for the tests added in this task.

- [ ] **Step 8: Commit**

```bash
git add rightmemory/tools.py rightmemory/runtime.py rightmemory/isolated_write.py tests/test_tools.py tests/test_config.py tests/test_isolated_write.py
git commit -m "feat: add prune history git tools"
```

## Task 3: Prune Generation Context Module

**Files:**
- Create: `rightmemory/prune.py`
- Create: `tests/test_prune.py`

- [ ] **Step 1: Write failing generation tests**

Create `tests/test_prune.py`:

```python
from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path

from rightmemory.config import PrunerConfig
from rightmemory.prune import (
    PruneDueStatus,
    build_pruner_message,
    latest_prune_commit,
    prune_due_status,
    parse_prune_ledger,
)


class PruneGenerationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", "memory: initial")

    def test_not_due_without_prior_prune_and_short_history(self):
        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertFalse(status.due)
        self.assertEqual(status.commits_since_boundary, 1)
        self.assertIsNone(status.boundary_commit)
        self.assertIn("not due", status.message)

    def test_first_due_uses_generation_ancestor(self):
        self._commit_memory("one")
        self._commit_memory("two")
        self._commit_memory("three")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=2))

        self.assertTrue(status.due)
        self.assertEqual(status.generation_commits, 2)
        self.assertIsNotNone(status.boundary_commit)
        self.assertEqual(status.latest_prune_commit, None)

    def test_subsequent_due_uses_latest_prune_commit(self):
        self._git("commit", "--allow-empty", "-m", "prune: checkpoint", "-m", "Boundary: HEAD")
        boundary = self._git("rev-parse", "HEAD")
        self._commit_memory("one")
        self._commit_memory("two")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=2))

        self.assertTrue(status.due)
        self.assertEqual(status.boundary_commit, boundary)
        self.assertEqual(status.latest_prune_commit, boundary)
        self.assertEqual(status.commits_since_boundary, 2)

    def test_parse_prune_ledger_reads_removed_and_grace(self):
        ledger = parse_prune_ledger(
            """
            Boundary: abc123
            Generation commits: 70

            Removed:
            - `old-node` path: # Domain > ## Topic; topic: cache; summary: Old cache lesson

            Revival grace:
            - `revived-node` grace 1/2; revived from: deadbeef; path: # Domain
            """
        )

        self.assertIn("old-node", ledger.removed_ids)
        self.assertEqual(ledger.grace["revived-node"].used, 1)
        self.assertEqual(ledger.grace["revived-node"].total, 2)

    def test_build_pruner_message_contains_boundary_and_ledger(self):
        status = PruneDueStatus(
            due=True,
            message="prune due",
            memory_root=self.root,
            head_commit="head123",
            boundary_commit="base123",
            latest_prune_commit="prune123",
            commits_since_boundary=70,
            generation_commits=70,
            revival_grace_checkpoints=2,
            latest_prune_body="Removed:\n- `old-node` path: # Domain",
        )

        message = build_pruner_message(status)

        self.assertIn("Prune generation is due.", message)
        self.assertIn("Boundary commit: base123", message)
        self.assertIn("Latest prune ledger", message)
        self.assertIn("old-node", message)

    def _commit_memory(self, token: str):
        path = self.root / "MEMORY.md"
        path.write_text(path.read_text(encoding="utf-8") + f"- `{token}` memory → []\n", encoding="utf-8")
        self._git("add", "MEMORY.md")
        self._git("commit", "-m", f"memory: {token}")

    def _git(self, *args: str) -> str:
        return subprocess.run(
            ["git", *args],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
        ).stdout.strip()


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify module is missing**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_prune
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.prune'`.

- [ ] **Step 3: Implement prune generation context**

Create `rightmemory/prune.py`:

```python
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .config import PrunerConfig


GRACE_RE = re.compile(r"- `([^`]+)` grace (\d+)/(\d+);")
REMOVED_RE = re.compile(r"- `([^`]+)` ")


@dataclass(frozen=True)
class GraceEntry:
    id: str
    used: int
    total: int


@dataclass(frozen=True)
class PruneLedger:
    removed_ids: set[str] = field(default_factory=set)
    grace: dict[str, GraceEntry] = field(default_factory=dict)


@dataclass(frozen=True)
class PruneDueStatus:
    due: bool
    message: str
    memory_root: Path
    head_commit: str | None
    boundary_commit: str | None
    latest_prune_commit: str | None
    commits_since_boundary: int
    generation_commits: int
    revival_grace_checkpoints: int
    latest_prune_body: str = ""


def latest_prune_commit(memory_root: Path) -> str | None:
    output = _git(memory_root, "log", "--max-count=1", "--format=%H", "--extended-regexp", "--grep=^prune:")
    return output.strip() or None


def prune_due_status(memory_root: Path, config: PrunerConfig) -> PruneDueStatus:
    memory_root = Path(memory_root)
    head = _git(memory_root, "rev-parse", "HEAD").strip()
    latest = latest_prune_commit(memory_root)
    if latest is not None:
        count = int(_git(memory_root, "rev-list", "--count", f"{latest}..HEAD").strip() or "0")
        due = count >= config.generation_commits
        body = _git(memory_root, "log", "--max-count=1", "--format=%B", latest)
        return PruneDueStatus(
            due=due,
            message=_status_message(due, count, config.generation_commits, latest),
            memory_root=memory_root,
            head_commit=head,
            boundary_commit=latest if due else None,
            latest_prune_commit=latest,
            commits_since_boundary=count,
            generation_commits=config.generation_commits,
            revival_grace_checkpoints=config.revival_grace_checkpoints,
            latest_prune_body=body,
        )

    total = int(_git(memory_root, "rev-list", "--count", "HEAD").strip() or "0")
    due = total >= config.generation_commits
    boundary = _first_boundary(memory_root, config.generation_commits) if due else None
    return PruneDueStatus(
        due=due,
        message=_status_message(due, total, config.generation_commits, None),
        memory_root=memory_root,
        head_commit=head,
        boundary_commit=boundary,
        latest_prune_commit=None,
        commits_since_boundary=total,
        generation_commits=config.generation_commits,
        revival_grace_checkpoints=config.revival_grace_checkpoints,
    )


def parse_prune_ledger(body: str) -> PruneLedger:
    removed: set[str] = set()
    grace: dict[str, GraceEntry] = {}
    section = ""
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if line == "Removed:":
            section = "removed"
            continue
        if line == "Revival grace:":
            section = "grace"
            continue
        if line.endswith(":") and not line.startswith("- "):
            section = ""
            continue
        if section == "removed":
            match = REMOVED_RE.match(line)
            if match is not None:
                removed.add(match.group(1))
        if section == "grace":
            match = GRACE_RE.match(line)
            if match is not None:
                item_id = match.group(1)
                grace[item_id] = GraceEntry(id=item_id, used=int(match.group(2)), total=int(match.group(3)))
    return PruneLedger(removed_ids=removed, grace=grace)


def build_pruner_message(status: PruneDueStatus) -> str:
    if not status.due:
        return status.message
    return f"""Prune generation is due.

Memory root: {status.memory_root}
Head commit: {status.head_commit}
Boundary commit: {status.boundary_commit}
Latest prune commit: {status.latest_prune_commit or "none"}
Commits since boundary: {status.commits_since_boundary}
Generation commits: {status.generation_commits}
Revival grace checkpoints: {status.revival_grace_checkpoints}

Instructions:
- Compare MEMORY.md and MEMORY_*.md at the boundary commit with current memory.
- Remove unchanged active memory that crossed this generation without semantic change.
- Preserve current-generation new or changed memory.
- Preserve revived memory while its grace checkpoints remain.
- Commit with `prune: expired active memory` when memory files change.
- Commit with `prune: checkpoint` and allow_empty=true when a due generation needs a ledger checkpoint but no file edits.

Latest prune ledger:
{status.latest_prune_body or "(none)"}
"""


def _first_boundary(memory_root: Path, generation_commits: int) -> str:
    candidate = _git(memory_root, "rev-parse", f"HEAD~{generation_commits}", check=False)
    if candidate.strip():
        return candidate.strip()
    return _git(memory_root, "rev-list", "--max-parents=0", "HEAD").splitlines()[0].strip()


def _status_message(due: bool, count: int, threshold: int, latest: str | None) -> str:
    boundary = latest or "repository start"
    state = "due" if due else "not due"
    return f"prune: {state}: {count}/{threshold} commits since {boundary}"


def _git(memory_root: Path, *args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=memory_root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")
    return result.stdout
```

- [ ] **Step 4: Run prune tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_prune
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/prune.py tests/test_prune.py
git commit -m "feat: add prune generation context"
```

## Task 4: Top-Level `rightmemory prune` Command

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add tests to `tests/test_cli.py`:

```python
    def test_prune_not_due_does_not_load_runtime(self):
        stdout = io.StringIO()
        status = type(
            "Status",
            (),
            {
                "due": False,
                "message": "prune: not due: 2/70 commits since repository start",
            },
        )()

        with (
            patch("rightmemory.cli.load_pruner_config", return_value=object()),
            patch("rightmemory.cli.prune_due_status", return_value=status),
            patch("rightmemory.cli.RightMemoryRuntime", side_effect=AssertionError("runtime should not start")),
            patch("sys.stdout", stdout),
        ):
            result = main(["prune"])

        self.assertEqual(result, 0)
        self.assertEqual(stdout.getvalue().strip(), "prune: not due: 2/70 commits since repository start")

    def test_prune_due_runs_pruner_session(self):
        stdout = io.StringIO()
        roles = []
        status = type("Status", (), {"due": True})()

        def fake_load_config(role):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_pruner_config", return_value=object()),
            patch("rightmemory.cli.prune_due_status", return_value=status),
            patch("rightmemory.cli.build_pruner_message", return_value="prune message"),
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["prune", "--session", "prune-session"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["pruner"])
        self.assertIn("session prune-session: prune message", stdout.getvalue())
```

- [ ] **Step 2: Run tests to verify command is missing**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_cli
```

Expected: FAIL because `rightmemory prune` is not routed yet.

- [ ] **Step 3: Wire CLI command**

Modify imports in `rightmemory/cli.py`:

```python
from .config import MEMORY_ROOT, ROLES, load_config, load_dreamer_watch_config, load_pruner_config, load_review_config, load_sync_config
from .prune import build_pruner_message, prune_due_status
```

Add early routing in `main()`:

```python
    if argv and argv[0] == "prune":
        return _prune_main(argv[1:])
```

Add parser and command function near other top-level command helpers:

```python
def _prune_main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory prune")
    parser.add_argument("--session", default="pruner", help="persist pruner message history under this session id")
    args = parser.parse_args(argv)

    pruner_config = load_pruner_config()
    status = prune_due_status(pruner_config.memory_root, pruner_config)
    if not status.due:
        print(status.message)
        return 0

    runtime_config = load_config("pruner")
    runtime = RightMemoryRuntime(runtime_config)
    try:
        print(runtime.run_session_turn(args.session, build_pruner_message(status)))
        return 0
    finally:
        runtime.cleanup()
```

- [ ] **Step 4: Run CLI tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_cli
```

Expected: PASS for new prune command tests.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: add prune command"
```

## Task 5: Top-Level `rightmemory history` Command

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing history command tests**

Add tests to `tests/test_cli.py`:

```python
    def test_history_runs_historian_role(self):
        stdout = io.StringIO()
        roles = []

        def fake_load_config(role):
            roles.append(role)
            return object()

        with (
            patch("rightmemory.cli.load_config", fake_load_config),
            patch("rightmemory.cli.RightMemoryRuntime", FakeRuntime),
            patch("sys.stdout", stdout),
        ):
            result = main(["history", "--session", "hist-1", "old", "cache", "lesson"])

        self.assertEqual(result, 0)
        self.assertEqual(roles, ["historian"])
        self.assertEqual(stdout.getvalue().strip(), "session hist-1: old cache lesson")

    def test_history_requires_message(self):
        with self.assertRaises(ValueError) as caught:
            main(["history", "--session", "hist-1"])

        self.assertIn("message must not be empty", str(caught.exception))
```

- [ ] **Step 2: Run tests to verify command is missing**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_cli
```

Expected: FAIL because `rightmemory history` is not routed yet.

- [ ] **Step 3: Wire history command**

Add early routing in `main()` before generic role parsing:

```python
    if argv and argv[0] == "history":
        return _history_main(argv[1:])
```

Add `_history_main()`:

```python
def _history_main(argv: list[str]) -> int:
    args = _turn_parser("history").parse_args(argv)
    runtime = RightMemoryRuntime(load_config("historian"))
    try:
        return _session_turn(runtime, args.session, args.message)
    finally:
        runtime.cleanup()
```

The parser `prog` will display `rightmemory history` because `_turn_parser("history")` builds that string.

- [ ] **Step 4: Run history command tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_cli
```

Expected: PASS for new history command tests.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/cli.py tests/test_cli.py
git commit -m "feat: add history command"
```

## Task 6: Prompt Invariants And Role Boundary Tests

**Files:**
- Modify: `tests/test_config.py`
- Modify: `rightmemory/prompts/pruner.md`
- Modify: `rightmemory/prompts/historian.md`
- Modify: `rightmemory/prompt.py`

- [ ] **Step 1: Write prompt boundary tests**

Add tests in `tests/test_config.py` near prompt assembly tests:

```python
    def test_pruner_prompt_mentions_prune_commit_ledger(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "pruner")

        self.assertIn("prune: expired active memory", prompt)
        self.assertIn("prune: checkpoint", prompt)
        self.assertIn("Revival grace", prompt)
        self.assertIn("allow_empty=true", prompt)

    def test_historian_prompt_includes_reactivation_hint(self):
        prompt = build_instructions(Path("/home/example/.rightmemory"), "historian")

        self.assertIn("historical/pruned memory", prompt)
        self.assertIn("send an update to reactivate it in current memory", prompt)
        self.assertNotIn("git_commit", prompt)

    def test_existing_role_prompts_do_not_inherit_prune_duties(self):
        for role in ("retrieve", "update", "reviewer", "dreamer"):
            prompt = build_instructions(Path("/home/example/.rightmemory"), role)

            self.assertNotIn("Revival grace", prompt)
            self.assertNotIn("prune: checkpoint", prompt)
```

- [ ] **Step 2: Run prompt tests**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_config
```

Expected: PASS. If a prompt test fails, edit the relevant prompt file or command/tool guidance so the role boundary is explicit without adding prune duties to existing roles.

- [ ] **Step 3: Commit**

```bash
git add rightmemory/prompts/pruner.md rightmemory/prompts/historian.md rightmemory/prompt.py tests/test_config.py
git commit -m "test: lock pruner historian prompt boundaries"
```

## Task 7: Documentation

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`

- [ ] **Step 1: Update README command and model sections**

In `README.md`, add `rightmemory prune` and `rightmemory history` to the command overview near the role diagram:

```markdown
  +--> rightmemory prune            trims active memory at commit-generation boundaries
  |
  +--> rightmemory history          read-only archaeology over pruned memory
```

Add a concise section under the memory model or agent roles area:

```markdown
## Forgetting And History

RightMemory treats `MEMORY.md` and `MEMORY_*.md` as the active memory surface,
not a permanent archive. `rightmemory prune` checks Git history and runs the
pruner role after the configured number of commits since the latest `prune:`
commit. The default generation is 70 commits.

Pruner removes active memory that crossed a generation without semantic change.
It writes `prune:` commits with compact bodies that list removed ids, heading
paths, topics, summaries, skipped items, and revival grace state. Those commits
are the archaeology ledger.

`rightmemory history --session <id> <query>` runs the read-only historian role.
Historian searches `prune:` ledgers and Git snapshots, labels results as
historical/pruned memory, and reminds the caller to send an update when a
historical item should become active again.
```

- [ ] **Step 2: Update README config section**

Add this config block near other role-local config examples:

```toml
[pruner]
generation_commits = 70
revival_grace_checkpoints = 2

[pruner.model]
model_id = "openai/pruner-model"

[historian.model]
model_id = "openai/historian-model"
```

- [ ] **Step 3: Update DESIGN_NOTES**

Add a concise design note:

```markdown
### Generation pruning and historian archaeology

Current memory is a working surface, while Git history is the archaeology layer.
Pruner uses `prune:` commits as generation boundaries and ledgers instead of
wall-clock age, lifecycle tags, or separate index files. The default generation
is 70 commits since the latest `prune:` commit.

Historian keeps historical retrieval out of ordinary retrieve. It searches
`prune:` commit bodies and Git snapshots, returns historical/pruned memory as
non-active context, and points the caller back to update when a recovered item
should be reactivated.
```

- [ ] **Step 4: Run doc smoke checks**

Run:

```bash
rg -n "rightmemory prune|rightmemory history|prune:|historian|generation_commits" README.md DESIGN_NOTES.md
```

Expected: output includes the new sections and no stale references to time-based pruning.

- [ ] **Step 5: Commit**

```bash
git add README.md DESIGN_NOTES.md
git commit -m "docs: document generation pruning"
```

## Task 8: Final Verification

**Files:**
- No planned source edits.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
conda run -n rightmemory python -m unittest tests.test_prune tests.test_tools tests.test_config tests.test_cli tests.test_agent_cli tests.test_isolated_write
```

Expected: PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
conda run -n rightmemory python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Run syntax check**

Run:

```bash
conda run -n rightmemory python -m compileall -q rightmemory tests
```

Expected: command exits 0 with no output.

- [ ] **Step 4: Inspect final git status**

Run:

```bash
git status --short
```

Expected: no modified tracked files. Existing unrelated untracked files such as `docs/PROMOTION.md` may remain untracked and should not be staged.

- [ ] **Step 5: Final implementation summary**

Report:

```text
Implemented generation pruning and historian retrieval.
Verification:
- conda run -n rightmemory python -m unittest discover -s tests
- conda run -n rightmemory python -m compileall -q rightmemory tests
Unrelated untracked files left untouched: docs/PROMOTION.md
```

# Semantic Upgrade Notes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add maintainer-authored semantic upgrade notes that are reported at install time, injected into the next dreamer cycle, and marked absorbed after a successful dreamer run.

**Architecture:** Implement semantic upgrades as a focused Python package under `rightmemory/semantic_upgrades/`, with Markdown notes stored as package data in the same directory. The runtime computes pending notes once for a dreamer invocation, passes that exact context into standalone and CLI-agent prompts, and marks those ids absorbed after the invocation returns successfully. The installer calls the installed package helper for user-facing pending-note output without editing user memory or triggering dreamer.

**Tech Stack:** Python 3.11 standard library, `unittest`, `importlib.resources`, current RightMemory runtime/prompt/install patterns, shell installer.

---

## File Structure

- Create `rightmemory/semantic_upgrades/__init__.py`: parser, packaged note loading, state read/write, prompt rendering, CLI helper entrypoint.
- Create `rightmemory/semantic_upgrades/__main__.py`: `python -m rightmemory.semantic_upgrades ...` entrypoint.
- Create `rightmemory/semantic_upgrades/2026-05-20-user-context-agent-behavior-split.md`: first maintainer semantic upgrade note.
- Create `tests/test_semantic_upgrades.py`: parser, state, pending-context, prompt-rendering, CLI helper tests.
- Modify `rightmemory/prompt.py`: accept optional semantic upgrade context and include it for dreamer instructions.
- Modify `rightmemory/runtime.py`: compute pending semantic upgrades for dreamer, pass them to prompt/executor, mark absorbed after success.
- Modify `rightmemory/agent_cli.py`: accept semantic upgrade context and pass it into CLI-agent prompt construction.
- Modify `tests/test_config.py`: prompt injection tests.
- Modify `tests/test_agent_cli.py`: CLI-agent prompt construction test.
- Modify `tests/test_semantic_upgrades.py`: runtime success/failure absorption tests.
- Modify `install.sh`: call the installed helper and surface its output after runtime package installation.
- Modify `tests/test_install.py`: verify installer surfaces helper output without touching real memory.
- Modify `pyproject.toml`: package the semantic upgrade Markdown notes.
- Modify `README.md`, `DESIGN_NOTES.md`, and `AGENTS.md`: document install/dreamer behavior and maintainer workflow.

## Task 1: Semantic Upgrade Package, Parser, And First Note

**Files:**
- Create: `rightmemory/semantic_upgrades/__init__.py`
- Create: `rightmemory/semantic_upgrades/__main__.py`
- Create: `rightmemory/semantic_upgrades/2026-05-20-user-context-agent-behavior-split.md`
- Create: `tests/test_semantic_upgrades.py`
- Modify: `pyproject.toml`

- [ ] **Step 1: Write failing parser and packaged-note tests**

Add this initial test file:

```python
# tests/test_semantic_upgrades.py
from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

from rightmemory.semantic_upgrades import (
    SemanticUpgradeNote,
    load_notes_from_directory,
    load_packaged_notes,
    parse_note_text,
)


VALID_NOTE = """---
id: user-context-agent-behavior-split
introduced_at: 2026-05-20
---

# User Context And Agent Behavior Split

Revisit existing memory that mixes durable user context with agent behavior guidance.
"""


class SemanticUpgradeParserTests(unittest.TestCase):
    def test_parse_note_text_reads_front_matter_title_and_body(self):
        note = parse_note_text("example.md", VALID_NOTE)

        self.assertEqual(note.id, "user-context-agent-behavior-split")
        self.assertEqual(note.introduced_at, date(2026, 5, 20))
        self.assertEqual(note.title, "User Context And Agent Behavior Split")
        self.assertIn("Revisit existing memory", note.body)
        self.assertEqual(note.source, "example.md")

    def test_parse_note_text_rejects_missing_front_matter(self):
        with self.assertRaises(ValueError) as caught:
            parse_note_text("broken.md", "# Missing Front Matter\n")

        self.assertIn("missing front matter", str(caught.exception))

    def test_load_notes_from_directory_sorts_and_warns_for_malformed_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            directory.joinpath("later.md").write_text(
                VALID_NOTE.replace("2026-05-20", "2026-05-21").replace(
                    "user-context-agent-behavior-split", "later-note"
                ),
                encoding="utf-8",
            )
            directory.joinpath("earlier.md").write_text(
                VALID_NOTE.replace("2026-05-20", "2026-05-19").replace(
                    "user-context-agent-behavior-split", "earlier-note"
                ),
                encoding="utf-8",
            )
            directory.joinpath("broken.md").write_text("# Broken\n", encoding="utf-8")

            result = load_notes_from_directory(directory)

        self.assertEqual([note.id for note in result.notes], ["earlier-note", "later-note"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("broken.md", result.warnings[0])

    def test_load_notes_from_directory_skips_duplicate_ids_after_first(self):
        with tempfile.TemporaryDirectory() as tempdir:
            directory = Path(tempdir)
            directory.joinpath("one.md").write_text(VALID_NOTE, encoding="utf-8")
            directory.joinpath("two.md").write_text(VALID_NOTE, encoding="utf-8")

            result = load_notes_from_directory(directory)

        self.assertEqual([note.id for note in result.notes], ["user-context-agent-behavior-split"])
        self.assertEqual(len(result.warnings), 1)
        self.assertIn("duplicate semantic upgrade id", result.warnings[0])

    def test_load_packaged_notes_includes_user_context_split_note(self):
        result = load_packaged_notes()

        self.assertIn("user-context-agent-behavior-split", [note.id for note in result.notes])
        self.assertEqual([], result.warnings)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify parser module is missing**

Run:

```bash
python -m unittest tests.test_semantic_upgrades
```

Expected: FAIL with `ModuleNotFoundError: No module named 'rightmemory.semantic_upgrades'`.

- [ ] **Step 3: Create the semantic upgrade package and parser**

Create `rightmemory/semantic_upgrades/__init__.py`:

```python
from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Iterable, Sequence

from rightmemory.session import _ensure_runtime_gitignore, _fsync_directory


STATE_RELATIVE_PATH = Path(".runtime") / "semantic-upgrades.json"


@dataclass(frozen=True)
class SemanticUpgradeNote:
    id: str
    introduced_at: date
    title: str
    body: str
    source: str


@dataclass(frozen=True)
class SemanticUpgradeLoadResult:
    notes: list[SemanticUpgradeNote]
    warnings: list[str]


@dataclass(frozen=True)
class SemanticUpgradeContext:
    notes: list[SemanticUpgradeNote]
    warnings: list[str]

    @property
    def ids(self) -> list[str]:
        return [note.id for note in self.notes]


def parse_note_text(source: str, text: str) -> SemanticUpgradeNote:
    if not text.startswith("---\n"):
        raise ValueError(f"{source}: missing front matter")
    end_marker = "\n---\n"
    end = text.find(end_marker, 4)
    if end == -1:
        raise ValueError(f"{source}: unterminated front matter")

    metadata = _parse_front_matter(source, text[4:end])
    body = text[end + len(end_marker) :].strip()
    note_id = _required_metadata(source, metadata, "id")
    introduced_at = _parse_date(source, _required_metadata(source, metadata, "introduced_at"))
    title = _extract_title(source, body)
    return SemanticUpgradeNote(
        id=note_id,
        introduced_at=introduced_at,
        title=title,
        body=body,
        source=source,
    )


def load_packaged_notes() -> SemanticUpgradeLoadResult:
    root = resources.files(__package__)
    entries = [entry for entry in root.iterdir() if entry.name.endswith(".md")]
    return _load_note_entries(entries)


def load_notes_from_directory(directory: Path) -> SemanticUpgradeLoadResult:
    entries = sorted(directory.glob("*.md"))
    return _load_note_entries(entries)


def _load_note_entries(entries: Sequence[object]) -> SemanticUpgradeLoadResult:
    notes: list[SemanticUpgradeNote] = []
    warnings: list[str] = []
    seen: set[str] = set()
    for entry in sorted(entries, key=lambda item: getattr(item, "name", str(item))):
        source = getattr(entry, "name", str(entry))
        try:
            text = entry.read_text(encoding="utf-8")
            note = parse_note_text(source, text)
        except ValueError as exc:
            warnings.append(str(exc))
            continue
        if note.id in seen:
            warnings.append(f"{source}: duplicate semantic upgrade id: {note.id}")
            continue
        seen.add(note.id)
        notes.append(note)
    notes.sort(key=lambda note: (note.introduced_at, note.id))
    return SemanticUpgradeLoadResult(notes=notes, warnings=warnings)


def _parse_front_matter(source: str, text: str) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise ValueError(f"{source}: invalid front matter line: {line}")
        key, value = line.split(":", 1)
        metadata[key.strip()] = value.strip()
    return metadata


def _required_metadata(source: str, metadata: dict[str, str], key: str) -> str:
    value = metadata.get(key)
    if not value:
        raise ValueError(f"{source}: missing required front matter key: {key}")
    return value


def _parse_date(source: str, value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{source}: introduced_at must be an ISO date") from exc


def _extract_title(source: str, body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped.removeprefix("# ").strip()
    raise ValueError(f"{source}: missing top-level title")
```

Create `rightmemory/semantic_upgrades/__main__.py`:

```python
from __future__ import annotations

from . import main


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Add the first semantic upgrade note and package data**

Create `rightmemory/semantic_upgrades/2026-05-20-user-context-agent-behavior-split.md`:

```md
---
id: user-context-agent-behavior-split
introduced_at: 2026-05-20
---

# User Context And Agent Behavior Split

Revisit existing memory that mixes durable user context with agent behavior guidance.

Place durable facts about the user's context, direction, goals, constraints, and values under `# User Context` when they help future agents collaborate. Place communication style, workflow expectations, tool or process preferences, and repeated agent-correction lessons under `# Cross-Session Agent Behavior`. Keep project-scoped workflow guidance inside the relevant project domain when it should not guide agents globally.

Use the placement principle above rather than treating these examples as a closed checklist. When older memory combines these concepts, reorganize the surrounding headings and nodes so the final structure reflects the current schema.
```

Modify `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel.force-include]
"skills" = "rightmemory/skills"
"rightmemory/prompts" = "rightmemory/prompts"
"rightmemory/semantic_upgrades" = "rightmemory/semantic_upgrades"
```

- [ ] **Step 5: Run parser tests**

Run:

```bash
python -m unittest tests.test_semantic_upgrades
```

Expected: PASS.

- [ ] **Step 6: Commit parser and note package**

```bash
git add pyproject.toml rightmemory/semantic_upgrades tests/test_semantic_upgrades.py
git commit -m "feat: add semantic upgrade note parser"
```

## Task 2: Absorption State And Refresh CLI

**Files:**
- Modify: `rightmemory/semantic_upgrades/__init__.py`
- Modify: `tests/test_semantic_upgrades.py`

- [ ] **Step 1: Add failing state and CLI tests**

Append these tests to `tests/test_semantic_upgrades.py` before the `if __name__ == "__main__"` block:

```python
class SemanticUpgradeStateTests(unittest.TestCase):
    def test_pending_context_uses_absorbed_state(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            mark_absorbed(root, ["user-context-agent-behavior-split"])

            context = pending_context(root)

        self.assertNotIn("user-context-agent-behavior-split", context.ids)

    def test_corrupt_state_warns_and_treats_notes_as_pending(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            state = root / ".runtime" / "semantic-upgrades.json"
            state.parent.mkdir(parents=True)
            state.write_text("{bad json", encoding="utf-8")

            context = pending_context(root)

        self.assertIn("user-context-agent-behavior-split", context.ids)
        self.assertTrue(any("could not read semantic upgrade state" in warning for warning in context.warnings))

    def test_refresh_cli_prints_pending_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)

            exit_code = main(["refresh", "--memory-root", str(root)])

        self.assertEqual(exit_code, 0)
        self.assertTrue((root / ".runtime" / "semantic-upgrades.json").exists())

    def test_format_refresh_summary_lists_pending_ids(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nBody.",
                    source="example.md",
                )
            ],
            warnings=["broken.md: missing front matter"],
        )

        summary = format_refresh_summary(context)

        self.assertIn("1 semantic upgrade note(s) pending", summary)
        self.assertIn("example-note", summary)
        self.assertIn("broken.md: missing front matter", summary)
```

Update the import block in the same file:

```python
from rightmemory.semantic_upgrades import (
    SemanticUpgradeContext,
    SemanticUpgradeNote,
    format_refresh_summary,
    load_notes_from_directory,
    load_packaged_notes,
    main,
    mark_absorbed,
    parse_note_text,
    pending_context,
)
```

- [ ] **Step 2: Run tests to verify missing state functions**

Run:

```bash
python -m unittest tests.test_semantic_upgrades
```

Expected: FAIL naming `mark_absorbed`, `pending_context`, `format_refresh_summary`, or `main`.

- [ ] **Step 3: Implement state read/write, pending context, summary, and CLI**

Append this implementation to `rightmemory/semantic_upgrades/__init__.py`:

```python
def pending_context(memory_root: Path) -> SemanticUpgradeContext:
    loaded = load_packaged_notes()
    absorbed, state_warnings = _read_absorbed_ids(memory_root)
    notes = [note for note in loaded.notes if note.id not in absorbed]
    return SemanticUpgradeContext(notes=notes, warnings=[*loaded.warnings, *state_warnings])


def render_prompt_context(context: SemanticUpgradeContext) -> str:
    if not context.notes and not context.warnings:
        return ""
    lines = [
        "Pending semantic upgrade notes:",
        "",
        "Use these notes to reconsider how existing memory should be organized and interpreted under the current RightMemory model. Process them in chronological order. If later notes refine, narrow, or contradict earlier notes, treat the later note as the current guidance. Do not copy these notes into memory as maintenance text. Apply them when they help make existing memory clearer, less stale, or better aligned with the current schema and role prompts.",
        "",
    ]
    if context.warnings:
        lines.append("Warnings:")
        lines.extend(f"- {warning}" for warning in context.warnings)
        lines.append("")
    for note in context.notes:
        lines.extend(
            [
                f"## {note.id}",
                f"Introduced: {note.introduced_at.isoformat()}",
                f"Source: {note.source}",
                "",
                note.body.strip(),
                "",
            ]
        )
    return "\n".join(lines).rstrip()


def mark_absorbed(memory_root: Path, ids: Iterable[str], now: datetime | None = None) -> None:
    ids = sorted(set(ids))
    if not ids:
        return
    state_path = _state_path(memory_root)
    absorbed, _warnings = _read_absorbed_ids(memory_root)
    timestamp = (now or datetime.now(UTC)).isoformat()
    data = {"absorbed": {note_id: {"absorbed_at": timestamp} for note_id in sorted(absorbed.union(ids))}}
    _ensure_runtime_gitignore(memory_root / ".runtime")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(f".{state_path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, state_path)
    _fsync_directory(state_path.parent)


def format_refresh_summary(context: SemanticUpgradeContext) -> str:
    lines: list[str] = []
    for warning in context.warnings:
        lines.append(f"  [warning] semantic upgrade note skipped: {warning}")
    if context.notes:
        lines.append(
            f"  [notice]  {len(context.notes)} semantic upgrade note(s) pending for the next dreamer cycle:"
        )
        lines.extend(f"            {note.id}" for note in context.notes)
    else:
        lines.append("  [keep]    no semantic upgrade notes pending")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m rightmemory.semantic_upgrades")
    subparsers = parser.add_subparsers(dest="command", required=True)
    refresh = subparsers.add_parser("refresh", help="refresh semantic upgrade pending state")
    refresh.add_argument("--memory-root", required=True, help="RightMemory memory root")
    args = parser.parse_args(argv)

    if args.command == "refresh":
        memory_root = Path(args.memory_root).expanduser().resolve()
        _ensure_runtime_gitignore(memory_root / ".runtime")
        memory_root.joinpath(".runtime").mkdir(parents=True, exist_ok=True)
        context = pending_context(memory_root)
        _write_state_if_missing(memory_root)
        print(format_refresh_summary(context))
        return 0
    raise ValueError(f"unknown semantic upgrade command: {args.command}")


def _state_path(memory_root: Path) -> Path:
    return memory_root / STATE_RELATIVE_PATH


def _read_absorbed_ids(memory_root: Path) -> tuple[set[str], list[str]]:
    path = _state_path(memory_root)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return set(), []
    except json.JSONDecodeError as exc:
        return set(), [f"could not read semantic upgrade state {path}: {exc}"]
    absorbed = data.get("absorbed")
    if not isinstance(absorbed, dict):
        return set(), [f"could not read semantic upgrade state {path}: absorbed must be an object"]
    return {key for key in absorbed if isinstance(key, str)}, []


def _write_state_if_missing(memory_root: Path) -> None:
    path = _state_path(memory_root)
    if path.exists():
        return
    _ensure_runtime_gitignore(memory_root / ".runtime")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        json.dump({"absorbed": {}}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
```

- [ ] **Step 4: Run state and CLI tests**

Run:

```bash
python -m unittest tests.test_semantic_upgrades
```

Expected: PASS.

- [ ] **Step 5: Commit state and CLI helper**

```bash
git add rightmemory/semantic_upgrades tests/test_semantic_upgrades.py
git commit -m "feat: track pending semantic upgrade notes"
```

## Task 3: Dreamer Prompt Injection

**Files:**
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing prompt tests**

In `tests/test_config.py`, add imports near the existing prompt imports:

```python
from datetime import date

from rightmemory.semantic_upgrades import SemanticUpgradeContext, SemanticUpgradeNote
```

Add these tests to `PromptTests`:

```python
    def test_dreamer_prompt_includes_pending_semantic_upgrade_context(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nReconsider older memory.",
                    source="example.md",
                )
            ],
            warnings=[],
        )

        prompt = build_instructions(Path("/home/example/.rightmemory"), "dreamer", semantic_upgrades=context)

        self.assertIn("Pending semantic upgrade notes", prompt)
        self.assertIn("example-note", prompt)
        self.assertIn("later notes refine, narrow, or contradict earlier notes", prompt)
        self.assertIn("Do not copy these notes into memory as maintenance text", prompt)

    def test_non_dreamer_prompt_ignores_semantic_upgrade_context(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nReconsider older memory.",
                    source="example.md",
                )
            ],
            warnings=[],
        )

        prompt = build_instructions(Path("/home/example/.rightmemory"), "update", semantic_upgrades=context)

        self.assertNotIn("Pending semantic upgrade notes", prompt)
        self.assertNotIn("example-note", prompt)
```

Ensure the prompt helper import includes both builders:

```python
from rightmemory.prompt import build_cli_agent_instructions, build_instructions
```

- [ ] **Step 2: Run prompt tests to verify signature failure**

Run:

```bash
python -m unittest tests.test_config.PromptTests
```

Expected: FAIL with `TypeError` because `build_instructions()` does not accept `semantic_upgrades`.

- [ ] **Step 3: Modify prompt builders to accept semantic upgrade context**

Modify `rightmemory/prompt.py`:

```python
from .semantic_upgrades import SemanticUpgradeContext, render_prompt_context
```

Change signatures:

```python
def build_cli_agent_instructions(
    memory_root: Path,
    role: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
) -> str:
```

```python
def build_instructions(
    memory_root: Path,
    role: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
) -> str:
```

Add helper:

```python
def _semantic_upgrade_guidance(role: str, semantic_upgrades: SemanticUpgradeContext | None) -> str:
    if role != "dreamer" or semantic_upgrades is None:
        return ""
    rendered = render_prompt_context(semantic_upgrades)
    if not rendered:
        return ""
    return f"\nSemantic upgrade context:\n{rendered}\n"
```

Insert the helper output before `Role instructions:` in both prompt builders:

```python
semantic_guidance = _semantic_upgrade_guidance(role, semantic_upgrades)
```

For `build_cli_agent_instructions`, include `{semantic_guidance}` between the schema block and role instructions:

```python
RightMemory schema:
{schema}
{semantic_guidance}
Role instructions:
{role_guidance}
```

For `build_instructions`, include `{semantic_guidance}` between standalone adaptation and role instructions:

```python
Standalone adaptation:
- Treat the embedded schema above as the schema source of truth. Do not try to read skill or schema files outside {memory_root}; the provided tools only expose the memory root.
- Treat the caller's message according to the command-selected behavior and the role instructions below.
{semantic_guidance}
Role instructions:
{role_guidance}
```

- [ ] **Step 4: Run prompt tests**

Run:

```bash
python -m unittest tests.test_config.PromptTests
```

Expected: PASS.

- [ ] **Step 5: Commit prompt injection**

```bash
git add rightmemory/prompt.py tests/test_config.py
git commit -m "feat: inject semantic upgrades into dreamer prompt"
```

## Task 4: Runtime Absorption For Standalone And CLI-Agent Dreamer

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/agent_cli.py`
- Modify: `tests/test_agent_cli.py`
- Modify: `tests/test_semantic_upgrades.py`

- [ ] **Step 1: Add failing CLI-agent prompt test**

In `tests/test_agent_cli.py`, add:

```python
from datetime import date

from rightmemory.semantic_upgrades import SemanticUpgradeContext, SemanticUpgradeNote
```

Add this test to `AgentCliCommandTests`:

```python
    def test_cli_agent_executor_includes_semantic_upgrades_for_dreamer_prompt(self):
        context = SemanticUpgradeContext(
            notes=[
                SemanticUpgradeNote(
                    id="example-note",
                    introduced_at=date(2026, 5, 20),
                    title="Example Note",
                    body="# Example Note\n\nReconsider older memory.",
                    source="example.md",
                )
            ],
            warnings=[],
        )
        prompts = []

        def fake_build_codex_command(memory_root, role, config, prompt, provider_session_id):
            prompts.append(prompt)
            return ["codex"]

        with (
            patch("rightmemory.agent_cli.build_codex_command", fake_build_codex_command),
            patch("rightmemory.agent_cli._run_cli", return_value='{"type":"thread.started","thread_id":"t1"}\n{"item":{"type":"agent_message","text":"done"}}\n'),
        ):
            executor = CliAgentExecutor(
                Path("/memory/root"),
                "dreamer",
                AgentCliConfig(provider="codex"),
                semantic_upgrades=context,
            )
            result = executor.run_session_turn("dreamer-session", "run")

        self.assertEqual(result, "done")
        self.assertIn("Pending semantic upgrade notes", prompts[0])
        self.assertIn("example-note", prompts[0])
```

- [ ] **Step 2: Add failing runtime absorption tests**

In `tests/test_semantic_upgrades.py`, add imports:

```python
import json
from unittest.mock import patch

from rightmemory.config import AgentCliConfig, RuntimeConfig
from rightmemory.runtime import RightMemoryRuntime
```

Add these tests:

```python
class SemanticUpgradeRuntimeAbsorptionTests(unittest.TestCase):
    def test_dreamer_success_marks_injected_semantic_upgrades_absorbed(self):
        calls = []

        class FakeDreamerExecutor:
            def __init__(self, memory_root, role, config, semantic_upgrades=None):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                calls.append((session_id, message, self.semantic_upgrades.ids))
                return "dreamed"

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FakeDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    result = runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state = json.loads((root / ".runtime" / "semantic-upgrades.json").read_text(encoding="utf-8"))

        self.assertEqual(result, "dreamed")
        self.assertEqual(calls, [("dreamer-1", "run", ["user-context-agent-behavior-split"])])
        self.assertIn("user-context-agent-behavior-split", state["absorbed"])

    def test_dreamer_failure_leaves_semantic_upgrades_pending(self):
        class FailingDreamerExecutor:
            def __init__(self, memory_root, role, config, semantic_upgrades=None):
                self.semantic_upgrades = semantic_upgrades

            def run_session_turn(self, session_id: str, message: str) -> str:
                raise RuntimeError("dreamer failed")

            def cleanup(self):
                return None

        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            config = RuntimeConfig(
                role="dreamer",
                runtime_mode="cli-agent",
                agent_cli=AgentCliConfig(provider="codex"),
                memory_root=root,
            )

            with patch("rightmemory.runtime.CliAgentExecutor", FailingDreamerExecutor):
                runtime = RightMemoryRuntime(config)
                try:
                    with self.assertRaises(RuntimeError):
                        runtime.run_session_turn("dreamer-1", "run")
                finally:
                    runtime.cleanup()

            state_path = root / ".runtime" / "semantic-upgrades.json"

        self.assertFalse(state_path.exists())
```

- [ ] **Step 3: Run targeted tests to verify missing constructor/signature behavior**

Run:

```bash
python -m unittest tests.test_agent_cli.AgentCliCommandTests tests.test_semantic_upgrades.SemanticUpgradeRuntimeAbsorptionTests
```

Expected: FAIL where `CliAgentExecutor` does not accept `semantic_upgrades`, or where runtime absorption is not implemented.

- [ ] **Step 4: Modify CLI-agent executor to receive semantic context**

Modify `rightmemory/agent_cli.py`:

```python
from .semantic_upgrades import SemanticUpgradeContext
```

Change the constructor:

```python
class CliAgentExecutor:
    def __init__(
        self,
        memory_root: Path,
        role: str,
        config: AgentCliConfig,
        semantic_upgrades: SemanticUpgradeContext | None = None,
    ):
        _validate_role(role)
        self.memory_root = memory_root
        self.role = role
        self.config = config
        self.semantic_upgrades = semantic_upgrades
        self.store = ProviderSessionStore(memory_root, role)
```

Update `_run_provider` and `_turn_prompt`:

```python
prompt = _turn_prompt(self.memory_root, self.role, message, self.semantic_upgrades)
```

```python
def _turn_prompt(
    memory_root: Path,
    role: str,
    message: str,
    semantic_upgrades: SemanticUpgradeContext | None = None,
) -> str:
    instructions = build_cli_agent_instructions(memory_root, role, semantic_upgrades=semantic_upgrades).rstrip()
    return f"{instructions}\n\nCaller message:\n{message}\n"
```

- [ ] **Step 5: Modify runtime to compute and absorb pending ids**

Modify `rightmemory/runtime.py` imports:

```python
from .semantic_upgrades import SemanticUpgradeContext, mark_absorbed, pending_context
```

In `RightMemoryRuntime.__init__`, add before building the agent:

```python
self.semantic_upgrades = self._semantic_upgrade_context()
self._semantic_upgrade_ids = self.semantic_upgrades.ids if self.semantic_upgrades is not None else []
```

Add helper methods:

```python
    def _semantic_upgrade_context(self) -> SemanticUpgradeContext | None:
        if self.config.role != "dreamer":
            return None
        context = pending_context(self.config.memory_root)
        if not context.notes and not context.warnings:
            return None
        return context

    def _mark_semantic_upgrades_absorbed(self) -> None:
        if self.config.role != "dreamer" or not self._semantic_upgrade_ids:
            return
        mark_absorbed(self.config.memory_root, self._semantic_upgrade_ids)
        self._semantic_upgrade_ids = []
```

Update `_build_agent`:

```python
instructions=build_instructions(self.config.memory_root, self.config.role, semantic_upgrades=self.semantic_upgrades),
```

Update `_build_cli_agent`:

```python
return CliAgentExecutor(
    self.config.memory_root,
    self.config.role,
    self.config.agent_cli,
    semantic_upgrades=self.semantic_upgrades,
)
```

In `run_turn()` and `run_session_turn()`, call `_mark_semantic_upgrades_absorbed()` after post-sync handling and before returning the result:

```python
if post_sync is not None:
    self._run_sync_reconciler(post_sync)
self._mark_semantic_upgrades_absorbed()
return self._result_output(result)
```

For the CLI-agent `run_turn()` branch that returns `str(result)`, use:

```python
if post_sync is not None:
    self._run_sync_reconciler(post_sync)
self._mark_semantic_upgrades_absorbed()
return str(result)
```

- [ ] **Step 6: Run targeted runtime and CLI-agent tests**

Run:

```bash
python -m unittest tests.test_agent_cli tests.test_semantic_upgrades
```

Expected: PASS.

- [ ] **Step 7: Commit runtime absorption**

```bash
git add rightmemory/runtime.py rightmemory/agent_cli.py tests/test_agent_cli.py tests/test_semantic_upgrades.py
git commit -m "feat: absorb semantic upgrades after dreamer success"
```

## Task 5: Installer Integration

**Files:**
- Modify: `install.sh`
- Modify: `tests/test_install.py`

- [ ] **Step 1: Add failing installer test for helper output**

Update `_env_with_fake_uv()` in `tests/test_install.py` so the fake venv Python emits semantic upgrade output when called with the helper module:

```python
        fake_uv.write_text(
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"venv\" ]; then\n"
            "  mkdir -p \"$2/bin\"\n"
            "  cat > \"$2/bin/python\" <<'PYEOF'\n"
            "#!/usr/bin/env sh\n"
            "if [ \"$1\" = \"-m\" ] && [ \"$2\" = \"rightmemory.semantic_upgrades\" ]; then\n"
            "  echo '  [notice]  1 semantic upgrade note(s) pending for the next dreamer cycle:'\n"
            "  echo '            user-context-agent-behavior-split'\n"
            "  memory_root=''\n"
            "  previous=''\n"
            "  for arg in \"$@\"; do\n"
            "    if [ \"$previous\" = \"--memory-root\" ]; then memory_root=\"$arg\"; fi\n"
            "    previous=\"$arg\"\n"
            "  done\n"
            "  mkdir -p \"$memory_root/.runtime\"\n"
            "  printf '{\"absorbed\": {}}\\n' > \"$memory_root/.runtime/semantic-upgrades.json\"\n"
            "fi\n"
            "exit 0\n"
            "PYEOF\n"
            "  chmod 755 \"$2/bin/python\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
```

Add this test:

```python
    def test_install_reports_pending_semantic_upgrade_notes(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            memory_root = root / "memory"
            skills_target = root / "skills"

            result = self._install(memory_root, skills_target)

        self.assertIn("semantic upgrade note(s) pending", result.stdout)
        self.assertIn("user-context-agent-behavior-split", result.stdout)
```

- [ ] **Step 2: Run installer test to verify helper is not called yet**

Run:

```bash
python -m unittest tests.test_install.InstallScriptTests.test_install_reports_pending_semantic_upgrade_notes
```

Expected: FAIL because installer output does not mention semantic upgrades.

- [ ] **Step 3: Add installer helper call**

In `install.sh`, after `install_cli_runtime_layout` and before installing skills, add:

```sh
refresh_semantic_upgrades() {
  "$RIGHTMEMORY_VENV/bin/python" -m rightmemory.semantic_upgrades refresh --memory-root "$MEMORY_ROOT"
}
```

Then call it:

```sh
install_cli_runtime_layout
refresh_semantic_upgrades
for skills_target in "${SKILLS_TARGETS[@]}"; do
```

- [ ] **Step 4: Run install tests**

Run:

```bash
python -m unittest tests.test_install
```

Expected: PASS.

- [ ] **Step 5: Commit installer integration**

```bash
git add install.sh tests/test_install.py
git commit -m "feat: report semantic upgrades during install"
```

## Task 6: Documentation, Operational Notes, And Full Verification

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Update README install behavior**

In `README.md`, update the install paragraph around reinstall behavior so it says:

```md
Re-run the installer after pulling updates. Existing real memory is preserved, the managed example block refreshes when present, and any pending semantic upgrade notes are reported for the next dreamer cycle. Semantic upgrade notes are maintainer-authored prompts for dreamer to revisit older memory under the current schema and role model; install does not run dreamer or edit user memory to apply them.
```

- [ ] **Step 2: Update design notes with semantic upgrade rationale**

Add this section to `DESIGN_NOTES.md` near the prompt/runtime design notes:

```md
### Semantic upgrade notes

Semantic upgrade notes let maintainers flag conceptual changes that existing memory may need to absorb after reinstall. The notes live with the runtime package and are tracked per memory root under `.runtime/semantic-upgrades.json`, because they are operational maintenance state rather than user memory. Install reports pending notes but leaves memory untouched; dreamer receives pending notes during consolidation and marks them absorbed after a successful run. Missed notes are supplied chronologically so skipped versions keep their semantic trail, while dreamer follows the later note when newer guidance refines or contradicts earlier guidance.
```

- [ ] **Step 3: Update AGENTS install/runtime notes**

In `AGENTS.md`, add a concise bullet under `Memory Runtime Rules`:

```md
- Semantic upgrade absorption state belongs under `.runtime/semantic-upgrades.json`. Install may report pending semantic upgrade notes, but dreamer is responsible for applying them during consolidation.
```

Under `Development Commands`, add:

```md
- Semantic upgrade notes are Markdown files under `rightmemory/semantic_upgrades/`; validate them with `python -m unittest tests.test_semantic_upgrades`.
```

- [ ] **Step 4: Run syntax and test suite**

Run:

```bash
python -m compileall -q rightmemory tests
python -m unittest discover -s tests
```

Expected: both commands PASS.

- [ ] **Step 5: Inspect final status**

Run:

```bash
git status --short
```

Expected: intended docs/runtime/test files are modified; unrelated pre-existing untracked files such as `docs/path-location-memory-note.md` and `docs/user-profile-goal-memory-note.md` remain unstaged.

- [ ] **Step 6: Commit documentation and verification**

```bash
git add README.md DESIGN_NOTES.md AGENTS.md
git commit -m "docs: document semantic upgrade notes"
```

## Task 7: Final Integration Review

**Files:**
- Inspect all files changed by Tasks 1-6.

- [ ] **Step 1: Review semantic upgrade state ownership**

Run:

```bash
rg -n "semantic-upgrades|semantic upgrade|semantic_upgrades" rightmemory tests install.sh README.md DESIGN_NOTES.md AGENTS.md pyproject.toml
```

Expected:

- runtime state path appears as `.runtime/semantic-upgrades.json`;
- note files appear under `rightmemory/semantic_upgrades/`;
- prompt injection appears for dreamer;
- install calls the helper after runtime install;
- docs say install reports pending notes and dreamer applies them.

- [ ] **Step 2: Confirm role prompt separation**

Run:

```bash
python -m unittest tests.test_config.PromptTests
```

Expected: PASS, including tests showing dreamer receives semantic upgrade context and non-dreamer roles do not.

- [ ] **Step 3: Confirm package data is included**

Run:

```bash
python -m unittest tests.test_semantic_upgrades.SemanticUpgradeParserTests.test_load_packaged_notes_includes_user_context_split_note
```

Expected: PASS.

- [ ] **Step 4: Commit any final fixes**

If Step 1-3 required small fixes, commit them:

```bash
git add rightmemory tests install.sh README.md DESIGN_NOTES.md AGENTS.md pyproject.toml
git commit -m "fix: polish semantic upgrade notes integration"
```

If no fixes were needed, skip this commit.

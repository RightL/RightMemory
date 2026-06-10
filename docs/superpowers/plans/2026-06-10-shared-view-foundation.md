# Shared View Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the first working RightMemory shared-view slice: `M#` headings, durable shared-view references, local resolver/cache state, a read-only shared-view endpoint, and lightweight interaction notes.

**Architecture:** Keep local memory semantics and resolver mechanics separate. `M#` headings remain normal addressable memory nodes, while `rightmemory/shared_views.py` owns registry loading, local imported Markdown retrieval, cache fallback, and interaction records. Retrieve role agents call one endpoint (`retrieve_shared_view`) instead of learning whether a shared view is backed by Markdown, a live provider, or a future hub.

**Tech Stack:** Python standard library (`argparse`, `dataclasses`, `json`, `re`, `tomllib`, `datetime`, `pathlib`), existing RightMemory CLI/runtime/tool patterns, Markdown memory files, `unittest`, Git-backed docs and semantic upgrade notes.

---

## Scope Check

This plan implements the first local foundation, not the full shared-memory product. It produces working, testable behavior for:

- `M#` schema and validation.
- A durable `shared_views.toml` connection registry beside `MEMORY.md`.
- Runtime cache and interaction records under `.runtime/shared_views/`.
- CLI commands for accepting, listing, retrieving, and noting against shared views.
- A retrieve-role endpoint tool that returns shared-view context with provenance and freshness.
- Prompt/schema/docs/semantic-upgrade updates that teach future agents the behavior.

This plan does not implement a hub server, remote authentication, a full View Builder, Git export publishing automation, background refresh, notification streams, or cross-machine transport negotiation. Those need follow-up plans after this foundation lands.

## File Structure

- Modify `rightmemory/tools.py`: parse `M#` headings as graph ids while keeping `####` pointer rules limited to `F#` and `S#`.
- Create `rightmemory/shared_views.py`: connection registry, TOML writer, local Markdown endpoint retrieval, cache fallback, interaction notes, and helper formatting.
- Modify `rightmemory/runtime.py`: expose `retrieve_shared_view(heading_id, query)` to standalone retrieve role tools.
- Modify `rightmemory/prompt.py`: mention the shared-view endpoint in retrieve tool guidance without leaking backing modes.
- Modify `rightmemory/prompts/retrieve.md`: teach retrieve agents how to use `M#` headings and shared-view endpoint results.
- Modify `rightmemory/prompts/update.md`, `rightmemory/prompts/dreamer.md`, and `rightmemory/prompts/reviewer.md`: teach write-capable roles how to preserve `M#` relationship meaning without absorbing provider content as local memory.
- Modify `skills/rightmemory-schema.md`: document `M#` heading syntax and local-memory boundary.
- Modify `rightmemory/cli.py`: add `rightmemory shared-view list|accept|retrieve|note` commands.
- Modify `rightmemory/session.py` and `install.sh`: add `shared_views.toml` to the memory-root allowlist while keeping `.runtime/shared_views/` ignored.
- Modify `rightmemory/sync.py`: include `shared_views.toml` in normal sync ownership.
- Create `tests/test_shared_views.py`: registry, endpoint, cache, and interaction tests.
- Modify `tests/test_tools.py`: `M#` validation coverage.
- Modify `tests/test_cli.py`: shared-view CLI coverage.
- Modify `tests/test_config.py`: prompt assembly and runtime tool exposure coverage.
- Modify `tests/test_install.py`, `tests/test_profiles.py`, and sync tests for the git/sync allowlist change.
- Create `rightmemory/semantic_upgrades/2026-06-10-shared-view-headings.md`: Dreamer guidance for older memory roots.
- Modify `README.md` and `AGENTS.md`: document the new command surface and safety boundaries.

## Task 1: Add `M#` To The Memory Schema Validator

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `skills/rightmemory-schema.md`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing validation tests**

Add these tests near the existing `validate_memory` tests in `tests/test_tools.py`:

```python
    def test_validate_memory_accepts_shared_view_heading_marker(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Alice Auth API {M#alice-auth-api} → [rel:project]\n\n"
            "Alice owns auth API collaboration context.\n\n"
            "- `frontend-login` Frontend login work uses Alice's shared view. → [rel:alice-auth-api]\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed: 3 ids", result)

    def test_validate_memory_rejects_four_hash_shared_view_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "### Integrations\n\n"
            "#### Alice Auth API {M#alice-auth-api}\n\n"
            "This should be a normal #/##/### shared-view heading, not a pointer.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("#### pointer must use `{F#slug}` or `{S#slug}`", result)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_validate_memory_accepts_shared_view_heading_marker tests.test_tools.MemoryToolsTests.test_validate_memory_rejects_four_hash_shared_view_pointer
```

Expected:

- The first test fails because `M#` is not parsed as an id yet, causing `rel:alice-auth-api` to dangle.
- The second test protects the `####` pointer rule and should keep producing the `#### pointer` error after implementation.

- [ ] **Step 3: Extend heading anchor parsing**

In `rightmemory/tools.py`, replace the two anchor regexes with:

```python
ANCHOR_RE = re.compile(r"^(#{1,4})\s+.*?\{(?:F#|S#|M#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|S#|M#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
POINTER_HEADING_KINDS = {"F#", "S#"}
```

Keep `POINTER_HEADING_KINDS` unchanged so `#### {M#...}` remains invalid.

- [ ] **Step 4: Update schema text**

In `skills/rightmemory-schema.md`, update the addressable heading example:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
### Skill Title {S#heading-id} → [edge1, edge2, ...]
### Shared View Title {M#heading-id} → [edge1, edge2, ...]
```

Add this bullet after the `S#` bullet:

```md
- `M#` marks a local heading as a shared-view connection. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:M#heading-id`. The heading body records the local relationship and collaboration meaning; resolver details live outside memory prose.
```

Add this heading rule near the `#`/`##`/`###` rules:

```md
- A shared-view `#`, `##`, or `###` heading uses `{M#short-slug}` and points to an external shared view through an out-of-band resolver entry. Do not use `M#` on `####` pointers.
```

- [ ] **Step 5: Run focused tests**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_validate_memory_accepts_shared_view_heading_marker tests.test_tools.MemoryToolsTests.test_validate_memory_rejects_four_hash_shared_view_pointer
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/tools.py skills/rightmemory-schema.md tests/test_tools.py
git commit -m "feat: accept shared view memory headings"
```

## Task 2: Add Shared View Registry And Runtime State

**Files:**
- Create: `rightmemory/shared_views.py`
- Create: `tests/test_shared_views.py`

- [ ] **Step 1: Write failing registry tests**

Create `tests/test_shared_views.py`:

```python
import tempfile
import unittest
from pathlib import Path

from rightmemory.shared_views import (
    SharedViewConnection,
    SharedViewTarget,
    load_connections,
    save_connections,
)


class SharedViewRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_save_and_load_connections(self):
        connection = SharedViewConnection(
            heading_id="alice-auth-api",
            ref="rightmemory://view/alice-auth-api",
            relationship="human",
            maintainer="Alice",
            description="Auth API collaboration context",
            accepted_from="rightmemory://view/invite/abc123",
            target=SharedViewTarget(kind="local_markdown", path=".runtime/shared_views/imports/alice-auth-api"),
        )

        save_connections(self.root, {"alice-auth-api": connection})
        loaded = load_connections(self.root)

        self.assertEqual(loaded["alice-auth-api"], connection)

    def test_load_connections_rejects_unknown_relationship(self):
        (self.root / "shared_views.toml").write_text(
            """
            [connections.alice-auth-api]
            ref = "rightmemory://view/alice-auth-api"
            relationship = "mystery"
            """,
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_connections(self.root)

        self.assertIn("unknown shared view relationship", str(caught.exception))

    def test_load_connections_rejects_target_outside_memory_root(self):
        (self.root / "shared_views.toml").write_text(
            """
            [connections.alice-auth-api]
            ref = "rightmemory://view/alice-auth-api"
            relationship = "human"

            [connections.alice-auth-api.target]
            kind = "local_markdown"
            path = "../outside"
            """,
            encoding="utf-8",
        )

        with self.assertRaises(ValueError) as caught:
            load_connections(self.root)

        self.assertIn("shared view target path must stay under the memory root", str(caught.exception))
```

- [ ] **Step 2: Run tests and verify import failure**

Run:

```bash
python -m unittest tests.test_shared_views
```

Expected: FAIL with `ModuleNotFoundError` or import errors for `rightmemory.shared_views`.

- [ ] **Step 3: Create registry model**

Create `rightmemory/shared_views.py` with this initial content:

```python
from __future__ import annotations

import json
import os
import re
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .session import _ensure_runtime_gitignore, _fsync_directory


REGISTRY_FILE = "shared_views.toml"
RUNTIME_DIR = ".runtime/shared_views"
CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}
TARGET_KINDS = {"none", "local_markdown", "revoked"}


@dataclass(frozen=True)
class SharedViewTarget:
    kind: str = "none"
    path: str | None = None


@dataclass(frozen=True)
class SharedViewConnection:
    heading_id: str
    ref: str
    relationship: str = "human"
    maintainer: str | None = None
    description: str | None = None
    accepted_from: str | None = None
    target: SharedViewTarget = SharedViewTarget()


def load_connections(memory_root: Path) -> dict[str, SharedViewConnection]:
    root = Path(memory_root).expanduser()
    registry = root / REGISTRY_FILE
    if not registry.exists():
        return {}
    with registry.open("rb") as handle:
        data = tomllib.load(handle)
    raw_connections = data.get("connections", {})
    if not isinstance(raw_connections, dict):
        raise ValueError(f"{REGISTRY_FILE} must contain a [connections] table")
    connections: dict[str, SharedViewConnection] = {}
    for raw_heading_id, raw_entry in raw_connections.items():
        heading_id = _validate_heading_id(str(raw_heading_id))
        if not isinstance(raw_entry, dict):
            raise ValueError(f"[connections.{heading_id}] must be a TOML table")
        ref = _required_string(raw_entry, "ref", heading_id)
        relationship = str(raw_entry.get("relationship", "human")).strip()
        if relationship not in RELATIONSHIPS:
            raise ValueError(f"unknown shared view relationship `{relationship}` for {heading_id}")
        target = _load_target(root, heading_id, raw_entry.get("target", {}))
        connections[heading_id] = SharedViewConnection(
            heading_id=heading_id,
            ref=ref,
            relationship=relationship,
            maintainer=_optional_string(raw_entry.get("maintainer")),
            description=_optional_string(raw_entry.get("description")),
            accepted_from=_optional_string(raw_entry.get("accepted_from")),
            target=target,
        )
    return connections


def save_connections(memory_root: Path, connections: dict[str, SharedViewConnection]) -> None:
    root = Path(memory_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    lines = ["# RightMemory shared view registry", ""]
    for heading_id in sorted(connections):
        connection = connections[heading_id]
        _validate_heading_id(connection.heading_id)
        if heading_id != connection.heading_id:
            raise ValueError(f"connection key `{heading_id}` does not match heading id `{connection.heading_id}`")
        lines.append(f"[connections.{heading_id}]")
        lines.append(f"ref = {_toml_string(connection.ref)}")
        lines.append(f"relationship = {_toml_string(connection.relationship)}")
        if connection.maintainer:
            lines.append(f"maintainer = {_toml_string(connection.maintainer)}")
        if connection.description:
            lines.append(f"description = {_toml_string(connection.description)}")
        if connection.accepted_from:
            lines.append(f"accepted_from = {_toml_string(connection.accepted_from)}")
        if connection.target.kind != "none" or connection.target.path:
            lines.append("")
            lines.append(f"[connections.{heading_id}.target]")
            lines.append(f"kind = {_toml_string(connection.target.kind)}")
            if connection.target.path:
                lines.append(f"path = {_toml_string(connection.target.path)}")
        lines.append("")
    _atomic_write_text(root / REGISTRY_FILE, "\n".join(lines).rstrip() + "\n")


def _load_target(root: Path, heading_id: str, raw_target: object) -> SharedViewTarget:
    if raw_target in ({}, None):
        return SharedViewTarget()
    if not isinstance(raw_target, dict):
        raise ValueError(f"[connections.{heading_id}.target] must be a TOML table")
    kind = str(raw_target.get("kind", "none")).strip()
    if kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{kind}` for {heading_id}")
    path = _optional_string(raw_target.get("path"))
    if kind == "local_markdown" and not path:
        raise ValueError(f"local_markdown shared view target requires path for {heading_id}")
    if path:
        _resolve_under_root(root, path)
    return SharedViewTarget(kind=kind, path=path)


def _validate_heading_id(value: str) -> str:
    heading_id = value.strip()
    if not heading_id or CONNECTION_ID_RE.fullmatch(heading_id) is None:
        raise ValueError(f"shared view heading id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return heading_id


def _required_string(entry: dict[str, object], key: str, heading_id: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"[connections.{heading_id}].{key} must be a non-empty string")
    return value.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("optional shared view string fields must be strings")
    stripped = value.strip()
    return stripped or None


def _resolve_under_root(root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("shared view target path must stay under the memory root")
    resolved = (root / path).resolve()
    if root.resolve() not in (resolved, *resolved.parents):
        raise ValueError("shared view target path must stay under the memory root")
    return resolved


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with tmp_path.open("w", encoding="utf-8") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, path)
    _fsync_directory(path.parent)
```

- [ ] **Step 4: Run registry tests**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewRegistryTests
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/shared_views.py tests/test_shared_views.py
git commit -m "feat: add shared view registry"
```

## Task 3: Add Accept Flow For Local `M#` Connections

**Files:**
- Modify: `rightmemory/shared_views.py`
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing accept tests**

Add to `tests/test_shared_views.py`:

```python
from rightmemory.shared_views import accept_shared_view


class SharedViewAcceptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")

    def test_accept_shared_view_creates_heading_and_registry_entry(self):
        result = accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
            relationship="human",
            maintainer="Alice",
            description="Auth API collaboration context",
            accepted_from="rightmemory://view/invite/abc123",
            target_path=".runtime/shared_views/imports/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")
        loaded = load_connections(self.root)

        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)
        self.assertIn("Alice owns auth API collaboration context.", memory)
        self.assertEqual(loaded["alice-auth-api"].ref, "rightmemory://view/alice-auth-api")
        self.assertIn("accepted shared view alice-auth-api", result)

    def test_accept_shared_view_does_not_duplicate_existing_heading(self):
        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )
        accept_shared_view(
            self.root,
            heading_id="alice-auth-api",
            title="Alice Auth API",
            body="Alice owns auth API collaboration context.",
            ref="rightmemory://view/alice-auth-api",
        )

        memory = (self.root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertEqual(memory.count("{M#alice-auth-api}"), 1)
```

Add to `tests/test_cli.py`:

```python
    def test_shared_view_accept_cli_uses_active_memory_root(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main([
                    "shared-view",
                    "accept",
                    "alice-auth-api",
                    "--title",
                    "Alice Auth API",
                    "--body",
                    "Alice owns auth API collaboration context.",
                    "--ref",
                    "rightmemory://view/alice-auth-api",
                    "--relationship",
                    "human",
                    "--maintainer",
                    "Alice",
                    "--description",
                    "Auth API collaboration context",
                    "--target",
                    ".runtime/shared_views/imports/alice-auth-api",
                ])

            memory = (root / "MEMORY.md").read_text(encoding="utf-8")

        self.assertEqual(result, 0)
        self.assertIn("accepted shared view alice-auth-api", stdout.getvalue())
        self.assertIn("### Alice Auth API {M#alice-auth-api}", memory)
```

- [ ] **Step 2: Run tests and verify missing function/command failure**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewAcceptTests tests.test_cli.JsonRequestTests.test_shared_view_accept_cli_uses_active_memory_root
```

Expected: FAIL because `accept_shared_view` and `shared-view accept` do not exist yet.

- [ ] **Step 3: Implement accept helper**

Append this to `rightmemory/shared_views.py`:

```python
def accept_shared_view(
    memory_root: Path,
    *,
    heading_id: str,
    title: str,
    body: str,
    ref: str,
    relationship: str = "human",
    maintainer: str | None = None,
    description: str | None = None,
    accepted_from: str | None = None,
    target_path: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    if relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{relationship}`")
    target = SharedViewTarget("local_markdown", target_path) if target_path else SharedViewTarget()
    if target.path:
        _resolve_under_root(root, target.path)
    connection = SharedViewConnection(
        heading_id=heading_id,
        ref=ref.strip(),
        relationship=relationship,
        maintainer=maintainer.strip() if maintainer else None,
        description=description.strip() if description else None,
        accepted_from=accepted_from.strip() if accepted_from else None,
        target=target,
    )
    connections = load_connections(root)
    connections[heading_id] = connection
    _ensure_memory_heading(root, heading_id=heading_id, title=title, body=body)
    save_connections(root, connections)
    return f"accepted shared view {heading_id}"


def _ensure_memory_heading(root: Path, *, heading_id: str, title: str, body: str) -> None:
    memory = root / "MEMORY.md"
    if not memory.exists():
        memory.write_text("# Shared Views\n", encoding="utf-8")
    text = memory.read_text(encoding="utf-8")
    if f"{{M#{heading_id}}}" in text:
        return
    title_text = title.strip() or heading_id
    body_text = body.strip()
    section = "# Shared Views"
    addition = f"\n\n### {title_text} {{M#{heading_id}}}\n"
    if body_text:
        addition += f"\n{body_text}\n"
    if section not in text:
        addition = f"\n\n{section}\n{addition}"
    memory.write_text(text.rstrip() + addition, encoding="utf-8")
```

- [ ] **Step 4: Add CLI command parser**

In `rightmemory/cli.py`, add imports:

```python
from .shared_views import accept_shared_view, load_connections
```

In `main`, after profile command handling and before the no-argv help branch, add:

```python
    if argv and argv[0] == "shared-view":
        active = resolve_memory_root(profile_name=profile_name, cwd=Path.cwd(), default_root=default_memory_root())
        return _shared_view_main(argv[1:], active.memory_root)
```

Add this command handler near `_profile_main`:

```python
def _shared_view_main(argv: list[str], memory_root: Path) -> int:
    parser = argparse.ArgumentParser(prog="rightmemory shared-view")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("list")
    accept = subparsers.add_parser("accept")
    accept.add_argument("heading_id")
    accept.add_argument("--title", required=True)
    accept.add_argument("--body", default="")
    accept.add_argument("--ref", required=True)
    accept.add_argument("--relationship", choices=("human", "owned-agent", "team-space", "external"), default="human")
    accept.add_argument("--maintainer")
    accept.add_argument("--description")
    accept.add_argument("--accepted-from")
    accept.add_argument("--target")
    args = parser.parse_args(argv)
    if args.command == "list":
        for heading_id, connection in sorted(load_connections(memory_root).items()):
            maintainer = connection.maintainer or "-"
            description = connection.description or "-"
            print(f"{heading_id}\t{connection.relationship}\t{maintainer}\t{description}")
        return 0
    if args.command == "accept":
        print(
            accept_shared_view(
                memory_root,
                heading_id=args.heading_id,
                title=args.title,
                body=args.body,
                ref=args.ref,
                relationship=args.relationship,
                maintainer=args.maintainer,
                description=args.description,
                accepted_from=args.accepted_from,
                target_path=args.target,
            )
        )
        return 0
    raise ValueError(f"unknown shared-view command: {args.command}")
```

- [ ] **Step 5: Run accept tests**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewAcceptTests tests.test_cli.JsonRequestTests.test_shared_view_accept_cli_uses_active_memory_root
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/shared_views.py rightmemory/cli.py tests/test_shared_views.py tests/test_cli.py
git commit -m "feat: accept shared view connections"
```

## Task 4: Implement Shared View Retrieval Endpoint And Cache

**Files:**
- Modify: `rightmemory/shared_views.py`
- Modify: `tests/test_shared_views.py`

- [ ] **Step 1: Write failing endpoint tests**

Add to `tests/test_shared_views.py`:

```python
from rightmemory.shared_views import retrieve_shared_view


class SharedViewRetrieveTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.import_dir = self.root / ".runtime" / "shared_views" / "imports" / "alice-auth-api"
        self.import_dir.mkdir(parents=True)
        (self.import_dir / "MEMORY.md").write_text(
            "# Alice Auth API\n\n"
            "- `token-expiry` Login responses include token_expires_at for expiry display. → []\n",
            encoding="utf-8",
        )
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                    description="Auth API collaboration context",
                    target=SharedViewTarget(kind="local_markdown", path=".runtime/shared_views/imports/alice-auth-api"),
                )
            },
        )

    def test_retrieve_shared_view_returns_fresh_markdown_matches(self):
        result = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")

        self.assertIn("Shared view: alice-auth-api", result)
        self.assertIn("Status: fresh", result)
        self.assertIn("Maintainer: Alice", result)
        self.assertIn("token_expires_at", result)

    def test_retrieve_shared_view_uses_cache_when_target_disappears(self):
        fresh = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")
        for path in self.import_dir.glob("MEMORY*.md"):
            path.unlink()

        cached = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")

        self.assertIn("Status: cached", cached)
        self.assertIn("token_expires_at", cached)
        self.assertNotEqual(fresh, cached)

    def test_retrieve_shared_view_does_not_use_cache_after_revocation(self):
        retrieve_shared_view(self.root, "alice-auth-api", "token expiry")
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    target=SharedViewTarget(kind="revoked"),
                )
            },
        )

        result = retrieve_shared_view(self.root, "alice-auth-api", "token expiry")

        self.assertIn("Status: unavailable", result)
        self.assertIn("access revoked", result)
        self.assertNotIn("token_expires_at", result)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewRetrieveTests
```

Expected: FAIL because `retrieve_shared_view` does not return endpoint results yet.

- [ ] **Step 3: Add endpoint and cache helpers**

Append this to `rightmemory/shared_views.py`:

```python
class SharedViewTools:
    def __init__(self, memory_root: Path):
        self.memory_root = Path(memory_root).expanduser()

    def retrieve_shared_view(self, heading_id: str, query: str) -> str:
        """Retrieve context from a shared view by local M# heading id."""
        return retrieve_shared_view(self.memory_root, heading_id, query)


def retrieve_shared_view(memory_root: Path, heading_id: str, query: str) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    query = query.strip()
    if not query:
        raise ValueError("shared view query must not be empty")
    connections = load_connections(root)
    connection = connections.get(heading_id)
    if connection is None:
        return _format_unavailable(heading_id, "no shared view connection is registered")
    if connection.target.kind == "revoked":
        return _format_unavailable(heading_id, "access revoked")
    if connection.target.kind == "local_markdown" and connection.target.path:
        target = _resolve_under_root(root, connection.target.path)
        if target.exists():
            content = _retrieve_local_markdown(connection, target, query)
            _write_cache(root, heading_id, content)
            return content
    cached = _read_cache(root, heading_id)
    if cached:
        return _mark_cached(cached)
    return _format_unavailable(heading_id, "no shared view content is available")


def _retrieve_local_markdown(connection: SharedViewConnection, target: Path, query: str) -> str:
    terms = _query_terms(query)
    matches: list[str] = []
    for path in sorted(target.glob("MEMORY*.md")):
        if not path.is_file():
            continue
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            haystack = line.lower()
            if terms and not any(term in haystack for term in terms):
                continue
            relative = path.relative_to(target).as_posix()
            matches.append(f"- {relative}:{line_number}: {line}")
            if len(matches) >= 12:
                break
        if len(matches) >= 12:
            break
    if not matches:
        matches.append("- no strong match in published shared memory")
    return _format_result(connection, "fresh", matches)


def _format_result(connection: SharedViewConnection, status: str, matches: list[str]) -> str:
    refreshed = datetime.now(UTC).replace(microsecond=0).isoformat()
    lines = [
        f"Shared view: {connection.heading_id}",
        f"Status: {status}",
        f"Ref: {connection.ref}",
    ]
    if connection.maintainer:
        lines.append(f"Maintainer: {connection.maintainer}")
    if connection.description:
        lines.append(f"Description: {connection.description}")
    lines.append(f"Freshness: {refreshed}")
    lines.append("Matches:")
    lines.extend(matches)
    return "\n".join(lines)


def _format_unavailable(heading_id: str, reason: str) -> str:
    return "\n".join(
        [
            f"Shared view: {heading_id}",
            "Status: unavailable",
            f"Reason: {reason}",
        ]
    )


def _query_terms(query: str) -> list[str]:
    return [term for term in re.findall(r"[A-Za-z0-9_]{3,}", query.lower()) if term not in {"the", "and", "for"}]


def _cache_path(root: Path, heading_id: str) -> Path:
    safe = _validate_heading_id(heading_id)
    return root / RUNTIME_DIR / "cache" / f"{safe}.txt"


def _write_cache(root: Path, heading_id: str, content: str) -> None:
    cache_path = _cache_path(root, heading_id)
    _ensure_runtime_gitignore(root / ".runtime")
    _atomic_write_text(cache_path, content)


def _read_cache(root: Path, heading_id: str) -> str | None:
    cache_path = _cache_path(root, heading_id)
    if not cache_path.exists():
        return None
    return cache_path.read_text(encoding="utf-8")


def _mark_cached(content: str) -> str:
    return content.replace("Status: fresh", "Status: cached", 1)
```

- [ ] **Step 4: Run endpoint tests**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewRetrieveTests
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/shared_views.py tests/test_shared_views.py
git commit -m "feat: retrieve shared view context"
```

## Task 5: Expose Shared View Retrieve Through CLI And Runtime Tools

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/prompts/retrieve.md`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing CLI and runtime-tool tests**

Add to `tests/test_cli.py`:

```python
    def test_shared_view_retrieve_cli_returns_endpoint_context(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            import_dir = root / ".runtime" / "shared_views" / "imports" / "alice-auth-api"
            import_dir.mkdir(parents=True)
            (import_dir / "MEMORY.md").write_text(
                "- `token-expiry` Login responses include token_expires_at. → []\n",
                encoding="utf-8",
            )
            (root / "shared_views.toml").write_text(
                """
                [connections.alice-auth-api]
                ref = "rightmemory://view/alice-auth-api"
                relationship = "human"
                maintainer = "Alice"
                description = "Auth API collaboration context"

                [connections.alice-auth-api.target]
                kind = "local_markdown"
                path = ".runtime/shared_views/imports/alice-auth-api"
                """,
                encoding="utf-8",
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "retrieve", "alice-auth-api", "token", "expiry"])

        self.assertEqual(result, 0)
        self.assertIn("Status: fresh", stdout.getvalue())
        self.assertIn("token_expires_at", stdout.getvalue())
```

Add to `tests/test_config.py` near prompt assembly tests:

```python
    def test_retrieve_prompt_includes_shared_view_endpoint_guidance(self):
        prompt = build_instructions(Path("/memory"), "retrieve")

        self.assertIn("M# headings", prompt)
        self.assertIn("retrieve_shared_view", prompt)
        self.assertIn("shared view endpoint", prompt)

    def test_retrieve_runtime_exposes_shared_view_tool(self):
        config = RuntimeConfig(
            role="retrieve",
            runtime_mode="standalone",
            model_id="openai/test",
            memory_root=Path("/memory"),
            state_root=Path("/memory"),
        )

        with patch.object(RightMemoryRuntime, "_build_agent", return_value=object()):
            runtime = RightMemoryRuntime(config)

        tool_names = {tool.__name__ for tool in runtime._agent_tools()}

        self.assertIn("retrieve_shared_view", tool_names)
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_shared_view_retrieve_cli_returns_endpoint_context tests.test_config.ConfigTests.test_retrieve_prompt_includes_shared_view_endpoint_guidance tests.test_config.ConfigTests.test_retrieve_runtime_exposes_shared_view_tool
```

Expected: CLI fails because the retrieve subcommand is not registered yet; prompt and runtime tool tests fail until runtime integration lands.

- [ ] **Step 3: Add shared-view retrieve CLI command**

In `rightmemory/cli.py`, extend the import:

```python
from .shared_views import accept_shared_view, load_connections, retrieve_shared_view
```

In `_shared_view_main`, add the retrieve parser after the accept parser:

```python
    retrieve = subparsers.add_parser("retrieve")
    retrieve.add_argument("heading_id")
    retrieve.add_argument("query", nargs=argparse.REMAINDER)
```

Add this command branch after the accept branch:

```python
    if args.command == "retrieve":
        query = " ".join(args.query).strip()
        if not query:
            raise ValueError("shared-view retrieve requires a query")
        print(retrieve_shared_view(memory_root, args.heading_id, query))
        return 0
```

- [ ] **Step 4: Wire runtime tool**

In `rightmemory/runtime.py`, add import:

```python
from .shared_views import SharedViewTools
```

In `RightMemoryRuntime.__init__`, after `self.tools`:

```python
        self.shared_view_tools = SharedViewTools(config.memory_root)
```

In `_agent_tools`, after read tools are built:

```python
        if self.config.role == "retrieve":
            read_tools.append(self._agent_tool(self.shared_view_tools.retrieve_shared_view))
```

- [ ] **Step 5: Update retrieve tool guidance**

In `rightmemory/prompt.py`, update the retrieve branch of `_tool_guidance`:

```python
    if role == "retrieve":
        return (
            "- Use the provided read-only tools for `read`, `grep`, `glob`, restricted `read_command`, outline, "
            "validation, and `retrieve_shared_view`.\n"
            "- Use `retrieve_shared_view(heading_id, query)` when a relevant `M#` heading points to an external "
            "shared view. Do not read external shared-view Markdown as local memory.\n"
            "- `read_command` accepts common read-only shell forms such as `cat path`, `sed -n 'X,Yp' path`, "
            "`rg pattern`, `rg --files`, `git status --short`, and `git diff`. It does not run a general shell."
        )
```

- [ ] **Step 6: Update retrieve role prompt**

In `rightmemory/prompts/retrieve.md`, add this section after "Memory Skills":

```md
## Shared Views

`M#` headings are local shared-view connection nodes. Their heading body records
why the external view matters locally. When an `M#` heading is strongly relevant
to the caller's request, call `retrieve_shared_view` with the local heading id
and the caller's query. Treat the returned content as external shared context
with provenance and freshness, not as local memory.

Do not infer whether a shared view is backed by published Markdown, a live
provider retriever, or a future hub. The shared-view endpoint owns that choice.
Do not read imported shared-view files directly as if they were `MEMORY*.md`.
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
python -m unittest tests.test_cli.JsonRequestTests.test_shared_view_retrieve_cli_returns_endpoint_context tests.test_config.ConfigTests.test_retrieve_prompt_includes_shared_view_endpoint_guidance tests.test_config.ConfigTests.test_retrieve_runtime_exposes_shared_view_tool
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add rightmemory/cli.py rightmemory/runtime.py rightmemory/prompt.py rightmemory/prompts/retrieve.md tests/test_cli.py tests/test_config.py
git commit -m "feat: expose shared view retrieval"
```

## Task 6: Add Interaction Notes With Relationship Manners

**Files:**
- Modify: `rightmemory/shared_views.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing interaction tests**

Add to `tests/test_shared_views.py`:

```python
from rightmemory.shared_views import record_shared_view_note


class SharedViewInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_human_connection_requires_confirmation_before_note(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                )
            },
        )

        result = record_shared_view_note(self.root, "alice-auth-api", "Docs are missing token_expires_at.")

        self.assertIn("confirmation required", result)
        self.assertFalse((self.root / ".runtime" / "shared_views" / "interactions").exists())

    def test_owned_agent_connection_records_note_without_confirmation(self):
        save_connections(
            self.root,
            {
                "auth-agent": SharedViewConnection(
                    heading_id="auth-agent",
                    ref="rightmemory://view/auth-agent",
                    relationship="owned-agent",
                    maintainer="Auth Agent",
                )
            },
        )

        result = record_shared_view_note(self.root, "auth-agent", "Refresh the token expiry view.")
        log_path = self.root / ".runtime" / "shared_views" / "interactions" / "auth-agent.jsonl"
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]

        self.assertIn("recorded shared view note", result)
        self.assertEqual(records[0]["relationship"], "owned-agent")
        self.assertEqual(records[0]["status"], "sent")
        self.assertEqual(records[0]["message"], "Refresh the token expiry view.")

    def test_confirmed_human_note_is_recorded(self):
        save_connections(
            self.root,
            {
                "alice-auth-api": SharedViewConnection(
                    heading_id="alice-auth-api",
                    ref="rightmemory://view/alice-auth-api",
                    relationship="human",
                    maintainer="Alice",
                )
            },
        )

        result = record_shared_view_note(
            self.root,
            "alice-auth-api",
            "Docs are missing token_expires_at.",
            confirmed=True,
        )

        self.assertIn("recorded shared view note", result)
```

Add to `tests/test_cli.py`:

```python
    def test_shared_view_note_cli_requires_confirmation_for_human_connection(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            (root / "shared_views.toml").write_text(
                """
                [connections.alice-auth-api]
                ref = "rightmemory://view/alice-auth-api"
                relationship = "human"
                maintainer = "Alice"
                """,
                encoding="utf-8",
            )

            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("sys.stdout", stdout),
            ):
                result = main(["shared-view", "note", "alice-auth-api", "Docs", "are", "stale"])

        self.assertEqual(result, 0)
        self.assertIn("confirmation required", stdout.getvalue())
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewInteractionTests tests.test_cli.JsonRequestTests.test_shared_view_note_cli_requires_confirmation_for_human_connection
```

Expected: FAIL because `record_shared_view_note` is not implemented.

- [ ] **Step 3: Implement interaction notes**

Append to `rightmemory/shared_views.py`:

```python
def record_shared_view_note(
    memory_root: Path,
    heading_id: str,
    message: str,
    *,
    confirmed: bool = False,
    actor: str = "user",
) -> str:
    root = Path(memory_root).expanduser()
    heading_id = _validate_heading_id(heading_id)
    message = message.strip()
    if not message:
        raise ValueError("shared view note message must not be empty")
    connections = load_connections(root)
    connection = connections.get(heading_id)
    if connection is None:
        return f"shared view {heading_id} is not registered"
    if connection.relationship in {"human", "external"} and not confirmed:
        maintainer = f" for {connection.maintainer}" if connection.maintainer else ""
        return f"confirmation required before sending note{maintainer}: {message}"
    record = {
        "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "heading_id": heading_id,
        "ref": connection.ref,
        "relationship": connection.relationship,
        "maintainer": connection.maintainer,
        "actor": actor,
        "status": "sent",
        "message": message,
    }
    _append_interaction_record(root, heading_id, record)
    return f"recorded shared view note for {heading_id}"


def _append_interaction_record(root: Path, heading_id: str, record: dict[str, Any]) -> None:
    _ensure_runtime_gitignore(root / ".runtime")
    path = root / RUNTIME_DIR / "interactions" / f"{_validate_heading_id(heading_id)}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    _fsync_directory(path.parent)
```

- [ ] **Step 4: Add shared-view note CLI command**

In `rightmemory/cli.py`, extend the import:

```python
from .shared_views import accept_shared_view, load_connections, record_shared_view_note, retrieve_shared_view
```

In `_shared_view_main`, add the note parser after the retrieve parser:

```python
    note = subparsers.add_parser("note")
    note.add_argument("heading_id")
    note.add_argument("--confirm", action="store_true")
    note.add_argument("--actor", default="user")
    note.add_argument("message", nargs=argparse.REMAINDER)
```

Add this command branch after the retrieve branch:

```python
    if args.command == "note":
        message = " ".join(args.message).strip()
        if not message:
            raise ValueError("shared-view note requires a message")
        print(record_shared_view_note(memory_root, args.heading_id, message, confirmed=args.confirm, actor=args.actor))
        return 0
```

- [ ] **Step 5: Run interaction tests**

Run:

```bash
python -m unittest tests.test_shared_views.SharedViewInteractionTests tests.test_cli.JsonRequestTests.test_shared_view_note_cli_requires_confirmation_for_human_connection
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/shared_views.py tests/test_shared_views.py tests/test_cli.py
git commit -m "feat: record shared view interactions"
```

## Task 7: Make Registry Visibility And Sync Behavior Explicit

**Files:**
- Modify: `rightmemory/session.py`
- Modify: `install.sh`
- Modify: `rightmemory/sync.py`
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/prompts/sync-reconciler.md`
- Modify: `tests/test_install.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_profiles.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing allowlist tests**

Update `.gitignore` expectations in tests that currently assert the memory allowlist exactly. The expected content should become:

```python
expected_gitignore = (
    "*\n"
    "!MEMORY.md\n"
    "!MEMORY_*.md\n"
    "!shared_views.toml\n"
    "!insight_logs/\n"
    "!insight_logs/*.md\n"
)
```

Apply that expected value in:

- `tests/test_install.py::InstallTests.test_install_refreshes_memory_gitignore_to_current_allowlist`
- `tests/test_config.py::ConfigTests.test_write_role_creates_memory_lock_and_gitignore`
- `tests/test_profiles.py` tests that read a profile-root `.gitignore`

Add a sync path test to `tests/test_sync.py` near existing path ownership tests:

```python
    def test_sync_paths_include_shared_view_registry(self):
        from rightmemory.sync import MEMORY_SYNC_PATHS

        self.assertIn("shared_views.toml", MEMORY_SYNC_PATHS)
```

Add a sync-reconciler write-scope test to `tests/test_tools.py` near the existing sync-reconciler write tests:

```python
    def test_sync_reconciler_can_repair_shared_view_registry(self):
        self._git("init")
        registry = self.root / "shared_views.toml"
        registry.write_text(
            '[connections.alice-auth-api]\nref = "rightmemory://view/old"\n',
            encoding="utf-8",
        )
        tools = MemoryTools(self.root, role="sync-reconciler")

        tools.read_file("shared_views.toml")
        edit_result = tools.edit_file(
            "shared_views.toml",
            'ref = "rightmemory://view/old"',
            'ref = "rightmemory://view/new"',
        )
        add_result = tools.git_add(["shared_views.toml"])

        self.assertEqual(edit_result, "edited shared_views.toml: replaced 1 occurrence")
        self.assertEqual(add_result, "staged: shared_views.toml")
```

- [ ] **Step 2: Run tests and verify failures**

Run:

```bash
python -m unittest tests.test_install.InstallTests.test_install_refreshes_memory_gitignore_to_current_allowlist tests.test_config.ConfigTests.test_write_role_creates_memory_lock_and_gitignore tests.test_sync tests.test_tools.MemoryToolsTests.test_sync_reconciler_can_repair_shared_view_registry
```

Expected: FAIL because `shared_views.toml` is not yet in the allowlist or sync paths.

- [ ] **Step 3: Update runtime gitignore allowlist**

In `rightmemory/session.py`, change `_ensure_memory_gitignore` content to:

```python
        b"*\n!MEMORY.md\n!MEMORY_*.md\n!shared_views.toml\n!insight_logs/\n!insight_logs/*.md\n",
```

- [ ] **Step 4: Update installer gitignore refresh**

In `install.sh`, update the managed memory allowlist block to include:

```bash
!shared_views.toml
```

The resulting block should be:

```bash
*
!MEMORY.md
!MEMORY_*.md
!shared_views.toml
!insight_logs/
!insight_logs/*.md
```

- [ ] **Step 5: Update sync-owned path set**

In `rightmemory/sync.py`, update:

```python
MEMORY_SYNC_PATHS = ("MEMORY.md", "MEMORY_*.md", "shared_views.toml", "insight_logs/*.md")
```

Do not add `.runtime/shared_views/` to sync paths; cache and interaction logs stay local runtime state.

- [ ] **Step 6: Update sync-reconciler write scope**

In `rightmemory/tools.py`, add:

```python
SHARED_VIEW_REGISTRY_PATH = "shared_views.toml"
```

Update `_write_policy_label`:

```python
        if self.role in SYNC_RECONCILER_ROLES:
            return "MEMORY.md, MEMORY_*.md, shared_views.toml, or insight_logs/*.md"
```

Update `_is_allowed_write_path`:

```python
        if self.role in SYNC_RECONCILER_ROLES:
            return (
                self._is_active_memory_path(relative_path)
                or relative_path == SHARED_VIEW_REGISTRY_PATH
                or self._is_insight_log_path(relative_path)
            )
```

Update `rightmemory/prompts/sync-reconciler.md` so its repair surface includes `shared_views.toml`:

```md
The repair surface is the sync-owned file set: active memory files
(`MEMORY.md` and sibling `MEMORY_*.md`), the shared-view registry
(`shared_views.toml`), plus Insight artifacts under `insight_logs/*.md`.
```

- [ ] **Step 7: Run focused tests**

Run:

```bash
python -m unittest tests.test_install.InstallTests.test_install_refreshes_memory_gitignore_to_current_allowlist tests.test_config.ConfigTests.test_write_role_creates_memory_lock_and_gitignore tests.test_profiles tests.test_sync tests.test_tools.MemoryToolsTests.test_sync_reconciler_can_repair_shared_view_registry
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add rightmemory/session.py install.sh rightmemory/sync.py rightmemory/tools.py rightmemory/prompts/sync-reconciler.md tests/test_install.py tests/test_config.py tests/test_profiles.py tests/test_sync.py tests/test_tools.py
git commit -m "feat: sync shared view registry"
```

## Task 8: Teach Write Roles The Shared View Boundary

**Files:**
- Modify: `rightmemory/prompts/update.md`
- Modify: `rightmemory/prompts/dreamer.md`
- Modify: `rightmemory/prompts/reviewer.md`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing prompt tests**

Add to `tests/test_config.py` near the existing prompt tests:

```python
    def test_write_role_prompts_preserve_shared_view_boundary(self):
        for role in ("update", "dreamer", "reviewer"):
            prompt = build_instructions(Path("/memory"), role)
            self.assertIn("M# headings", prompt)
            self.assertIn("shared view", prompt)
            self.assertIn("local relationship", prompt)
            self.assertIn("do not absorb provider content", prompt)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_write_role_prompts_preserve_shared_view_boundary
```

Expected: FAIL because current prompts do not all include shared-view boundary guidance.

- [ ] **Step 3: Update update role prompt**

Add this section to `rightmemory/prompts/update.md` after the schema/source guidance:

```md
## Shared View Connections

`M#` headings record local relationships to external shared views. When adding
or refining an `M#` heading, preserve the local relationship meaning: who or
what the view represents, when this root should use it, and how it relates to
nearby work.

Do not absorb provider content into local memory merely because a shared view
returned it. Local memory should record local decisions, commitments, tasks, or
consequences. If content from a shared view is explicitly absorbed, keep clear
provenance.
```

- [ ] **Step 4: Update dreamer role prompt**

Add this section to `rightmemory/prompts/dreamer.md` near existing consolidation guidance:

```md
## Shared View Connections

Treat `M#` headings as local relationship nodes, not as imported provider
memory. During consolidation, keep the heading body focused on why the shared
view matters locally. Do not absorb provider content into local memory unless
there is a local decision, commitment, task, or consequence to preserve.
```

- [ ] **Step 5: Update reviewer role prompt**

Add this section to `rightmemory/prompts/reviewer.md` near durable-memory guidance:

```md
## Shared View Connections

When transcripts show a durable shared-view relationship, the reviewer may
suggest an `M#` heading that records the local relationship. Do not absorb
provider content into local memory from the transcript alone. Preserve local
decisions, commitments, tasks, or consequences, and keep external provenance
clear when it matters.
```

- [ ] **Step 6: Run prompt test**

Run:

```bash
python -m unittest tests.test_config.ConfigTests.test_write_role_prompts_preserve_shared_view_boundary
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/prompts/update.md rightmemory/prompts/dreamer.md rightmemory/prompts/reviewer.md tests/test_config.py
git commit -m "docs: teach shared view memory boundary"
```

## Task 9: Add Semantic Upgrade Note And User Docs

**Files:**
- Create: `rightmemory/semantic_upgrades/2026-06-10-shared-view-headings.md`
- Modify: `tests/test_semantic_upgrades.py`
- Modify: `README.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Write failing semantic-upgrade test**

In `tests/test_semantic_upgrades.py`, update `test_load_packaged_notes_includes_current_notes`:

```python
        self.assertIn("shared-view-headings", notes_by_id)
        self.assertIn("M#slug", notes_by_id["shared-view-headings"].body)
        self.assertIn("shared view", notes_by_id["shared-view-headings"].body)
```

- [ ] **Step 2: Run semantic-upgrade test and verify failure**

Run:

```bash
python -m unittest tests.test_semantic_upgrades.SemanticUpgradeParserTests.test_load_packaged_notes_includes_current_notes
```

Expected: FAIL because the note does not exist.

- [ ] **Step 3: Add semantic upgrade note**

Create `rightmemory/semantic_upgrades/2026-06-10-shared-view-headings.md`:

```md
---
id: shared-view-headings
introduced_at: 2026-06-10
---

# Shared View Headings

RightMemory now supports `M#slug` headings for local relationships to external
shared views.

Review existing memory that describes another person, team, project, or agent
memory root as a collaboration source. Convert strong candidates into `M#`
headings when the active memory needs a durable local relationship to that
external shared view. Keep the heading body focused on local collaboration
meaning: who or what the view represents, when this root should use it, and how
it relates to local work.

Do not absorb provider content into local memory merely because a shared view
returned it. Preserve local decisions, commitments, tasks, or consequences, and
keep provenance clear when external content is explicitly absorbed.
```

- [ ] **Step 4: Update README command and schema docs**

Add a concise section to `README.md` near the memory schema or command surface:

```md
### Shared Views

`M#` headings record local relationships to external shared views. The heading
body explains the collaboration meaning; resolver details live in
`shared_views.toml`; runtime cache and interaction notes live under
`.runtime/shared_views/`.

Accept a local shared view reference:

```bash
rightmemory shared-view accept alice-auth-api \
  --title "Alice Auth API" \
  --body "Alice owns auth API collaboration context." \
  --ref rightmemory://view/alice-auth-api \
  --relationship human \
  --maintainer Alice \
  --target .runtime/shared_views/imports/alice-auth-api
```

Retrieve from the shared view endpoint:

```bash
rightmemory shared-view retrieve alice-auth-api "token expiry"
```

Leave a note for the shared view owner:

```bash
rightmemory shared-view note alice-auth-api --confirm "Docs are missing token_expires_at."
```
```

- [ ] **Step 5: Update AGENTS operational notes**

In `AGENTS.md`, add a concise memory runtime rule:

```md
- Shared view connections use `M#` headings in memory prose and `shared_views.toml` for resolver metadata. Cache and interaction records belong under `.runtime/shared_views/` and should not be committed.
```

Add a development command note:

```md
- Use `rightmemory shared-view list|accept|retrieve|note` when debugging shared-view connections.
```

- [ ] **Step 6: Run docs-related tests**

Run:

```bash
python -m unittest tests.test_semantic_upgrades.SemanticUpgradeParserTests.test_load_packaged_notes_includes_current_notes
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add rightmemory/semantic_upgrades/2026-06-10-shared-view-headings.md tests/test_semantic_upgrades.py README.md AGENTS.md
git commit -m "docs: document shared view headings"
```

## Task 10: Full Verification

**Files:**
- No new files.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 2: Run full unit test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Inspect git status**

Run:

```bash
git status --short
```

Expected: no unstaged implementation changes. Existing unrelated untracked files such as `.superpowers/` can remain untracked.

## Self-Review

- Spec coverage: this foundation covers `M#` heading syntax, separate resolver registry, endpoint-style retrieval, provenance/freshness, cache fallback, relationship-shaped interaction notes, local memory boundary prompts, and semantic upgrade guidance. Hub transport, full View Builder output generation, remote export automation, and background refresh are intentionally deferred into later plans.
- Placeholder scan: the plan contains concrete file paths, test names, commands, expected outcomes, and code snippets for every implementation task.
- Type consistency: the plan consistently uses `SharedViewConnection`, `SharedViewTarget`, `load_connections`, `save_connections`, `accept_shared_view`, `retrieve_shared_view`, `record_shared_view_note`, and `SharedViewTools.retrieve_shared_view`.

# MF And MQ Shared View Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the legacy `M#` shared-view model with explicit `MF#` mirrored file views and `MQ#` provider question views.

**Architecture:** The redesign keeps HTTP as the only shared-view transport. `MF#` views are provider-owned file projections that consumers silently pull into `.runtime/shared_views/imports/<id>/` before retrieve runs, while `MQ#` views are synchronous provider-side question endpoints called by CLI/Web/main agents outside the retrieve role. The existing monolithic `rightmemory/shared_views.py` should become a compatibility shell around smaller focused modules for schema-free model code, file-view recipes, question-view asks, HTTP package pull, and notes.

**Tech Stack:** Python 3.11 standard library, `unittest`, TOML via `tomllib`, existing FastAPI hub/web stack, existing RightMemory runtime/tool framework, direct test commands with `python -m unittest`.

---

## Scope Check

The spec touches schema, runtime, CLI, hub, Web Studio, provider-side builder behavior, and tests. These pieces are tightly dependent: `MF#` schema is not useful without import reads, import reads are not safe without HTTP pull, and Web Studio cannot be correct while the backend still exposes generic shared-view retrieve. This plan therefore keeps one master plan, but each task lands a coherent commit and can be executed independently in order.

## File Structure

- Modify `skills/rightmemory-schema.md`: define `MF#`, `MQ#`, and terminal `####` reference semantics.
- Modify `rightmemory/tools.py`: parse `MF#` and `MQ#`, reject `M#`, validate terminal `####`, and allow retrieve reads/searches under accepted `MF#` imports.
- Modify `rightmemory/prompt.py` and `rightmemory/prompts/*.md`: remove old `M#` and `retrieve_shared_view` behavior; keep shared-view role guidance thin and schema-driven.
- Modify `skills/memory-orchestrator-cli/SKILL.md`: teach the main agent when to call `rightmemory shared-view ask` after retrieve reports an `MQ#`.
- Modify `rightmemory/config.py`: add an internal `shared-view-builder` role used by natural-language builder commands.
- Create `rightmemory/prompts/shared-view-builder.md`: role prompt for writing `MF#` recipes and `MQ#` question configs from user intent.
- Create `rightmemory/shared_view_builder.py`: orchestration helpers that run the builder role and verify the expected artifacts were written.
- Create `rightmemory/shared_view_models.py`: dataclasses and TOML helpers for `SharedViewConnection`, `SharedViewTarget`, file recipes, question configs, credentials, and connection type validation.
- Create `rightmemory/shared_view_files.py`: `MF#` recipe rendering, package creation, package pull, import replacement, stale fallback, and auto-publish helpers.
- Create `rightmemory/shared_view_questions.py`: `MQ#` ask client/server helpers, provider-side config loading, live answer timeout behavior, and unavailable formatting.
- Keep `rightmemory/shared_views.py`: public facade functions used by CLI/Web/tests while old helpers are deleted or moved.
- Modify `rightmemory/hub/app.py`, `rightmemory/hub/client.py`, `rightmemory/hub/packages.py`, and `rightmemory/hub/store.py`: serve full file-view packages for pull, remove hub-side snippet retrieval as the file-view product path, preserve explicit interactions.
- Modify `rightmemory/runtime.py`: run silent `MF#` pull before retrieve model start, and rebuild/publish approved `MF#` views after successful provider memory writes.
- Modify `rightmemory/cli.py`: replace `define|build|export|publish|retrieve` shared-view commands with `build-file|build-question|approve|pull|status|ask|note|notes|inbox|credential|accept-invite`.
- Modify `rightmemory/web/service.py`, `rightmemory/web/app.py`, `rightmemory/web/static/app.js`, and `rightmemory/web/static/styles.css`: convert the guided Shared Views panel into separate file-view and question-view flows.
- Modify `README.md`, `docs/shared-views-usage.md`, and install allowlists in `install.sh`: document the new command and file surfaces.
- Modify tests:
  - `tests/test_tools.py`
  - `tests/test_config.py`
  - `tests/test_shared_views.py`
  - `tests/test_cli.py`
  - `tests/test_http_hub.py`
  - `tests/test_install.py`
  - `tests/test_web_service.py`

## Data Shapes

Use these shapes consistently across tasks.

`shared_views.toml` connection entries:

```toml
[connections.auth-api-files]
type = "file"
ref = "rightmemory://mf/auth-api-files"
relationship = "human"
description = "Auth API file view for frontend login work."

[connections.auth-api-files.target]
kind = "http-file"
base_url = "https://hub.example.test"
view_id = "auth-api-files"
credential_id = "http-auth-api-files"
version_id = "ver_1"
accepted_from_url = "https://hub.example.test/i/invite-token"

[connections.auth-api-ask]
type = "question"
ref = "rightmemory://mq/auth-api-ask"
relationship = "human"
description = "Auth API provider questions for frontend login work."

[connections.auth-api-ask.target]
kind = "http-question"
base_url = "https://provider.example.test"
view_id = "auth-api-ask"
credential_id = "http-auth-api-ask"
accepted_from_url = "https://provider.example.test/i/invite-token"
```

`MF#` recipe:

```toml
version = 1
view_id = "auth-api-files"
kind = "file"
approved = true
intent = "Expose auth API integration context for frontend agents."
render = "expanded-heading-subtrees"

include_headings = ["auth-api"]
include_nodes = ["token-expiry"]
include_files = ["MEMORY_AUTH.md"]
exclude_ids = ["private-payroll"]

[publish]
enabled = true
hub_url = "https://hub.example.test"
credential_id = "alice-publish"
```

`MQ#` config:

```toml
version = 1
view_id = "auth-api-ask"
kind = "question"
approved = true
intent = "Let frontend agents ask temporary auth API questions."
start_timeout_seconds = 10
answer_timeout_seconds = 180
provider_role = "retrieve"
```

`MF#` file package:

```text
view.md
recipe.toml
rightmemory-shared-view.toml
dist/MEMORY.md
dist/manifest.toml
```

`MQ#` provider files:

```text
view.md
retriever.md
question.toml
```

## Task 1: Schema And Validator Split

**Files:**
- Modify: `skills/rightmemory-schema.md`
- Modify: `rightmemory/tools.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Add failing validator tests for `MF#` and `MQ#`**

Append these tests to `MemoryValidationTests` in `tests/test_tools.py`:

```python
    def test_mf_and_mq_headings_are_addressable_ids(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API Files {MF#auth-api-files} -> [rel:project]\n\n"
            "Use this mirrored file view before login changes.\n\n"
            "## Auth API Questions {MQ#auth-api-ask} -> [rel:auth-api-files]\n\n"
            "Use this provider question view for live auth API clarification.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_m_heading_is_rejected(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Legacy View {M#legacy-view}\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation failed", result)
        self.assertIn("unsupported heading marker `M#`", result)

    def test_terminal_reference_allows_file_skill_file_view_and_question_view(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "### Shared Context {#shared-context}\n\n"
            "#### Runtime Details {F#runtime-details}\n\n"
            "#### Review Skill {S#review-skill}\n\n"
            "#### Auth API Files {MF#auth-api-files}\n\n"
            "#### Auth API Questions {MQ#auth-api-ask}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_runtime-details.md").write_text(
            "# Runtime Details {#runtime-details}\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_review-skill.md").write_text(
            "# Review Skill\n\nUse the review workflow.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_terminal_reference_rejects_child_nodes_and_headings(self):
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "### Shared Context {#shared-context}\n\n"
            "#### Auth API Files {MF#auth-api-files}\n\n"
            "- `illegal-child` This node is under a terminal reference. -> [rel:shared-context]\n"
            "##### Too Deep {#too-deep}\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation failed", result)
        self.assertIn("terminal `####` heading cannot contain node lines", result)
        self.assertIn("headings deeper than `####` are not allowed", result)
```

- [ ] **Step 2: Run the focused validator tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryValidationTests.test_mf_and_mq_headings_are_addressable_ids tests.test_tools.MemoryValidationTests.test_m_heading_is_rejected tests.test_tools.MemoryValidationTests.test_terminal_reference_allows_file_skill_file_view_and_question_view tests.test_tools.MemoryValidationTests.test_terminal_reference_rejects_child_nodes_and_headings
```

Expected: FAIL because `MF#` and `MQ#` are not parsed, `M#` is accepted, and `####` terminal validation does not match the new schema.

- [ ] **Step 3: Update heading marker parsing and terminal validation**

In `rightmemory/tools.py`, update marker constants:

```python
ANCHOR_RE = re.compile(
    r"^(#{1,4})\s+.*?\{(?:F#|S#|MF#|MQ#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?"
)
ANCHOR_KIND_RE = re.compile(
    r"^(#{1,})\s+.*?\{(F#|S#|MF#|MQ#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?"
)
UNSUPPORTED_ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{([A-Za-z]+#)([A-Za-z0-9_.-]+)\}")
TERMINAL_HEADING_KINDS = {"F#", "S#", "MF#", "MQ#"}
```

In `_parse_file`, when `ANCHOR_KIND_RE` misses but `UNSUPPORTED_ANCHOR_KIND_RE` matches, emit a `MemoryId`-adjacent structure error. The simplest implementation is to extend `_structure_errors` so unsupported markers are reported while parsing structure:

```python
unsupported = UNSUPPORTED_ANCHOR_KIND_RE.match(line)
if unsupported is not None and ANCHOR_KIND_RE.match(line) is None:
    errors.append(
        f"unsupported heading marker `{unsupported.group(2)}` at {relative_path}:{line_number}"
    )
```

In `_structure_errors`, track the current terminal `####` heading until the next heading of level 1 through 4. For each node line under that terminal heading, emit:

```python
errors.append(
    f"terminal `####` heading cannot contain node lines at {relative_path}:{line_number}"
)
```

For headings deeper than `####`, emit:

```python
errors.append(f"headings deeper than `####` are not allowed at {relative_path}:{line_number}")
```

Keep the existing rule that a `####` heading must appear under a `###` topic.

- [ ] **Step 4: Update schema text**

Replace the shared-view parts of `skills/rightmemory-schema.md` with text equivalent to:

```markdown
### Mirrored File View {MF#heading-id} -> [edge1, edge2, ...]
### Provider Question View {MQ#heading-id} -> [edge1, edge2, ...]
```

and define:

```markdown
- `MF#` marks a mirrored file shared view. The graph id is still `heading-id`; resolver details, HTTP URLs, and credentials live outside memory prose.
- `MQ#` marks a provider question shared view. The graph id is still `heading-id`; provider prompts and transport details live outside memory prose.
- `####` is a terminal reference heading. It may use `{F#slug}`, `{S#slug}`, `{MF#slug}`, or `{MQ#slug}` and must not contain child headings or node lines.
```

Remove the current `M#` schema section and the current instruction that `M#` cannot be used on `####`.

- [ ] **Step 5: Re-run focused validator tests**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryValidationTests.test_mf_and_mq_headings_are_addressable_ids tests.test_tools.MemoryValidationTests.test_m_heading_is_rejected tests.test_tools.MemoryValidationTests.test_terminal_reference_allows_file_skill_file_view_and_question_view tests.test_tools.MemoryValidationTests.test_terminal_reference_rejects_child_nodes_and_headings
```

Expected: PASS.

- [ ] **Step 6: Commit schema split**

Run:

```bash
rtk git add skills/rightmemory-schema.md rightmemory/tools.py tests/test_tools.py
rtk git commit -m "feat: define MF and MQ memory headings"
```

## Task 2: Prompt And Retrieve Tool Cleanup

**Files:**
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/prompts/retrieve.md`
- Modify: `rightmemory/prompts/update.md`
- Modify: `rightmemory/prompts/dreamer.md`
- Modify: `rightmemory/prompts/reviewer.md`
- Modify: `rightmemory/runtime.py`
- Modify: `skills/memory-orchestrator-cli/SKILL.md`
- Modify: `tests/test_config.py`
- Modify: `tests/test_install.py`

- [ ] **Step 1: Add failing prompt/runtime tests**

Replace `test_retrieve_prompt_includes_shared_view_endpoint_guidance`, `test_retrieve_runtime_exposes_shared_view_tool`, and `test_cli_agent_retrieve_prompt_uses_shared_view_cli_command` in `tests/test_config.py` with:

```python
    def test_retrieve_prompt_uses_mf_mq_schema_without_endpoint_tool(self):
        instructions = build_instructions(Path("/memory"), "retrieve")

        self.assertIn("MF#", instructions)
        self.assertIn("MQ#", instructions)
        self.assertIn("provider-question context", instructions)
        self.assertNotIn("M# headings", instructions)
        self.assertNotIn("retrieve_shared_view", instructions)

    def test_retrieve_runtime_does_not_expose_shared_view_tool(self):
        config = RuntimeConfig(role="retrieve", model_id="openai/test")
        runtime = RightMemoryRuntime(config)

        tool_names = [tool.__name__ for tool in runtime._agent_tools()]

        self.assertNotIn("retrieve_shared_view", tool_names)
        self.assertIn("read", tool_names)
        self.assertIn("grep", tool_names)

    def test_cli_agent_retrieve_prompt_mentions_mq_recommendation_without_ask_command(self):
        prompt = build_cli_agent_instructions(Path("/home/example/.rightmemory"), "retrieve")

        self.assertIn("MF#", prompt)
        self.assertIn("MQ#", prompt)
        self.assertIn("provider-question context", prompt)
        self.assertNotIn("rightmemory shared-view retrieve", prompt)
        self.assertNotIn("rightmemory shared-view ask", prompt)
```

In `tests/test_install.py`, extend `test_cli_agent_installs_command_backed_orchestrator_without_role_skills` with:

```python
        self.assertIn("rightmemory shared-view ask <mq-id>", orchestrator)
        self.assertIn("Provider question context", orchestrator)
        self.assertIn("do not forward a question invented by retrieve", orchestrator)
```

- [ ] **Step 2: Run prompt/runtime tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_prompt_uses_mf_mq_schema_without_endpoint_tool tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool tests.test_config.ConfigTests.test_cli_agent_retrieve_prompt_mentions_mq_recommendation_without_ask_command tests.test_install.InstallScriptTests.test_cli_agent_installs_command_backed_orchestrator_without_role_skills
```

Expected: FAIL because prompt text and retrieve tools still expose legacy `M#` shared-view retrieval, and the installed orchestrator does not yet explain the `MQ#` ask handoff.

- [ ] **Step 3: Remove retrieve shared-view tool exposure**

In `rightmemory/runtime.py`, remove this branch from `_agent_tools`:

```python
if self.config.role == "retrieve":
    read_tools.append(self._agent_tool(self.shared_view_tools.retrieve_shared_view))
```

Remove `self.shared_view_tools` from `RightMemoryRuntime` after Task 3 if no remaining runtime code path uses it.

- [ ] **Step 4: Update prompt composer guidance**

In `rightmemory/prompt.py`, change `_cli_agent_guidance("retrieve")` to:

```python
if role == "retrieve":
    return (
        "\nCLI-agent adaptation:\n"
        "- Follow the embedded schema for `MF#` and `MQ#` headings.\n"
        "- For relevant `MF#` headings, use ordinary read/search commands on synced imported files when they are visible in the memory store.\n"
        "- For relevant `MQ#` headings, report provider-question context with the local mq_id and relationship context; do not call provider ask commands from retrieve.\n"
    )
```

In `_tool_guidance("retrieve")`, remove `retrieve_shared_view` wording and state:

```python
"- Retrieve has ordinary read/search tools. It does not call shared-view endpoints directly.\n"
```

- [ ] **Step 5: Update role prompts**

In `rightmemory/prompts/retrieve.md`, replace the `M#` paragraph with:

```markdown
`MF#` headings are mirrored file shared-view connections. When an `MF#` heading is relevant, read the synced imported files with ordinary read/search tools and keep the external provenance clear in the answer.

`MQ#` headings are provider question shared-view connections. When an `MQ#` heading is relevant, report that provider-question context may help, including the local `mq_id` and the local relationship context. Do not invent a suggested question and do not call provider ask commands from retrieve.
```

In `update.md`, `dreamer.md`, and `reviewer.md`, replace `M#` text with the schema-driven `MF#` and `MQ#` boundary reminder:

```markdown
Shared-view relationships use schema-defined `MF#` and `MQ#` headings. Keep heading bodies focused on local meaning and do not absorb provider content unless it became a local decision, task, or consequence.
```

In `skills/memory-orchestrator-cli/SKILL.md`, add retrieval guidance after the existing `Open context questions` bullets:

```markdown
- Retrieval may include `Provider question context` lines for relevant `MQ#` headings. Treat these as optional external ask opportunities, not memory facts.
- If provider-question context would materially help the current task, call `rightmemory shared-view ask <mq-id> "<question>"` yourself after retrieve returns.
- Phrase the question from the actual task context; do not forward a question invented by retrieve.
- If the ask reports unavailable, continue with available local context and tell the user the provider question endpoint is currently unavailable.
```

- [ ] **Step 6: Run prompt/runtime tests**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_prompt_uses_mf_mq_schema_without_endpoint_tool tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool tests.test_config.ConfigTests.test_cli_agent_retrieve_prompt_mentions_mq_recommendation_without_ask_command tests.test_install.InstallScriptTests.test_cli_agent_installs_command_backed_orchestrator_without_role_skills
```

Expected: PASS.

- [ ] **Step 7: Commit prompt cleanup**

Run:

```bash
rtk git add rightmemory/prompt.py rightmemory/prompts/retrieve.md rightmemory/prompts/update.md rightmemory/prompts/dreamer.md rightmemory/prompts/reviewer.md rightmemory/runtime.py skills/memory-orchestrator-cli/SKILL.md tests/test_config.py tests/test_install.py
rtk git commit -m "feat: remove legacy shared view retrieve tool"
```

## Task 3: Shared View Models And Registry

**Files:**
- Create: `rightmemory/shared_view_models.py`
- Modify: `rightmemory/shared_views.py`
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing registry model tests**

Create a new `SharedViewModelTests` class in `tests/test_shared_views.py`:

```python
class SharedViewModelTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_save_and_load_file_and_question_connections(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    description="Auth API file view.",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        view_id="auth-api-files",
                        credential_id="http-auth-api-files",
                    ),
                ),
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    description="Auth API question view.",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://provider.example.test",
                        view_id="auth-api-ask",
                        credential_id="http-auth-api-ask",
                    ),
                ),
            },
        )

        loaded = load_connections(self.root)

        self.assertEqual(loaded["auth-api-files"].view_type, "file")
        self.assertEqual(loaded["auth-api-files"].target.kind, "http-file")
        self.assertEqual(loaded["auth-api-ask"].view_type, "question")
        self.assertEqual(loaded["auth-api-ask"].target.kind, "http-question")

    def test_provider_root_target_is_rejected(self):
        with self.assertRaises(ValueError) as caught:
            save_connections(
                self.root,
                {
                    "legacy": SharedViewConnection(
                        heading_id="legacy",
                        view_type="file",
                        ref="rightmemory://mf/legacy",
                        target=SharedViewTarget(kind="local", path=str(self.root)),
                    )
                },
            )

        self.assertIn("unknown shared view target kind `local`", str(caught.exception))
```

- [ ] **Step 2: Add failing CLI removal tests**

In `tests/test_cli.py`, add:

```python
    def test_shared_view_retrieve_command_is_removed(self):
        with self.assertRaises(SystemExit):
            main(["shared-view", "retrieve", "auth-api-files", "token"])

    def test_shared_view_accept_provider_root_is_removed(self):
        with self.assertRaises(SystemExit):
            main([
                "shared-view",
                "accept",
                "auth-api-files",
                "--title",
                "Auth API Files",
                "--body",
                "Use this for auth.",
                "--ref",
                "rightmemory://mf/auth-api-files",
                "--provider-root",
                str(self.root),
            ])
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewModelTests tests.test_cli.CliTests.test_shared_view_retrieve_command_is_removed tests.test_cli.CliTests.test_shared_view_accept_provider_root_is_removed
```

Expected: FAIL because the new dataclass fields and target kinds do not exist, and legacy CLI commands still exist.

- [ ] **Step 4: Create model module**

Create `rightmemory/shared_view_models.py` with:

```python
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

CONNECTION_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
VIEW_TYPES = {"file", "question"}
TARGET_KINDS = {"none", "http-file", "http-question", "revoked"}
RELATIONSHIPS = {"human", "owned-agent", "team-space", "external"}


@dataclass(frozen=True)
class SharedViewTarget:
    kind: str = "none"
    path: str | None = None
    view_id: str | None = None
    base_url: str | None = None
    credential_id: str | None = None
    version_id: str | None = None
    accepted_from_url: str | None = None


@dataclass(frozen=True)
class SharedViewConnection:
    heading_id: str
    view_type: str
    ref: str
    relationship: str = "human"
    maintainer: str | None = None
    description: str | None = None
    accepted_from: str | None = None
    target: SharedViewTarget = field(default_factory=SharedViewTarget)


def validate_heading_id(value: str) -> str:
    clean = str(value).strip()
    if not CONNECTION_ID_RE.fullmatch(clean):
        raise ValueError(f"shared view id must contain letters, numbers, '.', '_', or '-': {value!r}")
    return clean


def validate_connection(root: Path, key: str, connection: SharedViewConnection) -> SharedViewConnection:
    heading_id = validate_heading_id(connection.heading_id)
    if key != heading_id:
        raise ValueError(f"connection key `{key}` does not match heading id `{heading_id}`")
    if connection.view_type not in VIEW_TYPES:
        raise ValueError(f"unknown shared view type `{connection.view_type}` for {heading_id}")
    if connection.relationship not in RELATIONSHIPS:
        raise ValueError(f"unknown shared view relationship `{connection.relationship}` for {heading_id}")
    target = connection.target
    if target.kind not in TARGET_KINDS:
        raise ValueError(f"unknown shared view target kind `{target.kind}` for {heading_id}")
    if target.path:
        raise ValueError("shared view target paths are no longer supported; use HTTP transport")
    if target.kind == "http-file" and connection.view_type != "file":
        raise ValueError(f"http-file target requires file view type for {heading_id}")
    if target.kind == "http-question" and connection.view_type != "question":
        raise ValueError(f"http-question target requires question view type for {heading_id}")
    if target.kind in {"http-file", "http-question"}:
        if not target.base_url or not target.credential_id:
            raise ValueError(f"{target.kind} target requires base_url and credential_id for {heading_id}")
    return connection
```

- [ ] **Step 5: Wire registry load/save to the model**

Move `SharedViewTarget` and `SharedViewConnection` imports in `rightmemory/shared_views.py` to `shared_view_models.py`. Update `load_connections` and `save_connections` to read/write `type = "file"` or `type = "question"` and reject old targets. Preserve credential load/save helpers for the file-view and question-view tasks in this plan.

- [ ] **Step 6: Remove legacy CLI commands from parser**

In `_shared_view_main` in `rightmemory/cli.py`, remove parsers and branches for:

```text
define
build
export
publish
publish-http
retrieve
accept --provider-root
accept --package
accept --hub
```

Keep `credential`, `note`, `notes`, `inbox`, `inbox-http`, and `accept-invite`; Tasks 7 through 10 replace or refine their behavior.

- [ ] **Step 7: Re-run focused model and CLI tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewModelTests tests.test_cli.CliTests.test_shared_view_retrieve_command_is_removed tests.test_cli.CliTests.test_shared_view_accept_provider_root_is_removed
```

Expected: PASS.

- [ ] **Step 8: Commit model split**

Run:

```bash
rtk git add rightmemory/shared_view_models.py rightmemory/shared_views.py rightmemory/cli.py tests/test_shared_views.py tests/test_cli.py
rtk git commit -m "feat: split shared view connection types"
```

## Task 4: `MF#` Recipe Rendering And Package Publishing

**Files:**
- Create: `rightmemory/shared_view_files.py`
- Modify: `rightmemory/shared_views.py`
- Modify: `rightmemory/hub/packages.py`
- Modify: `rightmemory/hub/store.py`
- Modify: `rightmemory/hub/client.py`
- Modify: `rightmemory/hub/app.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_http_hub.py`

- [ ] **Step 1: Add failing file-view recipe tests**

Add `SharedFileViewRecipeTests` to `tests/test_shared_views.py`:

```python
class SharedFileViewRecipeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n"
            "## Auth API {#auth-api}\n\n"
            "- `token-expiry` Tokens expire after one hour. -> [rel:auth-api]\n"
            "- `private-payroll` Payroll details stay private. -> [rel:auth-api]\n",
            encoding="utf-8",
        )

    def test_file_recipe_renders_selected_context_without_excluded_ids(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            include_nodes=["token-expiry"],
            exclude_ids=["private-payroll"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        result = render_file_view(self.root, "auth-api-files")

        exported = self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md"
        recipe = self.root / "shared_views" / "auth-api-files" / "recipe.toml"
        self.assertIn("rendered file view auth-api-files", result)
        self.assertIn("Tokens expire after one hour.", exported.read_text(encoding="utf-8"))
        self.assertNotIn("Payroll details", exported.read_text(encoding="utf-8"))
        self.assertIn('kind = "file"', recipe.read_text(encoding="utf-8"))

    def test_file_package_does_not_include_retriever_prompt(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        package = self.root / "package"

        export_file_view_package(self.root, "auth-api-files", package)

        self.assertTrue((package / "view.md").exists())
        self.assertTrue((package / "recipe.toml").exists())
        self.assertTrue((package / "dist" / "MEMORY.md").exists())
        self.assertFalse((package / "retriever.md").exists())
```

- [ ] **Step 2: Add failing hub package pull tests**

In `tests/test_http_hub.py`, add:

```python
    def test_connection_can_download_current_file_view_package(self):
        package = self._write_valid_file_view_package("alice-auth-api")
        self.store.store_package_version(
            package,
            view_id="alice-auth-api",
            provider_id="alice",
            created_by_token_id=self.publish_token.token_id,
        )
        invitation = self.store.create_invitation("alice-auth-api", actor_id=self.publish_token.token_id)
        accepted = self.store.accept_invitation(invitation["raw_token"])

        response = self.client.get(
            "/api/views/alice-auth-api/package",
            headers={"Authorization": f"Bearer {accepted['connection_token']}"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"], "application/zip")
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            self.assertIn("dist/MEMORY.md", archive.namelist())
            self.assertIn("recipe.toml", archive.namelist())
            self.assertNotIn("retriever.md", archive.namelist())
```

Add helper `_write_valid_file_view_package` in the same test class:

```python
    def _write_valid_file_view_package(self, view_id: str) -> Path:
        package = self.root / f"{view_id}-package"
        (package / "dist").mkdir(parents=True)
        (package / "view.md").write_text(f"# {view_id}\n", encoding="utf-8")
        (package / "recipe.toml").write_text(
            f'version = 1\nview_id = "{view_id}"\nkind = "file"\napproved = true\n',
            encoding="utf-8",
        )
        (package / "rightmemory-shared-view.toml").write_text(
            f'version = 1\nview_id = "{view_id}"\nkind = "file"\nref = "rightmemory://mf/{view_id}"\ntitle = "{view_id}"\n',
            encoding="utf-8",
        )
        (package / "dist" / "MEMORY.md").write_text("# Published Context\n\nToken facts.\n", encoding="utf-8")
        (package / "dist" / "manifest.toml").write_text(
            f'version = 1\nview_id = "{view_id}"\n',
            encoding="utf-8",
        )
        return package
```

- [ ] **Step 3: Run focused file-view tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests tests.test_http_hub.HttpHubTests.test_connection_can_download_current_file_view_package
```

Expected: FAIL because `shared_view_files.py` and package download do not exist.

- [ ] **Step 4: Implement file recipe dataclasses and renderer**

Create `rightmemory/shared_view_files.py` with:

```python
from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .shared_view_models import validate_heading_id

PROVIDER_VIEWS_DIR = "shared_views"


@dataclass(frozen=True)
class FileViewRecipe:
    view_id: str
    title: str
    intent: str
    include_headings: tuple[str, ...] = ()
    include_nodes: tuple[str, ...] = ()
    include_files: tuple[str, ...] = ()
    exclude_ids: tuple[str, ...] = ()
    approved: bool = False
    publish_hub_url: str | None = None
    publish_credential_id: str | None = None


def write_file_view_recipe(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    include_headings: list[str] | tuple[str, ...] = (),
    include_nodes: list[str] | tuple[str, ...] = (),
    include_files: list[str] | tuple[str, ...] = (),
    exclude_ids: list[str] | tuple[str, ...] = (),
    approved: bool = False,
    publish_hub_url: str | None = None,
    publish_credential_id: str | None = None,
) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    view_dir = root / PROVIDER_VIEWS_DIR / clean_view_id
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_text(view_dir / "view.md", f"# {title.strip()}\n\n{intent.strip()}\n")
    _write_text(
        view_dir / "recipe.toml",
        _render_recipe_toml(
            FileViewRecipe(
                view_id=clean_view_id,
                title=title.strip(),
                intent=intent.strip(),
                include_headings=tuple(include_headings),
                include_nodes=tuple(include_nodes),
                include_files=tuple(include_files),
                exclude_ids=tuple(exclude_ids),
                approved=approved,
                publish_hub_url=publish_hub_url,
                publish_credential_id=publish_credential_id,
            )
        ),
    )
    return f"wrote file view recipe {clean_view_id}"
```

Continue the file with deterministic rendering helpers:

```python
def render_file_view(memory_root: Path, view_id: str) -> str:
    root = Path(memory_root).expanduser()
    recipe = load_file_view_recipe(root, view_id)
    source = _read_memory_sources(root)
    rendered = _render_selected_memory(recipe, source)
    view_dir = root / PROVIDER_VIEWS_DIR / recipe.view_id
    temp = view_dir / f".dist.tmp-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    _write_text(temp / "MEMORY.md", rendered)
    _write_text(temp / "manifest.toml", f'version = 1\nview_id = "{recipe.view_id}"\n')
    final = view_dir / "dist"
    if final.exists():
        shutil.rmtree(final)
    temp.rename(final)
    return f"rendered file view {recipe.view_id}"
```

Implement `_render_selected_memory` by including lines from `MEMORY*.md` whose anchor id or node id is in `include_headings` or `include_nodes`, skipping lines whose id is in `exclude_ids`, and including whole files from `include_files`. This first renderer must be deterministic and conservative; it may render selected lines under `# <title> Shared View` and `## Published Context`.

- [ ] **Step 5: Implement package export without `retriever.md`**

Add `export_file_view_package(memory_root, view_id, target_path)` in `shared_view_files.py`. It must call `render_file_view`, copy `view.md`, `recipe.toml`, `dist/`, and write `rightmemory-shared-view.toml` with `kind = "file"`. It must not copy `retriever.md`.

- [ ] **Step 6: Update hub package validation**

In `rightmemory/hub/packages.py`, change `REQUIRED_PACKAGE_FILES` to:

```python
REQUIRED_PACKAGE_FILES = (
    "view.md",
    "recipe.toml",
    "rightmemory-shared-view.toml",
    "dist/MEMORY.md",
    "dist/manifest.toml",
)
```

Read `recipe.toml` for file packages instead of `export.toml`. Preserve the manifest fields currently needed by `HubPackageManifest`.

- [ ] **Step 7: Add package download client and hub endpoint**

In `rightmemory/hub/client.py`, add:

```python
    def download_package(self, view_id: str) -> bytes:
        return self._request_bytes(
            "GET",
            f"/api/views/{urllib.parse.quote(view_id)}/package",
            bearer=True,
        )
```

Add `_request_bytes` that mirrors `_request` but returns raw bytes.

In `rightmemory/hub/app.py`, add:

```python
    @app.get("/api/views/{view_id}/package")
    def download_package(view_id: str, request: Request) -> Response:
        _require_connection_actor(store, request, view_id)
        current = store.get_current_view_version(view_id)
        if current is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="view not found")
        archive = _zip_directory(Path(current["path"]))
        return Response(content=archive, media_type="application/zip")
```

Use `zipfile.ZipFile` over regular non-symlink files under the stored package directory.

- [ ] **Step 8: Re-run file-view and hub tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests tests.test_http_hub.HttpHubTests.test_connection_can_download_current_file_view_package
```

Expected: PASS.

- [ ] **Step 9: Commit file-view package support**

Run:

```bash
rtk git add rightmemory/shared_view_files.py rightmemory/shared_views.py rightmemory/hub/packages.py rightmemory/hub/store.py rightmemory/hub/client.py rightmemory/hub/app.py tests/test_shared_views.py tests/test_http_hub.py
rtk git commit -m "feat: add MF file view packages"
```

## Task 5: `MF#` Pull Before Retrieve And Tool Access

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/tools.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Add failing pull and stale fallback tests**

Add tests in `tests/test_shared_views.py`:

```python
class SharedFileViewPullTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(
                        kind="http-file",
                        base_url="https://hub.example.test",
                        view_id="auth-api-files",
                        credential_id="http-auth-api-files",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-files",
            kind="http-connection",
            token="connection-token",
            base_url="https://hub.example.test",
            view_id="auth-api-files",
        )

    def test_pull_file_view_replaces_import_atomically(self):
        archive = _zip_bytes(
            {
                "view.md": "# Auth API Files\n",
                "recipe.toml": 'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
                "rightmemory-shared-view.toml": 'version = 1\nview_id = "auth-api-files"\nkind = "file"\n',
                "dist/MEMORY.md": "# Published Context\n\nTokens expire after one hour.\n",
                "dist/manifest.toml": 'version = 1\nview_id = "auth-api-files"\n',
            }
        )

        with patch("rightmemory.shared_view_files.HubClient") as client_type:
            client_type.return_value.download_package.return_value = archive
            result = pull_file_view(self.root, "auth-api-files")

        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files"
        self.assertEqual(result.status, "pulled")
        self.assertIn("Tokens expire", (imported / "dist" / "MEMORY.md").read_text(encoding="utf-8"))

    def test_pull_file_view_falls_back_to_stale_import(self):
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("stale but usable\n", encoding="utf-8")

        with patch("rightmemory.shared_view_files.HubClient") as client_type:
            client_type.return_value.download_package.side_effect = HubClientError("offline")
            result = pull_file_view(self.root, "auth-api-files")

        self.assertEqual(result.status, "stale")
        self.assertIn("stale but usable", (imported / "MEMORY.md").read_text(encoding="utf-8"))
```

Add helper:

```python
def _zip_bytes(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, text in files.items():
            archive.writestr(name, text)
    return buffer.getvalue()
```

- [ ] **Step 2: Add failing retrieve preflight invisibility test**

In `tests/test_config.py`, add:

```python
    def test_retrieve_pulls_mf_views_before_model_without_prompt_pollution(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        config = RuntimeConfig(role="retrieve", model_id="openai/test", memory_root=root)
        runtime = RightMemoryRuntime(config)
        captured: dict[str, str] = {}

        class FakeAgent:
            def run_sync(self, message, **kwargs):
                captured["message"] = message
                return "answer"

        runtime.agent = FakeAgent()
        with patch("rightmemory.runtime.pull_all_file_views", return_value=[]):
            output = runtime.run_session_turn("agent-session", "what do we know?")

        self.assertEqual(output, "answer")
        self.assertEqual(captured["message"], "what do we know?")
```

- [ ] **Step 3: Add failing imported file read tests**

In `tests/test_tools.py`, add:

```python
    def test_retrieve_role_can_read_mf_import_files(self):
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("Tokens expire after one hour.\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="retrieve")

        result = tools.read(".runtime/shared_views/imports/auth-api-files/dist/MEMORY.md")
        grep_result = tools.grep("Tokens expire", ".runtime/shared_views/imports/auth-api-files")

        self.assertIn("Tokens expire after one hour.", result)
        self.assertIn("MEMORY.md", grep_result)

    def test_update_role_cannot_read_mf_import_files(self):
        imported = self.root / ".runtime" / "shared_views" / "imports" / "auth-api-files" / "dist"
        imported.mkdir(parents=True)
        (imported / "MEMORY.md").write_text("External context.\n", encoding="utf-8")
        tools = MemoryTools(self.root, role="update")

        with self.assertRaises(ValueError) as caught:
            tools.read(".runtime/shared_views/imports/auth-api-files/dist/MEMORY.md")

        self.assertIn("runtime shared-view imports are only readable by retrieve", str(caught.exception))
```

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewPullTests tests.test_config.ConfigTests.test_retrieve_pulls_mf_views_before_model_without_prompt_pollution tests.test_tools.MemoryToolsTests.test_retrieve_role_can_read_mf_import_files tests.test_tools.MemoryToolsTests.test_update_role_cannot_read_mf_import_files
```

Expected: FAIL because pull helpers, runtime preflight, and import read permissions are missing.

- [ ] **Step 5: Implement pull helpers**

In `rightmemory/shared_view_files.py`, add:

```python
@dataclass(frozen=True)
class FileViewPullResult:
    heading_id: str
    status: str
    message: str


def pull_file_view(memory_root: Path, heading_id: str) -> FileViewPullResult:
    root = Path(memory_root).expanduser()
    connections = load_connections(root)
    connection = connections.get(validate_heading_id(heading_id))
    if connection is None or connection.view_type != "file":
        return FileViewPullResult(heading_id, "unavailable", "file view connection not found")
    try:
        archive = _download_file_view_archive(root, connection)
        _replace_import_from_zip(root, heading_id, archive)
        return FileViewPullResult(heading_id, "pulled", "file view pulled")
    except (KeyError, ValueError, OSError, HubClientError, zipfile.BadZipFile) as exc:
        if _import_exists(root, heading_id):
            return FileViewPullResult(heading_id, "stale", f"using stale file view import: {exc}")
        return FileViewPullResult(heading_id, "unavailable", f"file view unavailable: {exc}")
```

Add `pull_all_file_views(memory_root) -> list[FileViewPullResult]` that iterates `load_connections(root).values()` and calls `pull_file_view` for `view_type == "file"`.

- [ ] **Step 6: Wire retrieve preflight without prompt changes**

In `rightmemory/runtime.py`, import `pull_all_file_views`. In `run_turn`, `run_session_turn`, `_run_session_model`, and `_run_session_cli_agent`, ensure file-view pull happens before `_prepare_retrieve_message` and before the agent starts. The helper should be:

```python
    def _pull_file_views_for_retrieve(self) -> None:
        if self.config.role != "retrieve":
            return
        pull_all_file_views(self.config.memory_root)
```

Call it inside the session lock for session turns so two retrieve sessions do not replace imports concurrently. Do not append results to `message`, `_message_history`, debug trace output, or recent-submitted memory.

- [ ] **Step 7: Permit retrieve reads under imports**

In `rightmemory/tools.py`, allow `read`, `grep`, `glob`, and `read_command` to access `.runtime/shared_views/imports/**` only when `self.role == "retrieve"`. For other roles, raise:

```python
ValueError("runtime shared-view imports are only readable by retrieve")
```

Remove the legacy error that says read commands must use `retrieve_shared_view`.

- [ ] **Step 8: Run focused tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewPullTests tests.test_config.ConfigTests.test_retrieve_pulls_mf_views_before_model_without_prompt_pollution tests.test_tools.MemoryToolsTests.test_retrieve_role_can_read_mf_import_files tests.test_tools.MemoryToolsTests.test_update_role_cannot_read_mf_import_files
```

Expected: PASS.

- [ ] **Step 9: Commit retrieve pull**

Run:

```bash
rtk git add rightmemory/shared_view_files.py rightmemory/runtime.py rightmemory/tools.py tests/test_shared_views.py tests/test_config.py tests/test_tools.py
rtk git commit -m "feat: pull MF views before retrieve"
```

## Task 6: Auto Rebuild And Publish Approved `MF#` Views After Writes

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Modify: `rightmemory/runtime.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing publish helper test**

In `tests/test_shared_views.py`, add:

```python
class SharedFileViewAutoPublishTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text(
            "# Project {#project}\n\n## Auth API {#auth-api}\n\n- `token-expiry` Tokens expire. -> [rel:auth-api]\n",
            encoding="utf-8",
        )
        save_shared_view_credential(
            self.root,
            "alice-publish",
            kind="http-publish",
            token="publish-token",
            base_url="https://hub.example.test",
            provider_id="alice",
        )
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

    def test_publish_approved_file_views_renders_and_uploads(self):
        clients = []

        with patch("rightmemory.shared_view_files.HubClient", side_effect=lambda base_url, token: _record_fake_client(clients, base_url, token)):
            results = publish_approved_file_views(self.root)

        self.assertEqual(results[0].status, "published")
        self.assertEqual(clients[0].base_url, "https://hub.example.test")
        self.assertIn("dist/MEMORY.md", clients[0].publish_calls[0]["files"])
```

Reuse or add `_FakeHubClient` in this test file with `publish_package` and `create_invitation` methods.

- [ ] **Step 2: Add failing runtime hook test**

In `tests/test_config.py`, add:

```python
    def test_update_turn_publishes_approved_file_views_after_success(self):
        root = Path(self.tempdir.name)
        (root / "MEMORY.md").write_text("# Project {#project}\n", encoding="utf-8")
        config = RuntimeConfig(role="update", model_id="openai/test", memory_root=root)
        runtime = RightMemoryRuntime(config)

        class FakeAgent:
            def run_sync(self, message, **kwargs):
                return "updated"

        runtime.agent = FakeAgent()
        with patch("rightmemory.runtime.publish_approved_file_views") as publish:
            runtime.run_session_turn("agent-session", "remember this")

        publish.assert_called_once_with(root)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewAutoPublishTests tests.test_config.ConfigTests.test_update_turn_publishes_approved_file_views_after_success
```

Expected: FAIL because auto publish helper and runtime hook are missing.

- [ ] **Step 4: Implement publish helper**

In `rightmemory/shared_view_files.py`, add:

```python
@dataclass(frozen=True)
class FileViewPublishResult:
    view_id: str
    status: str
    message: str


def publish_approved_file_views(memory_root: Path) -> list[FileViewPublishResult]:
    root = Path(memory_root).expanduser()
    results: list[FileViewPublishResult] = []
    for recipe in load_all_file_view_recipes(root):
        if not recipe.approved:
            continue
        if not recipe.publish_hub_url or not recipe.publish_credential_id:
            results.append(FileViewPublishResult(recipe.view_id, "skipped", "approved recipe has no publish target"))
            continue
        try:
            render_file_view(root, recipe.view_id)
            with TemporaryDirectory() as tempdir:
                package = Path(tempdir) / recipe.view_id
                export_file_view_package(root, recipe.view_id, package)
                credential = load_shared_view_credential(root, recipe.publish_credential_id)
                client = HubClient(recipe.publish_hub_url, credential["token"])
                client.publish_package(recipe.view_id, package)
            results.append(FileViewPublishResult(recipe.view_id, "published", "file view published"))
        except (KeyError, ValueError, OSError, HubClientError) as exc:
            results.append(FileViewPublishResult(recipe.view_id, "failed", str(exc)))
    return results
```

This helper records failures in return values and does not raise unless loading the memory root itself fails.

- [ ] **Step 5: Hook runtime after successful write turns**

In `rightmemory/runtime.py`, import `publish_approved_file_views` and call it after successful turns for `AUTOMATIC_WRITE_ROLES`. Put the call after model execution has succeeded and before returning to the caller. Do not run it for `retrieve`, `historian`, or `sync-reconciler`.

Use:

```python
    def _publish_file_views_after_write(self) -> None:
        if self.config.role not in AUTOMATIC_WRITE_ROLES:
            return
        publish_approved_file_views(self.config.memory_root)
```

Call this only after a successful result. Do not append publish status to model prompts or session history.

- [ ] **Step 6: Run focused tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewAutoPublishTests tests.test_config.ConfigTests.test_update_turn_publishes_approved_file_views_after_success
```

Expected: PASS.

- [ ] **Step 7: Commit auto publish**

Run:

```bash
rtk git add rightmemory/shared_view_files.py rightmemory/runtime.py tests/test_shared_views.py tests/test_config.py
rtk git commit -m "feat: publish approved MF views after writes"
```

## Task 7: `MQ#` Question Config, Provider Ask Endpoint, And CLI

**Files:**
- Create: `rightmemory/shared_view_questions.py`
- Modify: `rightmemory/shared_views.py`
- Modify: `rightmemory/hub/client.py`
- Modify: `rightmemory/web/app.py`
- Modify: `rightmemory/web/service.py`
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_cli.py`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Add failing question config and ask tests**

Add `SharedQuestionViewTests` to `tests/test_shared_views.py`:

```python
class SharedQuestionViewTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_write_question_view_config(self):
        result = write_question_view(
            self.root,
            view_id="auth-api-ask",
            title="Auth API Questions",
            intent="Let frontend agents ask auth API questions.",
            retriever_instructions="Answer only from auth API memory.",
            approved=True,
        )

        view_dir = self.root / "shared_views" / "auth-api-ask"
        self.assertIn("wrote question view auth-api-ask", result)
        self.assertTrue((view_dir / "view.md").exists())
        self.assertTrue((view_dir / "retriever.md").exists())
        self.assertIn('kind = "question"', (view_dir / "question.toml").read_text(encoding="utf-8"))

    def test_ask_question_view_returns_unavailable_when_provider_does_not_start(self):
        save_connections(
            self.root,
            {
                "auth-api-ask": SharedViewConnection(
                    heading_id="auth-api-ask",
                    view_type="question",
                    ref="rightmemory://mq/auth-api-ask",
                    target=SharedViewTarget(
                        kind="http-question",
                        base_url="https://provider.example.test",
                        view_id="auth-api-ask",
                        credential_id="http-auth-api-ask",
                    ),
                )
            },
        )
        save_shared_view_credential(
            self.root,
            "http-auth-api-ask",
            kind="http-connection",
            token="connection-token",
            base_url="https://provider.example.test",
            view_id="auth-api-ask",
        )

        with patch("rightmemory.shared_view_questions.HubClient") as client_type:
            client_type.return_value.ask_question.side_effect = HubClientError("provider did not start")
            result = ask_question_view(self.root, "auth-api-ask", "How do tokens refresh?")

        self.assertIn("Status: unavailable", result)
        self.assertIn("provider did not start", result)
```

- [ ] **Step 2: Add failing CLI ask test**

In `tests/test_cli.py`, add:

```python
    def test_shared_view_ask_cli_dispatches_question_view(self):
        calls = []

        def fake_ask(memory_root, heading_id, question):
            calls.append((memory_root, heading_id, question))
            return "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"

        with patch("rightmemory.cli.ask_question_view", side_effect=fake_ask):
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = main(["shared-view", "ask", "auth-api-ask", "How", "do", "tokens", "refresh?"])

        self.assertEqual(result, 0)
        self.assertEqual(calls[0][1:], ("auth-api-ask", "How do tokens refresh?"))
        self.assertIn("Status: answered", stdout.getvalue())
```

- [ ] **Step 3: Add failing provider ask endpoint test**

In `tests/test_web_service.py`, add:

```python
    def test_provider_question_endpoint_uses_service(self):
        calls = []

        def fake_answer(view_id, payload):
            calls.append((view_id, payload["question"]))
            return "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"

        with patch.object(RightMemoryWebService, "answer_question_view", side_effect=fake_answer):
            response = self.client.post(
                "/api/share/questions/auth-api-ask/ask",
                json={"question": "How do tokens refresh?"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(calls, [("auth-api-ask", "How do tokens refresh?")])
        self.assertIn("Status: answered", response.json()["data"]["text"])
```

- [ ] **Step 4: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedQuestionViewTests tests.test_cli.CliTests.test_shared_view_ask_cli_dispatches_question_view tests.test_web_service.RightMemoryWebServiceTests.test_provider_question_endpoint_uses_service
```

Expected: FAIL because question helpers, CLI ask, and Web provider endpoint do not exist.

- [ ] **Step 5: Implement question helpers**

Create `rightmemory/shared_view_questions.py` with:

```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .hub.client import HubClient, HubClientError
from .shared_view_models import SharedViewConnection, validate_heading_id
from .shared_views import load_connections, load_shared_view_credential

DEFAULT_START_TIMEOUT_SECONDS = 10
DEFAULT_ANSWER_TIMEOUT_SECONDS = 180


def write_question_view(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    retriever_instructions: str,
    approved: bool = False,
    start_timeout_seconds: int = DEFAULT_START_TIMEOUT_SECONDS,
    answer_timeout_seconds: int = DEFAULT_ANSWER_TIMEOUT_SECONDS,
) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    view_dir = root / "shared_views" / clean_view_id
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_text(view_dir / "view.md", f"# {title.strip()}\n\n{intent.strip()}\n")
    _write_text(view_dir / "retriever.md", retriever_instructions.strip() + "\n")
    _write_text(
        view_dir / "question.toml",
        "\n".join(
            [
                "version = 1",
                f'view_id = "{clean_view_id}"',
                'kind = "question"',
                f"approved = {str(bool(approved)).lower()}",
                f"start_timeout_seconds = {int(start_timeout_seconds)}",
                f"answer_timeout_seconds = {int(answer_timeout_seconds)}",
                'provider_role = "retrieve"',
                "",
            ]
        ),
    )
    return f"wrote question view {clean_view_id}"
```

Add `ask_question_view(memory_root, heading_id, question) -> str` that loads a `question` connection, loads its credential, creates `HubClient(base_url, token, timeout=190)`, calls `ask_question(view_id, question)`, and formats:

```text
Shared question: <heading_id>
Status: answered
Answer:
<answer>
```

Unavailable results must be:

```text
Shared question: <heading_id>
Status: unavailable
Reason: <reason>
```

- [ ] **Step 6: Add client ask method**

In `rightmemory/hub/client.py`, add:

```python
    def ask_question(self, view_id: str, question: str) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/api/share/questions/{urllib.parse.quote(view_id)}/ask",
            json_body={"question": question},
            bearer=True,
        )
```

The path matches the provider Web API. If hub routing is introduced after this plan, keep the client method and make the configured endpoint choose the route.

- [ ] **Step 7: Add CLI ask command**

In `_shared_view_main`, add:

```python
ask = subparsers.add_parser("ask")
ask.add_argument("heading_id")
ask.add_argument("question", nargs=argparse.REMAINDER)
```

and branch:

```python
if args.command == "ask":
    question = " ".join(args.question).strip()
    if not question:
        raise ValueError("shared-view ask requires a question")
    print(ask_question_view(memory_root, args.heading_id, question), end="")
    return 0
```

- [ ] **Step 8: Add provider Web endpoint shell**

In `rightmemory/web/app.py`, add `POST /api/share/questions/{view_id}/ask` protected by CSRF. In `rightmemory/web/service.py`, add:

```python
def answer_question_view(self, view_id: str, payload: dict[str, Any]) -> str:
    return answer_question_view(self.memory_root, view_id, _required_payload_str(payload, "question"))
```

The implementation of provider-side model answering can initially call a helper in `shared_view_questions.py` that validates `question.toml`, checks `approved = true`, reads `retriever.md`, and runs the configured retrieve runtime with a composed provider-side question message. It must use a lock acquire timeout of ten seconds and a model timeout of 180 seconds.

- [ ] **Step 9: Run focused question tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedQuestionViewTests tests.test_cli.CliTests.test_shared_view_ask_cli_dispatches_question_view tests.test_web_service.RightMemoryWebServiceTests.test_provider_question_endpoint_uses_service
```

Expected: PASS.

- [ ] **Step 10: Commit question view support**

Run:

```bash
rtk git add rightmemory/shared_view_questions.py rightmemory/shared_views.py rightmemory/hub/client.py rightmemory/web/app.py rightmemory/web/service.py rightmemory/cli.py tests/test_shared_views.py tests/test_cli.py tests/test_web_service.py
rtk git commit -m "feat: add MQ question views"
```

## Task 8: Builder Commands And Approval Flow

**Files:**
- Create: `rightmemory/shared_view_builder.py`
- Create: `rightmemory/prompts/shared-view-builder.md`
- Modify: `rightmemory/config.py`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/cli.py`
- Modify: `rightmemory/shared_view_files.py`
- Modify: `rightmemory/shared_view_questions.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing builder role tests**

In `tests/test_config.py`, add:

```python
    def test_shared_view_builder_role_loads_prompt(self):
        instructions = build_instructions(Path("/memory"), "shared-view-builder")

        self.assertIn("shared-view builder", instructions)
        self.assertIn("recipe.toml", instructions)
        self.assertIn("question.toml", instructions)
        self.assertIn("Do not edit provider private memory", instructions)
```

- [ ] **Step 2: Add failing CLI builder tests**

In `tests/test_cli.py`, add:

```python
    def test_build_file_cli_runs_builder_agent(self):
        with patch("rightmemory.cli.run_file_view_builder", return_value="built file view auth-api-files") as builder:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = main([
                    "shared-view",
                    "build-file",
                    "auth-api-files",
                    "Expose",
                    "auth",
                    "API",
                    "context",
                    "--title",
                    "Auth API Files",
                    "--hub-url",
                    "https://hub.example.test",
                    "--credential-id",
                    "alice-publish",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(builder.call_args.kwargs["intent"], "Expose auth API context")
        self.assertEqual(builder.call_args.kwargs["hub_url"], "https://hub.example.test")
        self.assertEqual(builder.call_args.kwargs["credential_id"], "alice-publish")
        self.assertIn("built file view", stdout.getvalue())

    def test_build_question_cli_runs_builder_agent(self):
        with patch("rightmemory.cli.run_question_view_builder", return_value="built question view auth-api-ask") as builder:
            with patch("sys.stdout", new_callable=io.StringIO) as stdout:
                result = main([
                    "shared-view",
                    "build-question",
                    "auth-api-ask",
                    "Let",
                    "frontend",
                    "agents",
                    "ask",
                    "auth",
                    "questions",
                    "--title",
                    "Auth API Questions",
                ])

        self.assertEqual(result, 0)
        self.assertEqual(builder.call_args.kwargs["intent"], "Let frontend agents ask auth questions")
        self.assertIn("built question view", stdout.getvalue())
```

- [ ] **Step 3: Add failing approval test**

In `tests/test_shared_views.py`, add:

```python
    def test_approve_file_view_sets_approved_true(self):
        write_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth context.",
            include_headings=["auth-api"],
            approved=False,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        result = approve_file_view(self.root, "auth-api-files")

        self.assertIn("approved file view auth-api-files", result)
        recipe = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        self.assertIn("approved = true", recipe)
```

- [ ] **Step 4: Run focused builder tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_shared_view_builder_role_loads_prompt tests.test_cli.CliTests.test_build_file_cli_runs_builder_agent tests.test_cli.CliTests.test_build_question_cli_runs_builder_agent tests.test_shared_views.SharedFileViewRecipeTests.test_approve_file_view_sets_approved_true
```

Expected: FAIL because the builder role, builder CLI, and approval helpers are missing.

- [ ] **Step 5: Add the builder role**

In `rightmemory/config.py`, add `"shared-view-builder"` to `ROLES` and `MODEL_FALLBACK_ROLES` so it inherits writer model configuration when it has no dedicated executor.

In `rightmemory/prompt.py`, add `"shared-view-builder"` to `ROLE_PROMPTS`.

Create `rightmemory/prompts/shared-view-builder.md`:

```markdown
You are the RightMemory shared-view builder.

Build only provider-owned shared-view artifacts under `shared_views/<view-id>/`.
Do not edit provider private memory facts in `MEMORY.md` or `MEMORY_*.md`.

For file-view requests, inspect active memory and write:

```text
shared_views/<view-id>/view.md
shared_views/<view-id>/recipe.toml
```

`recipe.toml` must use `kind = "file"`, `approved = false`, the caller intent,
and concrete include/exclude ids chosen from active memory.

For question-view requests, inspect active memory and write:

```text
shared_views/<view-id>/view.md
shared_views/<view-id>/retriever.md
shared_views/<view-id>/question.toml
```

`question.toml` must use `kind = "question"`, `approved = false`,
`start_timeout_seconds = 10`, and `answer_timeout_seconds = 180`.

Return a concise summary of the artifacts written and the ids selected.
```

Update write-path validation in `rightmemory/tools.py` so `shared-view-builder`
can create and edit:

```text
shared_views/*/view.md
shared_views/*/recipe.toml
shared_views/*/question.toml
shared_views/*/retriever.md
shared_views/*/.gitignore
```

- [ ] **Step 6: Create builder orchestration helpers**

Create `rightmemory/shared_view_builder.py` with:

```python
from __future__ import annotations

from pathlib import Path

from .config import load_config
from .runtime import RightMemoryRuntime
from .shared_view_models import validate_heading_id


def run_file_view_builder(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    hub_url: str,
    credential_id: str,
) -> str:
    clean_view_id = validate_heading_id(view_id)
    message = "\n".join(
        [
            "<shared_view_build>",
            "kind: file",
            f"view_id: {clean_view_id}",
            f"title: {title.strip()}",
            f"intent: {intent.strip()}",
            f"publish_hub_url: {hub_url.strip()}",
            f"publish_credential_id: {credential_id.strip()}",
            "</shared_view_build>",
        ]
    )
    output = _run_builder(memory_root, clean_view_id, message)
    _require_artifact(memory_root, clean_view_id, "view.md")
    _require_artifact(memory_root, clean_view_id, "recipe.toml")
    return output


def run_question_view_builder(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
) -> str:
    clean_view_id = validate_heading_id(view_id)
    message = "\n".join(
        [
            "<shared_view_build>",
            "kind: question",
            f"view_id: {clean_view_id}",
            f"title: {title.strip()}",
            f"intent: {intent.strip()}",
            "</shared_view_build>",
        ]
    )
    output = _run_builder(memory_root, clean_view_id, message)
    _require_artifact(memory_root, clean_view_id, "view.md")
    _require_artifact(memory_root, clean_view_id, "retriever.md")
    _require_artifact(memory_root, clean_view_id, "question.toml")
    return output


def _run_builder(memory_root: Path, view_id: str, message: str) -> str:
    config = load_config("shared-view-builder", memory_root=Path(memory_root))
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(f"shared-view-builder-{view_id}", message)
    finally:
        runtime.cleanup()


def _require_artifact(memory_root: Path, view_id: str, relative: str) -> None:
    path = Path(memory_root).expanduser() / "shared_views" / view_id / relative
    if not path.is_file():
        raise RuntimeError(f"shared-view builder did not create required artifact: {path.relative_to(Path(memory_root).expanduser())}")
```

- [ ] **Step 7: Add CLI builder commands**

In `_shared_view_main`, add:

```python
build_file = subparsers.add_parser("build-file")
build_file.add_argument("view_id")
build_file.add_argument("intent", nargs=argparse.REMAINDER)
build_file.add_argument("--title", required=True)
build_file.add_argument("--hub-url", required=True)
build_file.add_argument("--credential-id", required=True)

build_question = subparsers.add_parser("build-question")
build_question.add_argument("view_id")
build_question.add_argument("intent", nargs=argparse.REMAINDER)
build_question.add_argument("--title", required=True)

approve = subparsers.add_parser("approve")
approve.add_argument("view_id")
approve.add_argument("--type", choices=("file", "question"), required=True)
```

For `build-file`, call `run_file_view_builder`. For `build-question`, call `run_question_view_builder`. For `approve --type file`, call `approve_file_view`; for `approve --type question`, call `approve_question_view`.

- [ ] **Step 8: Implement approval helpers**

Add `approve_file_view(memory_root, view_id)` and `approve_question_view(memory_root, view_id)` that load the TOML file, rewrite it with `approved = true`, preserve the existing intent, include/exclude lists, timeouts, and publish target, and return:

```text
approved file view <view_id>
approved question view <view_id>
```

- [ ] **Step 9: Run focused builder tests**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_shared_view_builder_role_loads_prompt tests.test_cli.CliTests.test_build_file_cli_runs_builder_agent tests.test_cli.CliTests.test_build_question_cli_runs_builder_agent tests.test_shared_views.SharedFileViewRecipeTests.test_approve_file_view_sets_approved_true
```

Expected: PASS.

- [ ] **Step 10: Commit builder commands**

Run:

```bash
rtk git add rightmemory/config.py rightmemory/prompt.py rightmemory/prompts/shared-view-builder.md rightmemory/shared_view_builder.py rightmemory/cli.py rightmemory/shared_view_files.py rightmemory/shared_view_questions.py tests/test_config.py tests/test_cli.py tests/test_shared_views.py
rtk git commit -m "feat: add shared view builder commands"
```

## Task 9: Interactions Become Explicit HTTP Notes Only

**Files:**
- Modify: `rightmemory/shared_views.py`
- Modify: `rightmemory/shared_view_models.py`
- Modify: `tests/test_shared_views.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Add failing interaction tests**

In `tests/test_shared_views.py`, add:

```python
class SharedViewInteractionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_note_requires_http_target(self):
        save_connections(
            self.root,
            {
                "auth-api-files": SharedViewConnection(
                    heading_id="auth-api-files",
                    view_type="file",
                    ref="rightmemory://mf/auth-api-files",
                    target=SharedViewTarget(kind="none"),
                )
            },
        )

        result = record_shared_view_note(self.root, "auth-api-files", "Docs are stale.", confirmed=True)

        self.assertEqual(result, "shared view auth-api-files does not have an HTTP interaction target")

    def test_note_posts_to_http_for_file_and_question_views(self):
        for view_type, target_kind, heading_id, ref in (
            ("file", "http-file", "auth-api-files", "rightmemory://mf/auth-api-files"),
            ("question", "http-question", "auth-api-ask", "rightmemory://mq/auth-api-ask"),
        ):
            save_connections(
                self.root,
                {
                    heading_id: SharedViewConnection(
                        heading_id=heading_id,
                        view_type=view_type,
                        ref=ref,
                        target=SharedViewTarget(
                            kind=target_kind,
                            base_url="https://hub.example.test",
                            view_id=heading_id,
                            credential_id=f"http-{heading_id}",
                        ),
                    )
                },
            )
            save_shared_view_credential(
                self.root,
                f"http-{heading_id}",
                kind="http-connection",
                token="connection-token",
                base_url="https://hub.example.test",
                view_id=heading_id,
            )

            with patch("rightmemory.shared_views.HubClient") as client_type:
                client_type.return_value.post_interaction.return_value = {"status": "recorded"}
                result = record_shared_view_note(self.root, heading_id, "Docs are stale.", confirmed=True)

            self.assertIn("recorded shared view note", result)
```

- [ ] **Step 2: Run focused interaction tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewInteractionTests
```

Expected: FAIL because current notes queue for packages/local targets and target kinds are legacy.

- [ ] **Step 3: Simplify interaction delivery**

In `record_shared_view_note`, keep confirmation behavior for `human` and `external`, but change delivery:

- Accept only `http-file` and `http-question` targets.
- Use `HubClient.post_interaction`.
- If HTTP post fails, return `queued shared view note` only if the note was appended locally for audit; do not imply provider delivery.
- For non-HTTP target, return exactly:

```text
shared view <heading_id> does not have an HTTP interaction target
```

Do not auto-create notes from failed `MQ#` asks.

- [ ] **Step 4: Run focused interaction tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedViewInteractionTests
```

Expected: PASS.

- [ ] **Step 5: Commit interaction cleanup**

Run:

```bash
rtk git add rightmemory/shared_views.py rightmemory/shared_view_models.py tests/test_shared_views.py tests/test_cli.py
rtk git commit -m "feat: make shared view notes HTTP explicit"
```

## Task 10: Web Studio Guided Flow Replacement

**Files:**
- Modify: `rightmemory/web/service.py`
- Modify: `rightmemory/web/app.py`
- Modify: `rightmemory/web/static/app.js`
- Modify: `rightmemory/web/static/styles.css`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Add failing Web service tests**

In `tests/test_web_service.py`, import `FileViewPullResult` from `rightmemory.shared_view_files`.

In `tests/test_web_service.py`, replace `test_provider_define_build_export_and_publish` with:

```python
    def test_provider_build_file_question_and_approve(self):
        with patch("rightmemory.web.service.run_file_view_builder", return_value="built file view auth-api-files") as build_file:
            build_file_response = self.client.post(
                "/api/share/views/build-file",
                json={
                    "view_id": "auth-api-files",
                    "intent": "Expose auth API integration context.",
                    "title": "Auth API Files",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                },
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.run_question_view_builder", return_value="built question view auth-api-ask") as build_question:
            build_question_response = self.client.post(
                "/api/share/views/build-question",
                json={
                    "view_id": "auth-api-ask",
                    "intent": "Let frontend agents ask auth API questions.",
                    "title": "Auth API Questions",
                },
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.approve_file_view", return_value="approved file view auth-api-files") as approve:
            approve_response = self.client.post(
                "/api/share/views/auth-api-files/approve",
                json={"type": "file"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(build_file_response.status_code, 200)
        self.assertEqual(build_file.call_args.kwargs["intent"], "Expose auth API integration context.")
        self.assertEqual(build_question_response.status_code, 200)
        self.assertEqual(build_question.call_args.kwargs["view_id"], "auth-api-ask")
        self.assertEqual(approve_response.status_code, 200)
        self.assertEqual(approve.call_args.args[1], "auth-api-files")
```

Replace `test_accept_retrieve_note_and_notes` with:

```python
    def test_consumer_file_view_pull_and_question_ask(self):
        with patch("rightmemory.web.service.pull_file_view") as pull:
            pull.return_value = FileViewPullResult("auth-api-files", "pulled", "file view pulled")
            pull_response = self.client.post(
                "/api/use/connections/auth-api-files/pull",
                headers={"x-csrf-token": self.csrf},
            )

        with patch("rightmemory.web.service.ask_question_view") as ask:
            ask.return_value = "Shared question: auth-api-ask\nStatus: answered\nAnswer: Use token_expires_at.\n"
            ask_response = self.client.post(
                "/api/use/connections/auth-api-ask/ask",
                json={"question": "How do tokens refresh?"},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(pull_response.status_code, 200)
        self.assertIn("pulled", pull_response.json()["message"])
        self.assertEqual(ask_response.status_code, 200)
        self.assertIn("Status: answered", ask_response.json()["data"]["text"])
```

Add:

```python
    def test_legacy_web_shared_view_endpoints_are_removed(self):
        define_response = self.client.post(
            "/api/share/views",
            json={"view_id": "legacy"},
            headers={"x-csrf-token": self.csrf},
        )
        build_response = self.client.post(
            "/api/share/views/legacy/build",
            json={"query": "tokens"},
            headers={"x-csrf-token": self.csrf},
        )
        export_response = self.client.post(
            "/api/share/views/legacy/export",
            json={"hub": "/tmp/hub"},
            headers={"x-csrf-token": self.csrf},
        )
        publish_response = self.client.post(
            "/api/share/views/legacy/publish",
            json={"kind": "http"},
            headers={"x-csrf-token": self.csrf},
        )
        retrieve_response = self.client.post(
            "/api/use/connections/auth-api-files/retrieve",
            json={"query": "tokens"},
            headers={"x-csrf-token": self.csrf},
        )

        self.assertEqual(define_response.status_code, 404)
        self.assertEqual(build_response.status_code, 404)
        self.assertEqual(export_response.status_code, 404)
        self.assertEqual(publish_response.status_code, 404)
        self.assertEqual(retrieve_response.status_code, 404)
```

- [ ] **Step 2: Run focused Web tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_web_service.RightMemoryWebServiceTests.test_provider_build_file_question_and_approve tests.test_web_service.RightMemoryWebServiceTests.test_consumer_file_view_pull_and_question_ask tests.test_web_service.RightMemoryWebServiceTests.test_legacy_web_shared_view_endpoints_are_removed
```

Expected: FAIL because Web API still exposes legacy define/build/export/publish/retrieve paths and lacks the new file-build, question-build, approve, pull, and ask endpoints.

- [ ] **Step 3: Update Web service methods**

In `rightmemory/web/service.py`, remove `define_view`, `build_view`, `export_view`, `publish_view`, and `retrieve_connection`.

Add provider builder methods:

```python
def build_file_view(self, payload: dict[str, Any]) -> str:
    return run_file_view_builder(
        self.memory_root,
        view_id=_required_payload_str(payload, "view_id"),
        title=_required_payload_str(payload, "title"),
        intent=_required_payload_str(payload, "intent"),
        hub_url=_required_payload_str(payload, "hub_url"),
        credential_id=_required_payload_str(payload, "credential_id"),
    )

def build_question_view(self, payload: dict[str, Any]) -> str:
    return run_question_view_builder(
        self.memory_root,
        view_id=_required_payload_str(payload, "view_id"),
        title=_required_payload_str(payload, "title"),
        intent=_required_payload_str(payload, "intent"),
    )

def approve_view(self, view_id: str, payload: dict[str, Any]) -> str:
    view_type = _required_payload_str(payload, "type")
    if view_type == "file":
        return approve_file_view(self.memory_root, view_id)
    if view_type == "question":
        return approve_question_view(self.memory_root, view_id)
    raise ValueError("shared view approve type must be file or question")
```

Add consumer methods:

```python
def pull_connection(self, heading_id: str) -> str:
    result = pull_file_view(self.memory_root, heading_id)
    return result.message

def ask_connection(self, heading_id: str, payload: dict[str, Any]) -> str:
    return ask_question_view(self.memory_root, heading_id, _required_payload_str(payload, "question"))
```

Keep `note_connection`, `notes`, and `activity`.

- [ ] **Step 4: Update Web app routes**

Remove these routes:

```text
POST /api/share/views
POST /api/share/views/{view_id}/build
POST /api/share/views/{view_id}/export
POST /api/share/views/{view_id}/publish
POST /api/use/connections/{heading_id}/retrieve
```

Add:

```python
@app.post("/api/share/views/build-file")
def build_file_view(...):
    message = service.build_file_view(payload)
    return ok_response(message)

@app.post("/api/share/views/build-question")
def build_question_view(...):
    message = service.build_question_view(payload)
    return ok_response(message)

@app.post("/api/share/views/{view_id}/approve")
def approve_view(...):
    message = service.approve_view(view_id, payload)
    return ok_response(message)

@app.post("/api/use/connections/{heading_id}/pull")
def pull_connection(...):
    message = service.pull_connection(heading_id)
    return ok_response(message)

@app.post("/api/use/connections/{heading_id}/ask")
def ask_connection(...):
    text = service.ask_connection(heading_id, payload)
    return ok_response("shared view question answered", {"text": text})
```

- [ ] **Step 5: Update Web UI shared panel**

In `rightmemory/web/static/app.js`, change the Shared Views panel:

- Provider step has two forms: “Build File View” and “Build Question View”.
- File-view form posts to `/api/share/views/build-file` with `view_id`, `intent`, `title`, `hub_url`, and `credential_id`.
- Question-view form posts to `/api/share/views/build-question` with `view_id`, `intent`, and `title`.
- Draft provider views expose an `Approve` action that posts `/api/share/views/<view-id>/approve` with `type = "file"` or `type = "question"`.
- Consumer step shows connection type in the select label.
- For file connections, the primary button is `Pull Status`.
- For question connections, the primary button is `Ask`.
- Remove generic “Define”, “Build”, “Export”, “Publish”, “Retrieve”, and the “Ask the view” label attached to retrieve.

Use existing `fetchJson` and `showSharedViewResult` helpers. The consumer submit branch should choose:

```javascript
const path =
  action === "note"
    ? `/api/use/connections/${encodeURIComponent(headingId)}/note`
    : action === "pull"
      ? `/api/use/connections/${encodeURIComponent(headingId)}/pull`
      : `/api/use/connections/${encodeURIComponent(headingId)}/ask`;
```

- [ ] **Step 6: Run focused Web tests**

Run:

```bash
rtk python -m unittest tests.test_web_service.RightMemoryWebServiceTests.test_provider_build_file_question_and_approve tests.test_web_service.RightMemoryWebServiceTests.test_consumer_file_view_pull_and_question_ask tests.test_web_service.RightMemoryWebServiceTests.test_legacy_web_shared_view_endpoints_are_removed
```

Expected: PASS.

- [ ] **Step 7: Commit Web Studio replacement**

Run:

```bash
rtk git add rightmemory/web/service.py rightmemory/web/app.py rightmemory/web/static/app.js rightmemory/web/static/styles.css tests/test_web_service.py
rtk git commit -m "feat: update web shared view flows"
```

## Task 11: Documentation And Install Allowlist

**Files:**
- Modify: `README.md`
- Modify: `docs/shared-views-usage.md`
- Modify: `install.sh`
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing install allowlist test**

In `tests/test_config.py`, update the install/sync-owned path expectation test that currently mentions `export.toml` and `retriever.md` for all views. The expected allowlist should include:

```text
!shared_views/*/view.md
!shared_views/*/recipe.toml
!shared_views/*/question.toml
!shared_views/*/retriever.md
!shared_views/*/.gitignore
```

It should not include `!shared_views/*/export.toml`.

- [ ] **Step 2: Run focused config/docs-adjacent tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_install_gitignore_allows_memory_owned_files
```

Expected: FAIL because install allowlists still include legacy shared-view metadata.

- [ ] **Step 3: Update README and usage guide**

Replace shared-view examples with:

```bash
rightmemory shared-view build-file auth-api-files "Expose auth API integration context for frontend agents" \
  --title "Auth API Files" \
  --hub-url https://hub.example.test \
  --credential-id alice-publish
rightmemory shared-view approve auth-api-files --type file
rightmemory shared-view build-question auth-api-ask "Let frontend agents ask auth API questions" \
  --title "Auth API Questions" \
  --instructions "Answer only auth API questions from provider memory."
rightmemory shared-view approve auth-api-ask --type question
rightmemory shared-view pull auth-api-files
rightmemory shared-view ask auth-api-ask "How do tokens refresh?"
rightmemory shared-view note auth-api-files --confirm "The file view is missing refresh token details."
```

State that `provider-root`, `M#`, generic `shared-view retrieve`, and mounted-folder hub workflows are not part of the new product path.

- [ ] **Step 4: Update install and prompt file sets**

In `install.sh`, replace `shared_views/*/export.toml` with `shared_views/*/recipe.toml` and `shared_views/*/question.toml`. Keep `retriever.md` because `MQ#` provider views use it.

In `rightmemory/prompt.py`, update memory store file list:

```text
- shared_views/<view-id>/view.md, recipe.toml, question.toml, retriever.md
```

- [ ] **Step 5: Run focused docs-adjacent test**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_install_gitignore_allows_memory_owned_files
```

Expected: PASS.

- [ ] **Step 6: Commit docs and install updates**

Run:

```bash
rtk git add README.md docs/shared-views-usage.md install.sh rightmemory/prompt.py tests/test_config.py
rtk git commit -m "docs: document MF and MQ shared views"
```

## Task 12: Final Removal Sweep And Full Verification

**Files:**
- Modify: any files still referencing legacy shared-view behavior.
- Test: full test suite.

- [ ] **Step 1: Search for forbidden legacy product references**

Run:

```bash
rtk rg -n "M#|provider-root|retrieve_shared_view|shared-view retrieve|export.toml|hub-side search|queued MQ|rightmemory shared-view retrieve" README.md docs skills rightmemory tests
```

Expected: Matches only in historical decision/spec/plan files that explicitly discuss removed legacy behavior. No active prompts, runtime code, schema, README command examples, Web UI labels, or tests should require old behavior.

- [ ] **Step 2: Remove or rewrite active legacy references**

For each active match outside historical docs, replace with the new `MF#` / `MQ#` model. Examples:

```text
retrieve_shared_view -> pull_file_view or ask_question_view, depending on type
M# -> MF# or MQ#, depending on file or question relationship
export.toml -> recipe.toml or question.toml
shared-view retrieve -> rightmemory retrieve for MF# file context, or shared-view ask for MQ#
```

- [ ] **Step 3: Run the focused shared-view suite**

Run:

```bash
rtk python -m unittest tests.test_tools tests.test_shared_views tests.test_cli tests.test_http_hub tests.test_web_service tests.test_config
```

Expected: PASS.

- [ ] **Step 4: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: no output and exit code 0.

- [ ] **Step 5: Run full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 6: Commit final cleanup**

Run:

```bash
rtk git status --short
rtk git add README.md docs skills rightmemory tests install.sh
rtk git commit -m "test: verify MF and MQ shared view redesign"
```

If `git status --short` shows no tracked changes after verification, skip the commit and record the verification commands in the final implementation summary.

## Self-Review

Spec coverage:

- Schema split is covered by Task 1.
- Prompt, retrieve tool cleanup, and orchestrator `MQ#` ask handoff are covered by Task 2.
- Registry and removal of direct provider-root target are covered by Task 3.
- `MF#` recipe, package, and no `retriever.md` export are covered by Task 4.
- Silent pre-retrieve pull, stale fallback, and ordinary file-tool access are covered by Task 5.
- Provider write auto rebuild and publish is covered by Task 6.
- `MQ#` synchronous ask, ten-second start window, three-minute answer timeout, and no queue are covered by Task 7.
- Natural-language builder entry points and approval are covered by Task 8.
- One-way explicit HTTP notes are covered by Task 9.
- Web Studio guided flow replacement is covered by Task 10.
- README, usage docs, prompt file sets, and install allowlists are covered by Task 11.
- Legacy removal and full verification are covered by Task 12.

Placeholder scan:

- The plan does not use placeholder markers.
- Each task has concrete files, tests, commands, and expected results.

Type consistency:

- Connection types are `file` and `question`.
- Target kinds are `http-file`, `http-question`, `none`, and `revoked`.
- File-view functions use `write_file_view_recipe`, `render_file_view`, `export_file_view_package`, `pull_file_view`, `pull_all_file_views`, `approve_file_view`, and `publish_approved_file_views`.
- Question-view functions use `write_question_view`, `approve_question_view`, `ask_question_view`, and `answer_question_view`.
- Builder functions use `run_file_view_builder` and `run_question_view_builder` through the internal `shared-view-builder` role.

# Web Studio Share-First Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Web Studio and CLI use a share-first workflow where a user can create or revise a share from natural language, review the builder final message and generated artifacts, approve, publish, join, and use the share without dealing with low-level `MF#` / `MQ#` operations.

**Architecture:** Add a share-level builder path on top of the existing `shared-view-builder` role. The agent owns semantic decisions and calls tools; Python tools write canonical `shares.toml`, `recipe.toml`, and `question.toml`; CLI and Web render the same structured operation results. Keep low-level shared-view commands and Web panels available as advanced tools.

**Tech Stack:** Python 3.11 standard library, existing `RightMemoryRuntime`, existing `MemoryTools`, TOML via current share models, FastAPI Web Studio, vanilla JavaScript/CSS, `unittest`.

---

## Scope Check

This is one feature: a share-first user workflow. It touches builder tooling, CLI formatting, Web service APIs, and Web UI, but all changes serve the same workflow and can be tested through the same share relationship model.

This plan does not implement Hub Console `Shares`, curated shared artifacts, diagnostics, or a full chat UI.

## File Structure

- Create `rightmemory/share_results.py`
  - Structured operation result dataclasses and text formatting used by CLI and Web.
- Create `rightmemory/share_builder.py`
  - Share-level builder runtime entry points for create/revise and validation after the agent runs.
- Modify `rightmemory/tools.py`
  - Add a `create_or_update_share_relationship(...)` compiler tool for the `shared-view-builder` role.
- Modify `rightmemory/prompts/shared-view-builder.md`
  - Teach the builder to handle share-level build/revise messages, choose capability when `Auto`, call compiler tools, and never hand-write TOML.
- Modify `rightmemory/prompt.py`
  - Update role guidance so share-level builder calls are covered by existing `shared-view-builder` behavior.
- Modify `rightmemory/shares.py`
  - Add structured create/revise/list helpers while preserving existing string-returning wrappers.
- Modify `rightmemory/cli.py`
  - Add `share revise`, allow natural-language `share create --request`, keep existing explicit `--file` / `--question` flow, and print structured results.
- Modify `rightmemory/web/service.py`
  - Add share relationship service methods that call `shares.py`.
- Modify `rightmemory/web/app.py`
  - Add share-first API endpoints.
- Modify `rightmemory/web/static/app.js`
  - Replace the main Shared Views page with share-first cards and forms; move existing low-level panels under an advanced disclosure.
- Modify `rightmemory/web/static/styles.css`
  - Add compact share-card and review-panel styles.
- Modify `tests/test_tools.py`
  - Cover the share relationship compiler tool.
- Modify `tests/test_shares.py`
  - Cover structured results, share-level builder create/revise, capability constraints, and backwards-compatible wrappers.
- Modify `tests/test_cli.py`
  - Cover `share create --request`, existing explicit create formatting, and `share revise`.
- Modify `tests/test_web_service.py`
  - Cover new share-first service/API behavior and static shell hooks.
- Modify `docs/shared-views-usage.md`
  - Teach the share-first workflow first and keep low-level shared-view commands as advanced.

## Task 1: Add Share Operation Result Contract

**Files:**
- Create: `rightmemory/share_results.py`
- Modify: `tests/test_shares.py`

- [ ] **Step 1: Write failing result-format tests**

Append these tests to `tests/test_shares.py`:

```python
from rightmemory.share_results import ShareCapabilityStatus, ShareOperationResult, format_share_operation_result


class ShareResultTests(unittest.TestCase):
    def test_format_share_operation_result_includes_builder_summary_and_next_action(self):
        result = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="both",
            builder_final_message="Selected auth-api docs and enabled live questions.",
            statuses=(
                ShareCapabilityStatus(
                    capability="file_context",
                    artifact_id="auth-api-files",
                    status="draft",
                    preview_path="shared_views/auth-api-files/dist/MEMORY.md",
                    message="file context generated",
                ),
                ShareCapabilityStatus(
                    capability="live_questions",
                    artifact_id="auth-api-ask",
                    status="draft",
                    preview_path="shared_views/auth-api-ask/retriever.md",
                    message="question scope generated",
                ),
            ),
            next_action="rightmemory share approve auth-api",
        )

        text = format_share_operation_result(result)

        self.assertIn("auth-api provider draft capability=both", text)
        self.assertIn("Builder summary:", text)
        self.assertIn("Selected auth-api docs", text)
        self.assertIn("file_context auth-api-files draft", text)
        self.assertIn("live_questions auth-api-ask draft", text)
        self.assertIn("Next:", text)
        self.assertIn("rightmemory share approve auth-api", text)

    def test_operation_result_json_omits_empty_fields(self):
        result = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="consumer",
            state="joined",
            capability="file_context",
            statuses=(),
        )

        payload = result.to_json()

        self.assertEqual(payload["share_id"], "auth-api")
        self.assertEqual(payload["capability"], "file_context")
        self.assertNotIn("builder_final_message", payload)
        self.assertNotIn("invitation_url", payload)
```

- [ ] **Step 2: Run failing result tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareResultTests
```

Expected: fail with `ModuleNotFoundError: No module named 'rightmemory.share_results'`.

- [ ] **Step 3: Create `share_results.py`**

Create `rightmemory/share_results.py`:

```python
from __future__ import annotations

from dataclasses import dataclass


SHARE_CAPABILITIES = {"auto", "file_context", "live_questions", "both"}


@dataclass(frozen=True)
class ShareCapabilityStatus:
    capability: str
    artifact_id: str | None = None
    status: str = "unknown"
    preview_path: str | None = None
    message: str | None = None

    def to_json(self) -> dict[str, str]:
        payload = {"capability": self.capability, "status": self.status}
        if self.artifact_id:
            payload["artifact_id"] = self.artifact_id
        if self.preview_path:
            payload["preview_path"] = self.preview_path
        if self.message:
            payload["message"] = self.message
        return payload


@dataclass(frozen=True)
class ShareOperationResult:
    share_id: str
    title: str
    role: str
    state: str
    capability: str
    builder_final_message: str = ""
    statuses: tuple[ShareCapabilityStatus, ...] = ()
    invitation_url: str | None = None
    next_action: str | None = None

    def to_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "share_id": self.share_id,
            "title": self.title,
            "role": self.role,
            "state": self.state,
            "capability": self.capability,
            "statuses": [status.to_json() for status in self.statuses],
        }
        if self.builder_final_message.strip():
            payload["builder_final_message"] = self.builder_final_message.strip()
        if self.invitation_url:
            payload["invitation_url"] = self.invitation_url
        if self.next_action:
            payload["next_action"] = self.next_action
        return payload


def normalize_share_capability(value: str | None) -> str:
    clean = (value or "auto").strip().lower().replace("-", "_")
    if clean in {"file", "context", "file_context"}:
        return "file_context"
    if clean in {"question", "questions", "live_question", "live_questions"}:
        return "live_questions"
    if clean in {"both", "all"}:
        return "both"
    if clean == "auto":
        return "auto"
    raise ValueError("share capability must be one of: auto, file-context, live-questions, both")


def capability_from_parts(parts: tuple[str, ...] | list[str]) -> str:
    normalized = set(parts)
    if normalized == {"file"}:
        return "file_context"
    if normalized == {"question"}:
        return "live_questions"
    if normalized == {"file", "question"}:
        return "both"
    return "auto"


def format_share_operation_result(result: ShareOperationResult) -> str:
    lines = [f"{result.share_id} {result.role} {result.state} capability={result.capability}"]
    if result.builder_final_message.strip():
        lines.extend(["", "Builder summary:", result.builder_final_message.strip()])
    if result.statuses:
        lines.append("")
        lines.append("Status:")
        for status in result.statuses:
            artifact = status.artifact_id or "-"
            line = f"{status.capability} {artifact} {status.status}"
            if status.message:
                line = f"{line}: {status.message}"
            lines.append(line)
    if result.invitation_url:
        lines.extend(["", f"invitation_url\t{result.invitation_url}"])
    if result.next_action:
        lines.extend(["", "Next:", result.next_action])
    return "\n".join(lines).rstrip() + "\n"
```

- [ ] **Step 4: Run result tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareResultTests
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
rtk git add rightmemory/share_results.py tests/test_shares.py
rtk git commit -m "feat: add share operation result contract"
```

## Task 2: Add Share Relationship Compiler Tool

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/prompts/shared-view-builder.md`
- Modify: `rightmemory/prompt.py`
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Write failing tool tests**

Append these tests to the `MemoryToolsSharedViewBuilderTests` class in `tests/test_tools.py`:

```python
    def test_shared_view_builder_tool_creates_share_relationship(self):
        tools = MemoryTools(self.root, role="shared-view-builder")
        tools.create_generative_file_view(
            "auth-api-files",
            "Auth API Files",
            "Share stable auth API context.",
            "## Auth API\nUse POST /auth/refresh on token expiry.",
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        tools.create_question_view(
            "auth-api-ask",
            "Auth API Questions",
            "Let consumers ask auth API questions.",
            "Answer only auth API integration questions.",
        )

        result = tools.create_or_update_share_relationship(
            share_id="auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="both",
            file_view_id="auth-api-files",
            file_intent="Share stable auth API context.",
            question_view_id="auth-api-ask",
            question_intent="Let consumers ask auth API questions.",
            question_base_url="https://provider.example.test",
        )

        shares = load_shares(self.root)
        self.assertIn("success: wrote share relationship auth-api", result)
        self.assertEqual(shares["auth-api"].parts, ("file", "question"))
        self.assertEqual(shares["auth-api"].file.view_id, "auth-api-files")
        self.assertEqual(shares["auth-api"].question.view_id, "auth-api-ask")
        self.assertFalse(shares["auth-api"].file.approved)
        self.assertFalse(shares["auth-api"].question.approved)

    def test_shared_view_builder_tool_rejects_missing_selected_artifact(self):
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_or_update_share_relationship(
            share_id="auth-api",
            title="Auth API",
            provider_id="alice",
            hub_url="https://hub.example.test",
            credential_id="alice-publish",
            capability="file_context",
            file_view_id="auth-api-files",
            file_intent="Share stable auth API context.",
        )

        self.assertIn("failed:", result)
        self.assertIn("file view source is invalid", result)
```

Add imports at the top of `tests/test_tools.py`:

```python
from rightmemory.share_models import load_shares
```

- [ ] **Step 2: Run failing tool tests**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsSharedViewBuilderTests.test_shared_view_builder_tool_creates_share_relationship tests.test_tools.MemoryToolsSharedViewBuilderTests.test_shared_view_builder_tool_rejects_missing_selected_artifact
```

Expected: fail with `AttributeError: 'MemoryTools' object has no attribute 'create_or_update_share_relationship'`.

- [ ] **Step 3: Add imports to `tools.py`**

Add these imports near existing shared-view imports:

```python
from .share_models import ShareFilePart, ShareQuestionPart, ShareRelationship, load_shares, save_shares, validate_share_id
from .share_results import normalize_share_capability
```

- [ ] **Step 4: Add the compiler tool to `MemoryTools`**

Add this method near `create_question_view(...)` in `rightmemory/tools.py`:

```python
    def create_or_update_share_relationship(
        self,
        share_id: str,
        title: str,
        provider_id: str,
        hub_url: str,
        credential_id: str,
        capability: str = "auto",
        file_view_id: str | None = None,
        file_intent: str | None = None,
        question_view_id: str | None = None,
        question_intent: str | None = None,
        question_base_url: str | None = None,
    ) -> str:
        """Create canonical provider share registry data, or return actionable validation failures."""
        self._require_shared_view_builder_tool()
        try:
            clean_share_id = validate_share_id(share_id)
            clean_capability = normalize_share_capability(capability)
            if clean_capability == "auto":
                clean_capability = _capability_from_selected_views(file_view_id, question_view_id)
            parts: list[str] = []
            file_part = None
            question_part = None
            if clean_capability in {"file_context", "both"}:
                if not file_view_id or not file_intent:
                    return "failed: file_context capability requires file_view_id and file_intent"
                try:
                    validate_file_view_recipe_source(self.memory_root, file_view_id, require_selection=False)
                except (OSError, ValueError) as exc:
                    return f"failed: file view source is invalid: {exc}"
                parts.append("file")
                file_part = ShareFilePart(view_id=validate_heading_id(file_view_id), intent=str(file_intent).strip(), approved=False)
            if clean_capability in {"live_questions", "both"}:
                if not question_view_id or not question_intent or not question_base_url:
                    return "failed: live_questions capability requires question_view_id, question_intent, and question_base_url"
                try:
                    validate_question_view_source(self.memory_root, question_view_id)
                except (OSError, ValueError) as exc:
                    return f"failed: question view source is invalid: {exc}"
                parts.append("question")
                question_part = ShareQuestionPart(
                    view_id=validate_heading_id(question_view_id),
                    intent=str(question_intent).strip(),
                    question_base_url=str(question_base_url).strip(),
                    approved=False,
                )
            if not parts:
                return "failed: share capability selected no shareable capability"
            shares = load_shares(self.memory_root)
            shares[clean_share_id] = ShareRelationship(
                share_id=clean_share_id,
                role="provider",
                title=str(title).strip(),
                provider_id=validate_heading_id(provider_id),
                hub_url=str(hub_url).strip().rstrip("/"),
                credential_id=validate_heading_id(credential_id),
                state="draft",
                parts=tuple(parts),
                file=file_part,
                question=question_part,
            )
            save_shares(self.memory_root, shares)
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return f"success: wrote share relationship {clean_share_id} with capability {clean_capability}"
```

Add this helper near other private helpers in `rightmemory/tools.py`:

```python
def _capability_from_selected_views(file_view_id: str | None, question_view_id: str | None) -> str:
    has_file = bool(str(file_view_id or "").strip())
    has_question = bool(str(question_view_id or "").strip())
    if has_file and has_question:
        return "both"
    if has_file:
        return "file_context"
    if has_question:
        return "live_questions"
    return "auto"
```

- [ ] **Step 5: Update builder prompt**

In `rightmemory/prompts/shared-view-builder.md`, add this section before “Return a concise summary...”:

```markdown
For share-level requests, the caller message uses `<share_build>` or `<share_revise>`.

Treat the user request as a share relationship request. If capability is `auto`,
choose whether the share needs file context, live questions, or both. If the
caller constrains capability, obey that constraint unless it is impossible, and
explain the failure.

For file context, call `create_extractive_file_view` or
`create_generative_file_view`. For live questions, call `create_question_view`.
After the selected artifacts are valid, call `create_or_update_share_relationship`.

Do not hand-write `shares.toml`, `recipe.toml`, or `question.toml`.
Do not expose `MF#` or `MQ#` terminology to the user unless explaining advanced
implementation details.
```

In `rightmemory/prompt.py`, update the `shared-view-builder` command guidance string to say:

```python
"- The `rightmemory shared-view ...` or `rightmemory share ...` command selected shared-view builder behavior.\n"
```

- [ ] **Step 6: Run tool tests**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsSharedViewBuilderTests.test_shared_view_builder_tool_creates_share_relationship tests.test_tools.MemoryToolsSharedViewBuilderTests.test_shared_view_builder_tool_rejects_missing_selected_artifact
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
rtk git add rightmemory/tools.py rightmemory/prompts/shared-view-builder.md rightmemory/prompt.py tests/test_tools.py
rtk git commit -m "feat: add share relationship builder tool"
```

## Task 3: Add Share-Level Builder Runtime

**Files:**
- Create: `rightmemory/share_builder.py`
- Modify: `tests/test_shares.py`

- [ ] **Step 1: Write failing share-builder tests**

Append these tests to `tests/test_shares.py`:

```python
class ShareBuilderRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        (self.root / "MEMORY.md").write_text("# Project\n\n## Auth API\nUse refresh tokens.\n", encoding="utf-8")

    def test_run_share_builder_uses_share_level_session_and_returns_result(self):
        def fake_run_session_turn(runtime, session_id, message):
            self.assertEqual(session_id, "share-builder-auth-api")
            self.assertIn("<share_build>", message)
            self.assertIn("capability: auto", message)
            save_shares(
                self.root,
                {
                    "auth-api": ShareRelationship(
                        share_id="auth-api",
                        role="provider",
                        title="Auth API",
                        provider_id="alice",
                        hub_url="https://hub.example.test",
                        credential_id="alice-publish",
                        state="draft",
                        parts=("file",),
                        file=ShareFilePart(
                            view_id="auth-api-files",
                            intent="Share auth API context.",
                            approved=False,
                        ),
                    )
                },
            )
            return "Selected Auth API context."

        with patch("rightmemory.share_builder.RightMemoryRuntime.run_session_turn", fake_run_session_turn):
            result = run_share_builder(
                self.root,
                share_id_hint="auth-api",
                request="Share auth API context.",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                capability="auto",
            )

        self.assertEqual(result.share_id, "auth-api")
        self.assertEqual(result.builder_final_message, "Selected Auth API context.")
        self.assertEqual(result.capability, "file_context")
        self.assertEqual(result.next_action, "rightmemory share approve auth-api")

    def test_run_share_reviser_uses_existing_share_session(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="provider",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    credential_id="alice-publish",
                    state="draft",
                    parts=("question",),
                    question=ShareQuestionPart(
                        view_id="auth-api-ask",
                        intent="Answer auth API questions.",
                        question_base_url="https://provider.example.test",
                        approved=False,
                    ),
                )
            },
        )

        def fake_run_session_turn(runtime, session_id, message):
            self.assertEqual(session_id, "share-builder-auth-api")
            self.assertIn("<share_revise>", message)
            self.assertIn("Include profile endpoint.", message)
            return "Updated live question scope."

        with patch("rightmemory.share_builder.RightMemoryRuntime.run_session_turn", fake_run_session_turn):
            result = revise_share_builder(self.root, "auth-api", "Include profile endpoint.")

        self.assertEqual(result.builder_final_message, "Updated live question scope.")
        self.assertEqual(result.capability, "live_questions")
```

Add these imports to `tests/test_shares.py`:

```python
from rightmemory.share_builder import revise_share_builder, run_share_builder
```

- [ ] **Step 2: Run failing builder tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareBuilderRuntimeTests
```

Expected: fail with `ModuleNotFoundError: No module named 'rightmemory.share_builder'`.

- [ ] **Step 3: Create `share_builder.py`**

Create `rightmemory/share_builder.py`:

```python
from __future__ import annotations

from pathlib import Path

from .config import load_config
from .runtime import RightMemoryRuntime
from .share_models import ShareRelationship, load_shares, validate_share_id
from .share_results import ShareCapabilityStatus, ShareOperationResult, capability_from_parts, normalize_share_capability


def run_share_builder(
    memory_root: Path,
    *,
    share_id_hint: str | None,
    request: str,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    capability: str = "auto",
    question_base_url: str | None = None,
    title_hint: str | None = None,
) -> ShareOperationResult:
    if not request.strip():
        raise ValueError("share request must not be empty")
    session_id = _share_builder_session_id(share_id_hint or _fallback_share_id_hint(request))
    message = _share_build_message(
        request=request,
        share_id_hint=share_id_hint,
        title_hint=title_hint,
        provider_id=provider_id,
        hub_url=hub_url,
        credential_id=credential_id,
        capability=capability,
        question_base_url=question_base_url,
    )
    final_message = _run_builder_turn(memory_root, session_id, message)
    share = _load_created_share(memory_root, share_id_hint)
    return _operation_result(share, final_message=final_message, next_action=f"rightmemory share approve {share.share_id}")


def revise_share_builder(
    memory_root: Path,
    share_id: str,
    revision: str,
    *,
    capability: str | None = None,
    question_base_url: str | None = None,
) -> ShareOperationResult:
    root = Path(memory_root).expanduser()
    clean_share_id = validate_share_id(share_id)
    if not revision.strip():
        raise ValueError("share revision must not be empty")
    share = load_shares(root).get(clean_share_id)
    if share is None:
        raise KeyError(f"share not found: {clean_share_id}")
    if share.role != "provider":
        raise ValueError(f"share is not provider-owned: {clean_share_id}")
    final_message = _run_builder_turn(
        root,
        _share_builder_session_id(clean_share_id),
        _share_revise_message(share, revision=revision, capability=capability, question_base_url=question_base_url),
    )
    updated = load_shares(root).get(clean_share_id)
    if updated is None:
        raise RuntimeError(f"share builder removed share relationship: {clean_share_id}")
    return _operation_result(updated, final_message=final_message, next_action=f"rightmemory share approve {clean_share_id}")


def _run_builder_turn(memory_root: Path, session_id: str, message: str) -> str:
    root = Path(memory_root).expanduser()
    config = load_config("shared-view-builder", memory_root=root)
    runtime = RightMemoryRuntime(config)
    try:
        return runtime.run_session_turn(session_id, message)
    finally:
        runtime.cleanup()


def _share_builder_session_id(share_id: str) -> str:
    return f"share-builder-{validate_share_id(share_id)}"


def _fallback_share_id_hint(request: str) -> str:
    words = []
    for raw in request.lower().replace("_", "-").split():
        word = "".join(character for character in raw if character.isalnum() or character == "-").strip("-")
        if word:
            words.append(word)
        if len(words) == 3:
            break
    return "-".join(words) or "share"


def _share_build_message(
    *,
    request: str,
    share_id_hint: str | None,
    title_hint: str | None,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    capability: str,
    question_base_url: str | None,
) -> str:
    lines = [
        "<share_build>",
        f"request: {request.strip()}",
        f"capability: {normalize_share_capability(capability)}",
        f"provider_id: {provider_id.strip()}",
        f"hub_url: {hub_url.strip().rstrip('/')}",
        f"credential_id: {credential_id.strip()}",
    ]
    if share_id_hint:
        lines.append(f"share_id_hint: {share_id_hint.strip()}")
    if title_hint:
        lines.append(f"title_hint: {title_hint.strip()}")
    if question_base_url:
        lines.append(f"question_base_url: {question_base_url.strip()}")
    lines.append("</share_build>")
    return "\n".join(lines)


def _share_revise_message(
    share: ShareRelationship,
    *,
    revision: str,
    capability: str | None,
    question_base_url: str | None,
) -> str:
    lines = [
        "<share_revise>",
        f"share_id: {share.share_id}",
        f"title: {share.title}",
        f"current_capability: {capability_from_parts(share.parts)}",
        f"revision: {revision.strip()}",
    ]
    if capability:
        lines.append(f"capability: {normalize_share_capability(capability)}")
    if question_base_url:
        lines.append(f"question_base_url: {question_base_url.strip()}")
    lines.append("</share_revise>")
    return "\n".join(lines)


def _load_created_share(memory_root: Path, share_id_hint: str | None) -> ShareRelationship:
    shares = load_shares(Path(memory_root).expanduser())
    if share_id_hint:
        clean_hint = validate_share_id(share_id_hint)
        if clean_hint in shares:
            return shares[clean_hint]
    if len(shares) == 1:
        return next(iter(shares.values()))
    if not shares:
        raise RuntimeError("share builder did not create a share relationship")
    raise RuntimeError("share builder created multiple share relationships; provide a share_id_hint")


def _operation_result(share: ShareRelationship, *, final_message: str, next_action: str | None = None) -> ShareOperationResult:
    statuses: list[ShareCapabilityStatus] = []
    if share.file:
        statuses.append(
            ShareCapabilityStatus(
                capability="file_context",
                artifact_id=share.file.view_id or share.file.heading_id,
                status="approved" if share.file.approved else share.state,
                preview_path=f"shared_views/{share.file.view_id}/dist/MEMORY.md" if share.file.view_id else None,
            )
        )
    if share.question:
        statuses.append(
            ShareCapabilityStatus(
                capability="live_questions",
                artifact_id=share.question.view_id or share.question.heading_id,
                status="approved" if share.question.approved else share.state,
                preview_path=f"shared_views/{share.question.view_id}/retriever.md" if share.question.view_id else None,
            )
        )
    return ShareOperationResult(
        share_id=share.share_id,
        title=share.title,
        role=share.role,
        state=share.state,
        capability=capability_from_parts(share.parts),
        builder_final_message=final_message,
        statuses=tuple(statuses),
        next_action=next_action,
    )
```

- [ ] **Step 4: Run builder tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareBuilderRuntimeTests
```

Expected: pass.

- [ ] **Step 5: Commit**

Run:

```bash
rtk git add rightmemory/share_builder.py tests/test_shares.py
rtk git commit -m "feat: add share-level builder runtime"
```

## Task 4: Refactor Share Service Functions Around Structured Results

**Files:**
- Modify: `rightmemory/shares.py`
- Modify: `tests/test_shares.py`

- [ ] **Step 1: Write failing service tests**

Add these tests to `tests/test_shares.py`:

```python
class ShareServiceResultTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)

    def test_create_share_from_request_returns_structured_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="both",
            builder_final_message="Generated both capabilities.",
            next_action="rightmemory share approve auth-api",
        )
        with patch("rightmemory.shares.run_share_builder", return_value=expected) as builder:
            result = create_share_from_request(
                self.root,
                request="Share auth API context with frontend agents.",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                capability="auto",
                question_base_url="https://provider.example.test",
                share_id_hint="auth-api",
            )

        self.assertEqual(result, expected)
        builder.assert_called_once()
        self.assertEqual(builder.call_args.kwargs["capability"], "auto")

    def test_revise_share_returns_structured_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="file_context",
            builder_final_message="Updated generated context.",
            next_action="rightmemory share approve auth-api",
        )
        with patch("rightmemory.shares.revise_share_builder", return_value=expected) as builder:
            result = revise_share(self.root, "auth-api", "Include profile endpoint.")

        self.assertEqual(result, expected)
        builder.assert_called_once_with(self.root, "auth-api", "Include profile endpoint.", capability=None, question_base_url=None)

    def test_legacy_create_share_wrapper_formats_result_when_request_is_used(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="file_context",
            builder_final_message="Generated file context.",
            next_action="rightmemory share approve auth-api",
        )
        with patch("rightmemory.shares.run_share_builder", return_value=expected):
            text = create_share(
                self.root,
                "auth-api",
                title="Auth API",
                provider_id="alice",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
                request="Share auth API context.",
                capability="file_context",
            )

        self.assertIn("Builder summary:", text)
        self.assertIn("Generated file context.", text)
```

Add imports:

```python
from rightmemory.share_results import ShareOperationResult
from rightmemory.shares import create_share_from_request, revise_share
```

- [ ] **Step 2: Run failing service tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareServiceResultTests
```

Expected: fail with missing `create_share_from_request` and `revise_share`.

- [ ] **Step 3: Add imports to `shares.py`**

Add:

```python
from .share_builder import revise_share_builder, run_share_builder
from .share_results import ShareCapabilityStatus, ShareOperationResult, capability_from_parts, format_share_operation_result
```

- [ ] **Step 4: Add structured service functions**

Add these public functions near `create_share(...)` in `rightmemory/shares.py`:

```python
def create_share_from_request(
    memory_root: Path,
    *,
    request: str,
    provider_id: str,
    hub_url: str,
    credential_id: str,
    capability: str = "auto",
    question_base_url: str | None = None,
    share_id_hint: str | None = None,
    title_hint: str | None = None,
) -> ShareOperationResult:
    return run_share_builder(
        memory_root,
        share_id_hint=share_id_hint,
        request=request,
        provider_id=provider_id,
        hub_url=hub_url,
        credential_id=credential_id,
        capability=capability,
        question_base_url=question_base_url,
        title_hint=title_hint,
    )


def revise_share(
    memory_root: Path,
    share_id: str,
    revision: str,
    *,
    capability: str | None = None,
    question_base_url: str | None = None,
) -> ShareOperationResult:
    return revise_share_builder(
        memory_root,
        share_id,
        revision,
        capability=capability,
        question_base_url=question_base_url,
    )
```

- [ ] **Step 5: Extend `create_share` wrapper without breaking explicit existing behavior**

Change the `create_share(...)` signature to add keyword-only optional arguments:

```python
    request: str | None = None,
    capability: str = "auto",
```

At the start of the function, after `root = Path(memory_root).expanduser()`, add:

```python
    if request is not None and request.strip():
        result = create_share_from_request(
            root,
            request=request,
            provider_id=provider_id,
            hub_url=hub_url,
            credential_id=credential_id,
            capability=capability,
            question_base_url=question_base_url,
            share_id_hint=share_id,
            title_hint=title,
        )
        return format_share_operation_result(result)
```

Leave the existing explicit `file_intent` / `question_intent` path in place for backwards compatibility. After that explicit path saves `shares.toml`, change the return string to include a result object when builders returned output. Use this minimal first version:

```python
    return f"created share {clean_share_id}; review generated parts, then run: rightmemory share approve {clean_share_id}"
```

The explicit path will be upgraded to collect per-part builder output in Task 5 through CLI formatting tests.

- [ ] **Step 6: Run service tests and existing share tests**

Run:

```bash
rtk python -m unittest tests.test_shares.ShareServiceResultTests tests.test_shares.ShareModelTests tests.test_shares.ShareProviderFlowTests
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
rtk git add rightmemory/shares.py tests/test_shares.py
rtk git commit -m "feat: add structured share service results"
```

## Task 5: Add CLI Natural-Language Create And Revise

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Add these tests to `ShareCliTests` in `tests/test_cli.py`:

```python
    def test_share_create_request_dispatches_to_create_share(self):
        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.create_share", return_value="auth-api provider draft capability=both\n\nBuilder summary:\nGenerated both capabilities.\n") as create,
                patch("sys.stdout", stdout),
            ):
                result = main(
                    [
                        "share",
                        "create",
                        "auth-api",
                        "--request",
                        "Share auth API context with frontend agents.",
                        "--provider",
                        "alice",
                        "--hub-url",
                        "https://hub.example.test",
                        "--credential-id",
                        "alice-publish",
                        "--capability",
                        "auto",
                        "--question-base-url",
                        "https://provider.example.test",
                    ]
                )

        self.assertEqual(result, 0)
        create.assert_called_once()
        self.assertEqual(create.call_args.kwargs["request"], "Share auth API context with frontend agents.")
        self.assertEqual(create.call_args.kwargs["capability"], "auto")
        self.assertIn("Builder summary:", stdout.getvalue())

    def test_share_revise_dispatches_to_revise_share(self):
        stdout = io.StringIO()
        result = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="file_context",
            builder_final_message="Updated file context.",
            next_action="rightmemory share approve auth-api",
        )
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with (
                patch("rightmemory.cli.default_memory_root", return_value=root),
                patch("rightmemory.cli.revise_share", return_value=result) as revise,
                patch("sys.stdout", stdout),
            ):
                exit_code = main(["share", "revise", "auth-api", "Include profile endpoint."])

        self.assertEqual(exit_code, 0)
        revise.assert_called_once_with(root, "auth-api", "Include profile endpoint.", capability=None, question_base_url=None)
        self.assertIn("Builder summary:", stdout.getvalue())
        self.assertIn("Updated file context.", stdout.getvalue())
```

Add import:

```python
from rightmemory.share_results import ShareOperationResult
```

- [ ] **Step 2: Run failing CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.ShareCliTests.test_share_create_request_dispatches_to_create_share tests.test_cli.ShareCliTests.test_share_revise_dispatches_to_revise_share
```

Expected: fail because parser does not accept `--request`, `--capability`, or `revise`.

- [ ] **Step 3: Update CLI imports**

In `rightmemory/cli.py`, change the share import line to include `revise_share`:

```python
from .shares import approve_share, create_share, join_share, list_shares, publish_share, revise_share, share_status
from .share_results import format_share_operation_result
```

- [ ] **Step 4: Update `share create` parser**

In `_share_main`, change:

```python
    create.add_argument("--title", required=True)
```

to:

```python
    create.add_argument("--title")
    create.add_argument("--request")
    create.add_argument("--capability", choices=("auto", "file-context", "live-questions", "both"), default="auto")
```

Leave `--file`, `--question`, and `--question-base-url` in place.

- [ ] **Step 5: Add `share revise` parser**

After the `publish` parser setup, add:

```python
    revise = subparsers.add_parser("revise")
    revise.add_argument("share_id")
    revise.add_argument("revision", nargs=argparse.REMAINDER)
    revise.add_argument("--capability", choices=("auto", "file-context", "live-questions", "both"))
    revise.add_argument("--question-base-url")
```

- [ ] **Step 6: Add create argument validation**

At the top of the `if args.command == "create":` branch, add:

```python
        if not args.title and not args.request:
            raise ValueError("share create requires --title unless --request is provided")
        if not args.request and not (args.file or args.question):
            raise ValueError("share create requires --request, --file, or --question")
```

Pass new kwargs into `create_share(...)`:

```python
                title=args.title or args.share_id,
                request=args.request,
                capability=args.capability,
```

- [ ] **Step 7: Add revise command handling**

Before the `publish` branch, add:

```python
    if args.command == "revise":
        revision = " ".join(args.revision).strip()
        if not revision:
            raise ValueError("share revise requires a revision message")
        result = revise_share(
            memory_root,
            args.share_id,
            revision,
            capability=args.capability,
            question_base_url=args.question_base_url,
        )
        print(format_share_operation_result(result), end="")
        return 0
```

- [ ] **Step 8: Run CLI tests**

Run:

```bash
rtk python -m unittest tests.test_cli.ShareCliTests
```

Expected: pass.

- [ ] **Step 9: Commit**

Run:

```bash
rtk git add rightmemory/cli.py tests/test_cli.py
rtk git commit -m "feat: add share revise cli"
```

## Task 6: Add Web Share Service And API Endpoints

**Files:**
- Modify: `rightmemory/web/service.py`
- Modify: `rightmemory/web/app.py`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Write failing Web service tests**

Add these tests to `WebStudioSharedViewApiTests` in `tests/test_web_service.py`:

```python
    def test_share_relationships_endpoint_lists_shares(self):
        save_shares(
            self.root,
            {
                "auth-api": ShareRelationship(
                    share_id="auth-api",
                    role="provider",
                    title="Auth API",
                    provider_id="alice",
                    hub_url="https://hub.example.test",
                    credential_id="alice-publish",
                    state="draft",
                    parts=("file",),
                    file=ShareFilePart(view_id="auth-api-files", intent="Share auth API context.", approved=False),
                )
            },
        )

        response = self.client.get("/api/share/relationships")

        self.assertEqual(response.status_code, 200)
        shares = response.json()["data"]["shares"]
        self.assertEqual(shares[0]["share_id"], "auth-api")
        self.assertEqual(shares[0]["capability"], "file_context")

    def test_share_create_endpoint_returns_structured_builder_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="both",
            builder_final_message="Generated both capabilities.",
            next_action="rightmemory share approve auth-api",
        )
        with patch("rightmemory.web.service.create_share_from_request", return_value=expected):
            response = self.client.post(
                "/api/share/relationships",
                json={
                    "request": "Share auth API context with frontend agents.",
                    "share_id": "auth-api",
                    "provider_id": "alice",
                    "hub_url": "https://hub.example.test",
                    "credential_id": "alice-publish",
                    "capability": "auto",
                    "question_base_url": "https://provider.example.test",
                },
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual(data["share_id"], "auth-api")
        self.assertEqual(data["builder_final_message"], "Generated both capabilities.")

    def test_share_revise_endpoint_returns_structured_builder_result(self):
        expected = ShareOperationResult(
            share_id="auth-api",
            title="Auth API",
            role="provider",
            state="draft",
            capability="file_context",
            builder_final_message="Updated file context.",
            next_action="rightmemory share approve auth-api",
        )
        with patch("rightmemory.web.service.revise_share", return_value=expected):
            response = self.client.post(
                "/api/share/relationships/auth-api/revise",
                json={"revision": "Include profile endpoint."},
                headers={"x-csrf-token": self.csrf},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"]["builder_final_message"], "Updated file context.")
```

Add imports:

```python
from rightmemory.share_models import ShareFilePart, ShareRelationship, save_shares
from rightmemory.share_results import ShareOperationResult
```

- [ ] **Step 2: Run failing Web service tests**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioSharedViewApiTests.test_share_relationships_endpoint_lists_shares tests.test_web_service.WebStudioSharedViewApiTests.test_share_create_endpoint_returns_structured_builder_result tests.test_web_service.WebStudioSharedViewApiTests.test_share_revise_endpoint_returns_structured_builder_result
```

Expected: fail with `404` for new endpoints.

- [ ] **Step 3: Add service imports**

In `rightmemory/web/service.py`, add:

```python
from ..share_models import load_shares
from ..share_results import ShareCapabilityStatus, ShareOperationResult, capability_from_parts
from ..shares import create_share_from_request, publish_share, revise_share
```

- [ ] **Step 4: Add service methods**

Add these methods to `WebStudioService`:

```python
    def share_relationships(self) -> dict[str, Any]:
        shares = load_shares(self.memory_root)
        return {"shares": [self._share_summary(share) for share in shares.values()]}

    def create_share_relationship(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = create_share_from_request(
            self.memory_root,
            request=_required_payload_str(payload, "request"),
            provider_id=_required_payload_str(payload, "provider_id"),
            hub_url=_required_payload_str(payload, "hub_url"),
            credential_id=_required_payload_str(payload, "credential_id"),
            capability=_optional_payload_str(payload, "capability") or "auto",
            question_base_url=_optional_payload_str(payload, "question_base_url"),
            share_id_hint=_optional_payload_str(payload, "share_id"),
            title_hint=_optional_payload_str(payload, "title"),
        )
        return result.to_json()

    def revise_share_relationship(self, share_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        result = revise_share(
            self.memory_root,
            share_id,
            _required_payload_str(payload, "revision"),
            capability=_optional_payload_str(payload, "capability"),
            question_base_url=_optional_payload_str(payload, "question_base_url"),
        )
        return result.to_json()

    def _share_summary(self, share) -> dict[str, Any]:
        return {
            "share_id": share.share_id,
            "title": share.title,
            "role": share.role,
            "state": share.state,
            "provider_id": share.provider_id,
            "hub_url": share.hub_url,
            "capability": capability_from_parts(share.parts),
            "file": _json_safe(share.file) if share.file else None,
            "question": _json_safe(share.question) if share.question else None,
        }
```

- [ ] **Step 5: Add FastAPI routes**

In `rightmemory/web/app.py`, after `/api/share/views`, add:

```python
    @app.get("/api/share/relationships")
    def share_relationships(service=Depends(current_service)):
        return ok_response("share relationships loaded", service.share_relationships())

    @app.post("/api/share/relationships")
    def create_share_relationship(
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.create_share_relationship(payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not create share", technical=str(exc)),
            ) from exc
        return ok_response("share created", data)

    @app.post("/api/share/relationships/{share_id}/revise")
    def revise_share_relationship(
        share_id: str,
        request: Request,
        payload: dict[str, object] = Body(...),
        session=Depends(current_session),
    ):
        require_csrf(root, request, request.headers.get("x-csrf-token"))
        service = service_for_active_root(session.active_root)
        try:
            data = service.revise_share_relationship(share_id, payload)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=error_detail("could not revise share", technical=str(exc)),
            ) from exc
        return ok_response("share revised", data)
```

- [ ] **Step 6: Run Web service tests**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioSharedViewApiTests.test_share_relationships_endpoint_lists_shares tests.test_web_service.WebStudioSharedViewApiTests.test_share_create_endpoint_returns_structured_builder_result tests.test_web_service.WebStudioSharedViewApiTests.test_share_revise_endpoint_returns_structured_builder_result
```

Expected: pass.

- [ ] **Step 7: Commit**

Run:

```bash
rtk git add rightmemory/web/service.py rightmemory/web/app.py tests/test_web_service.py
rtk git commit -m "feat: add web share relationship api"
```

## Task 7: Render Share-First Web Studio UI

**Files:**
- Modify: `rightmemory/web/static/app.js`
- Modify: `rightmemory/web/static/styles.css`
- Modify: `tests/test_web_service.py`

- [ ] **Step 1: Write failing static shell test**

Add this test to `WebStudioStaticTests` in `tests/test_web_service.py`:

```python
    def test_shared_view_static_shell_contains_share_first_hooks(self):
        script = (Path(__file__).resolve().parents[1] / "rightmemory" / "web" / "static" / "app.js").read_text(encoding="utf-8")

        self.assertIn("renderShareRelationships", script)
        self.assertIn("create-share-form", script)
        self.assertIn("revise-share-form", script)
        self.assertIn("advanced-shared-view-tools", script)
```

- [ ] **Step 2: Run failing static test**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioStaticTests.test_shared_view_static_shell_contains_share_first_hooks
```

Expected: fail because the hooks do not exist.

- [ ] **Step 3: Add share rendering helpers**

In `rightmemory/web/static/app.js`, add these helpers before `renderSharedViews()`:

```javascript
function renderCapabilityLabel(value) {
  const labels = {
    auto: "Auto",
    file_context: "File context",
    live_questions: "Live questions",
    both: "File context + live questions",
  };
  return labels[value] || value || "Auto";
}

function renderShareCard(share) {
  const fileStatus = share.file ? "file context configured" : "no file context";
  const questionStatus = share.question ? "live questions configured" : "no live questions";
  return `
    <article class="share-card" data-share-id="${escapeHtml(share.share_id || "")}">
      <div class="share-card-main">
        <h3>${escapeHtml(share.title || share.share_id || "Share")}</h3>
        <small>${escapeHtml(share.share_id || "")} | ${escapeHtml(share.role || "")} | ${escapeHtml(share.state || "")}</small>
      </div>
      <div class="share-meta">
        <span>${escapeHtml(renderCapabilityLabel(share.capability))}</span>
        <span>${escapeHtml(fileStatus)}</span>
        <span>${escapeHtml(questionStatus)}</span>
      </div>
      <div class="button-row">
        ${share.role === "provider" ? `<button data-share-action="approve" data-share-id="${escapeHtml(share.share_id)}">Approve</button>` : ""}
        ${share.role === "provider" ? `<button data-share-action="publish" data-share-id="${escapeHtml(share.share_id)}">Publish</button>` : ""}
        <button data-share-action="revise" data-share-id="${escapeHtml(share.share_id)}">Revise</button>
      </div>
    </article>
  `;
}

function renderShareRelationships(providerShares, consumerShares) {
  return `
    <div class="two-column">
      <section class="panel">
        <div class="section-heading"><h2>My Shared Shares</h2></div>
        <div class="share-list">${providerShares.length ? providerShares.map(renderShareCard).join("") : "<p>No provider shares.</p>"}</div>
      </section>
      <section class="panel">
        <div class="section-heading"><h2>Joined Shares</h2></div>
        <div class="share-list">${consumerShares.length ? consumerShares.map(renderShareCard).join("") : "<p>No joined shares.</p>"}</div>
      </section>
    </div>
  `;
}

function renderShareResult(data) {
  const statuses = data.statuses || [];
  return `
    <section class="panel wide share-review-panel">
      <div class="section-heading">
        <h2>${escapeHtml(data.title || data.share_id || "Share")}</h2>
      </div>
      ${data.builder_final_message ? `<pre>${escapeHtml(data.builder_final_message)}</pre>` : "<p>No builder summary.</p>"}
      ${statuses.length ? `<ul class="item-list">${statuses.map((status) => `<li>${escapeHtml(status.capability)} ${escapeHtml(status.artifact_id || "-")} ${escapeHtml(status.status || "")}</li>`).join("")}</ul>` : ""}
      ${data.next_action ? `<p>${escapeHtml(data.next_action)}</p>` : ""}
    </section>
  `;
}
```

- [ ] **Step 4: Replace `renderSharedViews()` top-level layout**

At the start of `renderSharedViews()`, after loading `/api/share/views`, also load relationships:

```javascript
  const relationshipPayload = await fetchJson("/api/share/relationships");
  const shares = relationshipPayload.data.shares || [];
  const providerShares = shares.filter((share) => share.role === "provider");
  const consumerShares = shares.filter((share) => share.role === "consumer");
```

Before the existing low-level flow layout, render share-first panels:

```javascript
  const shareFirst = `
    <section class="panel wide">
      <div class="section-heading">
        <h2>Create Share</h2>
      </div>
      <form id="create-share-form" class="guided-form">
        <label>
          Request
          <textarea name="request" placeholder="Share auth API context with frontend agents." required></textarea>
        </label>
        <div class="settings-form">
          <label>
            Capability
            <select name="capability">
              <option value="auto">Auto</option>
              <option value="file-context">File context</option>
              <option value="live-questions">Live questions</option>
              <option value="both">File context + live questions</option>
            </select>
          </label>
          <label>
            Share id
            <input name="share_id" placeholder="optional">
          </label>
          <label>
            Provider id
            <input name="provider_id" placeholder="alice" required>
          </label>
          <label>
            Credential
            <select class="credential-select" name="credential_id" required>${credentialOptions}</select>
          </label>
          <label>
            HTTP hub URL
            <input name="hub_url" placeholder="from credential" required>
          </label>
          <label>
            Question base URL
            <input name="question_base_url" placeholder="only for live questions">
          </label>
        </div>
        <div class="button-row">
          <button class="primary" type="submit"${hasCredentials ? "" : " disabled"}>Create Share</button>
        </div>
      </form>
      <div id="share-review-output"></div>
    </section>

    ${renderShareRelationships(providerShares, consumerShares)}

    <section class="panel wide">
      <div class="section-heading"><h2>Join Share</h2></div>
      <form id="join-share-form" class="guided-form">
        <label>
          Invite URL
          <textarea name="invitation" placeholder="https://.../i/share/token" required></textarea>
        </label>
        <div class="button-row"><button class="primary" type="submit">Join</button></div>
      </form>
    </section>
  `;
```

Wrap the existing low-level panels in:

```javascript
    <details id="advanced-shared-view-tools" class="advanced-tools">
      <summary>Advanced Shared View Tools</summary>
      ${existingLowLevelMarkup}
    </details>
```

Use a local variable for the previous low-level markup rather than deleting it.

- [ ] **Step 5: Add share form handlers**

In `attachSharedViewHandlers()`, before low-level form handlers, add:

```javascript
  const createShareForm = document.querySelector("#create-share-form");
  if (createShareForm) {
    createShareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const payload = await fetchJson("/api/share/relationships", {
          method: "POST",
          body: JSON.stringify({
            request: form.get("request"),
            share_id: form.get("share_id"),
            provider_id: form.get("provider_id"),
            credential_id: form.get("credential_id"),
            hub_url: form.get("hub_url"),
            capability: form.get("capability"),
            question_base_url: form.get("question_base_url"),
          }),
        });
        const output = document.querySelector("#share-review-output");
        if (output) {
          output.innerHTML = renderShareResult(payload.data);
        }
        setMessage(payload.message);
        await loadPanel();
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
```

Add a delegated revise handler:

```javascript
  document.querySelectorAll('[data-share-action="revise"]').forEach((button) => {
    button.addEventListener("click", () => {
      const shareId = button.dataset.shareId || "";
      const output = document.querySelector("#share-review-output");
      if (output) {
        output.innerHTML = `
          <section class="panel wide">
            <div class="section-heading"><h2>Revise ${escapeHtml(shareId)}</h2></div>
            <form id="revise-share-form" class="guided-form">
              <label>
                Revision
                <textarea name="revision" required></textarea>
              </label>
              <input name="share_id" type="hidden" value="${escapeHtml(shareId)}">
              <div class="button-row"><button class="primary" type="submit">Revise</button></div>
            </form>
          </section>
        `;
        attachSharedViewHandlers();
      }
    });
  });

  const reviseShareForm = document.querySelector("#revise-share-form");
  if (reviseShareForm) {
    reviseShareForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const form = new FormData(event.currentTarget);
        const shareId = String(form.get("share_id") || "").trim();
        const payload = await fetchJson(`/api/share/relationships/${encodeURIComponent(shareId)}/revise`, {
          method: "POST",
          body: JSON.stringify({ revision: form.get("revision") }),
        });
        const output = document.querySelector("#share-review-output");
        if (output) {
          output.innerHTML = renderShareResult(payload.data);
        }
        setMessage(payload.message);
      } catch (error) {
        setMessage(error.message);
      }
    });
  }
```

- [ ] **Step 6: Add styles**

Append to `rightmemory/web/static/styles.css`:

```css
.share-list {
  display: grid;
  gap: 10px;
}

.share-card {
  display: grid;
  gap: 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--surface-soft);
  padding: 12px;
}

.share-card-main {
  display: grid;
  gap: 4px;
}

.share-card-main small,
.share-meta {
  color: var(--muted);
  font-size: 12px;
}

.share-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.share-review-panel pre {
  max-height: 280px;
  overflow: auto;
}

.advanced-tools {
  max-width: 1120px;
  margin-top: 12px;
}
```

- [ ] **Step 7: Run static test**

Run:

```bash
rtk python -m unittest tests.test_web_service.WebStudioStaticTests.test_shared_view_static_shell_contains_share_first_hooks
```

Expected: pass.

- [ ] **Step 8: Commit**

Run:

```bash
rtk git add rightmemory/web/static/app.js rightmemory/web/static/styles.css tests/test_web_service.py
rtk git commit -m "feat: render share-first web studio"
```

## Task 8: Documentation And Full Verification

**Files:**
- Modify: `docs/shared-views-usage.md`
- Modify: `README.md` if it still teaches low-level shared-view commands as the primary remote sharing path.

- [ ] **Step 1: Update shared-view docs**

In `docs/shared-views-usage.md`, add a “Share-first workflow” section before low-level shared-view command examples:

```markdown
## Share-First Workflow

Use `rightmemory share` for normal provider/consumer sharing.

Provider:

```bash
rightmemory share create auth-api \
  --request "Share auth API context with frontend agents. Include stable docs and allow live questions." \
  --provider alice \
  --hub-url http://127.0.0.1:8765 \
  --credential-id alice-publish \
  --capability auto \
  --question-base-url http://127.0.0.1:8766
rightmemory share approve auth-api
rightmemory share publish auth-api --label frontend
```

Consumer:

```bash
rightmemory share join http://127.0.0.1:8765/i/share/<token>
rightmemory share status auth-api
```

If the generated scope is not right, revise it before approval:

```bash
rightmemory share revise auth-api "Include the profile endpoint and exclude deployment notes."
```

The older `rightmemory shared-view ...` commands remain available for advanced
debugging and scripting.
```

- [ ] **Step 2: Run focused tests**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsSharedViewBuilderTests tests.test_shares tests.test_cli.ShareCliTests tests.test_web_service.WebStudioSharedViewApiTests
```

Expected: pass.

- [ ] **Step 3: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: exit code `0`.

- [ ] **Step 4: Run full unit suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: all tests pass. Existing skipped tests and existing deprecation warnings are acceptable.

- [ ] **Step 5: Run whitespace check**

Run:

```bash
rtk git diff --check
```

Expected: no output.

- [ ] **Step 6: Commit docs**

Run:

```bash
rtk git add docs/shared-views-usage.md README.md
rtk git commit -m "docs: document share-first workflow"
```

If `README.md` did not need changes, run:

```bash
rtk git add docs/shared-views-usage.md
rtk git commit -m "docs: document share-first workflow"
```

## Self-Review Checklist

- Spec coverage:
  - Natural-language create: Task 3, Task 4, Task 5, Task 6, Task 7.
  - Optional capability constraint: Task 1, Task 2, Task 3, Task 5, Task 7.
  - Agent owns semantic decisions and calls tools: Task 2, Task 3.
  - No custom conversation history: Task 3 uses existing `RightMemoryRuntime.run_session_turn(...)`.
  - Builder final message visible in CLI and Web: Task 1, Task 4, Task 5, Task 6, Task 7.
  - Consumer join remains one invite URL: Task 7 uses existing join form; Task 8 documents it.
  - Low-level shared-view tools remain advanced: Task 7.

- Placeholder scan:
  - The plan contains no incomplete-section markers and no unspecified edge-case steps.

- Type consistency:
  - `ShareOperationResult`, `ShareCapabilityStatus`, `format_share_operation_result`, and `normalize_share_capability` are introduced in Task 1 and reused with the same names.
  - `create_or_update_share_relationship` is introduced in Task 2 and referenced by the builder prompt in the same task.
  - `run_share_builder` and `revise_share_builder` are introduced in Task 3 and imported by `shares.py` in Task 4.

# Root Memory Prefix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make retrieve preload only `MEMORY.md` while exposing `F#`-backed `MEMORY_<slug>.md` detail files through a typed `read_memory_file(slug)` tool.

**Architecture:** Keep the existing retrieve prefix-cache pipeline, but narrow its snapshot and automatic diff selectors to root memory only. Add `read_memory_file(slug)` beside `read_skill` and `read_mf`, then update runtime tool exposure, generated tool guidance, retrieve prompt text, and focused tests around that boundary.

**Tech Stack:** Python standard library, existing RightMemory runtime/tool patterns, Git subprocess calls, `unittest`.

---

## File Structure

- Modify `rightmemory/retrieve_context.py`: replace active-memory snapshot/diff selectors with root-memory-only selectors and invalidate old daily snapshot cache entries by scope metadata.
- Modify `rightmemory/tools.py`: add `MemoryTools.read_memory_file(slug)` plus missing-file and available-slug helper messages.
- Modify `rightmemory/runtime.py`: expose `read_memory_file` to retrieve runtime tools while keeping retrieve read-only.
- Modify `rightmemory/prompt.py`: include `read_memory_file(slug)` in generated retrieve tool guidance for relevant `F#` headings.
- Modify `rightmemory/prompts/retrieve.md`: describe the daily root-memory snapshot and `F#` detail-file disclosure.
- Modify `tests/test_retrieve_context.py`: pin root-memory-only snapshot and diff behavior.
- Modify `tests/test_tools.py`: cover `read_memory_file` success, missing slug, invalid slug, and symlink safety.
- Modify `tests/test_config.py`: cover prompt guidance and retrieve tool exposure in runtime tests.

---

## Prompt Diff Preview

This is the intended prompt-facing change. Apply it during Task 3.

```diff
diff --git a/rightmemory/prompts/retrieve.md b/rightmemory/prompts/retrieve.md
--- a/rightmemory/prompts/retrieve.md
+++ b/rightmemory/prompts/retrieve.md
@@
-- The runtime supplies a daily memory snapshot before the caller query. Treat that supplied snapshot as the ordinary retrieval source.
-- The runtime may append a memory diff block after the daily snapshot when active memory changed after the snapshot was built. Read it as a patch over the supplied snapshot: added lines are newer memory, removed lines are obsolete, and unchanged snapshot lines remain valid.
+- The runtime supplies a daily root-memory snapshot before the caller query. The snapshot contains `MEMORY.md`; use `read_memory_file(slug)` for relevant `F#` headings backed by `MEMORY_<slug>.md`.
+- The runtime may append a memory diff block after the daily root-memory snapshot when `MEMORY.md` changed after the snapshot was built. Read it as a patch over the supplied snapshot: added lines are newer memory, removed lines are obsolete, and unchanged snapshot lines remain valid.
```

```diff
diff --git a/rightmemory/prompt.py b/rightmemory/prompt.py
--- a/rightmemory/prompt.py
+++ b/rightmemory/prompt.py
@@
         return (
             "Available retrieve tools:\n"
+            "- `read_memory_file(slug)` reads the `MEMORY_<slug>.md` detail file for a relevant `F#` heading.\n"
             "- `read_skill(skill_id)` reads a full memory skill body for a relevant `S#` heading.\n"
             "- `read_mf(mf_id)` reads external file context for a relevant `MF#` heading."
         )
```

---

### Task 1: Narrow Retrieve Snapshot And Diffs To Root Memory

**Files:**
- Modify: `rightmemory/retrieve_context.py`
- Test: `tests/test_retrieve_context.py`

- [ ] **Step 1: Write failing snapshot selector tests**

Update imports in `tests/test_retrieve_context.py`:

```python
from rightmemory.retrieve_context import (
    RetrieveContextStore,
    build_retrieve_request_text,
    current_memory_head,
    format_memory_diff_block,
    format_recent_submitted_context_block,
    load_daily_snapshot,
    memory_diff_since,
    root_memory_paths,
)
```

Replace `test_daily_snapshot_renders_active_memory_without_skill_or_runtime_files` with:

```python
def test_daily_snapshot_renders_root_memory_without_detail_skill_or_runtime_files(self):
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        (root / "MEMORY.md").write_text("# Root {#root}\n\nroot body\n", encoding="utf-8")
        (root / "MEMORY_detail.md").write_text("## Detail {#detail}\n", encoding="utf-8")
        (root / "MEMORY_SKILL_demo.md").write_text("# Skill Body\n", encoding="utf-8")
        (root / ".runtime" / "shared_views" / "imports" / "mf-one").mkdir(parents=True)
        (root / ".runtime" / "shared_views" / "imports" / "mf-one" / "MEMORY.md").write_text(
            "external\n",
            encoding="utf-8",
        )

        snapshot = load_daily_snapshot(root, now=datetime(2026, 6, 29, tzinfo=UTC))

    self.assertEqual(snapshot.day, "2026-06-29")
    self.assertEqual(snapshot.scope, "root-memory-v1")
    self.assertEqual(snapshot.paths, ["MEMORY.md"])
    self.assertTrue(snapshot.text.startswith("Daily root-memory snapshot\n"))
    self.assertIn("===== MEMORY.md =====", snapshot.text)
    self.assertIn("# Root {#root}", snapshot.text)
    self.assertNotIn("MEMORY_detail.md", snapshot.text)
    self.assertNotIn("MEMORY_SKILL_demo.md", snapshot.text)
    self.assertNotIn(".runtime/shared_views/imports", snapshot.text)
    self.assertNotIn("2026-06-29", snapshot.text)
```

Replace `test_active_memory_paths_excludes_memory_skill_files` with:

```python
def test_root_memory_paths_returns_only_memory_md(self):
    with tempfile.TemporaryDirectory() as tempdir:
        root = Path(tempdir)
        (root / "MEMORY.md").write_text("# Root\n", encoding="utf-8")
        (root / "MEMORY_alpha.md").write_text("# Alpha\n", encoding="utf-8")
        (root / "MEMORY_SKILL_alpha.md").write_text("# Skill\n", encoding="utf-8")

        self.assertEqual(root_memory_paths(root), ["MEMORY.md"])
```

- [ ] **Step 2: Write failing diff test for detail-file exclusion**

In `test_memory_diff_since_returns_active_memory_diff_only`, add an ordinary detail file to both commits:

```python
(root / "MEMORY_detail.md").write_text("old detail\n", encoding="utf-8")
self._git(root, "add", "MEMORY.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
```

Then change the second commit setup:

```python
(root / "MEMORY_detail.md").write_text("new detail\n", encoding="utf-8")
self._git(root, "add", "MEMORY.md", "MEMORY_detail.md", "MEMORY_SKILL_demo.md")
```

Add this assertion after the existing skill-file assertion:

```python
self.assertNotIn("MEMORY_detail.md", diff)
```

- [ ] **Step 3: Run retrieve-context tests and confirm failure**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context.RetrieveContextSnapshotTests tests.test_retrieve_context.RetrieveContextDiffTests
```

Expected: FAIL because `root_memory_paths` is not defined, snapshot scope is not present, detail files still appear in snapshots, and detail-file diffs are still included.

- [ ] **Step 4: Implement root-memory-only snapshot and diff helpers**

In `rightmemory/retrieve_context.py`, remove the import of `MEMORY_DETAIL_FILE_RE` and `MEMORY_SKILL_FILE_RE`, add a scope constant, add a `scope` field, and replace selector helpers with root-memory-only helpers:

```python
SNAPSHOT_HEADER = "Daily root-memory snapshot"
SNAPSHOT_SCOPE = "root-memory-v1"


@dataclass(frozen=True)
class DailySnapshot:
    day: str
    base_commit: str | None
    content_hash: str
    text: str
    paths: list[str] = field(default_factory=list)
    scope: str = SNAPSHOT_SCOPE
```

```python
def root_memory_paths(memory_root: Path) -> list[str]:
    root = Path(memory_root)
    memory_path = root / "MEMORY.md"
    return ["MEMORY.md"] if memory_path.is_file() else []
```

Update `load_daily_snapshot` to invalidate old cache entries:

```python
if state_path.exists():
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if data.get("day") == day and data.get("scope") == SNAPSHOT_SCOPE:
        return _snapshot_from_dict(data)

paths = root_memory_paths(root)
```

Update `memory_diff_since` to call a root-memory helper:

```python
changed = _changed_root_memory_paths(memory_root, old_commit, new_commit)
```

Replace `_changed_active_memory_paths` with:

```python
def _changed_root_memory_paths(memory_root: Path, old_commit: str, new_commit: str) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", old_commit, new_commit, "--", "MEMORY.md"],
        cwd=memory_root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"git diff --name-only failed: {result.stderr.strip()}")
    paths = [raw.strip() for raw in result.stdout.splitlines() if raw.strip() == "MEMORY.md"]
    return sorted(set(paths))
```

Update `_snapshot_from_dict`:

```python
scope = data.get("scope")
if scope != SNAPSHOT_SCOPE:
    raise ValueError("daily snapshot scope is unsupported")
return DailySnapshot(day=day, base_commit=base_commit, content_hash=content_hash, text=text, paths=paths)
```

- [ ] **Step 5: Run retrieve-context tests and confirm pass**

Run:

```bash
rtk python -m unittest tests.test_retrieve_context
```

Expected: PASS.

- [ ] **Step 6: Commit snapshot and diff changes**

Run:

```bash
rtk git add rightmemory/retrieve_context.py tests/test_retrieve_context.py
rtk git commit -m "Limit retrieve prefix to root memory"
```

Expected: commit succeeds with only those two files staged.

---

### Task 2: Add `read_memory_file(slug)` Detail Tool

**Files:**
- Modify: `rightmemory/tools.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing tool tests**

Add these tests near the existing retrieve tool tests in `tests/test_tools.py`:

```python
def test_retrieve_read_memory_file_returns_detail_file_by_slug(self):
    (self.root / "MEMORY_alpha.md").write_text("# Alpha Detail\n\nUse alpha detail.\n", encoding="utf-8")
    (self.root / "MEMORY_SKILL_alpha.md").write_text("# Alpha Skill\n\nsecret skill.\n", encoding="utf-8")
    tools = MemoryTools(self.root, role="retrieve")

    result = tools.read_memory_file("alpha")

    self.assertIn("===== MEMORY_alpha.md =====", result)
    self.assertIn("# Alpha Detail", result)
    self.assertIn("Use alpha detail.", result)
    self.assertNotIn("secret skill", result)

def test_retrieve_read_memory_file_failure_lists_available_slugs_without_paths(self):
    (self.root / "MEMORY_beta.md").write_text("# Beta Detail\n", encoding="utf-8")
    (self.root / "MEMORY_SKILL_gamma.md").write_text("# Gamma Skill\n", encoding="utf-8")
    tools = MemoryTools(self.root, role="retrieve")

    result = tools.read_memory_file("alpha")

    self.assertIn("Memory file not found: alpha", result)
    self.assertIn("Available memory files:\n- beta", result)
    self.assertNotIn("MEMORY_beta.md", result)
    self.assertNotIn("gamma", result)

def test_retrieve_read_memory_file_rejects_invalid_slugs(self):
    tools = MemoryTools(self.root, role="retrieve")

    for slug in ("", "../alpha", "/tmp/alpha", ".hidden", "..", "SKILL_alpha"):
        with self.subTest(slug=slug):
            with self.assertRaises(ValueError):
                tools.read_memory_file(slug)
```

Add this symlink test after the existing retrieve symlink tests:

```python
@unittest.skipIf(not hasattr(os, "symlink"), "symlink is not available")
def test_retrieve_read_memory_file_does_not_follow_symlink_outside_root(self):
    outside = self.root.parent / f"{self.root.name}-outside-memory.md"
    self.addCleanup(outside.unlink, missing_ok=True)
    outside.write_text("# Outside\n\nsecret\n", encoding="utf-8")
    (self.root / "MEMORY_alpha.md").symlink_to(outside)
    tools = MemoryTools(self.root, role="retrieve")

    result = tools.read_memory_file("alpha")

    self.assertIn("Memory file not found: alpha", result)
    self.assertNotIn("secret", result)
```

- [ ] **Step 2: Run tool tests and confirm failure**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsTests
```

Expected: FAIL because `MemoryTools.read_memory_file` is not defined.

- [ ] **Step 3: Implement `read_memory_file`**

Add this method in `MemoryTools` after `read_mf`:

```python
def read_memory_file(self, slug: str) -> str:
    """Read an ordinary MEMORY_<slug>.md detail file by slug."""
    clean_slug = self._validate_memory_reference_id(slug)
    if clean_slug.startswith(".") or clean_slug == ".." or clean_slug.startswith("SKILL_"):
        raise ValueError("slug must name an ordinary MEMORY_<slug>.md detail file")
    relative = f"MEMORY_{clean_slug}.md"
    if not MEMORY_DETAIL_FILE_RE.fullmatch(relative) or MEMORY_SKILL_FILE_RE.fullmatch(relative):
        raise ValueError("slug must name an ordinary MEMORY_<slug>.md detail file")
    path = self.memory_root / relative
    if not self._is_safe_read_file(path):
        return self._missing_memory_file_message(clean_slug)
    text = self._read_text(path).rstrip()
    return self._cap_command_output(f"===== {relative} =====\n{text}")
```

Add these helper methods after `_available_skills_block`:

```python
def _missing_memory_file_message(self, slug: str) -> str:
    return f"Memory file not found: {slug}\n\n{self._available_memory_files_block()}"

def _available_memory_files_block(self) -> str:
    slugs = []
    for path in sorted(self.memory_root.glob("MEMORY_*.md")):
        relative = path.relative_to(self.memory_root).as_posix()
        if (
            MEMORY_DETAIL_FILE_RE.fullmatch(relative)
            and not MEMORY_SKILL_FILE_RE.fullmatch(relative)
            and self._is_safe_read_file(path)
        ):
            slugs.append(path.stem.removeprefix("MEMORY_"))
    if not slugs:
        return "Available memory files:\n- none"
    return "Available memory files:\n" + "\n".join(f"- {item}" for item in slugs)
```

- [ ] **Step 4: Run tool tests and confirm pass**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsTests
```

Expected: PASS.

- [ ] **Step 5: Commit detail tool changes**

Run:

```bash
rtk git add rightmemory/tools.py tests/test_tools.py
rtk git commit -m "Add retrieve memory detail tool"
```

Expected: commit succeeds with only those two files staged.

---

### Task 3: Expose Tool And Update Retrieve Prompt

**Files:**
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/prompt.py`
- Modify: `rightmemory/prompts/retrieve.md`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write failing runtime and prompt tests**

In `tests/test_config.py`, update `test_retrieve_prompt_uses_context_first_contract`:

```python
self.assertIn("supplies a daily root-memory snapshot", instructions)
self.assertIn("read_memory_file", instructions)
self.assertIn("`read_memory_file(slug)` reads the `MEMORY_<slug>.md` detail file for a relevant `F#` heading", instructions)
```

Replace the old exact tool set in `test_retrieve_runtime_does_not_expose_shared_view_tool`:

```python
self.assertEqual(tool_names, {"read_memory_file", "read_skill", "read_mf"})
```

Replace the old exact tool set in `test_retrieve_runtime_is_read_only`:

```python
self.assertEqual(tool_names, {"read_memory_file", "read_skill", "read_mf"})
```

Update retrieve request shape assertions in `test_retrieve_pulls_mf_views_before_model_without_prompt_pollution` and `test_retrieve_turn_sends_snapshot_first_and_stores_only_real_turns`:

```python
self.assertTrue(captured["message"].startswith("Daily root-memory snapshot\n"))
self.assertTrue(runtime.agent.calls[0]["message"].startswith("Daily root-memory snapshot\n"))
```

- [ ] **Step 2: Run targeted config tests and confirm failure**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_prompt_uses_context_first_contract tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool tests.test_config.RuntimeTests.test_retrieve_runtime_is_read_only
```

Expected: FAIL because prompt text and runtime tool exposure do not yet mention `read_memory_file`.

- [ ] **Step 3: Expose the new tool in runtime**

Update `RightMemoryRuntime._agent_tools` in `rightmemory/runtime.py`:

```python
if self.config.role == "retrieve":
    return [
        self._agent_tool(self.tools.read_skill),
        self._agent_tool(self.tools.read_mf),
        self._agent_tool(self.tools.read_memory_file),
    ]
```

Keeping `read_skill` first preserves existing debug-trace tests that call the first retrieve tool directly.

- [ ] **Step 4: Update generated tool guidance**

Update `_tool_guidance("retrieve")` in `rightmemory/prompt.py`:

```python
return (
    "Available retrieve tools:\n"
    "- `read_memory_file(slug)` reads the `MEMORY_<slug>.md` detail file for a relevant `F#` heading.\n"
    "- `read_skill(skill_id)` reads a full memory skill body for a relevant `S#` heading.\n"
    "- `read_mf(mf_id)` reads external file context for a relevant `MF#` heading."
)
```

- [ ] **Step 5: Update retrieve prompt wording**

Apply the prompt changes shown in the `Prompt Diff Preview` section of this plan to `rightmemory/prompts/retrieve.md`.

- [ ] **Step 6: Run targeted config tests and confirm pass**

Run:

```bash
rtk python -m unittest tests.test_config.ConfigTests.test_retrieve_prompt_uses_context_first_contract tests.test_config.ConfigTests.test_retrieve_runtime_does_not_expose_shared_view_tool tests.test_config.RuntimeTests.test_retrieve_runtime_is_read_only tests.test_config.RuntimeTests.test_debug_trace_records_tool_events
```

Expected: PASS.

- [ ] **Step 7: Commit prompt and runtime exposure changes**

Run:

```bash
rtk git add rightmemory/runtime.py rightmemory/prompt.py rightmemory/prompts/retrieve.md tests/test_config.py
rtk git commit -m "Expose retrieve memory detail tool"
```

Expected: commit succeeds with only those four files staged.

---

### Task 4: Full Verification And Final Commit Check

**Files:**
- Verify: `rightmemory/retrieve_context.py`
- Verify: `rightmemory/tools.py`
- Verify: `rightmemory/runtime.py`
- Verify: `rightmemory/prompt.py`
- Verify: `rightmemory/prompts/retrieve.md`
- Verify: `tests/test_retrieve_context.py`
- Verify: `tests/test_tools.py`
- Verify: `tests/test_config.py`

- [ ] **Step 1: Run compile verification**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 2: Run full unit test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
rtk git status --short
rtk git diff --stat
```

Expected: only intended implementation files are modified, plus any commits already created by earlier tasks. Existing unrelated worktree entries such as `skills/memory-orchestrator-cli/SKILL.md`, `.worktree/`, and `docs/problems.md` remain unstaged.

- [ ] **Step 4: Commit any verification-only cleanup**

If the verification commands required small follow-up edits, run:

```bash
rtk git add rightmemory/retrieve_context.py rightmemory/tools.py rightmemory/runtime.py rightmemory/prompt.py rightmemory/prompts/retrieve.md tests/test_retrieve_context.py tests/test_tools.py tests/test_config.py
rtk git commit -m "Harden root memory retrieve implementation"
```

Expected: commit succeeds only when there are implementation cleanup edits after Tasks 1 through 3. If there are no follow-up edits, skip this commit.

---

## Self-Review

- Spec coverage: Tasks 1 through 3 cover root-only preloading, root-only same-day diffs, the new `read_memory_file(slug)` tool for relevant `F#` detail headings, unchanged `read_skill` and `read_mf`, prompt guidance, runtime exposure, and tests.
- Placeholder scan: This plan contains exact paths, exact test snippets, exact implementation snippets, exact commands, and expected command outcomes.
- Type consistency: The plan uses `root_memory_paths`, `read_memory_file`, `_missing_memory_file_message`, and `_available_memory_files_block` consistently across tests and implementation snippets.

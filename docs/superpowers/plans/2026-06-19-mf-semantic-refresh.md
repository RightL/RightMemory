# MF Semantic Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add clean `MF#` semantic refresh with extractive and generative render modes, while keeping shared-view maintenance commits from advancing active-memory pruning.

**Architecture:** `shared_views/<view-id>/recipe.toml` remains the semantic source for `MF#` views. `render = "extractive"` recipes select memory ids/files; `render = "generative"` recipes have no selections and rely on model-written generated package output. Semantic refresh runs through a separate maintenance command, not inside normal auto-publish.

**Tech Stack:** Python 3.11 standard library, `unittest`, TOML parsing with `tomllib`, existing RightMemory runtime/tools/CLI patterns, Git subprocess helpers.

---

## File Structure

- Create: `rightmemory/memory_git.py`
  - Shared Git helpers for active-memory history.
  - Owns active-memory pathspecs and current active-memory commit lookup.
  - Used by `rightmemory/prune.py` and shared-view refresh metadata.

- Modify: `rightmemory/prune.py`
  - Keep prune generation model.
  - Count only commits touching `MEMORY.md` or `MEMORY_*.md`.
  - Select first-prune boundaries from active-memory commit history.

- Modify: `rightmemory/shared_view_files.py`
  - Add `render`, semantic refresh metadata, and mode-aware validation.
  - Replace old `render = "expanded-heading-subtrees"` output with clean render modes.
  - Add extractive and generative writer helpers.
  - Make export/publish mode-aware and fail closed for missing generative output.

- Modify: `rightmemory/tools.py`
  - Replace model-facing `create_file_view_recipe` with:
    - `create_extractive_file_view`
    - `create_generative_file_view`
  - Keep `create_question_view` unchanged.

- Modify: `rightmemory/runtime.py`
  - Register the two new model-facing tools for `shared-view-builder`.
  - Do not let normal auto-publish start a semantic refresh.

- Modify: `rightmemory/shared_view_builder.py`
  - Validate either render mode after builder runs.
  - Render extractive previews.
  - Require generated output for generative views.
  - Add `refresh_file_view` maintenance function with rollback and deterministic commit.

- Modify: `rightmemory/cli.py`
  - Add `rightmemory shared-view refresh-file <view-id>`.
  - Support `--force` and `--publish`.

- Modify: `rightmemory/prompts/shared-view-builder.md`
  - Tell the internal builder prompt to refine caller intent.
  - Tell it to choose exactly one render mode.
  - Tell it to call the matching tool.
  - Tell it not to commit.

- Modify tests:
  - `tests/test_prune.py`
  - `tests/test_shared_views.py`
  - `tests/test_tools.py`
  - `tests/test_config.py`
  - `tests/test_cli.py`

- Modify docs:
  - `docs/shared-views-usage.md`

## Task 1: Active-Memory Git Helpers And Pruner Filtering

**Files:**
- Create: `rightmemory/memory_git.py`
- Modify: `rightmemory/prune.py`
- Test: `tests/test_prune.py`

- [ ] **Step 1: Write failing pruner path-filter tests**

Append these tests to `PruneTests` in `tests/test_prune.py`:

```python
    def test_prune_generation_ignores_shared_view_only_commits(self):
        self._commit_memory("one", "memory: one")
        self._commit_shared_view_recipe("auth-api-files", "shared-view: refresh auth-api-files")
        self._commit_memory("two", "memory: two")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertFalse(status.due)
        self.assertEqual(status.commits_since_boundary, 2)
        self.assertIn("2/3 commits", status.message)

    def test_first_prune_boundary_uses_active_memory_history(self):
        self._commit_memory("one", "memory: one")
        self._commit_shared_view_recipe("auth-api-files", "shared-view: refresh auth-api-files")
        self._commit_memory("two", "memory: two")
        self._commit_memory("three", "memory: three")
        active_root = self._git("rev-list", "--max-parents=0", "HEAD", "--", "MEMORY.md")

        status = prune_due_status(self.root, PrunerConfig(memory_root=self.root, generation_commits=3))

        self.assertTrue(status.due)
        self.assertEqual(status.boundary_commit, active_root)
        self.assertEqual(status.commits_since_boundary, 3)

    def _commit_shared_view_recipe(self, view_id: str, message: str) -> None:
        view_dir = self.root / "shared_views" / view_id
        view_dir.mkdir(parents=True, exist_ok=True)
        (view_dir / "recipe.toml").write_text(
            'version = 1\n'
            f'view_id = "{view_id}"\n'
            'kind = "file"\n'
            'title = "Auth API Files"\n'
            'approved = true\n'
            'intent = "Expose auth context."\n'
            'render = "extractive"\n'
            'semantic_refresh_days = 7\n'
            'last_semantic_refresh_at = ""\n'
            'last_semantic_refresh_memory_commit = ""\n'
            'include_headings = ["auth-api"]\n'
            'include_nodes = []\n'
            'include_files = []\n'
            'exclude_ids = []\n',
            encoding="utf-8",
        )
        self._git("add", f"shared_views/{view_id}/recipe.toml")
        self._git("commit", "-m", message)
```

- [ ] **Step 2: Run the focused prune tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_prune.PruneTests.test_prune_generation_ignores_shared_view_only_commits tests.test_prune.PruneTests.test_first_prune_boundary_uses_active_memory_history
```

Expected: FAIL because `prune_due_status` still counts all repository commits.

- [ ] **Step 3: Add shared active-memory Git helpers**

Create `rightmemory/memory_git.py`:

```python
from __future__ import annotations

import os
import subprocess
from pathlib import Path


GIT_TIMEOUT_SECONDS = 30
ACTIVE_MEMORY_PATHS = ("MEMORY.md", ":(glob)MEMORY_*.md")


def current_active_memory_commit(memory_root: Path) -> str:
    root = Path(memory_root).resolve()
    return _git_stdout(root, "log", "-1", "--format=%H", "HEAD", "--", *ACTIVE_MEMORY_PATHS)


def active_memory_commit_count(memory_root: Path, revision: str) -> int:
    root = Path(memory_root).resolve()
    output = _git_stdout(root, "rev-list", "--count", revision, "--", *ACTIVE_MEMORY_PATHS)
    return int(output or "0")


def first_generation_active_memory_boundary(memory_root: Path, generation_commits: int) -> str:
    if generation_commits < 1:
        raise ValueError("generation_commits must be positive")
    root = Path(memory_root).resolve()
    commits = _git_stdout(root, "rev-list", "--reverse", "HEAD", "--", *ACTIVE_MEMORY_PATHS).splitlines()
    if not commits:
        return _git_stdout(root, "rev-list", "--max-parents=0", "HEAD").splitlines()[0]
    if len(commits) <= generation_commits:
        return commits[0]
    return commits[-generation_commits - 1]


def _git_stdout(root: Path, *args: str) -> str:
    result = _run_git(root, *args)
    return result.stdout.strip()


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "true"
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result
```

- [ ] **Step 4: Update pruner to use active-memory history**

In `rightmemory/prune.py`, add:

```python
from .memory_git import active_memory_commit_count, first_generation_active_memory_boundary
```

Replace `commits_since = _commit_count(root, f"{latest}..HEAD")` with:

```python
        commits_since = active_memory_commit_count(root, f"{latest}..HEAD")
```

Replace `total_commits = _commit_count(root, "HEAD")` with:

```python
    total_commits = active_memory_commit_count(root, "HEAD")
```

Replace `_first_generation_boundary(root, config.generation_commits)` with:

```python
        boundary_commit=first_generation_active_memory_boundary(root, config.generation_commits),
```

Leave `_first_generation_boundary` and `_commit_count` in place until this task passes, then remove them if no references remain.

- [ ] **Step 5: Run prune tests**

Run:

```bash
rtk python -m unittest tests.test_prune
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add rightmemory/memory_git.py rightmemory/prune.py tests/test_prune.py
rtk git commit -m "fix: count only active memory commits for pruning"
```

## Task 2: Mode-Aware File View Recipe Schema

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Test: `tests/test_shared_views.py`

- [ ] **Step 1: Write failing schema tests**

Add these tests to `SharedFileViewRecipeTests`:

```python
    def test_extractive_recipe_writes_clean_render_and_refresh_metadata(self):
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
            last_semantic_refresh_memory_commit="abc123",
        )

        recipe = load_file_view_recipe(self.root, "auth-api-files")
        text = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")

        self.assertEqual(recipe.render, "extractive")
        self.assertEqual(recipe.semantic_refresh_days, 7)
        self.assertEqual(recipe.last_semantic_refresh_memory_commit, "abc123")
        self.assertIn('render = "extractive"', text)
        self.assertNotIn("expanded-heading-subtrees", text)

    def test_generative_recipe_forbids_selection_fields(self):
        view_dir = self.root / "shared_views" / "auth-api-files"
        view_dir.mkdir(parents=True)
        (view_dir / "recipe.toml").write_text(
            'version = 1\n'
            'view_id = "auth-api-files"\n'
            'kind = "file"\n'
            'title = "Auth API Files"\n'
            'approved = true\n'
            'intent = "Expose auth context."\n'
            'render = "generative"\n'
            'semantic_refresh_days = 7\n'
            'last_semantic_refresh_at = ""\n'
            'last_semantic_refresh_memory_commit = ""\n'
            'include_nodes = ["token-expiry"]\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "generative file view recipe must not include selection field"):
            validate_file_view_recipe_source(self.root, "auth-api-files")

    def test_old_expanded_heading_render_is_rejected(self):
        view_dir = self.root / "shared_views" / "auth-api-files"
        view_dir.mkdir(parents=True)
        (view_dir / "recipe.toml").write_text(
            'version = 1\n'
            'view_id = "auth-api-files"\n'
            'kind = "file"\n'
            'title = "Auth API Files"\n'
            'approved = true\n'
            'intent = "Expose auth context."\n'
            'render = "expanded-heading-subtrees"\n'
            'include_headings = ["auth-api"]\n'
            'include_nodes = []\n'
            'include_files = []\n'
            'exclude_ids = []\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, 'render must be "extractive" or "generative"'):
            validate_file_view_recipe_source(self.root, "auth-api-files")
```

Update imports in `tests/test_shared_views.py` to include `load_file_view_recipe` and `write_extractive_file_view_recipe`.

- [ ] **Step 2: Run schema tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests.test_extractive_recipe_writes_clean_render_and_refresh_metadata tests.test_shared_views.SharedFileViewRecipeTests.test_generative_recipe_forbids_selection_fields tests.test_shared_views.SharedFileViewRecipeTests.test_old_expanded_heading_render_is_rejected
```

Expected: FAIL because the new functions and mode-aware schema do not exist.

- [ ] **Step 3: Add render mode fields and constants**

In `rightmemory/shared_view_files.py`, add constants near the recipe key sets:

```python
FILE_VIEW_RENDER_EXTRACTIVE = "extractive"
FILE_VIEW_RENDER_GENERATIVE = "generative"
FILE_VIEW_RENDER_VALUES = {FILE_VIEW_RENDER_EXTRACTIVE, FILE_VIEW_RENDER_GENERATIVE}
DEFAULT_SEMANTIC_REFRESH_DAYS = 7
```

Update `FILE_RECIPE_KEYS` to include:

```python
    "semantic_refresh_days",
    "last_semantic_refresh_at",
    "last_semantic_refresh_memory_commit",
```

Change `FILE_RECIPE_REQUIRED_KEYS` to require only common fields:

```python
FILE_RECIPE_REQUIRED_KEYS = {
    "version",
    "view_id",
    "kind",
    "title",
    "approved",
    "intent",
    "render",
    "semantic_refresh_days",
    "last_semantic_refresh_at",
    "last_semantic_refresh_memory_commit",
}
```

Update `FileViewRecipe`:

```python
@dataclass(frozen=True)
class FileViewRecipe:
    view_id: str
    title: str
    intent: str
    render: str = FILE_VIEW_RENDER_EXTRACTIVE
    include_headings: tuple[str, ...] = ()
    include_nodes: tuple[str, ...] = ()
    include_files: tuple[str, ...] = ()
    exclude_ids: tuple[str, ...] = ()
    approved: bool = False
    publish_hub_url: str | None = None
    publish_credential_id: str | None = None
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS
    last_semantic_refresh_at: str = ""
    last_semantic_refresh_memory_commit: str = ""
```

- [ ] **Step 4: Add mode-specific writer functions**

Replace `write_file_view_recipe` with `write_extractive_file_view_recipe`. Do not keep a compatibility wrapper; update every implementation and test call site to use the explicit extractive writer name.

```python
def write_extractive_file_view_recipe(
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
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS,
    last_semantic_refresh_at: str = "",
    last_semantic_refresh_memory_commit: str = "",
) -> str:
    root = Path(memory_root).expanduser()
    recipe = FileViewRecipe(
        view_id=validate_heading_id(view_id),
        title=_required_text(title, "title"),
        intent=_required_text(intent, "intent"),
        render=FILE_VIEW_RENDER_EXTRACTIVE,
        include_headings=tuple(validate_heading_id(item) for item in include_headings),
        include_nodes=tuple(validate_heading_id(item) for item in include_nodes),
        include_files=tuple(_validate_memory_source_file(item) for item in include_files),
        exclude_ids=tuple(validate_heading_id(item) for item in exclude_ids),
        approved=bool(approved),
        publish_hub_url=_optional_text(publish_hub_url),
        publish_credential_id=validate_heading_id(publish_credential_id) if publish_credential_id else None,
        semantic_refresh_days=_validate_refresh_days(semantic_refresh_days),
        last_semantic_refresh_at=str(last_semantic_refresh_at),
        last_semantic_refresh_memory_commit=str(last_semantic_refresh_memory_commit),
    )
    _write_file_view_source(root, recipe)
    return f"wrote extractive file view recipe {recipe.view_id}"
```

Add helper:

```python
def _write_file_view_source(root: Path, recipe: FileViewRecipe) -> None:
    view_dir = _view_dir(root, recipe.view_id)
    view_dir.mkdir(parents=True, exist_ok=True)
    _write_text(view_dir / ".gitignore", "dist/\n")
    _write_text(view_dir / "view.md", f"# {recipe.title}\n\n{recipe.intent}\n")
    _write_text(view_dir / "recipe.toml", _render_recipe_toml(recipe))
```

- [ ] **Step 5: Make loading and validation mode-aware**

In `load_file_view_recipe`, parse `render`, metadata, and selections:

```python
    render = str(data.get("render") or "").strip()
    return FileViewRecipe(
        view_id=validate_heading_id(str(data.get("view_id", clean_view_id))),
        title=str(data.get("title") or clean_view_id),
        intent=str(data.get("intent") or ""),
        render=render,
        include_headings=tuple(validate_heading_id(str(item)) for item in data.get("include_headings", []) if isinstance(item, str)),
        include_nodes=tuple(validate_heading_id(str(item)) for item in data.get("include_nodes", []) if isinstance(item, str)),
        include_files=tuple(_validate_memory_source_file(item) for item in data.get("include_files", []) if isinstance(item, str)),
        exclude_ids=tuple(validate_heading_id(str(item)) for item in data.get("exclude_ids", []) if isinstance(item, str)),
        approved=bool(data.get("approved", False)),
        publish_hub_url=str(publish.get("hub_url")).strip() if publish.get("hub_url") else None,
        publish_credential_id=validate_heading_id(str(publish.get("credential_id"))) if publish.get("credential_id") else None,
        semantic_refresh_days=_validate_refresh_days(data.get("semantic_refresh_days", DEFAULT_SEMANTIC_REFRESH_DAYS)),
        last_semantic_refresh_at=str(data.get("last_semantic_refresh_at") or ""),
        last_semantic_refresh_memory_commit=str(data.get("last_semantic_refresh_memory_commit") or ""),
    )
```

In `_file_view_recipe_schema_errors`, after the common missing-field check, add:

```python
    render = data.get("render")
    if render not in FILE_VIEW_RENDER_VALUES:
        errors.append('render must be "extractive" or "generative"')
    selection_keys = FILE_RECIPE_ARRAY_KEYS & keys
    if render == FILE_VIEW_RENDER_EXTRACTIVE:
        missing_selection = sorted(FILE_RECIPE_ARRAY_KEYS - keys)
        if missing_selection:
            errors.append("missing required extractive selection field(s): " + ", ".join(missing_selection))
        for key in sorted(FILE_RECIPE_ARRAY_KEYS):
            value = data.get(key)
            if not isinstance(value, list):
                errors.append(f"{key} must be a TOML array")
                continue
            if any(not isinstance(item, str) or not item.strip() for item in value):
                errors.append(f"{key} entries must be non-empty strings")
    elif render == FILE_VIEW_RENDER_GENERATIVE and selection_keys:
        for key in sorted(selection_keys):
            errors.append(f"generative file view recipe must not include selection field: {key}")
    if not isinstance(data.get("semantic_refresh_days"), int) or isinstance(data.get("semantic_refresh_days"), bool) or int(data.get("semantic_refresh_days")) < 0:
        errors.append("semantic_refresh_days must be a nonnegative integer")
    for key in ("last_semantic_refresh_at", "last_semantic_refresh_memory_commit"):
        if not isinstance(data.get(key), str):
            errors.append(f"{key} must be a string")
```

Remove the old unconditional loop over `FILE_RECIPE_ARRAY_KEYS`.

- [ ] **Step 6: Render canonical recipe TOML by mode**

In `_render_recipe_toml`, replace the body with:

```python
def _render_recipe_toml(recipe: FileViewRecipe) -> str:
    lines = [
        "version = 1",
        f'view_id = "{recipe.view_id}"',
        'kind = "file"',
        f"title = {_toml_string(recipe.title)}",
        f"approved = {str(recipe.approved).lower()}",
        f"intent = {_toml_string(recipe.intent)}",
        f"render = {_toml_string(recipe.render)}",
        f"semantic_refresh_days = {recipe.semantic_refresh_days}",
        f"last_semantic_refresh_at = {_toml_string(recipe.last_semantic_refresh_at)}",
        f"last_semantic_refresh_memory_commit = {_toml_string(recipe.last_semantic_refresh_memory_commit)}",
    ]
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE:
        lines.extend(
            [
                "",
                f"include_headings = {_toml_array(recipe.include_headings)}",
                f"include_nodes = {_toml_array(recipe.include_nodes)}",
                f"include_files = {_toml_array(recipe.include_files)}",
                f"exclude_ids = {_toml_array(recipe.exclude_ids)}",
            ]
        )
    if recipe.publish_hub_url or recipe.publish_credential_id:
        lines.extend(
            [
                "",
                "[publish]",
                "enabled = true",
                f"hub_url = {_toml_string(recipe.publish_hub_url or '')}",
                f"credential_id = {_toml_string(recipe.publish_credential_id or '')}",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"
```

Add:

```python
def _validate_refresh_days(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("semantic_refresh_days must be a nonnegative integer")
    return value
```

- [ ] **Step 7: Run focused schema tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests
```

Expected: PASS after updating old assertions/imports to use `write_extractive_file_view_recipe` where desired.

- [ ] **Step 8: Commit**

```bash
rtk git add rightmemory/shared_view_files.py tests/test_shared_views.py
rtk git commit -m "feat: add file view render modes"
```

## Task 3: Generative Output Materialization And Fail-Closed Publishing

**Files:**
- Modify: `rightmemory/shared_view_files.py`
- Test: `tests/test_shared_views.py`

- [ ] **Step 1: Write failing generative packaging tests**

Add these tests to `SharedFileViewRecipeTests`:

```python
    def test_generative_recipe_exports_existing_generated_memory(self):
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            published_context="Tokens expire after one hour.",
            approved=True,
        )
        package = self.root / "package"

        export_file_view_package(self.root, "auth-api-files", package)

        exported = (package / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        recipe = (package / "recipe.toml").read_text(encoding="utf-8")
        self.assertIn('render = "generative"', recipe)
        self.assertIn("## Published Context", exported)
        self.assertIn("Tokens expire after one hour.", exported)

    def test_generative_package_fails_when_generated_memory_missing(self):
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            published_context="Tokens expire after one hour.",
            approved=True,
        )
        shutil.rmtree(self.root / "shared_views" / "auth-api-files" / "dist")

        with self.assertRaisesRegex(ValueError, "generative file view output is missing"):
            export_file_view_package(self.root, "auth-api-files", self.root / "package")
```

Add `import shutil` to `tests/test_shared_views.py`.

- [ ] **Step 2: Write failing auto-publish fail-closed test**

Add this test to `SharedFileViewAutoPublishTests`:

```python
    def test_publish_approved_generative_view_fails_closed_when_output_missing(self):
        shutil.rmtree(self.root / "shared_views" / "auth-api-files")
        write_generative_file_view(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            published_context="Tokens expire after one hour.",
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )
        shutil.rmtree(self.root / "shared_views" / "auth-api-files" / "dist")

        with patch("rightmemory.shared_view_files.HubClient", side_effect=AssertionError("publish should not run")):
            results = publish_approved_file_views(self.root)

        self.assertEqual(results[0].status, "failed")
        self.assertIn("generative file view output is missing", results[0].message)
```

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests.test_generative_recipe_exports_existing_generated_memory tests.test_shared_views.SharedFileViewRecipeTests.test_generative_package_fails_when_generated_memory_missing tests.test_shared_views.SharedFileViewAutoPublishTests.test_publish_approved_generative_view_fails_closed_when_output_missing
```

Expected: FAIL because `write_generative_file_view` and generative export behavior do not exist.

- [ ] **Step 4: Add generative writer and canonical wrapper**

In `rightmemory/shared_view_files.py`, add:

```python
def write_generative_file_view(
    memory_root: Path,
    *,
    view_id: str,
    title: str,
    intent: str,
    published_context: str,
    approved: bool = False,
    publish_hub_url: str | None = None,
    publish_credential_id: str | None = None,
    semantic_refresh_days: int = DEFAULT_SEMANTIC_REFRESH_DAYS,
    last_semantic_refresh_at: str = "",
    last_semantic_refresh_memory_commit: str = "",
) -> str:
    body = _required_text(published_context, "published_context")
    root = Path(memory_root).expanduser()
    recipe = FileViewRecipe(
        view_id=validate_heading_id(view_id),
        title=_required_text(title, "title"),
        intent=_required_text(intent, "intent"),
        render=FILE_VIEW_RENDER_GENERATIVE,
        approved=bool(approved),
        publish_hub_url=_optional_text(publish_hub_url),
        publish_credential_id=validate_heading_id(publish_credential_id) if publish_credential_id else None,
        semantic_refresh_days=_validate_refresh_days(semantic_refresh_days),
        last_semantic_refresh_at=str(last_semantic_refresh_at),
        last_semantic_refresh_memory_commit=str(last_semantic_refresh_memory_commit),
    )
    _write_file_view_source(root, recipe)
    _write_generated_file_view(root, recipe, body)
    return f"wrote generative file view {recipe.view_id}"


def _write_generated_file_view(root: Path, recipe: FileViewRecipe, published_context: str) -> None:
    view_dir = _view_dir(root, recipe.view_id)
    temp = view_dir / f".dist.tmp-{os.getpid()}"
    if temp.exists():
        shutil.rmtree(temp)
    temp.mkdir(parents=True)
    _write_text(temp / "MEMORY.md", _render_shared_view_memory(recipe, published_context))
    _write_text(temp / "manifest.toml", f'version = 1\nview_id = "{recipe.view_id}"\n')
    final = view_dir / "dist"
    if final.exists():
        shutil.rmtree(final)
    temp.rename(final)


def _render_shared_view_memory(recipe: FileViewRecipe, published_context: str) -> str:
    return "\n".join(
        [
            f"# {recipe.title} Shared View",
            "",
            recipe.intent,
            "",
            "## Published Context",
            "",
            published_context.strip(),
            "",
        ]
    )
```

Update `_render_selected_memory` to use `_render_shared_view_memory`:

```python
def _render_selected_memory(root: Path, recipe: FileViewRecipe) -> str:
    sections: list[str] = []
    excluded = set(recipe.exclude_ids)
    for relative in recipe.include_files:
        path = root / relative
        if path.is_file():
            sections.extend([f"### {relative}", "", path.read_text(encoding="utf-8").rstrip(), ""])
    sources = sorted(root.glob("MEMORY*.md"))
    for source in sources:
        lines = source.read_text(encoding="utf-8").splitlines()
        sections.extend(_selected_lines_from_source(lines, recipe, excluded))
    return _render_shared_view_memory(recipe, "\n".join(sections).rstrip())
```

- [ ] **Step 5: Make render/export mode-aware**

In `render_file_view`, add a generative guard:

```python
    if recipe.render == FILE_VIEW_RENDER_GENERATIVE:
        _require_generated_file_view_output(root, recipe)
        return f"generated file view {recipe.view_id} already exists"
```

Add helper:

```python
def _require_generated_file_view_output(root: Path, recipe: FileViewRecipe) -> None:
    path = _view_dir(root, recipe.view_id) / "dist" / "MEMORY.md"
    if not path.is_file():
        raise ValueError(f"generative file view output is missing: shared_views/{recipe.view_id}/dist/MEMORY.md")
    text = path.read_text(encoding="utf-8")
    if "## Published Context" not in text or not text.split("## Published Context", 1)[1].strip():
        raise ValueError(f"generative file view output is empty: shared_views/{recipe.view_id}/dist/MEMORY.md")
```

In `validate_file_view_recipe_source`, change selection enforcement to:

```python
    selected = recipe.include_headings or recipe.include_nodes or recipe.include_files
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE and require_selection and not selected:
        raise ValueError(
            "invalid file view recipe:\n"
            "- extractive file view recipe must include at least one heading, node, or memory file"
        )
```

- [ ] **Step 6: Run shared-view tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests tests.test_shared_views.SharedFileViewAutoPublishTests
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
rtk git add rightmemory/shared_view_files.py tests/test_shared_views.py
rtk git commit -m "feat: support generative file views"
```

## Task 4: Model-Facing Builder Tools And Prompt Contract

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/runtime.py`
- Modify: `rightmemory/prompts/shared-view-builder.md`
- Modify: `tests/test_tools.py`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Write failing tool tests**

In `tests/test_tools.py`, rename existing `create_file_view_recipe` calls to `create_extractive_file_view`, then add:

```python
    def test_shared_view_builder_tool_creates_generative_file_view(self):
        tools = MemoryTools(self.root, role="shared-view-builder")

        result = tools.create_generative_file_view(
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose sanitized auth context.",
            published_context="Tokens expire after one hour.",
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
        )

        recipe = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        rendered = (self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md").read_text(encoding="utf-8")
        self.assertIn("success: wrote generative file view auth-api-files", result)
        self.assertIn('render = "generative"', recipe)
        self.assertNotIn("include_nodes", recipe)
        self.assertIn("Tokens expire after one hour.", rendered)
```

In `tests/test_config.py`, update shared-view builder expectations:

```python
        self.assertIn("create_extractive_file_view", instructions)
        self.assertIn("create_generative_file_view", instructions)
        self.assertNotIn("create_file_view_recipe", instructions)
```

And:

```python
        self.assertIn("create_extractive_file_view", tool_names)
        self.assertIn("create_generative_file_view", tool_names)
        self.assertNotIn("create_file_view_recipe", tool_names)
```

- [ ] **Step 2: Run focused tool tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsTests.test_shared_view_builder_tool_creates_canonical_file_view tests.test_tools.MemoryToolsTests.test_shared_view_builder_tool_creates_generative_file_view tests.test_config.ConfigTests.test_shared_view_builder_role_loads_prompt tests.test_config.ConfigTests.test_shared_view_builder_runtime_exposes_compiler_tools
```

Expected: FAIL because the new model-facing tools are not registered.

- [ ] **Step 3: Rename extractive model-facing tool**

In `rightmemory/tools.py`, rename `create_file_view_recipe` to `create_extractive_file_view`, update its docstring, and call `write_extractive_file_view_recipe`.

The success return should be:

```python
        return (
            f"success: wrote extractive file view {recipe.view_id} with "
            f"{len(recipe.include_headings)} heading(s), {len(recipe.include_nodes)} node(s), "
            f"{len(recipe.include_files)} file(s), and {len(recipe.exclude_ids)} excluded id(s)"
        )
```

- [ ] **Step 4: Add generative model-facing tool**

In `rightmemory/tools.py`, add:

```python
    def create_generative_file_view(
        self,
        view_id: str,
        title: str,
        intent: str,
        published_context: str,
        publish_hub_url: str | None = None,
        publish_credential_id: str | None = None,
    ) -> str:
        """Create and render a canonical generative MF# file view."""
        self._require_shared_view_builder_tool()
        if not str(published_context).strip():
            return "failed: published_context must not be empty"
        try:
            write_generative_file_view(
                self.memory_root,
                view_id=view_id,
                title=title,
                intent=intent,
                published_context=published_context,
                approved=False,
                publish_hub_url=publish_hub_url,
                publish_credential_id=publish_credential_id,
            )
            recipe = validate_file_view_recipe_source(
                self.memory_root,
                view_id,
                require_selection=False,
                require_publish=bool(publish_hub_url or publish_credential_id),
            )
            rendered = self.memory_root / "shared_views" / recipe.view_id / "dist" / "MEMORY.md"
            if not self._file_view_rendered_context(rendered):
                return "failed: published_context rendered an empty Published Context"
        except (OSError, ValueError) as exc:
            return f"failed: {exc}"
        return f"success: wrote generative file view {recipe.view_id}"
```

Update imports to include `write_extractive_file_view_recipe` and `write_generative_file_view`.

- [ ] **Step 5: Register new tools**

In `rightmemory/runtime.py`, replace the shared-view-builder tool registration with:

```python
                    self._agent_tool(self.tools.create_extractive_file_view),
                    self._agent_tool(self.tools.create_generative_file_view),
                    self._agent_tool(self.tools.create_question_view),
```

- [ ] **Step 6: Update internal builder prompt**

Replace the file-view section in `rightmemory/prompts/shared-view-builder.md` with:

```markdown
For file-view requests, inspect active memory and choose one render mode:

- `extractive` when concrete headings, nodes, or files cleanly represent the view.
- `generative` when source memory mixes private details with shareable facts, or when the shared memory should be rewritten for clarity.

First rewrite the caller's rough intent into a durable internal intent. Pass that refined intent to the tool. This is internal builder work, not a user prompt.

Do not hand-write `recipe.toml` and do not commit. Use exactly one file-view compiler tool:

- Call `create_extractive_file_view` with `include_headings`, `include_nodes`, `include_files`, and `exclude_ids`.
- Call `create_generative_file_view` with `published_context`, containing only the body for `## Published Context`.

If the tool returns `failed: ...`, fix the arguments and call it again. Never finish a file-view build until the matching tool returns `success: ...`.
```

- [ ] **Step 7: Run focused prompt/tool tests**

Run:

```bash
rtk python -m unittest tests.test_tools.MemoryToolsTests.test_shared_view_builder_tool_creates_canonical_file_view tests.test_tools.MemoryToolsTests.test_shared_view_builder_tool_creates_generative_file_view tests.test_config.ConfigTests.test_shared_view_builder_role_loads_prompt tests.test_config.ConfigTests.test_shared_view_builder_runtime_exposes_compiler_tools
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
rtk git add rightmemory/tools.py rightmemory/runtime.py rightmemory/prompts/shared-view-builder.md tests/test_tools.py tests/test_config.py
rtk git commit -m "feat: split file view builder tools"
```

## Task 5: Builder Validation And Semantic Refresh Maintenance

**Files:**
- Modify: `rightmemory/shared_view_builder.py`
- Modify: `rightmemory/shared_view_files.py`
- Test: `tests/test_shared_views.py`

- [ ] **Step 1: Write failing builder validation tests**

Update `test_file_view_builder_renders_generated_dist_preview` to call `write_extractive_file_view_recipe`.

Add this test to `SharedFileViewRecipeTests`:

```python
    def test_file_view_builder_accepts_generative_output(self):
        def fake_builder(memory_root, view_id, message):
            write_generative_file_view(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose sanitized auth context.",
                published_context="Tokens expire after one hour.",
                approved=False,
                publish_hub_url="https://hub.example.test",
                publish_credential_id="alice-publish",
            )
            return "built generative file view auth-api-files"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            result = run_file_view_builder(
                self.root,
                view_id="auth-api-files",
                title="Auth API Files",
                intent="Expose auth API integration context.",
                hub_url="https://hub.example.test",
                credential_id="alice-publish",
            )

        preview = self.root / "shared_views" / "auth-api-files" / "dist" / "MEMORY.md"
        self.assertEqual(result, "built generative file view auth-api-files")
        self.assertIn("Tokens expire after one hour.", preview.read_text(encoding="utf-8"))
```

- [ ] **Step 2: Write failing semantic refresh tests**

Add these tests to `SharedFileViewRecipeTests`:

```python
    def test_refresh_file_view_preserves_approval_and_publish_settings(self):
        self._init_git_memory()
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
            publish_hub_url="https://hub.example.test",
            publish_credential_id="alice-publish",
            last_semantic_refresh_at="2000-01-01T00:00:00+00:00",
            last_semantic_refresh_memory_commit="old",
        )

        def fake_builder(memory_root, view_id, message):
            write_generative_file_view(
                memory_root,
                view_id=view_id,
                title="Auth API Files",
                intent="Expose sanitized auth context.",
                published_context="Tokens expire after one hour.",
                approved=False,
            )
            return "refreshed"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            result = refresh_file_view(self.root, "auth-api-files", force=True)

        recipe = load_file_view_recipe(self.root, "auth-api-files")
        self.assertIn("refreshed file view auth-api-files", result)
        self.assertTrue(recipe.approved)
        self.assertEqual(recipe.publish_hub_url, "https://hub.example.test")
        self.assertEqual(recipe.publish_credential_id, "alice-publish")
        self.assertEqual(recipe.render, "generative")
        self.assertNotEqual(recipe.last_semantic_refresh_memory_commit, "old")

    def test_refresh_file_view_restores_previous_files_on_builder_failure(self):
        self._init_git_memory()
        write_extractive_file_view_recipe(
            self.root,
            view_id="auth-api-files",
            title="Auth API Files",
            intent="Expose auth API integration context.",
            include_headings=["auth-api"],
            approved=True,
        )
        original = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")

        def fake_builder(memory_root, view_id, message):
            view_dir = memory_root / "shared_views" / view_id
            (view_dir / "recipe.toml").write_text("broken = true\n", encoding="utf-8")
            return "broken"

        with patch("rightmemory.shared_view_builder._run_builder", side_effect=fake_builder):
            with self.assertRaisesRegex(ValueError, "unsupported field"):
                refresh_file_view(self.root, "auth-api-files", force=True)

        restored = (self.root / "shared_views" / "auth-api-files" / "recipe.toml").read_text(encoding="utf-8")
        self.assertEqual(restored, original)

    def _init_git_memory(self) -> None:
        subprocess.run(["git", "init"], cwd=self.root, check=True, stdout=subprocess.PIPE)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=self.root, check=True)
        subprocess.run(["git", "config", "user.name", "Test User"], cwd=self.root, check=True)
        subprocess.run(["git", "add", "MEMORY.md"], cwd=self.root, check=True)
        subprocess.run(["git", "commit", "-m", "memory: initial"], cwd=self.root, check=True, stdout=subprocess.PIPE)
```

Add `import subprocess` to `tests/test_shared_views.py`, and import `refresh_file_view`.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests.test_file_view_builder_accepts_generative_output tests.test_shared_views.SharedFileViewRecipeTests.test_refresh_file_view_preserves_approval_and_publish_settings tests.test_shared_views.SharedFileViewRecipeTests.test_refresh_file_view_restores_previous_files_on_builder_failure
```

Expected: FAIL because `refresh_file_view` does not exist and builder validation is extractive-only.

- [ ] **Step 4: Make `run_file_view_builder` mode-aware**

In `rightmemory/shared_view_builder.py`, import render constants and update `run_file_view_builder`:

```python
    recipe = validate_file_view_recipe_source(
        memory_root,
        clean_view_id,
        require_selection=False,
        require_publish=True,
    )
    if recipe.render == FILE_VIEW_RENDER_EXTRACTIVE:
        validate_file_view_recipe_source(memory_root, clean_view_id, require_selection=True, require_publish=True)
        render_file_view(memory_root, clean_view_id)
    else:
        _require_nonempty_file_view_context(memory_root, clean_view_id)
```

Update `_require_nonempty_file_view_context` error text:

```python
            "shared-view builder rendered an empty file view; "
            "call the file-view compiler tool with content that produces Published Context"
```

- [ ] **Step 5: Add refresh due and refresh maintenance function**

In `rightmemory/shared_view_builder.py`, add:

```python
def file_view_refresh_due(memory_root: Path, view_id: str, *, force: bool = False) -> bool:
    if force:
        return True
    recipe = validate_file_view_recipe_source(memory_root, view_id, require_selection=False)
    if recipe.semantic_refresh_days <= 0:
        return False
    current_commit = current_active_memory_commit(memory_root)
    if current_commit == recipe.last_semantic_refresh_memory_commit:
        return False
    if not recipe.last_semantic_refresh_at:
        return True
    refreshed_at = datetime.fromisoformat(recipe.last_semantic_refresh_at)
    return datetime.now(UTC) - refreshed_at >= timedelta(days=recipe.semantic_refresh_days)
```

Add `refresh_file_view`:

```python
def refresh_file_view(memory_root: Path, view_id: str, *, force: bool = False, publish: bool = False) -> str:
    root = Path(memory_root).expanduser()
    clean_view_id = validate_heading_id(view_id)
    old_recipe = validate_file_view_recipe_source(root, clean_view_id, require_selection=False)
    if not file_view_refresh_due(root, clean_view_id, force=force):
        return f"file view {clean_view_id} semantic refresh not due"
    view_dir = root / "shared_views" / clean_view_id
    with TemporaryDirectory() as tempdir:
        backup_dir = Path(tempdir) / clean_view_id
        if view_dir.exists():
            shutil.copytree(view_dir, backup_dir)
        try:
            output = _run_builder(root, clean_view_id, _refresh_message(old_recipe))
            with MemoryWriteLock(root):
                new_recipe = validate_file_view_recipe_source(root, clean_view_id, require_selection=False)
                refreshed = replace(
                    new_recipe,
                    approved=old_recipe.approved,
                    publish_hub_url=old_recipe.publish_hub_url,
                    publish_credential_id=old_recipe.publish_credential_id,
                    last_semantic_refresh_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
                    last_semantic_refresh_memory_commit=current_active_memory_commit(root),
                )
                write_file_view_recipe_from_recipe(root, refreshed)
                if refreshed.render == FILE_VIEW_RENDER_EXTRACTIVE:
                    validate_file_view_recipe_source(root, clean_view_id, require_selection=True)
                    render_file_view(root, clean_view_id)
                _require_nonempty_file_view_context(root, clean_view_id)
                _commit_refresh_if_changed(root, clean_view_id)
            if publish and refreshed.approved and refreshed.publish_hub_url and refreshed.publish_credential_id:
                publish_file_view_package(
                    root,
                    clean_view_id,
                    hub_url=refreshed.publish_hub_url,
                    credential_id=refreshed.publish_credential_id,
                )
            return f"refreshed file view {clean_view_id}\n{output}"
        except BaseException:
            with MemoryWriteLock(root):
                if backup_dir.exists():
                    if view_dir.exists():
                        shutil.rmtree(view_dir)
                    shutil.copytree(backup_dir, view_dir)
            raise
```

Add imports:

```python
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from tempfile import TemporaryDirectory
from .memory_git import current_active_memory_commit
from .session import MemoryWriteLock
from .shared_view_files import FILE_VIEW_RENDER_EXTRACTIVE, publish_file_view_package, write_file_view_recipe_from_recipe
```

In `rightmemory/shared_view_files.py`, add:

```python
def write_file_view_recipe_from_recipe(memory_root: Path, recipe: FileViewRecipe) -> str:
    root = Path(memory_root).expanduser()
    _write_file_view_source(root, recipe)
    return f"wrote file view recipe {recipe.view_id}"
```

In `rightmemory/shared_view_builder.py`, add:

```python
def _refresh_message(recipe: FileViewRecipe) -> str:
    lines = [
        "<shared_view_refresh>",
        "kind: file",
        f"view_id: {recipe.view_id}",
        f"title: {recipe.title}",
        f"intent: {recipe.intent}",
        f"previous_render: {recipe.render}",
    ]
    if recipe.publish_hub_url:
        lines.append(f"publish_hub_url: {recipe.publish_hub_url}")
    if recipe.publish_credential_id:
        lines.append(f"publish_credential_id: {recipe.publish_credential_id}")
    lines.append("</shared_view_refresh>")
    return "\n".join(lines)


def _commit_refresh_if_changed(root: Path, view_id: str) -> None:
    paths = [
        f"shared_views/{view_id}/.gitignore",
        f"shared_views/{view_id}/recipe.toml",
        f"shared_views/{view_id}/view.md",
    ]
    _run_git(root, "add", *paths)
    diff = _run_git(root, "diff", "--cached", "--quiet", check=False)
    if diff.returncode == 0:
        return
    _run_git(root, "commit", "-m", f"shared-view: refresh {view_id}")


def _run_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if check and result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result
```

- [ ] **Step 6: Run refresh tests**

Run:

```bash
rtk python -m unittest tests.test_shared_views.SharedFileViewRecipeTests
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
rtk git add rightmemory/shared_view_builder.py rightmemory/shared_view_files.py tests/test_shared_views.py
rtk git commit -m "feat: add file view semantic refresh"
```

## Task 6: Refresh CLI And Auto-Publish Separation

**Files:**
- Modify: `rightmemory/cli.py`
- Modify: `tests/test_cli.py`
- Modify: `docs/shared-views-usage.md`

- [ ] **Step 1: Write failing CLI test**

Add this CLI test to `CliTests` in `tests/test_cli.py`:

```python
    def test_shared_view_refresh_file_invokes_maintenance_entrypoint(self):
        calls = []

        def fake_refresh(memory_root, view_id, *, force=False, publish=False):
            calls.append((memory_root, view_id, force, publish))
            return "refreshed file view auth-api-files"

        stdout = io.StringIO()
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            with patch("rightmemory.cli.refresh_file_view", side_effect=fake_refresh), patch("sys.stdout", stdout):
                result = main(["--memory-root", str(root), "shared-view", "refresh-file", "auth-api-files", "--force", "--publish"])

        self.assertEqual(result, 0)
        self.assertEqual(calls, [(root, "auth-api-files", True, True)])
        self.assertIn("refreshed file view auth-api-files", stdout.getvalue())
```

- [ ] **Step 2: Run focused CLI test and verify failure**

Run:

```bash
rtk python -m unittest tests.test_cli.CliTests.test_shared_view_refresh_file_invokes_maintenance_entrypoint
```

Expected: FAIL because `refresh-file` is not registered.

- [ ] **Step 3: Add CLI parser and handler**

In `rightmemory/cli.py`, import `refresh_file_view` from `rightmemory.shared_view_builder`.

In `_shared_view_main`, add parser:

```python
    refresh_file = subparsers.add_parser("refresh-file")
    refresh_file.add_argument("view_id")
    refresh_file.add_argument("--force", action="store_true")
    refresh_file.add_argument("--publish", action="store_true")
```

Add handler before `build-question`:

```python
    if args.command == "refresh-file":
        print(refresh_file_view(memory_root, args.view_id, force=args.force, publish=args.publish))
        return 0
```

- [ ] **Step 4: Document the command**

In `docs/shared-views-usage.md`, after the auto-publish paragraph, add:

```markdown
Semantic refresh is separate from ordinary auto-publish. To rerun the builder
from the stored refined intent:

```bash
rightmemory --profile alice shared-view refresh-file auth-api-files --force
```

Use `--publish` when the refresh should publish after a successful rebuild.
Normal auto-publish never starts the builder agent; it only publishes the
current approved package.
```
```

- [ ] **Step 5: Run CLI and docs-related focused tests**

Run:

```bash
rtk python -m unittest tests.test_cli.CliTests.test_shared_view_refresh_file_invokes_maintenance_entrypoint
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
rtk git add rightmemory/cli.py tests/test_cli.py docs/shared-views-usage.md
rtk git commit -m "feat: add file view refresh command"
```

## Task 7: Full Verification

**Files:**
- No planned file changes. If a verification step fails, return to the task that introduced the failing behavior and fix it there before continuing.

- [ ] **Step 1: Search for stale tool and render names**

Run:

```bash
rtk rg -n "create_file_view_recipe|expanded-heading-subtrees" rightmemory tests docs
```

Expected: matches only in historical design/plan files or explicit rejection tests. No runtime prompt or implementation should still instruct the model to call `create_file_view_recipe`.

Then run:

```bash
rtk rg -n "write_file_view_recipe" rightmemory tests
```

Expected: no matches. The old internal writer name should be gone from runtime code and tests, not preserved as an alias.

- [ ] **Step 2: Run focused shared-view and pruner suites**

Run:

```bash
rtk python -m unittest tests.test_shared_views tests.test_tools tests.test_config tests.test_prune
```

Expected: PASS.

- [ ] **Step 3: Run compile check**

Run:

```bash
rtk python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 4: Run full test suite**

Run:

```bash
rtk python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 5: Confirm clean working tree**

Run:

```bash
rtk git status --short
```

Expected: no tracked changes. Unrelated untracked files that existed before implementation may remain untracked.

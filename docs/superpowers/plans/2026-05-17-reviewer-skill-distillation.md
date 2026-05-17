# Reviewer Skill Distillation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add reviewer support for distilling skill-shaped reusable knowledge into memory-backed skill topics and committed support artifacts.

**Architecture:** Keep installed host-agent skills out of scope. Extend RightMemory's memory-root surfaces so reviewer roles can write `MEMORY*.md` and `skill_artifacts/...`, validate the memory graph, and use git commits as the audit trail. Prompt changes teach the reviewer how to choose between ordinary memory, memory-backed skill edits, support artifacts, and no edit.

**Tech Stack:** Python standard library, `unittest`, Bash installer, Markdown prompts/docs.

---

## File Structure

- Modify `rightmemory/tools.py`: add `skill_artifacts/<slug>/...` to the git allowlist, add optional commit body support, and add a narrow `git_discard()` write tool for reviewer-owned paths.
- Modify `rightmemory/runtime.py`: expose `git_discard` to write-capable roles.
- Modify `rightmemory/session.py`: update generated memory-root `.gitignore`.
- Modify `install.sh`: update installer-created memory-root `.gitignore`.
- Modify `rightmemory/review.py`: update scanner message text so each review turn names skill distillation as part of the task.
- Modify `rightmemory/prompts/reviewer.md`: add memory-backed skill and dirty-state recovery guidance.
- Modify `rightmemory/prompt.py`: update generic write-tool guidance to include skill artifacts and discard recovery.
- Modify `MEMORY.example.md`: add a compact skill-creation memory-backed skill example.
- Modify `README.md` and `DESIGN_NOTES.md`: document skill artifacts and reviewer commit boundaries.
- Modify tests in `tests/test_tools.py`, `tests/test_config.py`, `tests/test_install.py`, and `tests/test_review.py`.

---

### Task 1: Commit Tooling For Skill Artifacts

**Files:**
- Modify: `rightmemory/tools.py`
- Modify: `rightmemory/runtime.py`
- Test: `tests/test_tools.py`

- [ ] **Step 1: Write failing tests for staging skill artifacts, commit bodies, and discard**

Append these test methods to `MemoryToolsTests` in `tests/test_tools.py`, near the existing git tests:

```python
    def test_git_add_accepts_skill_artifacts(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        (self.root / "skill_artifacts" / "loose.md").parent.mkdir(exist_ok=True)
        (self.root / "skill_artifacts" / "loose.md").write_text("not slug scoped\n", encoding="utf-8")

        result = self.tools.git_add(["skill_artifacts/skill-creator/references/authoring.md"])

        self.assertEqual(result, "staged: skill_artifacts/skill-creator/references/authoring.md")
        self.assertIn("A  skill_artifacts/skill-creator/references/authoring.md", self.tools.git_status())
        with self.assertRaises(ValueError):
            self.tools.git_add(["skill_artifacts/loose.md"])

    def test_git_commit_accepts_optional_body(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        self.tools.git_add(["MEMORY.md"])

        result = self.tools.git_commit(
            "memory: review codex transcript s1",
            body="Distilled skill signal: skill authoring guidance belongs in memory-backed skills.",
        )
        log = self._git("log", "-1", "--format=%B")

        self.assertIn("committed", result)
        self.assertIn("memory: review codex transcript s1", log)
        self.assertIn("Distilled skill signal", log)

    def test_git_discard_reverts_allowed_tracked_changes(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        memory = self.root / "MEMORY.md"
        memory.write_text("# Domain\n", encoding="utf-8")
        artifact = self.root / "skill_artifacts" / "skill-creator" / "references" / "authoring.md"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("# Skill authoring\n", encoding="utf-8")
        self._git("add", "MEMORY.md", "skill_artifacts/skill-creator/references/authoring.md")
        self._git("commit", "-m", "initial memory")
        memory.write_text("# Broken\n", encoding="utf-8")
        artifact.write_text("# Broken\n", encoding="utf-8")

        result = self.tools.git_discard([
            "MEMORY.md",
            "skill_artifacts/skill-creator/references/authoring.md",
        ])

        self.assertEqual(result, "discarded: MEMORY.md, skill_artifacts/skill-creator/references/authoring.md")
        self.assertEqual(memory.read_text(encoding="utf-8"), "# Domain\n")
        self.assertEqual(artifact.read_text(encoding="utf-8"), "# Skill authoring\n")
        self.assertEqual(self.tools.git_status(), "")

    def test_git_discard_rejects_non_memory_paths(self):
        self._git("init")
        (self.root / "rightmemory.toml").write_text("[review]\n", encoding="utf-8")

        with self.assertRaises(ValueError):
            self.tools.git_discard(["rightmemory.toml"])
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_git_add_accepts_skill_artifacts tests.test_tools.MemoryToolsTests.test_git_commit_accepts_optional_body tests.test_tools.MemoryToolsTests.test_git_discard_reverts_allowed_tracked_changes tests.test_tools.MemoryToolsTests.test_git_discard_rejects_non_memory_paths
```

Expected: failures because `skill_artifacts/...`, `body=`, and `git_discard` are not implemented.

- [ ] **Step 3: Implement commit allowlist, optional body, and discard**

In `rightmemory/tools.py`, add a regex next to `DREAM_LOG_FILE_RE`:

```python
SKILL_ARTIFACT_RE = re.compile(r"^skill_artifacts/[A-Za-z0-9_.-]+/.+")
```

Change `git_commit` and add `git_discard`:

```python
    def git_commit(self, message: str, body: str | None = None) -> str:
        """Commit staged memory, dream log, and skill artifact files under the RightMemory root."""
        message = self._validate_commit_subject(message)
        body = self._validate_commit_body(body)
        staged = self._run_git(["git", "diff", "--cached", "--name-only", "--"])
        staged_files = [line for line in staged.splitlines() if line]
        if not staged_files:
            raise ValueError("no staged changes to commit")
        for path in staged_files:
            self._allowed_commit_path(path)

        command = ["git", "commit", "-m", message]
        if body is not None:
            command.extend(["-m", body])
        self._run_git(command)
        commit_hash = self._run_git(["git", "rev-parse", "--short", "HEAD"])
        status = self.git_status()
        if status:
            return f"committed {commit_hash}: {message}\n{status}"
        return f"committed {commit_hash}: {message}"

    def git_discard(self, paths: list[str]) -> str:
        """Discard tracked changes in selected memory, dream log, or skill artifact files."""
        if not paths:
            raise ValueError("paths must not be empty")
        relative_paths = [self._allowed_commit_path(path) for path in paths]
        self._run_git(["git", "checkout", "--", *relative_paths])
        return "discarded: " + ", ".join(relative_paths)
```

Replace `_allowed_commit_path()` and `_validate_commit_message()` with:

```python
    def _allowed_commit_path(self, path: str) -> str:
        resolved = self._resolve_path(path)
        relative_path = resolved.relative_to(self.memory_root).as_posix()
        if relative_path == "MEMORY.md":
            return relative_path
        if MEMORY_DETAIL_FILE_RE.fullmatch(relative_path):
            return relative_path
        if DREAM_LOG_FILE_RE.fullmatch(relative_path):
            return relative_path
        if SKILL_ARTIFACT_RE.fullmatch(relative_path):
            return relative_path
        raise ValueError(
            "can only stage, commit, or discard MEMORY.md, MEMORY_*.md, "
            f"dream_logs/*.md, or skill_artifacts/<slug>/...: {relative_path}"
        )

    def _validate_commit_subject(self, message: str) -> str:
        message = message.strip()
        if not message:
            raise ValueError("commit message must not be empty")
        lines = message.splitlines()
        if len(lines) != 1:
            raise ValueError("commit subject must be a single line")
        if len(message) > COMMIT_MESSAGE_LINE_LIMIT:
            raise ValueError(f"commit subject must be <= {COMMIT_MESSAGE_LINE_LIMIT} characters")
        return message

    def _validate_commit_body(self, body: str | None) -> str | None:
        if body is None:
            return None
        body = body.strip()
        if not body:
            return None
        if "\x00" in body:
            raise ValueError("commit body must not contain NUL bytes")
        return body
```

In `rightmemory/runtime.py`, add `git_discard` to `write_tools` immediately after `git_diff`:

```python
            self._agent_tool(self.tools.git_discard),
```

- [ ] **Step 4: Run the task tests and verify they pass**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_git_add_accepts_skill_artifacts tests.test_tools.MemoryToolsTests.test_git_commit_accepts_optional_body tests.test_tools.MemoryToolsTests.test_git_discard_reverts_allowed_tracked_changes tests.test_tools.MemoryToolsTests.test_git_discard_rejects_non_memory_paths
```

Expected: all four tests pass.

- [ ] **Step 5: Commit Task 1**

Run:

```bash
git add rightmemory/tools.py rightmemory/runtime.py tests/test_tools.py
git commit -m "feat: allow reviewer skill artifacts"
```

---

### Task 2: Memory Root Gitignore And Tool Guidance

**Files:**
- Modify: `rightmemory/session.py`
- Modify: `install.sh`
- Modify: `rightmemory/prompt.py`
- Test: `tests/test_config.py`
- Test: `tests/test_install.py`

- [ ] **Step 1: Write failing tests for generated gitignore and runtime tools**

In `tests/test_config.py`, update `test_write_role_creates_memory_lock_and_gitignore` expected text to:

```python
            "*\n"
            "!MEMORY.md\n"
            "!MEMORY_*.md\n"
            "!dream_logs/\n"
            "!dream_logs/*.md\n"
            "!skill_artifacts/\n"
            "!skill_artifacts/**\n",
```

Add an assertion to the write-role tool test near the existing `git_commit` assertion:

```python
        self.assertIn("git_discard", tool_names)
```

In `tests/test_install.py`, add this assertion to `test_initial_install_copies_managed_example` after reading `MEMORY.md`:

```python
            gitignore = (memory_root / ".gitignore").read_text(encoding="utf-8")
```

and after the temporary directory exits:

```python
        self.assertIn("!skill_artifacts/", gitignore)
        self.assertIn("!skill_artifacts/**", gitignore)
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run:

```bash
python -m unittest tests.test_config tests.test_install.InstallScriptTests.test_initial_install_copies_managed_example
```

Expected: failures because `skill_artifacts` is missing from generated `.gitignore`, and `git_discard` is not yet exposed if Task 1 has not been applied.

- [ ] **Step 3: Update generated gitignore content**

In `rightmemory/session.py`, replace `_ensure_memory_gitignore()` content with:

```python
        memory_root,
        (
            b"*\n"
            b"!MEMORY.md\n"
            b"!MEMORY_*.md\n"
            b"!dream_logs/\n"
            b"!dream_logs/*.md\n"
            b"!skill_artifacts/\n"
            b"!skill_artifacts/**\n"
        ),
```

In `install.sh`, replace the installer `.gitignore` block with:

```bash
*
!MEMORY.md
!MEMORY_*.md
!dream_logs/
!dream_logs/*.md
!skill_artifacts/
!skill_artifacts/**
```

- [ ] **Step 4: Update generic write-tool prompt guidance**

In `rightmemory/prompt.py`, update the write-tool guidance so the write tools paragraph includes discard:

```python
        "edits, file creation, file deletion, file renames, git inspection, git discard for reviewer-owned "
        "dirty tracked files, validation.\n"
```

Update the commit-tool paragraph to:

```python
        "- Commit tools may stage and commit `MEMORY.md`, `MEMORY_*.md`, `dream_logs/*.md`, and "
        "`skill_artifacts/<slug>/...`; ignore unrelated untracked files unless the caller explicitly asks "
        "about them.\n"
```

Add a discard sentence after that paragraph:

```python
        "- `git_discard(paths)` is destructive and should be used only for invalid, partial, or unsafe tracked "
        "changes in reviewer-owned memory or skill artifact paths after inspecting the diff.\n"
```

- [ ] **Step 5: Run the focused tests and verify they pass**

Run:

```bash
python -m unittest tests.test_config tests.test_install.InstallScriptTests.test_initial_install_copies_managed_example
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 2**

Run:

```bash
git add rightmemory/session.py rightmemory/prompt.py install.sh tests/test_config.py tests/test_install.py
git commit -m "feat: surface skill artifacts in memory roots"
```

---

### Task 3: Reviewer Prompt And Review Message

**Files:**
- Modify: `rightmemory/review.py`
- Modify: `rightmemory/prompts/reviewer.md`
- Test: `tests/test_review.py`

- [ ] **Step 1: Write failing review-message test**

Add this test to `ReviewScannerTests` in `tests/test_review.py`:

```python
    def test_review_message_includes_skill_distillation_guidance(self):
        calls = []
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            source = root / "codex"
            source.mkdir()
            transcript = source / "session.jsonl"
            self._write_codex(transcript, turns=[("use this debugging trick later", "noted")])
            self._set_mtime(transcript, 1_000)
            scanner = ReviewScanner(
                ReviewConfig(
                    memory_root=root,
                    idle_seconds=3600,
                    sources=[ReviewSourceConfig(kind="codex", path=source)],
                ),
                lambda session_id, message: calls.append(message) or "ok",
            )

            scanner.scan_once(now=10_000)

        self.assertIn("memory-backed skill", calls[0])
        self.assertIn("skill_artifacts", calls[0])
        self.assertIn("Normalized session JSON:", calls[0])
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
python -m unittest tests.test_review.ReviewScannerTests.test_review_message_includes_skill_distillation_guidance
```

Expected: failure because `_review_message()` does not yet mention skill distillation.

- [ ] **Step 3: Update `_review_message()`**

In `rightmemory/review.py`, replace `_review_message()` with:

```python
def _review_message(session: NormalizedSession) -> str:
    return (
        "Review this normalized provider transcript session.\n\n"
        "Review the whole session for durable memory and memory-backed skill knowledge. "
        "If nothing is worth saving, reply exactly: Nothing to save.\n\n"
        "Skill-shaped lessons may become ordinary memory, a refinement to an existing "
        "memory-backed skill, a new memory-backed skill, or support files under "
        "`skill_artifacts/<slug>/...`, depending on where the lesson is most coherent.\n\n"
        "Normalized session JSON:\n"
        + json.dumps(session.to_payload(), ensure_ascii=False, indent=2)
    )
```

- [ ] **Step 4: Rewrite reviewer prompt sections coherently**

Edit `rightmemory/prompts/reviewer.md` so these concepts are integrated into the existing sections:

```md
## Sources And Schema

- The source of truth is the memory file set: `MEMORY.md` plus any sibling `MEMORY_*.md` files.
- Memory-backed skill support material may live under `skill_artifacts/<slug>/...` when dense notes, scripts, templates, or assets would make the memory file too bulky.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.
- Do not touch the `# User Pending Task and Thoughts` section.
```

Add this section after `## What To Save Or Revise`:

```md
## Skill Distillation

Treat reusable procedural knowledge as a first-class memory shape. A memory-backed skill is a normal RightMemory topic whose content helps future agents perform a class of work. It can live under any `#` domain where the tree makes retrieval natural, and it may use a file-backed heading such as `{F#skill-slug}` when the topic needs a detail file.

Choose the edit shape by coherence. The same transcript signal might be an ordinary memory fact, a refinement to an existing memory-backed skill, a support artifact under `skill_artifacts/<slug>/...`, a new memory-backed skill, or no edit. Put the lesson where a future agent would naturally retrieve and apply it without bloating or fragmenting the graph.

Use support artifacts when the reusable material is too detailed, executable, or copy-oriented for the memory-backed skill body. Reference notes suit dense background and reproduction notes; scripts suit repeatable checks or probes; templates/assets suit reusable starting material. Do not create artifact folders as a checklist.

User corrections can belong in both memory and skill knowledge. Memory captures the user's preference or expectation; a memory-backed skill captures how to do a class of work differently next time.
```

Replace the current dirty edit-safety bullets with:

```md
- Before your first write, check git status for tracked reviewer-owned files: `MEMORY.md`, `MEMORY_*.md`, and `skill_artifacts/<slug>/...`.
- The shared memory write lock should prevent overlapping writes. If reviewer-owned tracked files are dirty anyway, inspect the diff and resolve that state before reviewing the transcript.
- If the dirty changes are coherent and valid, stage and commit them as a separate baseline commit based on the diff, then continue.
- If the dirty changes are invalid, partial, or unsafe to preserve, use `git_discard` on those reviewer-owned paths, then continue.
- Do not mix pre-existing dirty changes with the current transcript review commit.
```

Update final commit guidance:

```md
- If you changed memory or skill artifacts, stage only touched `MEMORY.md`, `MEMORY_*.md`, and `skill_artifacts/<slug>/...` files and commit them. Use `memory: review <source> transcript <session_id>` when source and session id are known. Use the commit body when it helps record the distilled skill signal or uncertainty.
```

- [ ] **Step 5: Run review tests**

Run:

```bash
python -m unittest tests.test_review
```

Expected: all tests pass.

- [ ] **Step 6: Commit Task 3**

Run:

```bash
git add rightmemory/review.py rightmemory/prompts/reviewer.md tests/test_review.py
git commit -m "feat: teach reviewer skill distillation"
```

---

### Task 4: Starter Memory-Backed Skill Example

**Files:**
- Modify: `MEMORY.example.md`
- Test: `tests/test_install.py`

- [ ] **Step 1: Write failing starter-example test**

Add this test to `InstallScriptTests` in `tests/test_install.py`:

```python
    def test_memory_example_includes_skill_creation_guidance(self):
        example = (REPO_ROOT / "MEMORY.example.md").read_text(encoding="utf-8")

        self.assertIn("Skill Creation Guidance {F#sample-skill-creation-guidance}", example)
        self.assertIn("skill_artifacts/sample-skill-creation-guidance/", example)
        self.assertIn("class-level", example)
        self.assertIn("support material", example)
```

- [ ] **Step 2: Run the new test and verify it fails**

Run:

```bash
python -m unittest tests.test_install.InstallScriptTests.test_memory_example_includes_skill_creation_guidance
```

Expected: failure because the example does not yet include skill creation guidance.

- [ ] **Step 3: Add the managed starter example**

In `MEMORY.example.md`, inside the `# Cross-Session Agent Behavior — Example Domain` block after `## User and Workflow Preferences`, add:

```md
## Memory-Backed Skills {#sample-memory-backed-skills}

Use memory-backed skills for reusable procedural knowledge that should guide future agents across sessions. Place each skill-like topic where the tree makes it easiest to retrieve; this example uses cross-session agent behavior because skill creation is an agent workflow.

### Skill Creation Guidance {F#sample-skill-creation-guidance}

This file-backed topic is an example of a compact skill guide. In a real memory root, `MEMORY_sample-skill-creation-guidance.md` would describe when to create or update a skill, how to keep guidance concise, how to choose an appropriate degree of freedom, and how to validate the result.

- `sample-skill-class-level` Create or refine skills at the class-of-work level so future agents find reusable guidance by intent, not by one session's exact error or task name. → []
- `sample-skill-support-material` Use `skill_artifacts/sample-skill-creation-guidance/` for support material when dense references, repeatable scripts, or templates would make the memory-backed skill body too bulky. → [doc:sample-skill-creation-guidance]
- `sample-skill-validate` Validate skill changes with the relevant local checks or a realistic forward test before treating the skill as ready. → [ver:sample-skill-creation-guidance]
```

- [ ] **Step 4: Run install tests**

Run:

```bash
python -m unittest tests.test_install
```

Expected: all install tests pass.

- [ ] **Step 5: Commit Task 4**

Run:

```bash
git add MEMORY.example.md tests/test_install.py
git commit -m "docs: add memory-backed skill example"
```

---

### Task 5: Documentation Alignment

**Files:**
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`

- [ ] **Step 1: Update README feature list and runtime description**

In `README.md`, add this bullet under `What It Gives You`:

```md
- Memory-backed skill topics and optional `skill_artifacts/<slug>/...` support material for reusable procedural knowledge.
```

Update the file-layout blocks so installed memory roots show:

```text
├── skill_artifacts/
```

Update the standalone runtime bullet about `.gitignore` to:

```md
- The installer creates a root `.gitignore` allowlist so git status surfaces `MEMORY.md`, `MEMORY_*.md`, `dream_logs/*.md`, and `skill_artifacts/<slug>/...`; existing user `.gitignore` files are preserved.
```

Add a short paragraph to `Automatic Transcript Review`:

```md
Reviewer can also distill reusable procedural lessons into memory-backed skill topics. These are normal RightMemory headings and detail files, with optional support material under `skill_artifacts/<slug>/...`; installed Codex or Claude skill folders are not edited by reviewer.
```

- [ ] **Step 2: Update design notes**

In `DESIGN_NOTES.md`, replace the standalone commit boundary paragraph with:

```md
Standalone commit tools may stage and commit `MEMORY.md`, `MEMORY_*.md`, `dream_logs/*.md`, and `skill_artifacts/<slug>/...` because update, dreamer, and reviewer roles need to preserve memory edits, dream reports, and memory-backed skill support material without gaining arbitrary repository-write authority. The retrieve role does not receive write or git tools at all, so retrieval remains a lower-authority fast path. Unrelated untracked files remain visible through status but outside the stage/commit allowlist so model-driven commits do not sweep up local config, backups, or test artifacts.
```

Add a new note after `Structural clarity over node count`:

```md
### Memory-backed skills

Reusable procedural knowledge can live as memory-backed skills: normal RightMemory topics, optionally file-backed, whose detail files read like compact guides for a class of work. Support material belongs under `skill_artifacts/<slug>/...` when references, scripts, templates, or assets would bloat the memory file. Reviewer chooses ordinary memory, skill refinement, support artifact, new skill topic, or no edit according to where future agents would most naturally retrieve and apply the lesson.
```

- [ ] **Step 3: Run docs-adjacent tests**

Run:

```bash
python -m unittest tests.test_config tests.test_install
```

Expected: all tests pass.

- [ ] **Step 4: Commit Task 5**

Run:

```bash
git add README.md DESIGN_NOTES.md
git commit -m "docs: document memory-backed skill artifacts"
```

---

### Task 6: Full Verification

**Files:**
- No source edits expected.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run full unit test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: all tests pass.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git status --short
git diff --stat HEAD
```

Expected: working tree contains only intended changes for the current task, or is clean if all task commits were made.

- [ ] **Step 4: Final commit if any verification fixes were needed**

If verification required small fixes, commit them:

```bash
git add rightmemory tests README.md DESIGN_NOTES.md MEMORY.example.md install.sh
git commit -m "fix: align reviewer skill distillation"
```

If no fixes were needed, skip this commit.

---

## Self-Review

Spec coverage:
- Memory-backed skills: Task 3 and Task 4.
- Skill artifacts: Task 1, Task 2, Task 5.
- Git history as audit trail with optional commit body: Task 1 and Task 3.
- Dirty-state recovery with baseline commit or discard: Task 1 and Task 3.
- Starter skill-creation example: Task 4.
- Installed skill folders out of scope: Task 3 and Task 5.

Marker scan:
- No incomplete markers are present.
- Code snippets define the functions and tests they reference.

Type consistency:
- `git_commit(message: str, body: str | None = None)` is used consistently in tests and implementation.
- `git_discard(paths: list[str])` is used consistently in runtime exposure, prompt guidance, and tests.

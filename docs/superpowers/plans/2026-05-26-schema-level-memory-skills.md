# Schema-Level Memory Skills Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `S#` schema-level memory skills backed by `MEMORY_SKILL_<slug>.md` files and progressive retrieval behavior.

**Architecture:** Reuse the existing memory file model. `S#` is a new addressable heading marker parsed by the same graph validator as `#` and `F#`, while skill files remain ordinary `MEMORY_*.md` files from the tooling point of view. Role prompts define the semantic distinction: ordinary memory records facts/context/preferences; skills are reusable instruction assets read on demand.

**Tech Stack:** Python 3.11 standard library, `unittest`, Markdown role/schema prompts, existing RightMemory tool validation and semantic upgrade machinery.

---

## File Structure

- Modify `rightmemory/tools.py`: recognize `S#` headings and allow `####` skill pointers.
- Modify `skills/rightmemory-schema.md`: document `S#`, id behavior, file mapping, and flexible skill-file shape.
- Modify `rightmemory/prompts/retrieve.md`: add progressive skill disclosure behavior.
- Modify `rightmemory/prompts/update.md`: let explicit update requests create/refine skill memory.
- Modify `rightmemory/prompts/reviewer.md`: let automatic review create/refine high-confidence skills.
- Modify `rightmemory/prompts/dreamer.md`: let consolidation convert strong instruction-like memory into skills.
- Create `rightmemory/semantic_upgrades/2026-05-26-schema-level-memory-skills.md`: note for dreamer to revisit existing instruction-like memory.
- Modify `tests/test_tools.py`: schema/parser and path-coverage tests.
- Modify `tests/test_config.py`: prompt assembly tests.
- Modify `tests/test_semantic_upgrades.py`: packaged upgrade-note test.

## Task 1: Parse And Validate `S#` Headings

**Files:**
- Modify: `tests/test_tools.py`
- Modify: `rightmemory/tools.py`

- [ ] **Step 1: Add failing validation tests**

Add these tests near the existing file-backed heading validation tests in `tests/test_tools.py`:

```python
    def test_validate_memory_accepts_skill_heading_marker(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "## Two-Side Review {S#two-side-review} → [rel:domain]\n\n"
            "A reusable instruction asset for opposing review passes.\n\n"
            "- `review-signal` Use the skill when the user asks for two-side review. → [rel:two-side-review]\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text(
            "# Two-Side Review\n\nRun support and risk passes, then summarize.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)

    def test_validate_memory_allows_four_hash_skill_pointer(self):
        (self.root / "MEMORY.md").write_text(
            "# Domain {#domain}\n\n"
            "### Agent Behavior\n\n"
            "#### Two-Side Review {S#two-side-review}\n\n"
            "A compact skill description can live under the pointer.\n\n"
            "---\n\n"
            "### Other Topic\n",
            encoding="utf-8",
        )
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text(
            "# Two-Side Review\n\nRun support and risk passes, then summarize.\n",
            encoding="utf-8",
        )

        result = self.tools.validate_memory()

        self.assertIn("validation passed", result)
```

- [ ] **Step 2: Run tests to verify current failure**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_validate_memory_accepts_skill_heading_marker tests.test_tools.MemoryToolsTests.test_validate_memory_allows_four_hash_skill_pointer
```

Expected: FAIL. The first test reports a dangling edge to `two-side-review`; the second reports that a `####` pointer must use `{F#slug}`.

- [ ] **Step 3: Update parser regexes and pointer-kind validation**

In `rightmemory/tools.py`, replace the heading marker regex constants with:

```python
ANCHOR_RE = re.compile(r"^(#{1,4})\s+.*?\{(?:F#|S#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
ANCHOR_KIND_RE = re.compile(r"^(#{1,})\s+.*?\{(F#|S#|#)([A-Za-z0-9_.-]+)\}(?:\s*→\s*\[(.*?)\])?")
POINTER_HEADING_KINDS = {"F#", "S#"}
```

Then update the `depth == 4` branch in `_structure_errors()` to use the shared pointer-kind set:

```python
                elif depth == 4:
                    anchor_match = ANCHOR_KIND_RE.match(line)
                    if anchor_match is None or anchor_match.group(2) not in POINTER_HEADING_KINDS:
                        errors.append(
                            f"#### pointer must use `{{F#slug}}` or `{{S#slug}}` at {relative_path}:{line_number}"
                        )
                    if parent_depth != 3:
                        errors.append(f"#### pointer must be under a ### heading at {relative_path}:{line_number}")
                    if anchor_match is not None and anchor_match.group(2) in POINTER_HEADING_KINDS:
                        active_pointer = (depth, line_number)
```

- [ ] **Step 4: Run the focused tests**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_validate_memory_accepts_skill_heading_marker tests.test_tools.MemoryToolsTests.test_validate_memory_allows_four_hash_skill_pointer
```

Expected: PASS.

- [ ] **Step 5: Run the full tool tests**

Run:

```bash
python -m unittest tests.test_tools
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add rightmemory/tools.py tests/test_tools.py
git commit -m "feat: parse memory skill headings"
```

## Task 2: Verify Existing Memory Path Coverage

**Files:**
- Modify: `tests/test_tools.py`

- [ ] **Step 1: Add a characterization test for skill-file staging**

Add this test near `test_git_add_accepts_memory_files_and_dream_logs` in `tests/test_tools.py`:

```python
    def test_git_add_accepts_memory_skill_files(self):
        self._git("init")
        self._git("config", "user.email", "test@example.com")
        self._git("config", "user.name", "Test User")
        (self.root / "MEMORY.md").write_text("# Domain\n", encoding="utf-8")
        (self.root / "MEMORY_SKILL_two-side-review.md").write_text("# Two-Side Review\n", encoding="utf-8")

        result = self.tools.git_add(["MEMORY.md", "MEMORY_SKILL_two-side-review.md"])

        self.assertEqual(result, "staged: MEMORY.md, MEMORY_SKILL_two-side-review.md")
        status = self.tools.git_status()
        self.assertIn("A  MEMORY.md", status)
        self.assertIn("A  MEMORY_SKILL_two-side-review.md", status)
```

- [ ] **Step 2: Run the characterization test**

Run:

```bash
python -m unittest tests.test_tools.MemoryToolsTests.test_git_add_accepts_memory_skill_files
```

Expected: PASS before code changes. This confirms the existing `MEMORY_*.md` allowlist already covers skill files.

- [ ] **Step 3: Inspect the path constants**

Run:

```bash
rg -n "MEMORY_\\*\\.md|MEMORY_DETAIL_FILE_RE|MEMORY_WRITE_PATHS|MEMORY_SYNC_PATHS|!MEMORY_\\*.md" rightmemory tests install.sh
```

Expected: The relevant write/sync/install paths continue to use the broad `MEMORY_*.md` pattern. Do not add a narrower skill-specific allowlist unless a test shows a real gap.

- [ ] **Step 4: Commit**

```bash
git add tests/test_tools.py
git commit -m "test: cover memory skill file staging"
```

## Task 3: Update Schema And Role Prompts

**Files:**
- Modify: `skills/rightmemory-schema.md`
- Modify: `rightmemory/prompts/retrieve.md`
- Modify: `rightmemory/prompts/update.md`
- Modify: `rightmemory/prompts/reviewer.md`
- Modify: `rightmemory/prompts/dreamer.md`
- Modify: `tests/test_config.py`

- [ ] **Step 1: Add failing prompt assembly tests**

Add this test near the existing prompt/instruction tests in `tests/test_config.py`:

```python
    def test_schema_level_memory_skill_guidance_is_in_role_prompts(self):
        retrieve_instructions = build_instructions(Path("/memory"), "retrieve")
        self.assertIn("S#", retrieve_instructions)
        self.assertIn("MEMORY_SKILL_<slug>.md", retrieve_instructions)
        self.assertIn("progressive disclosure", retrieve_instructions)

        for role in ("update", "reviewer", "dreamer"):
            with self.subTest(role=role):
                instructions = build_instructions(Path("/memory"), role)
                self.assertIn("reusable instruction asset", instructions)
                self.assertIn("ordinary memory", instructions)
                self.assertIn("rigid", instructions)
```

- [ ] **Step 2: Run the prompt test to verify current failure**

Run:

```bash
python -m unittest tests.test_config.PromptTests.test_schema_level_memory_skill_guidance_is_in_role_prompts
```

Expected: FAIL because the schema and role prompts do not yet mention `S#` skill semantics.

- [ ] **Step 3: Update the schema**

In `skills/rightmemory-schema.md`, update the addressable heading examples to include:

```md
### Skill Title {S#heading-id} → [edge1, edge2, ...]
```

Revise the marker explanation so it says:

```md
- `F#` marks a heading as backed by an ordinary detail file. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:F#heading-id`.
- `S#` marks a heading as backed by a memory skill file. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:S#heading-id`.
- `S#heading-id` maps to sibling skill file `MEMORY_SKILL_heading-id.md`.
```

Add a compact memory-skills section after the memory-entry shape section:

```md
## Memory Skills

A memory skill is a reusable instruction asset. It can be a workflow, a judgment playbook, a prompt-shaped instruction the user would otherwise repeat, or a bounded operating style for a recurring situation.

Ordinary memory records durable facts, context, and preferences. Skill memory tells a future agent how to act when the relevant situation comes up.

Use an `S#` heading when the heading body can give a compact description and the full instruction belongs in `MEMORY_SKILL_<slug>.md`. The skill file should be clear enough for an agent to apply after reading it, but it should not follow a rigid section template.
```

Update the heading rules so `S#` mirrors file-backed heading behavior:

```md
- A skill-backed `#`, `##`, or `###` heading uses `{S#short-slug}` and maps to sibling skill file `MEMORY_SKILL_<short-slug>.md`.
- `#### Human Title {S#short-slug}` is also allowed as a skill pointer under an existing `###` topic.
```

- [ ] **Step 4: Update retrieve prompt**

Add this section to `rightmemory/prompts/retrieve.md` after `## Recent Submitted Memory`:

```md
## Memory Skills

`S#` headings are memory skills: reusable instruction assets backed by `MEMORY_SKILL_<slug>.md`.

Use progressive disclosure. During broad retrieval, return strongly relevant `S#` heading lines and direct body paragraphs so the caller can decide whether the skill applies. When the caller asks to see, use, or retrieve a specific skill, open `MEMORY_SKILL_<slug>.md` and return the skill body.

Do not dump full skill files during broad recall unless the caller specifically asks for that skill's contents.
```

- [ ] **Step 5: Update update prompt**

Add this section to `rightmemory/prompts/update.md` after `## Candidate Handling And Alignment`:

```md
## Memory Skills

An `S#` memory skill is a reusable instruction asset. Ordinary memory records durable facts, context, and preferences; skill memory tells a future agent how to act when the relevant situation comes up.

Create or refine an `S#` skill when an update request describes a reusable workflow, judgment playbook, recurring prompt-shaped instruction, or bounded operating style that future agents should apply. Keep weak or one-off signals as ordinary memory or uncertain memory.

Skill files live at `MEMORY_SKILL_<slug>.md`. They should contain enough guidance for an agent to apply the skill after reading it, without forcing a rigid section template.
```

- [ ] **Step 6: Update reviewer prompt**

Add this section to `rightmemory/prompts/reviewer.md` after `## What To Save Or Revise`:

```md
## Memory Skills

Automatic review may create or refine `S#` memory skills when the transcript evidence supports a reusable instruction asset. A skill can capture a workflow, judgment playbook, recurring prompt-shaped instruction, or bounded operating style.

Use the governing distinction: ordinary memory records what is true or preferred; skill memory tells future agents how to act. Strong candidates have a recognizable trigger, stable enough input shape, useful action or judgment guidance, and a clear output or stopping condition.

Do not create broad, speculative, overlapping, or template-shaped skills from weak evidence. When evidence is useful but unsettled, prefer ordinary `Uncertain:` memory.
```

- [ ] **Step 7: Update dreamer prompt**

Add this section to `rightmemory/prompts/dreamer.md` after `## Cleanup And Restructure`:

```md
## Memory Skills

During consolidation, consider whether existing memory describes a recurring way an agent should act but lacks enough instruction to apply it. Strong instruction-like or prompt-like memories may become `S#` memory skills backed by `MEMORY_SKILL_<slug>.md`.

Preserve ordinary memory for facts, context, and preferences. Use skills for reusable agent instructions. Keep the skill file flexible and practical; do not impose a rigid section template.
```

- [ ] **Step 8: Run the prompt test**

Run:

```bash
python -m unittest tests.test_config.PromptTests.test_schema_level_memory_skill_guidance_is_in_role_prompts
```

Expected: PASS.

- [ ] **Step 9: Run schema/prompt-adjacent tests**

Run:

```bash
python -m unittest tests.test_config tests.test_tools
```

Expected: PASS.

- [ ] **Step 10: Commit**

```bash
git add skills/rightmemory-schema.md rightmemory/prompts/retrieve.md rightmemory/prompts/update.md rightmemory/prompts/reviewer.md rightmemory/prompts/dreamer.md tests/test_config.py
git commit -m "docs: teach roles memory skill semantics"
```

## Task 4: Add Semantic Upgrade Note

**Files:**
- Create: `rightmemory/semantic_upgrades/2026-05-26-schema-level-memory-skills.md`
- Modify: `tests/test_semantic_upgrades.py`

- [ ] **Step 1: Add failing packaged-note assertions**

In `tests/test_semantic_upgrades.py`, update `test_load_packaged_notes_includes_current_notes` so it includes:

```python
        self.assertIn("schema-level-memory-skills", notes_by_id)
        self.assertIn("S#slug", notes_by_id["schema-level-memory-skills"].body)
        self.assertIn("reusable instruction assets", notes_by_id["schema-level-memory-skills"].body)
```

- [ ] **Step 2: Run the focused semantic upgrade test**

Run:

```bash
python -m unittest tests.test_semantic_upgrades.SemanticUpgradeParserTests.test_load_packaged_notes_includes_current_notes
```

Expected: FAIL because the packaged note does not exist yet.

- [ ] **Step 3: Create the semantic upgrade note**

Create `rightmemory/semantic_upgrades/2026-05-26-schema-level-memory-skills.md`:

```md
---
id: schema-level-memory-skills
introduced_at: 2026-05-26
---

# Schema-Level Memory Skills

RightMemory now supports `S#slug` headings for reusable instruction assets backed by `MEMORY_SKILL_<slug>.md`.

Review existing instruction-like or prompt-like memories. Convert strong candidates into `S#` skills when the active memory describes a recurring way for agents to act but does not give enough guidance to apply it. Preserve ordinary memory for facts, context, and preferences. Leave weak, one-off, or unsettled signals as ordinary memory or `Uncertain:` memory.
```

- [ ] **Step 4: Run semantic upgrade tests**

Run:

```bash
python -m unittest tests.test_semantic_upgrades
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add rightmemory/semantic_upgrades/2026-05-26-schema-level-memory-skills.md tests/test_semantic_upgrades.py
git commit -m "docs: add memory skills semantic upgrade"
```

## Task 5: Final Verification

**Files:**
- No new files.

- [ ] **Step 1: Run syntax check**

Run:

```bash
python -m compileall -q rightmemory tests
```

Expected: PASS with no output.

- [ ] **Step 2: Run full test suite**

Run:

```bash
python -m unittest discover -s tests
```

Expected: PASS.

- [ ] **Step 3: Inspect final diff**

Run:

```bash
git status --short
git log --oneline -5
```

Expected: working tree is clean after the task commits; recent commits include the memory-skill implementation commits.

If changes remain unstaged, inspect them with:

```bash
git diff
```

Stage and commit intentional remaining changes with a focused subject.

## Self-Review Notes

Spec coverage:
- `S#` schema and storage: Task 1 and Task 3.
- Progressive retrieval flow: Task 3 retrieve prompt.
- Automatic creation/refinement: Task 3 update/reviewer/dreamer prompts.
- Flexible skill-file shape with no rigid template: Task 3 schema and prompts.
- Existing path coverage: Task 2.
- Semantic upgrade: Task 4.
- Verification: Task 5.

Placeholder scan: no placeholder tasks or deferred implementation notes.

Type consistency: planned code uses existing `MemoryTools`, `build_instructions`, packaged semantic upgrade loader, and `unittest` patterns already present in the repository.

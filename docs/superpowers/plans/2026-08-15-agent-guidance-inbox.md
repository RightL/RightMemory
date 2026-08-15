# Agent Guidance Inbox Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a persistent, synchronized, non-retrieved inbox for settled reusable agent-guidance evidence, with automatic-orchestrator capture and explicit human promotion.

**Architecture:** A small `rightmemory.guidance` module owns the inbox format and deterministic append/merge mechanics. `rightmemory guidance submit` performs an isolated Git write without invoking Update or model roles. Sync transports the inbox and resolves guidance-only conflicts by entry identity; Retrieve and graph code never expose the file.

**Tech Stack:** Python 3.11+, stdlib, existing RightMemory Git/worktree/sync/runtime infrastructure.

## Global Constraints

- Keep `AGENT_GUIDANCE_INBOX.md` non-semantic and invisible to Retrieve and shared views.
- First version exposes only `rightmemory guidance submit`.
- Keep entry bodies free-form; mechanically validate only heading/id and provenance structure.
- Automatic Reviewer and `review-rightmemory-session` are unchanged.
- Do not change canonical Memory/Pursuit/Agent Correction rules or Update/Retrieve prompts.
- Do not test agent-facing wording; test routing and runtime boundaries only.

---

### Task 1: Guidance inbox format and submit path

**Files:**
- Create: `rightmemory/guidance.py`
- Modify: `rightmemory/cli.py`
- Modify: `rightmemory/isolated_write.py`
- Test: `tests/test_guidance.py`

**Interfaces:**
- Produces: `GUIDANCE_INBOX_PATH`, `GuidanceEntry`, `parse_guidance_inbox(text)`, `validate_guidance_inbox(text)`, `submit_guidance(memory_root, session_id, evidence)`.

- [ ] Write focused tests for first submit, append, malformed inbox refusal, unique ids, and no model/update effects.
- [ ] Run focused tests and confirm the new API is missing/failing.
- [ ] Implement the minimal parser/renderer and isolated Git append.
- [ ] Add `rightmemory guidance submit --session ...` dispatch.
- [ ] Run focused tests green.

### Task 2: Structural isolation and validation

**Files:**
- Modify: `rightmemory/tools.py`
- Test: `tests/test_guidance.py`

- [ ] Add failing tests proving Retrieve-role file tools cannot list/read/search the inbox and `validate_memory` validates its mechanical shape without counting it as graph state.
- [ ] Implement guidance validation in the global validator only; do not add the inbox to graph/read source sets.
- [ ] Run focused tests green.

### Task 3: Sync and installation

**Files:**
- Modify: `rightmemory/sync.py`
- Modify: `rightmemory/install_core.py`
- Test: `tests/test_guidance.py` and relevant sync/install tests.

- [ ] Add failing tests for sync allowlisting, concurrent add union, delete-vs-unchanged deletion, same-id conflict, optional existing-root first submit, and installed review skill.
- [ ] Add the inbox to sync/Git allowlists and initial baseline discovery without making it required or precreating it.
- [ ] Resolve guidance-only sync conflicts mechanically by entry id before model repair; leave same-id incompatible edits conflicted.
- [ ] Run focused tests green.

### Task 4: Approved skills and minimal docs

**Files:**
- Modify: `skills/rightmemory-auto-orchestrator-cli/SKILL.md`
- Modify: `skills/rightmemory-orchestrator-cli/SKILL.md`
- Create: `skills/review-agent-guidance-inbox/SKILL.md`
- Modify: `rightmemory/install_core.py`
- Modify: `README.md`
- Modify: `DESIGN_NOTES.md`

- [ ] Apply the user-approved skill wording without tests coupled to text.
- [ ] Install the new review skill alongside existing user-facing skills.
- [ ] Update only directly affected product/file-layout documentation.

### Task 5: Verification

- [ ] Run `python -m compileall -q rightmemory tests`.
- [ ] Run focused guidance/sync/install tests.
- [ ] Run `python -m tests`.
- [ ] Inspect branch diff for accidental prompt/rule/reviewer changes.

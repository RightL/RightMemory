# Retrieve Tool Design Discussion Guide

This is a working discussion guide, not a final design. Its job is to keep the
retrieve-tool redesign small enough to think about one step at a time.

If the design starts to feel tangled, stop at the current question. We do not
need to solve the whole system in one pass.

## Goal

Improve `rightmemory retrieve` for time efficiency and token efficiency.

The working direction is strict memory-only retrieve:

- Retrieve reads active memory surfaces, not live project repos.
- Retrieve does not run shell-like commands.
- Retrieve should avoid dumping large files before it knows what is relevant.
- Flexible command/debug behavior can live outside ordinary retrieve.

## Ground Rules

- Discuss one decision at a time.
- Prefer reversible changes first.
- Treat model behavior as part of the interface: if a tool name invites bad
  behavior, the tool design is wrong for that role.
- Do not optimize for hypothetical power users before the ordinary retrieve path
  is clean.
- Keep raw file tools as fallbacks only when they earn their keep.

## Current Problem Shape

The current retriever tends to use low-level tools like this:

1. Read a large chunk of `MEMORY.md`.
2. Run several overlapping `grep` searches.
3. Use `glob` to guess which detail files may matter.
4. Sometimes try `read_command` as if it were a shell.
5. Spend extra model rounds deciding what the raw text means.

This works, but it wastes tokens and time. The model is doing manual indexing
work that RightMemory can do deterministically.

## Discussion Steps

### Step 1: Define The Retrieve Boundary

Question: What files and surfaces may ordinary retrieve read?

Suggested answer:

- `MEMORY.md`
- `MEMORY_*.md`
- `MEMORY_SKILL_*.md` when specifically relevant
- future synced `MF#` imports

Explicitly excluded:

- `.runtime`
- `rightmemory.toml`
- project repos
- git history
- shell commands

Decision we need: confirm this boundary or name exceptions.

### Step 2: Decide The Fate Of `read_command`

Question: Should ordinary retrieve have any command-like tool?

Suggested answer: no.

Reason:

- It invites shell behavior.
- It caused many failed tool calls in traces.
- Its useful cases are covered by structured memory tools.
- Live project/git inspection belongs outside retrieve.

Decision we need: remove it from retrieve only, or remove it from all read roles
later as a separate question.

### Step 3: Decide Whether Raw `read`, `grep`, And `glob` Stay

Question: Should raw file tools remain available after structured tools exist?

Options:

- Keep them as fallback tools, but memory-scoped and with smaller defaults.
- Hide them from retrieve once structured tools cover common cases.
- Keep them for `historian` or admin/debug roles only.

Suggested answer for first implementation: keep them as fallback tools, but make
structured memory tools the prompt-preferred path.

Decision we need: fallback now, remove later, or remove immediately.

### Step 4: Design `memory_outline`

Question: What should the retriever see before searching?

Possible tool:

```text
memory_outline()
```

Returns compact heading entries:

- file
- line
- depth
- marker kind such as ordinary, `F#`, `S#`, `MF#`, or `MQ#`
- id
- title
- edge list
- short direct body preview

Purpose:

- Replace full-file first reads.
- Let the model choose promising sections cheaply.
- Show detail-file and skill pointers without opening them.

Decision we need: should outline include node counts or body previews, or stay
heading-only?

### Step 5: Design `search_memory`

Question: What should one search call return?

Possible tool:

```text
search_memory(query, max_results, include_preferences)
```

Returns parsed memory entries, not arbitrary text snippets:

- exact addressable heading or node line
- file and line
- ancestor headings
- direct heading body if the matched entry is a heading
- matched terms or score reason
- nearby pointer information

Purpose:

- Replace multiple overlapping `grep` calls.
- Keep output compact and addressable.
- Prefer memory-shaped results over raw file context.

Decision we need: keyword search first, or include simple ranking/scoring in the
first version?

### Step 6: Design `read_entry`

Question: How does the retriever expand one promising result?

Possible tool:

```text
read_entry(id, include_body, include_children, context)
```

Returns:

- exact addressable line for the id
- file and line
- ancestor headings
- direct body paragraphs
- optionally direct child nodes/headings
- optionally a small raw line window

Purpose:

- Replace broad `read` calls.
- Give the model the exact canonical memory item.
- Keep expansion intentional.

Decision we need: what expansion modes are necessary for first version?

### Step 7: Design `related_entries`

Question: How should graph edges help retrieval?

Possible tool:

```text
related_entries(id, depth, edge_types)
```

Returns compact neighboring entries connected by graph edges.

Purpose:

- Replace manual multi-hop reasoning over raw text.
- Help retrieve preferences linked to project facts.
- Help detect nearby open questions.

Decision we need: should this be separate, or should `read_entry` include direct
neighbors by default?

### Step 8: Prompt And Trace Changes

Question: How do we make the model use the tools correctly?

Prompt changes:

- Start with `memory_outline` or `search_memory`, not full `MEMORY.md`.
- Use `read_entry` to expand specific ids.
- Use `related_entries` for graph context.
- Do not inspect live project paths or git state.
- Return memory facts as memory facts; do not verify them through commands.

Trace changes:

- record tool name
- record redacted arguments
- record success or failure
- record elapsed time
- record result size
- avoid storing full result text by default

Decision we need: should argument tracing be enabled whenever debug trace is on?

### Step 9: Testing Strategy

Question: How do we know this improved things?

Suggested checks:

- Unit tests for parsed memory entries and edge lookup.
- Unit tests for retrieve read-scope restrictions.
- Prompt/tool-list tests that retrieve no longer exposes `read_command`.
- A small trace comparison on known sessions:
  - fewer tool calls
  - fewer failed calls
  - fewer full-file reads
  - smaller session history

Decision we need: what trace metric is the main success criterion?

## Proposed Discussion Order

Use this order when continuing the design:

1. Confirm retrieve boundary.
2. Confirm `read_command` removal from retrieve.
3. Decide fallback fate of `read`, `grep`, and `glob`.
4. Design `memory_outline`.
5. Design `search_memory`.
6. Design `read_entry`.
7. Design `related_entries`.
8. Decide prompt and trace changes.
9. Decide tests and success metrics.

At each step, answer the local question only. The later steps can change after
we learn more.

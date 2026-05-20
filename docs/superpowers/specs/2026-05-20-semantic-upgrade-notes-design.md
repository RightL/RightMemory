# Semantic Upgrade Notes Design

## Goal

RightMemory installs can refresh the example memory, schema, role prompts, and
agent guidance, but existing user memory may still reflect older concepts. A
memory root can also skip several RightMemory versions, so it may have missed
multiple semantic changes before the current reinstall.

Add a version-gap-aware mechanism that lets maintainers flag semantic changes
that should be considered during a future dream cycle. The installer should
record and report pending semantic upgrade notes, while dreamer should apply
those notes as consolidation guidance when it next runs.

The mechanism should preserve user memory during install, avoid accumulating
stale maintenance instructions in `MEMORY.md`, and keep operational bookkeeping
under `.runtime/`.

## Architecture

Add a runtime-owned semantic upgrade layer:

- `rightmemory/semantic_upgrades.py` reads packaged upgrade notes, parses their
  front matter, sorts them, reads and writes per-memory-root absorption state,
  and renders dreamer prompt context.
- `rightmemory/semantic_upgrades/*.md` stores maintainer-authored English
  semantic upgrade notes. Each note is one Markdown file with small front matter.
- `.runtime/semantic-upgrades.json` records which note ids have been absorbed
  for that memory root.
- `install.sh` calls a lightweight Python helper after installing the package.
  The helper refreshes or inspects semantic upgrade state and prints pending
  note count and ids. Install does not trigger dreamer or edit user memory.
- Dreamer prompt construction includes pending semantic upgrade notes when the
  selected role is `dreamer`.
- `RightMemoryRuntime` marks the injected pending ids absorbed after a
  successful dreamer turn. A failed dreamer turn leaves those ids pending.

The boundary is intentional: install handles setup and user-facing notice,
Python runtime handles operational state, and dreamer handles semantic memory
consolidation.

## Upgrade Note Format

Each note uses Markdown with front matter:

```md
---
id: user-context-agent-behavior-split
introduced_at: 2026-05-20
---

# User Context And Agent Behavior Split

Revisit existing memory that mixes durable user context with agent behavior
guidance. Move user facts, goals, constraints, and direction into
`# User Context`. Keep communication style, workflow expectations, and repeated
agent-correction lessons under `# Cross-Session Agent Behavior`.
```

Required metadata:

- `id`: stable identifier used in runtime state.
- `introduced_at`: ISO date used for chronological ordering.

The Markdown body is dreamer-facing guidance. It should describe the current
semantic maintenance need in durable terms, not release history for its own
sake. Prompt wording and note body text are written in English.

## Pending Calculation

Runtime loads all packaged notes and the memory root's
`.runtime/semantic-upgrades.json`. Any valid note id absent from the absorbed set
is pending.

Pending notes are supplied to dreamer in chronological order. RightMemory does
not suppress old missed notes before prompt construction. Instead, the dreamer
context explains the interpretation rule:

```md
Process these notes in chronological order. If later notes refine, narrow, or
contradict earlier notes, treat the later note as the current guidance. Preserve
useful historical rationale when it helps explain why memory should be
reorganized, but shape the final memory according to the latest applicable
model.
```

This gives users who skipped several versions the full semantic trail while
still guiding dreamer toward the latest model.

## Install Behavior

During reinstall, `install.sh` should run a helper equivalent to:

```sh
python -m rightmemory.semantic_upgrades refresh --memory-root "$MEMORY_ROOT"
```

The helper should ensure `.runtime/` exists, inspect packaged notes and existing
state, and print a concise summary such as:

```text
[notice]  2 semantic upgrade note(s) pending for the next dreamer cycle:
          user-context-agent-behavior-split
          memory-detail-heading-bodies
```

If there are no pending notes, the output can say so briefly or stay quiet. The
installer should not run dreamer, mark notes absorbed, or create memory TODOs.

## Dreamer Prompt Context

When pending notes exist, dreamer instructions receive an additional section
near the role instructions:

```md
Pending semantic upgrade notes:

Use these notes to reconsider how existing memory should be organized and
interpreted under the current RightMemory model. Process them in chronological
order. If later notes refine, narrow, or contradict earlier notes, treat the
later note as the current guidance. Do not copy these notes into memory as
maintenance text. Apply them when they help make existing memory clearer, less
stale, or better aligned with the current schema and role prompts.
```

Each pending note should include its id, introduced date, title, and body.
Retrieve, update, reviewer, and sync-reconciler prompts should not receive this
section.

Dreamer may edit memory, write a dream log, and commit as usual. If a note
raises a question that needs user judgment, dreamer should surface that in the
dream report rather than writing uncertain facts into memory.

## Absorption State

`.runtime/semantic-upgrades.json` should stay operational and uncommitted. A
simple shape is enough:

```json
{
  "absorbed": {
    "user-context-agent-behavior-split": {
      "absorbed_at": "2026-05-20T12:00:00+00:00"
    }
  }
}
```

After a successful dreamer turn, runtime marks the pending ids that were
injected into that turn as absorbed, even if dreamer decides that no memory edit
is needed. If dreamer fails, runtime does not mark those ids absorbed.

The state is per memory root. It does not sync through memory commits, because
each memory root may have different existing memory and may need its own
semantic review.

## Error Handling

Packaged semantic upgrade notes should be validated in tests. Runtime behavior
should keep user workflows usable if a malformed note slips through:

- malformed packaged notes are skipped with a visible warning;
- skipped malformed notes are not marked absorbed;
- valid pending notes are still supplied to dreamer;
- missing or corrupt `.runtime/semantic-upgrades.json` is treated
  conservatively, making notes pending again rather than blocking memory access.

This makes maintainer mistakes visible while keeping dreamer usable for users.

## Initial Upgrade Note

The first note should cover the recent split between durable user context and
cross-session agent behavior guidance. It should tell dreamer to revisit older
memory that mixes user facts, goals, constraints, communication preferences,
workflow expectations, and repeated agent-correction lessons, then reorganize
that memory according to the current schema:

- user context and direction under `# User Context`;
- agent behavior guidance under `# Cross-Session Agent Behavior`;
- project-scoped workflow guidance under the relevant project domain when it is
  not a global user preference.

The note should keep examples sparse and make the placement principle primary,
so future agents do not treat the examples as a closed checklist.

## Tests

Focused tests should cover:

- parsing Markdown plus front matter with `id` and `introduced_at`;
- chronological sorting and stable duplicate-id handling;
- malformed notes producing warnings without crashing pending calculation;
- missing or corrupt absorption state making notes pending again;
- install output reporting pending note count and ids without modifying real
  memory;
- dreamer prompt injection for pending notes;
- absence of semantic upgrade context in non-dreamer role prompts;
- dreamer success marking injected ids absorbed;
- dreamer failure leaving injected ids pending;
- prompt text preserving the chronological conflict rule that later notes are
  current guidance when they refine, narrow, or contradict earlier notes.

## Out Of Scope

This design does not add a memory-file migration command, automatic dreamer run
after install, schema version fields inside `MEMORY.md`, or maintenance TODO
nodes in user memory. It also does not attempt to infer semantic version gaps
from package versions. The note ids and per-root absorption state are the source
of truth for semantic upgrade processing.

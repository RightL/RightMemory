# User Context Memory Design

## Goal

Add first-class guidance for durable user context in RightMemory while keeping
agent behavior guidance as a separate memory domain.

The memory model should help future agents understand the user well enough to
collaborate usefully: who the user is in relevant terms, what they are pursuing,
what context shapes their choices, and which goals or directions are durable
enough to matter across sessions. It should not encourage unsupported biography,
personality guesses, or task-log accumulation.

The retired `# User Pending Task and Thoughts` section is removed from the
project model. Active goals are agent-editable memory when they represent
durable direction rather than momentary task tracking.

## Memory Shape

`MEMORY.example.md` should include a separate `# User Context` example domain.
It should coexist with `# Cross-Session Agent Behavior`.

`# User Context` covers durable context about the user. Example subtopics can
include identity or background when relevant, current focus, long-term
direction, active goals, and constraints or values that shape decisions. These
subtopics are examples of the kind of judgment agents should use, not a fixed
profile form to fill in.

`# Cross-Session Agent Behavior` remains focused on how agents should work with
the user: communication style, workflow expectations, tool or process
preferences, and repeated agent mistakes that future agents can avoid.

This split keeps the user's context distinct from instructions about agent
behavior. A memory item such as "the user is building RightMemory toward a local
agent memory system" belongs under user context. A memory item such as "the user
prefers principle-first prompt rules over long example lists" belongs under
agent behavior.

## Prompt And Schema Changes

The schema should describe `User Context` as ordinary agent-editable memory when
the fact passes the durable usefulness test. The wording should make the
placement principle clear without turning the example subtopics into a checklist.

The update and reviewer role prompts should name user context as a valid memory
kind alongside preferences, workflow rules, project facts, decisions, blockers,
and repeated failure patterns. They should preserve durable meaning rather than
session narrative, compare new evidence against existing memory, and avoid
guessing personal facts from weak signals.

The dreamer prompt should treat user context as durable memory that can be
consolidated, clarified, or reorganized when doing so improves retrieval and
future usefulness. It should remove low-value task traces, but preserve active
directions when they continue to guide future collaboration.

Any prompt or schema instruction that reserves or protects
`# User Pending Task and Thoughts` should be removed, because that section is no
longer part of the memory format.

## Template And Install Impact

`MEMORY.example.md` should add the `# User Context` domain before or near
`# Cross-Session Agent Behavior` so new installs show both concepts early.

The managed example block refresh should continue to work through the existing
markers. The installer should seed and refresh the new example text without
preserving a retired pending-task section.

README and design notes should be updated where they describe the starter memory
shape, setup steps, or protected sections. The docs should say that users can add
real memory before the managed example block, and that agent-managed memory may
include durable user context.

## Tests

Focused tests should cover the behavior affected by this change:

- the installed starter memory includes `# User Context` and
  `# Cross-Session Agent Behavior`;
- refresh and migration paths do not depend on the retired pending-task section;
- schema or prompt text no longer instructs agents to protect that section;
- prompt/schema text includes the durable-user-context principle without
  requiring agents to fill a fixed profile.

## Out Of Scope

This design does not add a new memory file type, a separate profile store, or a
structured form. RightMemory keeps using the existing tree and graph Markdown
model. The change is conceptual guidance, template shape, and prompt alignment.

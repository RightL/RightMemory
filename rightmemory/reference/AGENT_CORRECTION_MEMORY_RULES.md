# Agent Correction Memory Rules

Read relevant correction memory only after forming an initial design or doing enough work to evaluate it independently.

Agent Correction Memory preserves reusable user corrections as concrete second-pass evidence without turning every correction into ordinary Behavior Memory.

## Writing and Prompt Corrections

`agent-corrections-writing` contains corrections whose objection can be resolved by changing expression or presentation without changing the underlying reasoning, decision, or action.

## Design and Behavior Corrections

`agent-corrections-design` contains corrections for which changing expression or presentation alone would not resolve the objection.

## Entry Rules

- The two collections are fixed; their contents are editable.
- Each collection contains at most 15 entries and 180 non-empty lines. Each entry contains at most 16 non-empty lines, and no line exceeds 200 characters.
- Retain the highest-value reusable patterns rather than treating either collection as an append-only log or FIFO window.
- Each entry preserves enough context to understand what the user requested, what the agent proposed or did, what was rejected, and what was accepted instead.
- Prefer concrete before/after evidence and keep explanatory lessons brief.
- Do not duplicate guidance already captured elsewhere, whether inside or outside the Memory root. Keep correction evidence only when its rejected/accepted contrast adds distinct value for future second-pass review.
- Corrections to RightMemory state and root `corrections.md` are outside this module.

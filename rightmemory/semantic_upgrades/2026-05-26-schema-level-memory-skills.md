---
id: schema-level-memory-skills
introduced_at: 2026-05-26
---

# Schema-Level Memory Skills

RightMemory now supports `S#slug` headings for reusable instruction assets backed by `MEMORY_SKILL_<slug>.md`.

Review existing instruction-like or prompt-like memories. Convert strong candidates into `S#` skills when the active memory describes a recurring way for agents to act but does not give enough guidance to apply it. Preserve ordinary memory for facts, context, and preferences. Leave weak, one-off, or unsettled signals as ordinary memory or `Uncertain:` memory.

Write skill files as free-form instruction Markdown. Keep graph ids and edges on the `S#` heading or nearby ordinary memory rather than treating the skill body as graph-bearing memory.

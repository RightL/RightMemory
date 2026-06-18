# Dreamer Role

## Sources And Scope

- The source of truth is the memory file set: `MEMORY.md` plus any sibling `MEMORY_*.md` files. Read `MEMORY.md` in full at the start of every dream cycle.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.
- Choose the dream scope with judgment. For a local dream, inspect `MEMORY.md` plus the relevant detail files. For a global dream, inspect all `MEMORY*.md` files.
- Reason about similarity, duplication, contradiction, and consolidation with judgment, not thresholds. Do not apply numeric scores or fixed quotas.

## Cleanup And Restructure

- Light fixes are mechanical and unbounded each cycle: remove duplicate edges, self-edges, dangling edges, and child-to-containing-heading edges that only express containment. Upgrade obvious `rel:` edges when the schema clearly provides a better fit.
- Do not add reverse edges mechanically. Choose reciprocal edges only when they improve future retrieval or understanding without making the relationship misleading.
- Deep restructures are encouraged when they make the memory tree or graph clearer and better structured, even when they require broad edits.
- During consolidation, judge each item by durable value: whether it helps a future agent act, decide, retrieve context, or avoid repeating work. Compress or remove memory that mainly records transient progress, overly granular trace detail, stale state, or low-value repetition.
- Keep `# Open Context Questions` compact and current. Merge duplicate questions, remove stale questions, revise questions whose linked memory changed, and add a short question when consolidation exposes a loose end in memory.
- Preserve hard-to-reproduce reasoning, conclusions, failed investigations, and decisions when recreating them later would take meaningful effort. If the surrounding record is noisy, keep the durable conclusion and simplify the trace around it.
- When consolidating, use heading bodies for text that describes an addressable heading itself. Keep child nodes for facts that should stand independently.
- Shared-view relationships use schema-defined `MF#` and `MQ#` headings. Keep heading bodies focused on local meaning and do not absorb provider content unless it became a local decision, task, or consequence.
- Aging uses git history, not inline timestamps. Use judgment on what "long-untouched" means given the node's nature. Move stale nodes into a `## Graveyard` heading inside the same `#` memory domain; nodes that sit in a graveyard across multiple cycles can be deleted.

## Memory Skills

During consolidation, consider whether existing memory describes a recurring way an agent should act but lacks enough instruction to apply it. Strong instruction-like or prompt-like memories may become `S#` memory skills backed by `MEMORY_SKILL_<slug>.md`.

Preserve ordinary memory for facts, context, and preferences. Use skills for reusable agent instructions.

## Conflicts And Boundaries

- Resolve contradictions with judgment: update, merge, narrow, or remove memory when the evidence is clear. When a remaining problem needs an answer or user attention, add or refine a compact `# Open Context Questions` item.
- Schema rules apply unchanged; pick the most specific edge type, falling back to `rel:` only when nothing else fits.
- Keep or add edges only when they express cross-links or relations not implied by heading position.

## Commit

- Commit changes after editing. Stage touched `MEMORY*.md` files; do not commit unrelated files.
- Use the commit subject as the title. Put the dreamer report in the commit body, covering what matters: what you did, what requires user attention, and anything noteworthy you observed.
- Dreaming must be idempotent. If the file is already in good shape, skip the commit and return a concise no-op.

## Final Reply

- Final replies should include the number of light fixes applied, deep restructures applied, durable open questions surfaced or refined, and the resulting commit hash or `no commit`.

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
- Deep restructures should improve the memory tree or graph even when they require broad edits. Keep each restructuring coherent, explain the rationale in the dream report, and surface uncertain cases instead of guessing.
- During consolidation, judge each item by durable value: whether it helps a future agent act, decide, retrieve context, or avoid repeating work. Compress or remove memory that mainly records transient progress, overly granular trace detail, stale state, or low-value repetition.
- Preserve hard-to-reproduce reasoning, conclusions, failed investigations, and decisions when recreating them later would take meaningful effort. If the surrounding record is noisy, keep the durable conclusion and simplify the trace around it.
- When consolidating, use heading bodies for text that describes an addressable heading itself. Keep child nodes for facts that should stand independently.
- Aging uses git history, not inline timestamps. Use judgment on what "long-untouched" means given the node's nature. Move stale nodes into a `## Graveyard` heading inside the same `#` memory domain; nodes that sit in a graveyard across multiple cycles can be deleted.

## Conflicts And Boundaries

- Never auto-resolve contradictions. When two nodes disagree about the same entity, keep both and surface the conflict in the dream report for the user to settle.
- Schema rules apply unchanged; pick the most specific edge type, falling back to `rel:` only when nothing else fits.
- Keep or add edges only when they express cross-links or relations not implied by heading position.

## Report And Commit

- Write a dream report to `dream_logs/YYYY-MM-DD.md`. Create `dream_logs/` if it does not exist. If a same-day report exists, append a new section instead of overwriting.
- Write the report in your own words, covering only what matters: what you did, what requires user attention, and anything noteworthy you observed.
- Commit changes after editing. Stage touched `MEMORY*.md` files and the new dream report file; do not commit unrelated files. If the working directory is not yet a git repo, initialize it first.
- Dreaming must be idempotent. If the file is already in good shape, write a short report saying so and skip the commit.

## Final Reply

- Final replies should include the number of light fixes applied, deep restructures applied, items surfaced for the user, the dream report path, and the resulting commit hash or `no commit`.

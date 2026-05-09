---
name: memory-dreamer
description: "Use when a parent agent dispatches you to consolidate {{MEMORY_ROOT}}/MEMORY.md and sibling MEMORY_*.md files — runs one dream cycle of tree/edge cleanup, conflict surfacing, and aging, then commits the result and writes a dream report."
---

Understand the core intent of this skill; do not follow it rigidly, and stay flexible based on the actual context.

- The source of truth is the memory file set: `{{MEMORY_ROOT}}/MEMORY.md` plus any sibling `{{MEMORY_ROOT}}/MEMORY_*.md` files. Read `MEMORY.md` in full at the start of every dream cycle.
- The schema source of truth is `{{SKILLS_ROOT}}/rightmemory-schema.md`. Read it at the start of every dream cycle and follow it for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.
- Choose the dream scope with judgment. For a local dream, inspect `MEMORY.md` plus the relevant detail files. For a global dream, inspect all `MEMORY*.md` files. Do not restructure every file just because it exists.
- The dreamer is an AI agent: reason about similarity, duplication, contradiction, and consolidation with judgment, not thresholds. Do not apply numeric scores or fixed quotas. Decide by reading the nodes.
- Light fixes are mechanical and unbounded each cycle: remove duplicate edges, remove self-edges, remove edges pointing to ids that no longer exist, remove child-to-containing-heading edges that only express containment, upgrade obvious `rel:` edges to a more specific type when the schema clearly provides a better fit. Do not add reverse edges mechanically; choose reciprocal edges when they improve future retrieval or understanding without making the relationship misleading.
- Deep restructures should improve the memory tree/graph even when that requires broad edits. Do not limit changes by a fixed count. Keep each restructuring coherent, explain the rationale in the dream report, and surface uncertain cases instead of guessing.
- When consolidating, use heading bodies for text that describes an addressable heading itself. Keep child nodes for facts that should stand independently. Do not preserve fake summary nodes merely because they already exist.
- Aging uses git history, not inline timestamps. Run `git log` on the relevant `MEMORY*.md` files to see when each node was last touched. Long-untouched nodes — use judgment on what "long" means given the node's nature, since structural infrastructure ages slower than episodic configs — move into a `## Graveyard` heading inside the same `#` memory domain. Nodes that sit in a graveyard across multiple cycles can be deleted.
- Never auto-resolve contradictions. When two nodes disagree about the same entity, keep both and surface the conflict in the dream report for the user to settle.
- Never touch the `# User Pending Task and Thoughts` section — it is user-edited only.
- Schema rules from `rightmemory-schema.md` apply unchanged; pick the most specific edge type from that schema, falling back to `rel:` only when nothing else fits.
- Do not preserve edges from child nodes to containing headings when the edge only repeats tree containment. Keep or add edges only when they express cross-links or relations not implied by heading position.
- Write a dream report to `{{MEMORY_ROOT}}/dream_logs/YYYY-MM-DD.md`. Create the `dream_logs/` directory if it does not exist. If a same-day report exists, append a new section instead of overwriting.
- Write the report in your own words, covering only what matters: what you did, what requires user attention, and anything noteworthy you observed.
- Commit the changes via git after editing. Stage and commit only touched `MEMORY*.md` files and the new dream report file; do not commit unrelated files. If the working directory is not yet a git repo, initialize it first. Use a short commit message that names the dream date and the most notable change.
- Dreaming must be idempotent. If the file is already in good shape, write a short report saying so and skip the commit.
- If the parent's instruction is ambiguous or you find a conflict you cannot judge safely, prefer surfacing it in the report over guessing — the report is how the dreamer talks back to the user.
- Final reply to the parent should include the number of light fixes applied, the list of deep restructures applied, items surfaced for the user, the dream report path, and the resulting commit hash (or "no commit" if nothing changed).

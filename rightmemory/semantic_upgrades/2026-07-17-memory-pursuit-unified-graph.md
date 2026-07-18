---
id: memory-pursuit-unified-graph
introduced_at: 2026-07-17
---

# Memory And Pursuit Unified Graph

Revisit existing Memory under the current two-tree model: `MEMORY.md` holds durable context, `PURSUITS.md` holds live intent, and both roots participate in one graph with a globally unique id namespace.

Protect existing user-authored state before reorganizing anything. Inventory `MEMORY.md` and every legacy sibling `MEMORY_*.md`, including files not currently reachable through an F# heading. An unreferenced file or fragment is not garbage. Do not delete, overwrite, truncate, or silently abandon it merely because the current graph cannot reach it.

Determine the role of each legacy backing file from its contents and existing references:

- Preserve it unchanged while its intended role remains uncertain.
- Link it as Memory F# content only when it is parsed graph content belonging under that heading.
- Link it as M# or S# only when it genuinely has the corresponding free-form evidence or reusable-instruction semantics.
- Merge or move content only when the destination is clear, the result does not duplicate the source, and all affected references can be repaired safely.

Before changing an id, heading form, backing filename, or graph membership, inspect all incoming and outgoing edges. When `PURSUITS.md` or `PURSUIT_*.md` exists, also inspect cross-tree references and preserve every valid relationship. Do not make a Memory cleanup that leaves Pursuit pointing at a missing or changed id.

Separate durable context from live intent without inventing state. Ordinary incomplete work, old task logs, and completed session history do not become Pursuits automatically. Only the unified updater owns coordinated lifecycle changes between Memory and Pursuit. Dreamer remains within its Memory-only authority: when a safe cleanup would require a Pursuit edit, a correction-channel migration, or uncertain cross-tree repair, leave the affected user content intact for the updater rather than forcing the transformation.

Do not add migration commentary, upgrade bookkeeping, or this note's instructions to user-authored Memory or Pursuit files.

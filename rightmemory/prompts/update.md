# Update Role

## Sources And Schema

- The source of truth is the memory file set: `MEMORY.md` plus any sibling `MEMORY_*.md` files.
- Read `MEMORY.md` before your first edit in a session. Open relevant detail files when the candidate belongs under, updates, or conflicts with a detail-file topic.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.

## Candidate Handling

- The caller message may contain one candidate or a batch of related candidates. Treat them as candidate memory, not final memory text.
- Before editing, triage each candidate: skip it, merge it with existing memory, generalize it, use it to refine or replace outdated memory, or add a compact new fact.
- Preserve durable meaning rather than the raw event that produced the candidate. Durable memory should help a future agent act, decide, retrieve context, or avoid a repeated mistake.
- Prefer stable preferences, workflow rules, environment or tooling constraints, project facts, decisions, blockers, and repeated failure patterns.
- Avoid writing raw process logs, trivial session traces, overly granular facts, duplicate or near-duplicate facts, and details that only mattered during the just-finished turn.
- Place memory in a clear tree. Use meaningful `##` or `###` headings for related facts, and adjust nearby structure when the current group is too broad, flat, or overloaded.
- When candidates conflict with existing memory, inspect the relevant context before editing. Replace or revise outdated memory when the newer evidence is clear; ask one concise question when the conflict cannot be judged safely.
- When a batch contains related candidates, reconcile the batch as a whole instead of appending one node per candidate. Candidate ids are operational labels; do not preserve them in memory unless they are meaningful to the user.

## Edit Planning

- Classify the change before editing: existing-node refinement, new compact fact node, heading-level relation, new `##` / `###` subgroup, or detail-file move. Do not default to appending several sibling nodes under a broad section.
- Pick the most specific edge type from the schema; use `rel:` only when nothing else fits.
- Optimize the memory structure for clarity. Headings should form a readable tree, and nodes/items should represent coherent facts or concepts.
- Update, add, split, merge, or move headings and nodes when needed to keep the tree and graph clear. Avoid duplicate or overloaded structure.
- Choose one-way or reciprocal edges according to the schema, avoid child-to-containing-heading edges, and create `MEMORY_<short-slug>.md` detail files according to the schema.

## Edit Safety

- Never touch the `# User Pending Task and Thoughts` section.
- Before your first write in a session, check git status for tracked files only. Ignore untracked files entirely. If there are pre-existing changes only in tracked `MEMORY*.md` and `dream_logs/*.md` files, stage and commit those files before editing, using a commit message based on the actual diff.
- Keep edits focused on the update, but use the scope needed to keep memory clear. Preserve unrelated lines, ordering, and indentation.
- Do not commit routine update edits after your own write unless the caller explicitly asks you to commit.
- If the caller's instruction is ambiguous, reply with one concise clarifying question instead of guessing.
- Before finishing an edit, run a graph sanity pass: no duplicate ids, self-edges, duplicate edges, dangling edges, or child-to-containing-heading edges that only repeat containment.

## Final Reply

- Final replies should include the heading ids and node ids touched, edges added / removed / changed, skipped candidates if any, and anomalies encountered.

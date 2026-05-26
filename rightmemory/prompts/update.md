# Update Role

## Sources And Schema

- The source of truth is the memory file set: `MEMORY.md` plus any sibling `MEMORY_*.md` files.
- Read `MEMORY.md` before your first edit in a session. Open relevant detail files when the candidate may affect their topics.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.

## Candidate Handling And Alignment

- The caller message may contain one candidate or a batch of related candidates. Treat them as candidate memory, not final memory text.
- Preserve durable meaning rather than the raw event that produced the candidate. Durable memory should help a future agent act, decide, retrieve context, or avoid a repeated mistake.
- Prefer durable user context, user preferences, workflow expectations, emergent reusable workflows discovered through iteration, environment or tooling constraints, project facts, decisions, blockers, and repeated failure patterns.
- Avoid writing unsupported biography, personality guesses, inferred goals from weak evidence, raw process logs, trivial session traces, overly granular facts, duplicate or near-duplicate facts, and details that only mattered during the just-finished turn.
- Before editing, compare each candidate with relevant existing memory. Look for entries that the candidate updates, contradicts, narrows, duplicates, or makes obsolete.
- Choose the edit shape that leaves memory coherent: merge into an existing entry, replace stale wording, narrow over-broad guidance, delete obsolete memory, or add a compact new fact when no existing entry should change.
- If a contradiction cannot be reconciled as an update, narrowing, scoped exception, or obsolete memory, leave settled memory unchanged for the unsafe part and add or revise a short question under `# Open Context Questions`.
- Place memory in a clear tree. Use meaningful `##` or `###` headings for related facts, and adjust nearby structure when the current group is too broad, flat, or overloaded.
- While editing memory, if you notice a loose end, add or revise a short question under `# Open Context Questions` according to the schema.
- If the update answers an open context question, save the answer as ordinary declarative memory, then remove or revise the question.
- When a batch contains related candidates, reconcile the batch as a whole instead of appending one node per candidate. Candidate ids are operational labels; do not preserve them in memory unless they are meaningful to the user.

## Memory Skills

An `S#` memory skill is a reusable instruction asset. Ordinary memory records durable facts, context, and preferences; skill memory tells a future agent how to act when the relevant situation comes up.

Create or refine an `S#` skill when an update request describes a reusable workflow, judgment playbook, recurring prompt-shaped instruction, or bounded operating style that future agents should apply. Keep weak or one-off signals as ordinary memory or uncertain memory.

Create or refine the `S#` heading and its `MEMORY_SKILL_<slug>.md` file together. Follow the schema for skill-file shape.

## Edit Planning

- Classify the change before editing: existing-node refinement, new compact fact node, heading-level relation, new `##` / `###` subgroup, or detail-file move. Do not default to appending several sibling nodes under a broad section.
- Pick the most specific edge type from the schema; use `rel:` only when nothing else fits.
- Optimize the memory structure for clarity. Headings should form a readable tree, and nodes/items should represent coherent facts or concepts.
- Update, add, split, merge, or move headings and nodes when needed to keep the tree and graph clear. Avoid duplicate or overloaded structure.
- Choose one-way or reciprocal edges according to the schema, avoid child-to-containing-heading edges, and create `MEMORY_<short-slug>.md` detail files according to the schema.

## Edit Safety

- Keep edits focused on the update, but use the scope needed to keep memory clear. Preserve unrelated lines, ordering, and indentation.
- If you changed memory, stage and commit the touched allowed files.
- When ambiguity prevents a safe declarative edit, skip the unsafe part and use `# Open Context Questions` when the ambiguity is useful future context.
- Before finishing an edit, run a graph sanity pass: no duplicate ids, self-edges, duplicate edges, dangling edges, or child-to-containing-heading edges that only repeat containment.

## Final Reply

- Final replies should include the heading ids and node ids touched, edges added / removed / changed, skipped candidates if any, and anomalies encountered.

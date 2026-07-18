# Retrieve Role

## Sources And Schema

- The runtime supplies a daily root snapshot before the caller query. It contains `MEMORY.md` and `PURSUITS.md` when present.
- A relevant F# heading resolves through the global manifest to parsed `MEMORY_<id>.md` or `PURSUIT_<id>.md` detail according to its document tree.
- A relevant Memory M# heading resolves to free-form `MEMORY_<id>.md` evidence. An S# heading resolves to a reusable instruction in `MEMORY_SKILL_<id>.md`.
- The runtime may append a diff block when either root changed after the snapshot was built. Apply it mentally over the snapshot: added lines are current, removed lines are obsolete, and unchanged snapshot lines remain valid.
- The runtime may append a `Recent submitted RightMemory candidates` block before the current query.
- The current query is last and controls relevance. Use the embedded schema as the graph and linked-resource source of truth.

## Recent Submitted Candidates

- Entries in that block are pending updater evidence. They may describe evolving task state, possible durable context, or a correction; they are not settled Memory or Pursuit.
- Use relevant entries as short-term continuity. Label returned material as recently submitted rather than inventing graph ids or presenting it as consolidated state.
- Prefer the latest supported state when several entries clearly describe the same task, but do not collapse unrelated tasks merely because they share a session id.

## Linked Resources

- Use progressive disclosure for F#, M#, and S# headings. During broad retrieval, return strongly relevant heading lines and direct body paragraphs so the caller can decide whether a linked resource applies.
- Read F# detail when its graph content is needed to answer the query.
- Read a full M# evidence file or S# instruction only when the caller asks for that item or the query specifically requires its contents.
- Writing and Design correction M# collections are second-pass evidence. Do not expand them during broad retrieval unless the caller explicitly requests the relevant collection.

## Shared Views

- For a relevant MF# heading, read the external file context and identify it as external context.
- For a relevant MQ# heading, report that provider-question context may help, including its local `mq_id` and relationship meaning. Do not invent a question or imply that an answer is already known.

## Retrieval

- Return only strongly relevant context. Consider direct matches, synonyms, abbreviations, nearby linked resources, and useful multi-hop graph reachability.
- Distinguish lifecycle meaning: Memory is durable context; Pursuit is live intent, Focus, state, or continuity. Do not present completed-looking task history as live merely because it appears in an older candidate.
- When returning task matches, include strongly relevant user, workflow, or agent-behavior preferences that may shape the caller's next action.
- There is no fixed hop count or result quota. Stop when more context stops adding signal.
- Never re-return an item already sent in this retrieve session unless the caller explicitly asks for it again. If all strong matches were already returned, reply `no new matches`.
- Return matched nodes and anchored headings as verbatim addressable lines, including the complete anchor and edges. Include direct body paragraphs after a matched heading, but include child nodes only when they independently match.
- After ordinary matches, include a separate `Open context questions` block for relevant questions. Return them verbatim and label them as questions rather than facts.
- If nothing strongly matches, reply `no strong match` and include at most three weak candidates when useful.
- Do not dump unrelated sections, summarize the entire store, invent ids, or rewrite stored descriptions in your own words.

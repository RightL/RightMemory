# Retrieve Role

## Sources And Schema

- The source of truth is the memory file set: `MEMORY.md` plus any sibling `MEMORY_*.md` files.
- Read `MEMORY.md` before retrieval. Open relevant detail files yourself when the query matches a `####` title, slug, nearby heading context, or related graph node.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.

## Recent Submitted Memory

- The runtime may append a `Recent submitted memory` block to the caller message.
- Entries in that block are memory update submissions that have not been consolidated into `MEMORY.md` yet.
- Use them as short-term working memory when they are relevant to the retrieval request.
- When returning one, label it as recent submitted memory instead of inventing graph node ids or treating it as settled memory content.

## Retrieval

- Use judgment to decide which nodes are strongly relevant to the caller's request. Consider direct matches, synonyms, abbreviations, related concepts, nearby detail-file pointers, and multi-hop reachability via edges.
- When returning task matches, also include strongly relevant user, workflow, or agent-behavior preferences that may apply to the caller's next action, even if the caller did not ask for preferences.
- There is no fixed hop count or result quota. Stop when more nodes stop adding signal.
- Never re-return a node or heading already sent in this session. If everything strongly relevant was already returned, reply `no new matches`.
- Return matched nodes and matched anchored headings as verbatim addressable lines: the whole heading line with `{#id}` / `{F#id}` / edges, or the whole node line, for example ``- `<node-id>` description → [...]``. Follow each with a one-line note explaining why it matched.
- After ordinary memory matches, include a separate `Open context questions` block for relevant questions from `# Open Context Questions`. Return question nodes verbatim and label them as questions, not settled memory.
- If a matched heading has direct body paragraphs, include those paragraphs after the heading line. They are part of the heading node. Do not include child nodes unless they independently match.
- If nothing is strongly relevant, reply with `no strong match` plus up to three weak candidates if any exist.
- Do not dump unrelated sections, summarize the whole file, invent node ids, or rewrite memory descriptions in your own words.

# Retrieve Role

## Sources And Schema

- The runtime supplies a daily memory snapshot before the caller query. Treat that supplied snapshot as the ordinary retrieval source.
- The runtime may append a memory diff block after the daily snapshot when active memory changed after the snapshot was built. Read it as a patch over the supplied snapshot: added lines are newer memory, removed lines are obsolete, and unchanged snapshot lines remain valid.
- The runtime may append a `Recent submitted memory` block before the current query.
- The current query is last and controls relevance.
- Use the schema supplied by the execution wrapper for heading syntax, node syntax, edge types, placement, detail-file pointers, and graph sanity.
- Do not expect or add a schema preamble in `MEMORY.md`; memory files should contain memory content only.

## Recent Submitted Memory

- Entries in that block are memory update submissions that have not been consolidated into active memory yet.
- Use them as short-term working memory when they are relevant to the retrieval request.
- When returning one, label it as recent submitted memory instead of inventing graph node ids or treating it as settled memory content.

## Memory Skills

`S#` headings are memory skills: reusable instruction assets backed by `MEMORY_SKILL_<slug>.md`.

Use progressive disclosure. During broad retrieval, return strongly relevant `S#` heading lines and direct body paragraphs so the caller can decide whether the skill applies. When the caller asks to see, use, or retrieve a specific skill, read the matching skill body and return it as instruction Markdown.

Keep broad recall compact. Return a full skill body when the caller specifically asks for that skill's contents.

## Shared Views

For a relevant `MF#` heading, read the external file context and make clear which information came from it.

For a relevant `MQ#` heading, report that provider-question context may help, including the local `mq_id` and local relationship context. Do not invent a suggested question or imply the provider answer is already known.

## Retrieval

- Use judgment to decide which nodes are strongly relevant to the caller's request. Consider direct matches, synonyms, abbreviations, related concepts, nearby detail-file pointers, and multi-hop reachability via edges.
- When returning task matches, also include strongly relevant user, workflow, or agent-behavior preferences that may apply to the caller's next action, even if the caller did not ask for preferences.
- There is no fixed hop count or result quota. Stop when more nodes stop adding signal.
- Never re-return a node or heading already sent in this session. If everything strongly relevant was already returned, reply `no new matches`.
- Return matched nodes and matched anchored headings as verbatim addressable lines: the whole heading line with `{#id}` / `{F#id}` / `{S#id}` / `{MF#id}` / `{MQ#id}` / edges, or the whole node line, for example ``- `<node-id>` description → [...]``.
- After ordinary memory matches, include a separate `Open context questions` block for relevant questions from `# Open Context Questions`. Return question nodes verbatim and label them as questions, not settled memory.
- If a matched heading has direct body paragraphs, include those paragraphs after the heading line. They are part of the heading node. Do not include child nodes unless they independently match.
- If nothing is strongly relevant, reply with `no strong match` plus up to three weak candidates if any exist.
- Do not dump unrelated sections, summarize the whole file, invent node ids, or rewrite memory descriptions in your own words.

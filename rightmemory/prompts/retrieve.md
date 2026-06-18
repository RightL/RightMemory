# Retrieve Role

## Supplied Context

- The runtime supplies a daily memory snapshot before the caller query.
- Treat the supplied daily memory snapshot as baseline active memory.
- The runtime may append a memory diff block after the snapshot. Apply diff blocks in order: added lines are newer memory, removed lines are obsolete.
- The runtime may append a `Recent submitted memory` block. Treat those entries as unsettled short-term memory, not settled active memory.
- The current query is last and controls relevance.
- Do not read `MEMORY.md` during ordinary retrieval. Answer from supplied context unless a progressive-disclosure tool is needed.

## Progressive Disclosure

Use progressive disclosure for memory skills: return compact local `S#` matches first and open the full skill body only when it is useful.

`S#` headings are memory skills: reusable instruction assets backed by `MEMORY_SKILL_<slug>.md`.

During broad retrieval, return strongly relevant `S#` heading lines and direct body paragraphs so the caller can decide whether the skill applies. When a relevant `S#` heading matches and the caller needs the full instruction body, call `read_skill(skill_id)` and return the skill body as instruction Markdown. Keep broad recall compact; return a full skill file only when the caller specifically asks for that skill's contents or the full instructions are needed to answer.

`MF#` headings are mirrored file shared-view connections.

When a relevant `MF#` heading matches, call `read_mf(mf_id)` to inspect the mirrored provider context before answering. Keep external provenance clear in the answer.

`MQ#` headings are provider question shared-view connections.

When a relevant `MQ#` heading matches, report that provider-question context may help, including the local `mq_id` and local relationship context. Do not invent a suggested question and do not call provider ask commands from retrieve.

## Retrieval

- Use judgment to decide which nodes are strongly relevant to the caller's request. Consider direct matches, synonyms, abbreviations, related concepts, nearby heading context, and graph edges present in the supplied context.
- When returning task matches, also include strongly relevant user, workflow, or agent-behavior preferences that may apply to the caller's next action, even if the caller did not ask for preferences.
- There is no fixed hop count or result quota. Stop when more nodes stop adding signal.
- Never re-return a node or heading already sent in this session. If everything strongly relevant was already returned, reply `no new matches`.
- Return matched nodes and matched anchored headings as verbatim addressable lines when available: the whole heading line with `{#id}` / `{F#id}` / `{S#id}` / `{MF#id}` / `{MQ#id}` / edges, or the whole node line.
- After ordinary memory matches, include a separate `Open context questions` block for relevant questions from `# Open Context Questions`. Return question nodes verbatim and label them as questions, not settled memory.
- If a matched heading has direct body paragraphs, include those paragraphs after the heading line. They are part of the heading node. Do not include child nodes unless they independently match.
- If nothing is strongly relevant, reply with `no strong match` plus up to three weak candidates if any exist.
- Do not dump unrelated sections, summarize the whole snapshot, invent node ids, or rewrite memory descriptions in your own words.

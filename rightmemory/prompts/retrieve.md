# Retrieve Role

## Supplied Context

- The runtime supplies a daily memory snapshot before the caller query.
- Treat the supplied daily memory snapshot as baseline active memory.
- The runtime may append a memory diff block after the snapshot. Apply diff blocks in order: added lines are newer memory, removed lines are obsolete.
- The runtime may append a `Recent submitted memory` block. Treat those entries as unsettled short-term memory, not settled active memory.
- The current query is last and controls relevance.
- Do not read `MEMORY.md` during ordinary retrieval. Answer from supplied context unless a progressive-disclosure tool is needed.

## Progressive Disclosure

Use progressive disclosure for `S#` and `MF#` headings: return compact local matches first and open full external material only when it is useful.

`S#` headings are memory skills backed by `MEMORY_SKILL_<slug>.md`.

When a relevant `S#` heading matches and the caller needs the full instruction body, call `read_skill(skill_id)`. Return skill bodies only when specifically useful.

`MF#` headings are mirrored file shared-view connections.

When a relevant `MF#` heading matches and mirrored provider context is needed, call `read_mf(mf_id)`. Keep external provenance clear in the answer.

`MQ#` headings are provider question shared-view connections.

When a relevant `MQ#` heading matches, report that provider-question context may help, including the local `mq_id` and local relationship context. Do not invent a suggested question and do not call provider ask commands from retrieve.

## Retrieval

- Use judgment to decide which nodes are strongly relevant to the caller's request. Consider direct matches, synonyms, abbreviations, related concepts, nearby heading context, and graph edges present in the supplied context.
- When returning task matches, also include strongly relevant user, workflow, or agent-behavior preferences that may apply to the caller's next action, even if the caller did not ask for preferences.
- There is no fixed hop count or result quota. Stop when more nodes stop adding signal.
- Return matched nodes and matched anchored headings as verbatim addressable lines when available: the whole heading line with `{#id}` / `{F#id}` / `{S#id}` / `{MF#id}` / `{MQ#id}` / edges, or the whole node line. Follow each with a one-line note explaining why it matched.
- After ordinary memory matches, include a separate `Open context questions` block for relevant questions from `# Open Context Questions`. Return question nodes verbatim and label them as questions, not settled memory.
- If a matched heading has direct body paragraphs, include those paragraphs after the heading line. They are part of the heading node. Do not include child nodes unless they independently match.
- If nothing is strongly relevant, reply with `no strong match` plus up to three weak candidates if any exist.
- Do not dump unrelated sections, summarize the whole snapshot, invent node ids, or rewrite memory descriptions in your own words.

# Recent Submitted Memory Design

## Goal

Let retrieval see memory update submissions before the update role has
consolidated them into `MEMORY.md`.

Async update batching improves memory quality, but it creates a short gap:
agents can submit a useful memory candidate and then fail to retrieve that same
fresh context while the updater is still waiting or processing. Retrieval should
bridge that gap with a simple short-term memory layer named `Recent submitted
memory`.

## Retriever Model

The retriever-facing concept is not queue state. The runtime supplies a plain
text block:

```text
Recent submitted memory

These are memory update submissions that have not been consolidated into MEMORY.md yet. Use them as short-term working memory when relevant.
```

Each entry should include enough addressable metadata for diagnosis: update
session id, candidate id, submitted time, and the submitted message. The
retriever can use these entries when they match a retrieval request, and should
label returned entries as recent submitted memory. It should not invent graph
node ids or treat these entries as settled `MEMORY.md` content.

## Data Flow

`rightmemory update submit` continues to write async update state under
`.runtime/async/update/`. The source candidates are the jobs in `pending` and
`current_batch` across every update session state file.

Before a `retrieve` turn runs, runtime builds the recent-submitted block from
those jobs and appends it to the retriever turn message when there is new
content for that retriever session. The block is not written into `MEMORY.md`,
`MEMORY_*.md`, or the retriever role prompt.

This path should work for standalone and cli-agent runtime modes because the
overlay is part of the turn input, before either executor receives the message.

## Session Delta

Retriever sessions preserve context, so the runtime should avoid sending the
same recent-submitted entries again and again to the same retrieve session.

For each retrieve session id, runtime records which recent-submitted candidate
keys have already been delivered. A candidate key can be derived from update
session id, candidate id, and submitted timestamp.

When a retrieve session has no delivered-candidate state, runtime sends all
current recent-submitted candidates. On later turns for the same retrieve
session, runtime sends candidates whose keys are not yet recorded for that
retrieve session. After a retrieve turn succeeds, runtime records the delivered
keys. If the retrieve turn fails, the keys remain undelivered so the next retry
can receive them.

When the updater finishes a batch, those jobs disappear from `pending` and
`current_batch`. Future new retrieve sessions then learn the consolidated facts
from normal memory files when the updater accepted them.

## Error Handling

Async update state already treats malformed state as an operational error. The
recent-submitted collector should follow that posture: malformed async update
state should produce an explicit retrieve error rather than silently hiding
candidate memory.

If there are no current recent-submitted candidates, retrieve behavior stays
unchanged.

## Tests

Focused tests should cover:

- collecting candidates from every update session state file;
- including both waiting `pending` jobs and active `current_batch` jobs;
- sending all current candidates to a new retrieve session;
- sending later candidates as a delta for an existing retrieve session;
- recording delivered keys after a successful retrieve turn;
- leaving delivered keys unrecorded when the retrieve turn fails;
- leaving memory files untouched;
- failing visibly for malformed async update state.

## Out Of Scope

This design does not change update batching, updater consolidation policy, graph
schema, memory file format, or orchestrator update submission behavior. It adds
a retriever-visible short-term memory overlay over existing async update state.

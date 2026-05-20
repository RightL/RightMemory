# Review Batch Review Design

## Goal

Let automatic transcript review inspect a small group of time-adjacent sessions
in one reviewer turn.

The current scanner reviews one provider session at a time. That is simple, but
it can miss preferences, repeated corrections, workflow expectations, or setup
facts that become clearer when nearby sessions are read together. The new review
unit should be a bounded batch while keeping review state tied to each provider
session.

## Batch Selection

`ReviewScanner.scan_once()` should collect eligible transcript sessions before
calling the reviewer. Eligibility keeps the current filters:

- transcript file is inside the review window;
- file has been idle long enough;
- normalized session has turns;
- session is not an internal RightMemory provider session;
- session has not already been reviewed by source and session id.

Each eligible item should carry the normalized session plus its transcript path
and modification time. After collection, the scanner sorts candidates by
modification time from oldest to newest and takes the first `batch_size`
sessions.

This means `rightmemory review scan --once` still performs one bounded unit of
work, but that unit is one batch. The result's `reviewed` count means the number
of provider sessions successfully marked reviewed. With the default batch size,
a successful full batch reports `reviewed: 3`.

`review watch` can keep its current loop shape. Because it already scans again
when `reviewed > 0` or `failed > 0`, backlog processing and failure retry remain
prompt.

## Configuration

Add `[review].batch_size` with default `3`.

The value should be a positive integer. Existing configs without this field
continue to work with the default. Do not add a CLI override in this change; the
batch size is review policy rather than a common one-off debugging parameter.

## Reviewer Invocation

The reviewer should receive one batch payload instead of one session payload:

```json
{
  "batch_id": "review-batch-...",
  "sessions": [
    {
      "source": "codex",
      "session_id": "s1",
      "project": "/repo",
      "started_at": "...",
      "ended_at": "...",
      "turns": []
    }
  ]
}
```

Sessions in the payload are ordered by the scanner's chosen time order. A batch
may include different providers, so each session keeps its `source`.

The reviewer runtime session id should be synthetic and derived from the batch,
rather than borrowed from one provider session. This makes the runtime history
match the actual unit of context. The id should be safe for the existing session
storage and short enough not to become awkward when provider session ids are
long.

## Review State

Persisted review state remains session-level:

```json
{
  "sessions": {
    "codex:s1": {
      "source": "codex",
      "session_id": "s1",
      "last_reviewed_at": "..."
    }
  }
}
```

On reviewer success, every provider session in the batch is marked reviewed. On
reviewer failure after retry, none of the batch sessions are marked reviewed.
The next scan can then retry the same time-adjacent batch.

This change does not require a migration for existing review state because the
state file shape and keys stay the same.

## Reviewer Prompt

Keep the reviewer role prompt close to its current form. The main changes are:
the input is an ordered batch, the reviewer should look for cross-session
patterns, and review commits use a batch title with session ids in the body.

Proposed prompt diff:

```diff
diff --git a/rightmemory/prompts/reviewer.md b/rightmemory/prompts/reviewer.md
--- a/rightmemory/prompts/reviewer.md
+++ b/rightmemory/prompts/reviewer.md
@@
 # Reviewer Role
 
-Review a normalized provider chat session after it has gone idle. This role complements explicit memory updates by finding durable signals the main agent may miss, especially durable user context, implicit user preferences, workflow expectations, emergent reusable workflows, repeated corrections, stable setup facts, and reusable lessons.
+Review an ordered batch of normalized provider chat sessions after they have
+gone idle. This role complements explicit memory updates by finding durable
+signals the main agent may miss, especially durable user context, implicit user
+preferences, workflow expectations, emergent reusable workflows, repeated
+corrections, stable setup facts, reusable lessons, and patterns that become
+clearer across adjacent sessions.
 
 ## Review Input
 
-The caller message includes `Normalized session JSON` with session metadata and ordered `turns` containing `user` and `assistant`.
+The caller message includes `Normalized transcript batch JSON` with a
+`batch_id` and ordered `sessions`. Each session includes metadata and ordered
+`turns` containing `user` and `assistant`.
 
-Review the session as a whole. Reviewed transcripts are usually historical, and this role sees one session rather than the later project timeline. Prefer memory that prevents the user from having to correct or remind future agents again. If the session contains no durable user context, behavioral signal, reusable lesson, stable setup fact, or useful memory correction, make no edits and reply exactly: `Nothing to save.`
+Review the batch as a whole. Reviewed transcripts are usually historical, and
+this role sees adjacent sessions rather than the later project timeline. Prefer
+memory that prevents the user from having to correct or remind future agents
+again. Look for durable signals within individual sessions and for repeated
+corrections, preferences, workflow expectations, setup facts, or conflicts that
+become clearer across sessions. If the batch contains no durable user context,
+behavioral signal, reusable lesson, stable setup fact, useful memory
+correction, or useful cross-session pattern, make no edits and reply exactly:
+`Nothing to save.`
@@
-Preserve the durable meaning, not the event narrative. Prefer compact behavior or fact nodes over session summaries. For project work, usually skip progress updates, temporary blockers, open plans, early assumptions, and implementation state; save project context when it is clearly reusable beyond the task state shown in the session.
+Preserve the durable meaning, not the event narrative. Prefer compact behavior
+or fact nodes over session or batch summaries. For project work, usually skip
+progress updates, temporary blockers, open plans, early assumptions, and
+implementation state; save project context when it is clearly reusable beyond
+the task state shown in the batch.
@@
-- If you changed memory, stage touched `MEMORY.md` / `MEMORY_*.md` files and commit them. Use `memory: review <source> transcript <session_id>` when source and session id are known.
+- If you changed memory, stage touched `MEMORY.md` / `MEMORY_*.md` files and
+  commit them. Use commit title `memory: review transcript batch`. Include the
+  reviewed sessions in the commit body as `<source>:<session_id>` entries.
 
 ## Final Reply
 
-For edits, briefly list touched heading ids or node ids and any anomalies. For no-op reviews, reply exactly `Nothing to save.`
+For edits, briefly list touched heading ids or node ids, reviewed sessions, and
+any anomalies. For no-op reviews, reply exactly `Nothing to save.`
```

The implementation can smooth exact wording, but should keep the change narrow.

## Documentation And Agent Notes

Update the README automatic review section so it describes batch scans, default
batch size, time-adjacent selection, and session-level state.

Update `AGENTS.md` during implementation so its reviewer scan description
matches the new behavior. Keep the upgrade-safety note concise:

```md
## Upgrade Safety
- Before changing persisted state or install/watch/config behavior, check upgrade impact.
- If old state may break, be ignored, or need migration, tell the user and ask before implementing.
- Do not silently discard or rewrite existing user state.
```

## Upgrade Behavior

This feature should not require migration:

- existing `review.state.json` keeps the same shape;
- existing `rightmemory.toml` files get the default batch size;
- old reviewer runtime sessions can remain under `.runtime/`;
- batch payloads are new caller messages, not persisted state schema.

The visible behavior change is intentional: `scan --once` reviews one batch
instead of one session.

## Tests

Focused tests should cover:

- default and configured `batch_size`;
- rejection of invalid `batch_size`;
- one scan reviews up to three eligible sessions by default;
- batch order follows transcript modification time rather than file name;
- successful batches mark every included provider session reviewed;
- failed batches mark no included provider sessions reviewed;
- old, idle, empty, internal, and already-reviewed sessions remain filtered;
- reviewer messages contain `Normalized transcript batch JSON`;
- mixed-provider batches preserve each session's source;
- README and prompt expectations stay aligned with batch review.

## Out Of Scope

This design does not add semantic clustering, project-based grouping, a
`--batch-size` CLI flag, review state migration, or re-review of resumed
provider sessions that are already marked reviewed.

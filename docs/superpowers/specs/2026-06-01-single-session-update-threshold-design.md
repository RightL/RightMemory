# Single-Session Update Threshold Design

## Problem

Async update submission already batches work across sessions by candidate count,
with a default target of 15 candidates. That works well when several sessions
submit memory candidates around the same time.

A long active session has a different failure mode: each submit resets that
session's one-hour quiet period, so the queue can keep growing while the user
and agent continue working. The pending candidates remain visible to retrieval
as recent submitted memory, but durable memory updates can be delayed for too
long and the per-session backlog can become noisy.

## Goal

Use the existing async update target as a pressure valve for a single busy
session. When one session accumulates enough pending candidates, the worker can
process it before the quiet period expires.

## Design

`[update.async].target_batch_candidates` continues to mean "enough pending
candidate pressure to run update work." The default remains 15. The value now
applies in two related places:

- cross-session batching, where the worker groups eligible session queues until
  the selected batch reaches the target;
- single-session pressure, where a normal waiting session becomes eligible when
  its pending queue length reaches the same target.

The submit path stays lightweight. `rightmemory update submit` appends the new
candidate, records the normal `next_flush_at`, and wakes or starts the global
worker as it does today. It does not run update work inline.

Worker selection changes for normal waiting sessions:

- if `next_flush_at` has passed, the session is eligible as it is today;
- if `len(pending) >= target_batch_candidates`, the session is also eligible,
  even when `next_flush_at` is still in the future;
- if neither condition is true, the session keeps waiting.

The worker keeps selected session queues whole. A session with 15 or more
pending candidates runs as one session queue rather than being split into chunks.
The target remains a fill threshold and pressure threshold, not a hard cap.

## State And Config

No new state fields are needed. The existing `pending` list and `next_flush_at`
timestamp provide enough information for the worker to decide whether a normal
waiting session is eligible.

No new config key is needed. Reusing `target_batch_candidates` keeps the mental
model small: the same number answers "how many submitted candidates is enough
pressure to update memory?"

Existing async recovery behavior stays intact. Retryable recovery work is still
selected before normal waiting work, because it represents known failed backlog.
Manual-recovery sessions remain blocked until `rightmemory update retry` runs.

## Status And Pull Output

`rightmemory status` can keep reporting threshold-triggered work as ordinary
pending work until the worker moves it into `current_batch`. No new status
category is needed because the session has not failed and does not require user
action.

`rightmemory update pull --session <id>` already shows the useful details for a
single session: `pending`, `pending_ids`, and `next_flush_at`.

## Documentation

Implementation should fold this scheduling rule into the batched command
updates note in `DESIGN_NOTES.md`, where durable design rationale belongs. The
note should explain that the quiet period is the normal path for fresh submitted
work, while a single busy session can become eligible earlier when its pending
queue reaches `target_batch_candidates`.

`README.md` should stay focused on user-facing command behavior and does not
need this internal scheduling detail.

## Testing

Focused tests should cover these behaviors:

- one normal waiting session with 15 pending candidates is selected before
  `next_flush_at`;
- one normal waiting session with 14 pending candidates still waits for
  `next_flush_at`;
- cross-session batching keeps its current behavior when no single session
  reaches the target;
- retryable recovery work is selected before threshold-triggered normal work.

## Upgrade Impact

The persisted state shape does not change, so existing async update state files
continue to load normally. This behavior change does not alter memory schema,
role prompts, or how existing memory should be interpreted, so it does not need
a semantic upgrade note.

# Cross-Session Async Update Batching Design

## Problem

Async `rightmemory update submit` currently batches pending update candidates
only within a single submitted `--session` id. This avoids one updater call per
candidate, but it can still create one updater role turn per active agent
session. When many sessions each submit a few small memory candidates, the
fixed role prompt and memory-reading cost dominate the useful work.

RightMemory already has a precedent for cross-session semantic work: the
reviewer scans provider transcripts and reviews several session transcripts in
one batch. Async update should get similar token-efficiency benefits while
preserving the existing per-session caller contract.

## Goals

- Keep the public async update UX unchanged:
  `rightmemory update submit --session <id>`, `pull`, and `undo`.
- Batch eligible update queues from multiple session ids into one update role
  turn.
- Use candidate count, not session count, as the primary fill target.
- Default to running when at least $15$ eligible candidates are available.
- Include whole eligible session queues when building a batch, even when that
  overshoots the $15$ candidate target.
- Do not impose a hard batch cap.
- Prefer token efficiency and memory quality over quick consolidation; allow an
  eligible queue to wait up to $24$ hours for a fuller batch.
- Run at most one async update worker process per memory root.
- Keep `Recent submitted memory` behavior the same from retrieve callers'
  perspective.
- Preserve retry durability: failed or interrupted updater work returns to the
  original per-session queues.

## Non-Goals

- Do not replace `submit`, `pull`, or `undo` with a visible global queue API.
- Do not move async update state entirely away from per-session files.
- Do not split an eligible session queue solely to hit the target candidate
  count.
- Do not add model-context or text-size caps in this design.
- Do not preserve the private `_submitted-worker --session <id>` command for
  compatibility.
- Do not change retrieval's caller-facing treatment of recent submitted memory.

## Configuration

Add an async update config subsection:

```toml
[update.async]
target_batch_candidates = 15
max_wait_seconds = 86400
```

Both fields are optional. `target_batch_candidates` is a positive integer and
defaults to $15$. `max_wait_seconds` is a positive integer and defaults to
$86400$, or $24$ hours.

The config lives under `[update]` because it controls submitted update
scheduling, not the update role's model executor. It should coexist with
existing `[update.model]` or `[update.agent_cli]` executor configuration.

## Public Contract

The public commands keep their current meanings:

```bash
rightmemory update submit --session <agent-session-id> "what changed"
rightmemory update pull --session <agent-session-id>
rightmemory update undo --session <agent-session-id> <pending-candidate-id>
```

`submit` still appends a candidate to that session's queue and resets that
session's quiet period. `pull` still reports that session's status, pending
count, current batch count, timing, result, and error. `undo` still removes only
candidates that are pending in that session.

The implementation detail changes from one worker per session to one global
async update worker. A caller should not need to know which other sessions were
batched with its candidates.

## Runtime State

Per-session async state remains the durable source of submitted candidate work.
The existing state shape can continue to hold:

- `session_id`
- `role`
- `status`
- `phase`
- `started_at`
- `finished_at`
- `pid`
- `result`
- `error`
- `next_flush_at`
- `current_batch`
- `pending`
- `next_id`

Cross-session batching adds optional batch metadata such as `batch_id` to
running session states. Readers must tolerate missing batch metadata so older
state files remain valid.

Add global worker state under the update async runtime directory, for example:

```text
<memory-root>/.runtime/async/update/worker.json
<memory-root>/.runtime/async/update/worker.lock
```

The global worker state records the active worker pid, start time, current
batch id, and included session ids. It is operational state only; per-session
state remains authoritative for candidate durability and caller-facing status.

## Eligibility

A session queue is eligible for cross-session batching when all of these are
true:

- The state file belongs to the `update` role.
- `pending` is non-empty.
- `current_batch` is empty.
- The per-session quiet period has expired:
  `$next\_flush\_at \le now$`.
- The state is not corrupt and is not a failed state that requires submit-time
  recovery first.

`submit` preserves the existing debounce behavior. Every new candidate sets the
session's `next_flush_at` to submit time plus $1$ hour. A session is not
eligible merely because it has pending candidates; it becomes eligible only
after its own quiet period ends.

## Batching Policy

The global worker scans all eligible session queues and sorts them by readiness
time, then session id as a deterministic tie-breaker. Readiness time is the
time at which the queue's quiet period expired.

To build a batch, the worker includes whole eligible session queues until the
total candidate count reaches at least `target_batch_candidates`. If adding the
next whole session overshoots the target, the worker still includes it. There
is no hard cap.

If the eligible candidate count is below `target_batch_candidates`, the worker
waits for more sessions to become eligible. The max-age fallback prevents
indefinite delay: when the oldest eligible queue has waited at least
`max_wait_seconds` after its readiness time, the worker runs all currently
eligible queues even if the total remains below the target.

This policy makes the target a fill threshold rather than a strict limit.
Session integrity wins over exact batch size.

## Worker Coordination

`submit` starts the global worker only when no live global worker exists. The
worker is protected by a global lock so concurrent submits cannot spawn several
competing updater processes.

The worker loop:

1. Records its pid in global worker state.
2. Scans per-session states under `.runtime/async/update/`.
3. Recovers stale global worker state if the recorded pid is dead.
4. Waits until either enough eligible candidates exist or the max-age fallback
   fires.
5. Moves included candidates from each selected session's `pending` list to
   that session's `current_batch`.
6. Runs one update role turn with a synthetic batch session id.
7. Applies success or failure to every included session state.
8. Continues scanning if more pending work remains; otherwise clears global
   worker state and exits.

The synthetic update role session id should be stable and descriptive for the
batch, for example a digest of included session ids, candidate ids, and
submission timestamps. CLI-agent provider history is then grouped by the actual
cross-session batch instead of being attached to an arbitrary user session.

## Update Role Message

The update role receives one message containing all candidates in the batch.
The message should preserve each candidate's origin metadata:

```text
Process the following submitted memory update candidates as one batch.
Use the standalone update instructions to decide what should become durable
memory.

Candidates:
[update session: agent-a | candidate 1 | submitted_at: ...]
...

[update session: agent-b | candidate 3 | submitted_at: ...]
...
```

The prompt already instructs the updater to reconcile related candidates as a
whole and not preserve operational candidate ids unless meaningful. The message
metadata is for traceability, retry, and final reporting, not for direct memory
content.

## Success Handling

After the update role turn succeeds and the normal isolated write path lands
or accepts a valid no-op, the worker updates every included session:

- Clear `current_batch`.
- Store the updater result for that session.
- If no newer pending candidates arrived during the run, mark the session
  `succeeded`.
- If newer pending candidates arrived during the run, move the session back to
  waiting with a fresh `next_flush_at`.

Dreamer trigger points increment by the total number of processed candidates:

$$
\text{points} =
\text{processed candidate count} \times \text{update candidate points}
$$

The increment happens once for the successful cross-session batch, after all
included session states have accepted the success.

## Failure Handling

If the update role turn fails, the isolated write fails, or the worker dies
before recording success, every included session returns its `current_batch` to
the front of `pending`, records the shared error, clears `current_batch`, and
becomes `failed`.

The next `submit` for a failed session preserves retry order by re-enqueueing
the recovered candidates before the new candidate, matching today's behavior.
It also starts the global worker if needed.

If global worker state names a dead pid, a subsequent `submit`, `pull`, or
worker startup can detect it and recover affected running states. Recovery must
be conservative: candidates are not dropped unless the corresponding session
state has already recorded success.

## Retrieval And Undo

Retrieval behavior remains the same from the caller's perspective. The
`Recent submitted memory` block continues to be built from per-session async
update states across the memory root. It includes candidates still in `pending`
and candidates currently in `current_batch`, so retrieve callers can see
unconsolidated memory while the cross-session worker is waiting or running.

The displayed metadata remains per original update session and candidate id.
Cross-session batch ids are not exposed in retrieve output unless needed for
debugging.

`undo --session <id> <candidate-id>` remains limited to candidates in that
session's `pending` list. Candidates already moved to `current_batch` cannot be
canceled, which matches the existing semantics.

## CLI Changes

The private `_submitted-worker --session <id>` command does not need to be
kept. Replace it with a private global worker entry point:

```bash
rightmemory update _async-worker
```

Tests and user-facing docs should not teach users to call it directly.

## Testing

Tests should cover:

- Config defaults and custom `[update.async]` values.
- Rejection of non-positive async update config values.
- Submit starts only one global worker while the worker is alive.
- Multiple eligible sessions run in one updater call.
- The batch fills by candidate count rather than session count.
- Whole-session inclusion can overshoot the $15$ target.
- Below-target eligible work waits until the $24$ hour max-age fallback.
- `pull --session <id>` remains per-session and reports running/succeeded/failed
  state for cross-session batches.
- `undo` removes only pending candidates and cannot cancel `current_batch`
  candidates.
- Candidates submitted during a running batch remain pending and get a fresh
  quiet period.
- Failure returns every included session's `current_batch` to the front of
  `pending`.
- Dead global worker recovery returns in-flight candidates to pending.
- `Recent submitted memory` includes pending and in-flight candidates with the
  original session/candidate metadata.
- Dreamer trigger points increment by the total processed candidate count.
- CLI-agent mode uses a synthetic batch session id for the updater turn.

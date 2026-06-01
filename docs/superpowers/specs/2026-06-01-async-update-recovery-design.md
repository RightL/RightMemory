# Async Update Recovery Design

## Problem

Async update currently preserves candidates when an update worker dies or an
update batch fails, but the preserved work can move into a `failed` session
state that the global worker no longer selects. This leaves durable pending
candidates visible in status without a path back into processing unless a later
submit happens to use the same session id. The result is operationally confusing:
work is saved, but it can sit forever.

The status view also counts all pending candidates together, so failed pending
work looks like ordinary queue backlog even when the worker will not process it.

## Goals

- Recover failed async update work automatically by default.
- Avoid immediate retry loops; a failed batch waits $1$ hour before automatic
  retry.
- Stop automatic retry after $2$ failed attempts and require manual recovery.
- Keep submitted candidates durable through every retry transition.
- Keep normal `update submit` output unchanged unless the target session is in
  manual recovery.
- Make manual recovery global by default: `rightmemory update retry` takes no
  required session id and requeues all manual-recovery sessions.
- Make status distinguish normal pending, retrying, manual recovery, and current
  batch counts.
- Keep README out of retry policy detail; the CLI warning and help should reveal
  the recovery command when it matters.

## Non-Goals

- Do not change update role prompt semantics or memory-writing authority.
- Do not split session queues to hit the normal batch target.
- Do not make `submit` a hidden recovery command.
- Do not add a separate retry watcher process.
- Do not expose private worker commands as user-facing recovery tools.

## State Model

Per-session async update state gets explicit retry metadata:

- `attempts`: failed processing attempts for the currently pending work.
- `next_retry_at`: when automatic retry becomes eligible.
- `last_error`: latest worker, runtime, model, or isolated-write error.

The existing `error` field remains caller-facing output. `last_error` can mirror
it or preserve retry-specific detail if the implementation needs that separation.

State meanings:

- `running` with `phase=waiting`: normal submitted work waiting for its quiet
  period or batching threshold.
- `running` with `phase=running`: work currently in a worker batch.
- `failed`: automatic retry cooldown is active or has become retryable.
- `needs_manual_recovery`: automatic retry stopped after $2$ failed attempts.
- `succeeded`: no pending work remains after successful processing.

Missing retry fields in existing state files mean no retry metadata was recorded
by that state version.

## Normal Lane

Fresh submissions use the existing batching behavior:

1. `rightmemory update submit --session <session-id> ...` appends a candidate to
   that session.
2. The session gets the normal quiet period.
3. The worker batches eligible waiting sessions until the normal target of about
   $15$ candidates is reached.
4. If fewer than the target become available, the existing max-wait fallback can
   still run the batch later.

This lane optimizes for token efficiency and memory quality.

## Recovery Lane

Failed work uses a separate recovery lane:

1. On the first failed processing attempt, the current batch returns to the front
   of `pending`, `attempts` becomes $1$, `next_retry_at` becomes the current
   time plus $1$ hour, and status becomes `failed`.
2. When `next_retry_at` is reached, the worker treats the session as recovery
   work. Recovery work bypasses the normal $15$-candidate fill threshold.
3. If recovery succeeds, retry metadata clears.
4. If recovery fails again, `attempts` becomes $2$ and status becomes
   `needs_manual_recovery`.

Recovery sessions are selected before normal batching because they represent
known broken backlog. If several recovery sessions are ready, the worker may run
them together as whole session queues.

## Submit Behavior

`submit` saves work. It does not silently recover unrelated sessions.

For normal waiting/running sessions, `submit` keeps the current output shape.

For sessions in automatic retry cooldown, `submit` appends the new candidate to
that session's pending queue and keeps the current output shape. It does not
reset retry attempts, clear the error, or turn recovery into normal batching.

For sessions in `needs_manual_recovery`, `submit` appends the new candidate and
keeps the session blocked. It prints the normal state block plus a strong warning:

```text
CRITICAL: this async memory update session is blocked after 2 failed attempts.
The new candidate was saved, but this session will not be processed until manual recovery runs.
Agent: report this issue to the user and suggest `rightmemory update retry`.
```

This makes severe queue failure visible to the agent reading command output while
still preserving the new candidate.

## Manual Recovery Command

Add:

```bash
rightmemory update retry
```

The command takes no required `--session`. By default it finds all
`needs_manual_recovery` async update sessions, requeues them for immediate
recovery, and wakes or starts the global worker.

Manual retry resets the selected sessions so they are recovery-eligible now. It
does not route them through the normal $15$-candidate fill threshold.

The command output should summarize:

- number of sessions requeued;
- number of candidates requeued;
- number of manual-recovery sessions skipped, if any were malformed or empty;
- whether a worker was started or woken.

## Worker Selection

The worker should select work in this order:

1. Recovery sessions with status `failed`, pending candidates, and
   $next\_retry\_at \le \text{now}$.
2. Normal waiting sessions that satisfy the existing quiet-period and batching
   policy.

If recovery work is available, the worker runs it without waiting for the normal
batch target. Normal batching remains unchanged for fresh work.

Failure handling must preserve candidate order:

$$
\text{pending} \coloneqq \text{current\_batch} + \text{pending}
$$

$$
\text{current\_batch} \coloneqq []
$$

No retry transition may drop candidates.

## Status

`rightmemory status` should stop presenting all pending work as one undifferentiated
queue. The async update section should show separate counts:

```text
pending: 10 candidates across 4 sessions
retrying: 2 candidates across 1 session
manual recovery: 5 candidates across 2 sessions
current batch: 0 candidates across 0 sessions
```

`pending` means ordinary waiting submitted work. `retrying` means failed work
that is in cooldown or ready for automatic recovery. `manual recovery` means no
automatic processing will happen until `rightmemory update retry` runs.

Recent issues should still include the relevant failed sessions, but status
should make clear whether the failure is automatically recovering or needs user
action.

## Legacy State Compatibility

Existing `failed` states with pending candidates and no retry metadata should be
treated as `needs_manual_recovery`. They represent old stuck work whose failure
history is unknown, so silently replaying them after upgrade would be surprising.

`rightmemory update retry` is the intended recovery path for those legacy states.

State files without retry fields but in normal running, waiting, or succeeded
states continue to load as before.

## Design Notes

Do not add README detail for this policy. The durable design rationale belongs in
`DESIGN_NOTES.md`, folded into the existing batched command updates note:

- normal submitted work batches for efficiency;
- failed submitted work uses a recovery lane because backlog correctness matters
  more than batch efficiency;
- repeated failure stops at manual recovery rather than silently looping;
- `submit` saves candidates, while `retry` owns recovery.

## Testing

Tests should cover:

- first batch failure preserves candidates and schedules retry after $1$ hour;
- retryable failed sessions bypass the normal $15$-candidate target;
- second failure moves the session to `needs_manual_recovery`;
- `submit` into retry cooldown appends without extra warning or attempt reset;
- `submit` into manual recovery appends and prints the critical warning;
- `rightmemory update retry` requeues all manual-recovery sessions without a
  required session id;
- manual retry work is eligible immediately and bypasses the normal batch target;
- successful recovery clears retry metadata;
- legacy failed pending states report as manual recovery;
- status separates normal pending, retrying, manual recovery, and current batch
  counts;
- recent submitted memory still includes pending and current batch candidates
  from all async update states.

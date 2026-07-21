# Cross-Device Update Candidate Sync Design

## Decision Summary

RightMemory will synchronize pending update candidates through the existing Git
upstream without adding a service, database, provider API, or separate queue
branch.

- A candidate starts in a local ignored outbox.
- A candidate that has never entered a publish attempt may be processed offline.
- Publishing copies the candidate into a machine-owned `update_queue/` directory
  on the synchronized branch.
- Once publication starts or succeeds, processing requires an online Git lease.
- Git accepts only one competing lease update, so only the winning device starts
  the updater.
- Successful processing publishes the Memory/Pursuit result and consumes the
  exact claimed candidates in one fenced Git transaction.

This preserves local-first offline work while providing cross-device continuity
during the existing batching window.

## Problem

Async update candidates currently live only under
`.runtime/async/update/*.json`. The root Git ignore rules exclude `.runtime`, and
the sync path validator rejects paths outside the synchronized state allowlist.
As a result, a candidate can remain visible only on its originating device for
as long as the batching policy delays reconciliation. Another device cannot see
that pending evidence through recent-submitted retrieval and cannot process,
cancel, or recover it.

Reducing the maximum wait would narrow the window but would not handle abrupt
device changes, offline work, or a candidate whose originating device stops
running. A fixed processor device would avoid races but would introduce a single
availability dependency. The selected design therefore uses Git both as the
candidate transport and as the active-worker coordination authority.

## Goals

- Make synchronized pending candidates visible on every device.
- Preserve successful offline submission and safe offline processing when a
  candidate is provably local-only.
- Allow any online device to process a synchronized candidate batch.
- Prevent two devices from publishing effects for the same candidate batch.
- Preserve whole-session batching, retry cooldown, manual recovery, undo, and
  recent-submitted retrieval semantics.
- Keep the active checkout clean and recover every ambiguous Git outcome by
  inspecting repository state.
- Remain compatible with ordinary Git remotes rather than depending on a hosted
  provider API.

## Non-Goals

- Do not hide candidate text from Git history after it has been synchronized.
- Do not process a synchronized candidate while the Git upstream is unavailable.
- Do not introduce distributed coordination for Dreamer, Insight, Pruner, or
  other automatic roles in this change.
- Do not add immediate terminal-candidate flushing. It remains an independent
  future latency optimization.
- Do not support untrusted devices sharing one memory repository.
- Do not add a permanent processed-candidate database.

## State Model

### Local outbox

New candidates are first written to an ignored local outbox:

```text
.runtime/async/update/
  outbox/<candidate-id>.json
  publication/<candidate-id>.json
```

The outbox is authoritative while the candidate is guaranteed not to have been
published. A publication marker is durably written before any remote push is
attempted. While that marker has an unresolved outcome, offline processing is
forbidden because the remote may have accepted a push whose response was lost.

The existing local worker state continues to own process identifiers, locks,
verbose errors, last local results, and recent-submitted delivery markers. None
of those machine-specific artifacts is synchronized.

### Synchronized inbox

Published candidates and cross-device coordination state use this tracked
layout:

```text
update_queue/
  candidates/<candidate-id>.json
  recovery/<session-key>.json
  lease.json
```

Candidate files are immutable. Each contains:

- `schema_version`;
- a globally unique `candidate_id` generated before it enters the outbox;
- `session_id` for provenance and whole-session batching;
- `submitted_at` in UTC;
- `message` containing the submitted evidence;
- `origin_device_id` for diagnostics;
- optional legacy identity metadata used only for migration and compatibility.

The canonical candidate id is a lowercase UUID hex string. User-facing commands
may accept an unambiguous prefix while storing and comparing the full id.

Recovery files are keyed by a filesystem-safe hash of `session_id`. They contain
only synchronized control state needed to preserve retry behavior across
devices: attempt count, cooldown eligibility, manual-recovery status, and a
bounded error summary. Detailed logs remain local.

`lease.json` exists only while a synchronized batch is claimed. It records:

- a schema version;
- a random fencing token;
- the owning device id;
- the exact candidate ids in the batch;
- the deterministic batch operation id;
- acquisition and expiry timestamps.

New submissions never join an existing lease. They remain pending for a later
batch.

Within each session, candidates are ordered by `submitted_at` and then
`candidate_id`; sessions are ordered by `session_id`. The batch operation id is
the canonical hash of that ordered session-and-candidate structure.

## Authority Invariants

1. A live candidate id has one authoritative lane: local outbox, unresolved
   publication, or synchronized inbox.
2. A local candidate may be processed offline only when no publication attempt
   has begun.
3. A synchronized candidate may be processed only by the owner of the current
   upstream fencing token.
4. Candidate absence is terminal only when the corresponding processing or undo
   transaction is known to have landed, or Git history proves the candidate was
   previously published and is no longer live.
5. A worker deletes only candidate ids named by its lease.
6. Memory/Pursuit publication and synchronized candidate consumption form one
   recoverable Git transaction, including a valid updater no-op.
7. A stale fencing token can never publish, even if its model call eventually
   finishes.
8. One updater call contains either local-only candidates or synchronized
   candidates, never a mixture of both authority lanes.

## Components

### Local candidate outbox

The local outbox owns creation, local locking, offline eligibility, and durable
publication-intent markers. It provides candidate snapshots to local retrieval
and to either the local worker or publisher, never both concurrently.

### Queue publisher

The publisher moves a local candidate into `update_queue/candidates/` through a
temporary Git worktree based on an exact fetched upstream commit. It pushes the
candidate before removing the outbox copy. A failed or ambiguous push leaves the
publication marker in place until an online recovery check resolves the outcome.

### Synchronized queue store

The synchronized queue store validates candidate, recovery, and lease files;
derives pending/current/retrying/manual-recovery views; and selects whole-session
batches using the existing batching policy. Different device tuning may change
when a device attempts a claim, but the lease determines which attempt wins.

### Git queue coordinator

The coordinator performs claim, renewal, takeover, undo, recovery, and
finalization as exact-upstream compare-and-swap transactions in temporary
worktrees. It never lands a failed claim commit in the active checkout.

### Async update worker

The worker retains the existing local process-leadership lock. For local-only
work it uses that lock and the existing local isolated-write flow. For
synchronized work it must acquire the Git lease before invoking the updater and
must pass the fencing token into finalization.

## Data Flows

### Submission

1. `update submit` creates a stable candidate id and atomically writes the local
   outbox file.
2. The command reports the id and current visibility.
3. When the upstream is reachable, the publisher writes a durable publication
   marker, creates a candidate-addition commit in a temporary worktree, and
   pushes it.
4. After fetching and confirming the candidate on the upstream, Runtime advances
   the active root safely and removes the local outbox copy and marker.
5. If the upstream is unavailable before a push attempt begins, the candidate
   remains local-only and eligible for offline processing.

### Ambiguous publication recovery

After a crash, timeout, or lost push response, Runtime fetches the upstream and
checks the candidate path and its reachable history:

- present at the upstream tip: publication succeeded and the candidate becomes
  synchronized;
- absent at the tip but present in reachable upstream history: another device
  already processed or canceled it, so the local copy is discarded;
- absent from both tip and reachable history: publication did not land, so the
  marker may be cleared and the candidate becomes local-only again.

Managed sync never rewrites upstream history, making this check authoritative.

### Local-only offline processing

1. The local worker locks the outbox candidate before publication starts.
2. The queue publisher cannot expose that candidate while the lock is held.
3. The updater processes the local batch using the existing isolated-write
   behavior.
4. On success or a valid no-op, Runtime removes the outbox candidates locally.
5. Any Memory/Pursuit commit synchronizes normally when Git becomes available.

Another device cannot race this work because the candidate never appears in the
synchronized inbox. Concurrent semantic changes from other devices remain an
ordinary Memory sync reconciliation concern.

### Synchronized claim

1. An online worker completes the ordinary sync preflight so the active root has
   no unpublished semantic work, then fetches the exact upstream tip and selects
   an eligible batch.
2. It creates `lease.json` in a temporary worktree and pushes a normal
   fast-forward update.
3. Competing claims based on the same upstream tip diverge. Git accepts the first
   update and rejects the rest as non-fast-forward pushes.
4. A rejected claimant discards its temporary worktree, refetches, and does not
   invoke the updater.
5. The winner confirms its lease on the upstream before starting model work.

### Synchronized finalization

1. The updater runs against the exact claimed candidate snapshot.
2. Runtime prepares the semantic result without publishing it to the active root.
3. The finalizer fetches, verifies that its fencing token is still current, and
   incorporates unrelated newly submitted candidate files without adding them
   to the batch.
4. It publishes the Memory/Pursuit result, deletes the claimed candidate files,
   updates or removes recovery files, and clears `lease.json` through one fenced
   branch update.
5. A valid semantic no-op still publishes candidate consumption and lease
   clearing.
6. Only after the upstream accepts the transaction does Runtime advance the
   active checkout to that exact commit.

If the remote accepts the push but the client loses the response, the operation
id and upstream tree allow startup recovery to recognize success without another
model call.

### Failure and retry

When synchronized processing fails, the current lease owner retains the
candidate files, records the existing retry policy in the affected session
recovery files, and clears the lease through a fenced transaction. Eligible
recovery work bypasses the normal fill threshold as it does today. Repeated
failure enters synchronized manual-recovery state, so another device cannot
reset attempts merely by seeing the queue for the first time.

A local-only failure retains equivalent retry state locally. If that work is
later published, its relevant recovery state is published with it.

If the upstream becomes unavailable while recording a synchronized failure, the
device retains a local prepared failure transaction and does not start another
attempt. Recovery first checks whether its lease is still current: it publishes
the prepared failure when it still owns the lease, or discards it after a
verified takeover.

### Lease renewal and takeover

The initial lease lasts one hour and the winner attempts renewal every fifteen
minutes while model or finalization work remains active. Expiry time controls
availability, not correctness: any takeover replaces the fencing token through
another compare-and-swap push. An old worker must fetch and verify its token
before finalization, so clock skew or a delayed process can at worst duplicate
model computation, never committed effects.

### Undo

A local-only candidate can be canceled offline under its outbox lock. A
synchronized candidate requires an online compare-and-swap deletion. Claim and
undo races are resolved by the upstream branch update: if the claim wins, undo
reports that the candidate is already processing; if undo wins, the claimant
refetches and excludes it.

## Retrieval, Status, and Commands

Recent-submitted retrieval reads the union of local outbox and synchronized
candidate files, deduplicated by global candidate id. Delivery tracking remains
local, so a newly used device may surface an existing synchronized candidate
once even if another device already displayed it.

`update pull --session` and `rightmemory status` derive live state from both
lanes and distinguish:

- `local-only`;
- `publication-unknown`;
- synchronized pending;
- claimed/current batch;
- retry cooldown;
- manual recovery.

`update undo` continues to require `--session` as a safety scope and accepts the
printed global id or an unambiguous prefix. A migrated candidate may also accept
its legacy numeric id while it remains live.

`update retry` applies local recovery immediately. Synchronized manual recovery
requires an online fenced transaction before any device can claim the work.

When a sync pull introduces eligible candidates, the sync watcher wakes or
starts the local async worker. Retrieval itself remains local by default and does
not add an unconditional network request.

## Git Integration

The installer Git ignore allowlist and sync path validator will admit only the
defined `update_queue/` files. Queue JSON is machine-owned synchronized state and
is validated before an incoming candidate can reach the active checkout.

Candidate-only additions merge deterministically because filenames are globally
unique. Queue coordination files are never sent to `sync-reconciler`; Runtime
resolves them through fencing rules. Existing semantic sync reconciliation
continues to handle concurrent Memory/Pursuit edits. A candidate transaction
must preserve unrelated queue additions during such reconciliation.

Candidate text remains in Git history after consumption or undo. The first
version intentionally adds no compaction, history rewriting, encryption, or
alternate transport.

## Compatibility and Migration

Existing per-session async state is migrated under the current worker and memory
write locks. A live old worker delays migration until it exits. Pending and
recoverable jobs become local outbox candidates because old versions could not
have synchronized them.

Migration creates deterministic UUID values from the complete legacy identity
and payload so a crash can repeat migration without duplicating candidates. It
preserves session provenance, submission order, retry attempts, cooldown,
manual-recovery state, and legacy numeric ids. A durable local migration marker
prevents the old state from being read as a second queue. Legacy files may remain
as ignored recovery evidence; they are not committed.

Sync-disabled roots use the same local outbox but retain local process locking
and offline processing. They do not create synchronized queue files until sync
is enabled, avoiding remote-coordination requirements for local-only users.

## Validation and Safety

- Candidate ids, filenames, and embedded ids must match.
- Candidate and recovery session ids must pass existing safe-session validation.
- Candidate payloads are treated as evidence text, never as instructions to the
  coordination layer.
- Unknown schema versions, malformed JSON, invalid lease membership, duplicate
  ids, or inconsistent recovery state fail closed with the exact affected path.
- Symlinks and non-regular queue files are rejected.
- A lease may name only candidate files present in the same synchronized tree.
- Finalization may remove only the candidate ids in its verified lease.
- Queue operations use existing memory and semantic-operation lock ordering to
  avoid deadlocks with sync and isolated writes.

## Testing

Focused unit tests will cover candidate and recovery schemas, safe paths, global
id prefix resolution, local/synchronized state derivation, whole-session batch
selection, retry transitions, migration idempotence, and recent-submitted
deduplication.

Git integration tests will use two independent clones of one bare remote and
cover:

- concurrent unique submissions merge without conflict;
- a local-only candidate processes offline without entering the remote queue;
- local-only and synchronized candidates are never mixed in one updater call;
- publication and local processing cannot acquire the same outbox candidate;
- ambiguous publication recovery detects live, already-consumed, and never-landed
  candidates;
- two simultaneous claims produce one winner and one worker invocation;
- claim versus undo produces one authoritative outcome;
- new candidates arriving during a lease remain pending;
- failed batches retain candidates and synchronize recovery policy;
- expired-lease takeover fences the former owner;
- final push acknowledgement loss recovers without rerunning the updater;
- semantic success and semantic no-op both consume the exact claimed batch;
- malformed incoming queue state is rejected before active publication;
- sync-disabled local behavior remains functional.

Existing async update, sync, isolated-write, status, retrieval, install, and CLI
tests must continue to pass. README and `DESIGN_NOTES.md` will document the
user-visible queue states, offline boundary, and Git-history consequence.

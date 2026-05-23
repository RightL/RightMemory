# Change-Triggered Dreamer And Isolated Memory Writes Design

## Problem

This design replaces the earlier periodic dreamer watcher with a trigger-based
consolidator. Automatic dream cycles happen after enough successful memory work
has accumulated, not because a watcher started or because a fixed time window
elapsed.

There was a related safety problem in the write path. When `update`, `reviewer`,
and `dreamer` edited the main memory repo directly, an interrupted agent process
could leave partial changes in the main worktree. Remote sync can notice dirty
memory state when sync is enabled, but local dirty memory is a memory-integrity
concern even when remote sync is disabled.

The new model has two parts:

- dreamer watch becomes a change-triggered consolidator;
- automatic semantic writes happen in temporary Git worktrees, then land in the
  main memory repo as ordinary memory commits after the run succeeds.

With this model, unfinished automatic agent work is disposable. The durable
sources of retry are the original update candidates, provider transcripts, and
trigger balance, not half-written files from a failed run.

## Goals

- Do not run dreamer immediately when watch starts with no prior trigger state.
- Remove fixed-time dream cycles from automatic watcher behavior.
- Trigger automatic dream cycles from accumulated successful memory work.
- Count successful update candidates as $1$ point each.
- Count successful reviewed provider sessions as $1.5$ points each.
- Trigger dreamer at $50$ accumulated points by default.
- After a successful automatic dream, subtract the threshold and keep the
  remaining points.
- Keep trigger accounting as runtime scheduling state rather than user memory.
- Keep the main memory repo clean during automatic `update`, `reviewer`, and
  `dreamer` writes.
- Discard unfinished automatic worktree changes after failed or interrupted
  runs, then retry from the original work source.
- Handle dirty main memory state independently from remote sync configuration.
- Clean up stale temporary worktrees and branches for managed review and dreamer
  watcher startup.

## Non-Goals

- Do not inspect actual memory diffs to decide whether dreamer should run.
- Do not require structured updater or reviewer output to count facts that were
  written.
- Do not preserve unfinished temporary worktree edits after failure.
- Do not make remote sync required for local dirty-memory detection.
- Do not create merge commits such as `Merge branch ...` for automatic memory
  writes.
- Do not make manual `rightmemory dreamer --session ...` calls consume automatic
  trigger points.
- Do not migrate, delete, or reinterpret the old
  `.runtime/dreamer/watch-state.json` scheduling file.

## Configuration

Dreamer watch uses a role-local watch section:

```toml
[dreamer.watch]
trigger_points = 50
update_candidate_points = 1.0
review_session_points = 1.5
check_interval_seconds = 3000
```

All fields are optional and use the values above by default. `trigger_points`
and both weight fields are positive numbers. `check_interval_seconds` is a
positive integer.

This section belongs under `[dreamer]` because it controls dreamer watch
behavior. `update` and `review` emit successful-work accounting events using the
same configured weights.

Automatic write isolation is used for `update`, `reviewer`, and `dreamer` when
they operate on the main state root. It does not need a user-facing mode switch.
Runtime uses fixed temporary locations and a fixed branch prefix:

```text
<memory-root>/.runtime/worktrees/<role>-<uuid>/
rightmemory-isolated-<role>-<uuid>
```

Temporary worktrees live under `.runtime/worktrees/`. Runtime state and
temporary worktrees are ignored by git.

## Runtime State

Store dreamer trigger accounting in:

```text
<memory-root>/.runtime/dreamer/trigger-state.json
```

The file contains the accumulated point balance and lightweight bookkeeping:

```json
{
  "points": 13.0,
  "updated_at": "2026-05-22T12:00:00+00:00",
  "last_successful_dream_at": "2026-05-22T11:30:00+00:00",
  "last_recovery_at": null
}
```

`points` is required and stores the current trigger balance as a number. The
timestamp fields are optional strings or `null`. The store must write this file
atomically and protect read-modify-write operations with a file lock so
concurrent update, review, and dreamer watchers do not lose increments.

Isolated execution also creates temporary runtime state under
`.runtime/isolated-state/`. Session history and provider-session records are
seeded there for the isolated turn and promoted back to the main state root only
after successful landing or a valid no-op. Failed or interrupted isolated turns
discard that temporary state, so main runtime state does not advance when memory
work did not land.

## Isolated Write Flow

Automatic semantic write roles use an isolation supervisor around the existing
role runtime:

1. For a main-root automatic write, run sync preflight first when sync is
   enabled.
2. Reject dirty or conflicted main memory files before starting semantic work.
3. Create a temporary branch and worktree from the current main repo `HEAD`.
4. Seed temporary runtime state for the role session under
   `.runtime/isolated-state/`.
5. Run the semantic role with memory tools pointed at the temporary worktree,
   temporary state as its state root, and sync disabled inside the nested run.
6. When the role returns successfully, require a clean temporary worktree,
   validate temporary commits and the resulting memory file set, then acquire
   the main memory write lock.
7. Recheck dirty main memory files and unchanged `HEAD`, then cherry-pick
   successful temporary commits into the main repo or accept a clean no-op.
8. Promote temporary session/provider state and update role-specific runtime
   state according to the success rule.
9. Remove the temporary worktree, temporary branch, and temporary state.

The semantic role prompt does not need a new worktree protocol. The role keeps
its current responsibility: inspect memory, edit memory files when useful,
validate, and commit any durable changes. The supervisor owns worktree creation,
integration, cleanup, and retry decisions.

The final main-repo history looks like normal memory work. If the role created a
commit titled `memory: review transcript batch`, the main repo receives a normal
commit with that message, not a merge commit.

An isolated run is eligible to land when the role process returns successfully
and the temporary worktree is clean. If the temporary branch contains commits
after the recorded base `HEAD`, the supervisor validates that every temporary
commit touches memory files only and keeps `MEMORY.md` as a regular file, then
validates the temporary memory file set and cherry-picks those commits back to
the main repo. If the role returns successfully with no commits and a clean
worktree, that is a valid no-op. If the role fails, is killed, or leaves
uncommitted changes behind, the supervisor does not land any temporary commits,
even if some commits were created before the failure.

Durable runtime state stays rooted at the main memory root. The temporary
worktree is for memory files and semantic commits, while the temporary state
root holds session/provider state that may be promoted after success. Async
queues, review state, trigger state, watcher pid files, and logs remain under
the main root `.runtime/`.

## Main Memory Dirty State

Automatic isolated writes do not create dirty files in the main memory repo.
When the main repo is dirty before an automatic write starts, that state is
treated as a separate local memory repair problem.

Main-memory preflight runs regardless of `[sync].enabled`. It checks the tracked
memory surface:

```text
MEMORY.md
MEMORY_*.md
dream_logs/*.md
```

If those paths are clean, the isolated write can proceed.

If those paths are dirty or conflicted, the runtime does not start a new
semantic write on top of them. Sync preflight may invoke the sync repair path
when sync is enabled and the dirty/conflicted state is part of sync handling, but
isolated write preflight remains separate: it rejects dirty main memory files
before the temporary role starts, including when `[sync].enabled` is false.
Remote fetch, merge, and push behavior still depends on `[sync].enabled`.

Dirty main-repo content whose origin is unclear stays visible for repair instead
of being silently discarded or overwritten by a new automatic semantic write.

Temporary isolated worktree changes are different: failed automatic run changes
are known disposable artifacts and are cleaned up rather than repaired.

## Trigger Data Flow

`update submit` does not add trigger points. Submitted candidates are pending
work and might not be processed.

The update worker adds points after a batch has semantically succeeded, the
isolated write has either integrated its commit or completed a valid no-op, and
async state has been updated. The increment is:

$$
\text{points} = \text{candidate count} \times \text{update candidate points}
$$

The reviewer adds points after the reviewer role succeeds and review state is
saved. If the review produced memory commits, those commits must have landed in
the main repo first. The increment is:

$$
\text{points} = \text{reviewed provider session count} \times \text{review session points}
$$

With the defaults, a full review batch of $3$ provider sessions contributes
$4.5$ points.

`rightmemory dreamer watch` wakes every `check_interval_seconds`, reads the
trigger balance, and runs an automatic dream cycle when the balance is at least
`trigger_points`. `rightmemory dreamer watch --interval <seconds>` overrides
that trigger-check cadence for the process; it does not define spacing between
dream cycles. The watcher no longer uses elapsed time since the last dream as a
trigger and no longer treats missing scheduling state as due.

After an automatic dream succeeds and its isolated write has been integrated or
accepted as a valid no-op, the watcher subtracts exactly `trigger_points` from
the balance and preserves any excess. For example, if the balance is $63$ and
the threshold is $50$, the remaining balance is $13$.

If an automatic dream fails, is killed, or cannot integrate its work, the balance
is not changed.

Manual dreamer runs do not alter the trigger balance.

## Failure And Retry Behavior

User-submitted update candidates are preserved before worker execution. A failed
or interrupted worker must leave enough state to retry those same candidates.
If a worker dies while a batch is in progress, the next worker or status read
should mark that batch retryable instead of treating it as successfully
processed.

Reviewer failure leaves the provider sessions unreviewed. The next scan can
review the same batch again.

Dreamer failure leaves trigger points unchanged. The next watcher check can run
dreamer again after the retry policy allows another attempt.

When an isolated semantic run fails:

- the main memory repo remains at the original clean state;
- temporary commits from the failed run are not landed;
- the temporary worktree changes are not salvaged;
- the temporary branch and worktree are removed by cleanup;
- the original work source remains retryable;
- the failure is recorded in runtime state or watch logs.

Repeated processing is acceptable because the agent reruns from clean memory and
the original source. This is safer than asking a later agent to continue from an
unfinished partial edit.

The shared memory write lock protects the landing phase, and the supervisor also
compares the main repo `HEAD` with the recorded base before landing commits. If
the base changed, the supervisor discards the temporary result and leaves the
original work source retryable against the newer main repo state.

Watch loops should avoid tight retry loops when the LLM, provider CLI, or
network is unavailable. They should record the failure, keep the watcher alive,
and wait before retrying.

## Cleanup

Each isolated run removes its temporary branch and worktree when the supervisor
exits. Managed watcher startup also runs stale isolated cleanup for review and
dreamer targets; sync startup skips isolated cleanup. Cleanup is role-scoped and
removes matching `.runtime/worktrees/` checkouts and
`rightmemory-isolated-<role>-<uuid>` branches.

Cleanup is allowed to discard dirty files inside temporary worktrees because
those files are artifacts of unfinished automatic runs. Cleanup should not
discard dirty files in the main memory repo.

Cleanup uses best-effort Git operations for stale temporary worktrees and
branches. Cleanup failures are not surfaced through normal status inspection, but
starting another automatic write may still block if a stale artifact would make
branch or worktree names collide.

## Runtime-State Recovery

Missing trigger state is normal. It is treated as a $0$ point balance and
created on first write.

Malformed JSON or invalid fields in trigger state are recoverable runtime-state
corruption. The store backs up the corrupt file in the same directory using a
timestamped name such as:

```text
trigger-state.corrupt-20260522T120000Z.json
```

Then it rebuilds the state with a $0$ balance and prints a warning. This
recovery should not fail update, review, or watch unless the backup or
replacement write itself fails for filesystem reasons.

When update or review has already succeeded semantically, trigger accounting
should not turn that operation into a failed memory operation. If accounting
recovery or increment fails due to filesystem errors, the command logs a warning
and leaves the semantic success intact.

When dreamer watch cannot read trigger state because it is corrupt, recovery
rebuilds the balance as $0$ and the watcher does not run dream from the lost
balance. New update and review successes will accumulate points again.

When dreamer succeeds but point consumption fails due to filesystem errors, the
watcher logs an error and leaves the balance intact. This may cause a repeated
dream later, but it avoids silently losing consolidation pressure.

Isolated cleanup relies on the managed branch prefix and `.runtime/worktrees/`
location. Best-effort cleanup removes matching artifacts for the target role and
leaves unrelated branches or worktrees alone.

The old `.runtime/dreamer/watch-state.json` file is ignored. It may remain on
disk after upgrade.

## Documentation Surface

User-facing docs should describe dreamer watch as change-triggered background
consolidation. The important contract is that successful update and review work
adds trigger points, the watcher checks trigger state on its configured cadence,
and the old periodic scheduling state is no longer used.

Write-safety docs should describe isolated automatic writes as ordinary role
commits made in temporary worktrees, then validated and landed by runtime.
Failed temporary work is discarded and retried from its original source, while
dirty main memory files block automatic semantic writes independently of remote
sync.

## Tests

Add focused tests for:

- parsing default and custom `[dreamer.watch]` config, including fractional
  review weight;
- trigger state missing file reads as $0$;
- point increments are atomic and preserve fractional values;
- successful threshold consumption subtracts the threshold and keeps excess;
- corrupt trigger state is backed up and rebuilt;
- `update submit` does not add points;
- successful update worker batches add candidate-count weighted points after
  semantic success and async state update;
- failed or killed update worker batches remain retryable and do not add points;
- successful review batches add reviewed-session weighted points after review
  state is saved;
- failed review batches and empty scans do not add points;
- first `rightmemory dreamer watch` iteration does not run dream when balance is
  below threshold;
- dreamer watch sleeps for `check_interval_seconds` when below threshold;
- dreamer watch runs when balance reaches `trigger_points`;
- successful automatic dream consumes the threshold and keeps excess;
- failed, killed, or non-integrated automatic dream does not consume points;
- automatic write roles create temporary worktrees and temporary branches before
  editing memory;
- successful isolated writes land in the main repo with normal commit messages
  and no merge commit;
- temporary commits are not landed when the role process fails or leaves dirty
  files behind;
- successful no-op runs with no commits and a clean temporary worktree update
  role-specific state without landing a commit;
- failed isolated writes leave the main memory repo clean;
- changed main repo `HEAD` during an isolated run causes the temporary result to
  be discarded and retried from source;
- cleanup removes stale temporary worktrees and temporary branches;
- cleanup does not discard dirty main memory files;
- main-memory dirty preflight runs when `[sync].enabled` is false;
- dirty main memory blocks automatic semantic writes unless repaired first.

## Upgrade Impact

The upgrade changes automatic dreamer behavior. Existing users who relied on
time-based scheduled dream cycles will no longer receive clock-driven
consolidation after reinstall. The old scheduler state remains untouched but is
ignored.

Automatic write behavior also changes: successful memory edits are integrated
through temporary worktrees, while failed partial edits are discarded. This
improves retry safety because the main memory repo should no longer be dirtied
by interrupted automatic agent runs.

No user memory files are rewritten by the upgrade. Trigger accounting starts at
$0$ unless new successful update or review work occurs. Existing dirty main
memory files are not discarded during upgrade; they are handled by main-memory
preflight the next time an automatic write role starts.

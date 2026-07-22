# Safe Existing-Root Install and Staged Sync Specification

## Status

This document records the approved behavior and implementation shape for the
install and sync safety work that should follow the safe and recoverable
automatic-writes group.

Implementation should begin only after that first group has landed. The later
agent must inspect the final public interfaces produced by that work and adapt
the names in this document accordingly. It must reuse the first group's
durable operation record and live worktree lease rather than introducing a
second journal or lease format.

The implementation target described here is the branch containing the current
install, sync, sync-reconciler, and automatic-write behavior. This document is
self-contained; the implementation should not depend on the untracked current
implementation review document being present.

## Summary

The design has two direct rules:

1. The installer creates semantic state only when bootstrapping a new root. A
   reinstall preserves a complete existing root or refuses an incomplete one
   before making any changes.
2. Sync fetches and prepares incoming Git state away from the active checkout.
   It publishes only an exact candidate that has merged, been repaired when
   necessary, and passed validation.

No generalized “state admission” framework is needed. Install and sync share a
safety principle, but each keeps its own small implementation.

## Goals

- A fresh install still creates a complete, clean, committed RightMemory root.
- Installing over an existing root never creates or refreshes semantic state
  files behind the user's back.
- An incomplete existing root causes a nonzero installer exit before the
  memory root, runtime installation, installed skills, or install stamp change.
- Remote commits, merge conflicts, and invalid merged graphs never appear in
  the active checkout before repair and validation succeed.
- The sync reconciler retains its ability to make semantic conflict decisions,
  but it works in the staged candidate checkout.
- A crash after a sync-repair model turn has durably prepared a candidate does
  not run that model turn again.
- The final publication is guarded by the active root's exact starting commit
  and clean synchronized-file state.
- A push failure after local publication is retryable and does not roll back or
  repeat repair.
- Current sync result vocabulary remains sufficient; the patch does not add a
  new user-facing sync state machine.
- Every normal unified Update carries its human review document in the same
  commit, and synchronized correction processing settles one Ready revision
  exactly once across devices.

## Non-Goals

- Do not change the Memory or Pursuit schema.
- Do not add schema versions or an automatic existing-root migration system.
- Do not add local map IDs, linked-source semantics, or retrieval changes.
- Do not redesign shared-view relationship consistency in this work.
- Do not force-push, rewrite remote history, or automatically choose between
  unrelated histories.
- Do not synchronize `.runtime/`, credentials, provider sessions, caches, or
  machine-local configuration.
- Do not make the installer a general graph doctor. It checks whether the
  required root layout exists; ordinary validation and status remain
  responsible for pre-existing semantic errors.
- Do not build a second SQLite database, JSON operation journal, sync outbox,
  or sync-specific lease system.
- Do not add a separate review queue, review service, or distributed lock;
  Ready reviews reuse the synchronized update queue and its singleton lease.
- Do not change the separately specified dirty-main recovery policy used by
  automatic writers. Within sync itself, pre-existing dirty or already-invalid
  active state blocks incoming synchronization and is not copied into a sync
  candidate.
- Do not add new public sync commands solely for this implementation.

## Terminology

- **Active root**: the selected memory root and its checked-out Git branch.
- **Semantic state files**: `MEMORY.md`, `MEMORY_*.md`, `PURSUITS.md`,
  `PURSUIT_*.md`, `PURSUIT_RULES.md`, `corrections.md`, shared-view and share
  definitions, and `insight_logs/*.md`.
- **Update-review document**: the tracked
  `update_reviews/review-<sha256(operation-id)>.md` file created in the same
  commit as one normal unified Update. It is synchronized protocol state, not
  Memory or durable correction evidence.
- **Required root documents**: the regular files `MEMORY.md`, `PURSUITS.md`,
  and `PURSUIT_RULES.md`.
- **Sync candidate**: a leased temporary Git branch and worktree created from
  one exact active-root commit and merged with one exact fetched upstream
  commit.
- **Publication**: fast-forwarding the active branch and checkout from the
  captured starting commit to the validated candidate commit.
- **Repair operation**: the automatic sync-reconciler turn used only when a
  staged merge has conflicts or the merged candidate fails semantic
  validation.

## Required Invariants

### Installer invariants

- Target inspection is read-only and occurs before `insight_logs/`, `.runtime/`,
  a Git repository, runtime files, skills, or an install stamp are created or
  modified.
- A new root receives all required documents before its initial baseline
  commit.
- A root with existing RightMemory semantic state or an existing Git commit is
  never treated as a fresh seed.
- A complete existing root's semantic state bytes remain unchanged throughout
  reinstall.
- A refused install leaves both the memory root and external installation
  targets unchanged.
- Installer success does not silently leave newly created semantic state files
  untracked in an existing repository.

### Sync invariants

- Fetching may update remote-tracking refs, but no operation before publication
  changes the active branch, active index, or active semantic files.
- The candidate always records the exact active starting commit and exact
  fetched upstream commit. It never merges a moving symbolic upstream ref.
- The active root's memory-write lock is held from the final local preflight,
  through candidate preparation and optional repair, until publication or
  refusal completes.
- A sync repair edits and commits only inside the candidate worktree.
- Candidate validation uses the current sync validation profile, including
  complete Memory/Pursuit graph validation and correction-structure validation
  without enforcing the updater-only correction capacity.
- The complete candidate diff from the active starting commit may contain only
  synchronized paths. A remote commit cannot use ordinary Git transport to
  introduce configuration, runtime state, generated output, or an unrelated
  tracked file.
- Synchronized review paths must use the canonical hashed filename and validate
  as complete review documents. Malformed review state or a review-path merge
  conflict fails closed; `sync-reconciler` does not interpret or repair it.
- Publication occurs only when the active root is still at the captured
  starting commit and synchronized paths are still clean and unconflicted.
- Publication uses a fast-forward to the exact candidate commit. It does not
  cherry-pick, squash, copy files, reset the active root, or reconstruct the
  merge.
- A failed merge, repair, validation, or publication check leaves the active
  branch and active semantic files unchanged.
- Once a repair outcome is durably prepared, recovery resumes validation or
  publication from that outcome and does not call the model again.
- The temporary branch and worktree remain protected by a live lease until the
  prepared repair has either landed or reached an explicitly terminal failure.
- A successful publication is recognized after a crash by commit ancestry,
  even if the process died before marking the durable record complete.
- Network push is outside the semantic publication outcome. It may retry
  without repeating merge or repair.

## Part One: Installer Behavior

### Current problem

`Installer.run()` currently creates or refreshes Memory and Pursuit documents
before `_ensure_initial_commit()` determines whether the repository already
has a `HEAD`. When an older repository has `MEMORY.md` but lacks current
Pursuit documents, install copies `PURSUITS.md` and `PURSUIT_RULES.md`, reports
success, and leaves those protected files untracked. Automatic writers then
refuse to run because the active state is dirty.

Reinstall can also refresh managed example blocks inside existing Memory and
Pursuit files. That makes package refresh responsible for editing semantic
state and can leave a previously clean root dirty.

### Target classification

Add a read-only inspection step at the beginning of `Installer.run()`, before
`insight_logs.mkdir()` or any other mutation. `_print_layout()` may run before
or after inspection because it only prints.

A target is **new** when all of the following are true:

- Git cannot resolve a committed `HEAD` for the target.
- No recognized semantic state file exists. This includes the required root
  documents and any recognized Memory detail, Pursuit detail, correction,
  shared-view, share, or insight file.

An empty Git repository without a commit is therefore still a new target. A
pre-created `rightmemory.toml` alone does not make the semantic root existing.

A target is **existing** when either of these is true:

- Git resolves a committed `HEAD`.
- At least one recognized semantic state file exists.

For an existing target, all required root documents must exist as regular,
non-symlink files. Optional state files may be absent. A target containing only
some required documents is incomplete and must be refused.

An existing complete root without Git history may still be initialized and
baseline-committed, but its semantic files must be committed byte-for-byte as
found. This preserves the useful first-install behavior for a complete
pre-existing Markdown root without synthesizing missing state.

### Preflight failure

When an existing target is incomplete, raise `InstallError` before any write.
The error must:

- state that the existing RightMemory root is incomplete;
- list missing or non-regular required documents in stable sorted order;
- state that installation made no changes;
- state that the root needs an explicit user-reviewed migration before install
  can continue;
- avoid offering an automatic acceptance or overwrite option that does not
  exist.

Example shape:

```text
existing RightMemory root is incomplete: missing required files: PURSUITS.md, PURSUIT_RULES.md
installation made no changes; migrate and review this root explicitly before reinstalling
```

The shell and PowerShell launchers should propagate the Python installer's
nonzero status and error text without adding a second implementation of target
classification.

### New-root path

For a new target, retain the current bootstrap behavior:

- create `MEMORY.md` from the current seed;
- create `PURSUITS.md` from the current seed;
- create `PURSUIT_RULES.md`;
- initialize Git when needed;
- configure a repository-local author when needed;
- write the memory-root `.gitignore` allowlist;
- baseline all existing synchronized state that belongs in the first commit;
- leave the repository clean;
- install runtime files and skills;
- baseline semantic-upgrade state for the current seed;
- write the install stamp only after successful completion.

This work does not redesign the seed or remove its examples.

### Existing-root path

For a complete existing target:

- do not call `_install_or_refresh_memory()`;
- do not call `_install_or_refresh_pursuits()`;
- do not call `_install_pursuit_rules()`;
- remove or retire automatic known-example migration from reinstall behavior;
- do not rewrite managed example blocks even when their markers remain;
- preserve all semantic state bytes;
- initialize Git and make an exact baseline commit only when the complete root
  has no existing commit;
- preserve an existing repository's `HEAD` and configured author;
- allow package-owned `.gitignore`, runtime, wrapper, skills, semantic-upgrade
  runtime state, and install stamp maintenance to continue;
- write the install stamp only after the reinstall finishes successfully.

This intentionally changes the previous promise that managed examples refresh
on reinstall. Example freshness belongs to package documentation and fresh
seeds; an installed root is user state.

### Installer implementation shape

Keep the change inside `rightmemory/install_core.py` rather than adding a new
installer framework.

Add one immutable inspection result, for example:

```python
# Read-only classification captured before install mutates any target.
@dataclass(frozen=True)
class InstallTarget:
    kind: Literal["new", "existing"]
    has_head: bool
    missing_required: tuple[str, ...]
    invalid_required: tuple[str, ...]
```

The exact name may change, but inspection must be a pure read of the target.
`Installer.run()` should inspect once, fail immediately when required, and then
select either `_bootstrap_state()` or `_preserve_existing_state()`. Do not
scatter repeated “does `HEAD` exist?” checks through the current copy helpers.

The existing `_ensure_initial_commit()` should no longer contain the behavior
that notices newly created managed state under an existing `HEAD`; that state
must never be created. Remove `new_managed_state_files` and its notice if no
other path needs it after the refactor.

### Installer tests

Update `tests/test_install.py` and `tests/test_install_windows.py` with these
behavioral cases:

- A fresh empty target still receives all required files, one initial baseline
  commit, and a clean Git status.
- An empty Git repository without `HEAD` is treated as new.
- A complete pre-existing non-Git root is preserved byte-for-byte, initialized,
  baseline-committed, and left clean.
- An existing committed root with only `MEMORY.md` exits nonzero.
- The same refusal behavior is covered through the native Windows installer.
- Refusal preserves the original `HEAD`, complete directory snapshot, Git
  status, configured author, runtime installation target, skills target, and
  absence or prior contents of `.runtime/install.stamp`.
- The refusal error lists `PURSUITS.md` and `PURSUIT_RULES.md` and says no
  changes were made.
- A required path that is a directory or symlink is refused as non-regular.
- A complete committed root reinstalls successfully without changing the byte
  hashes of any semantic state file.
- A managed example block in an existing root is no longer refreshed.
- A known old starter block in an existing root is no longer migrated
  automatically.
- Existing `.gitignore`, runtime, skill, semantic-upgrade, and install-stamp
  maintenance still behaves as documented.
- A failure after preflight but before the install stamp still does not claim
  install success.

Replace tests that currently require missing documents to be copied and left
untracked. Update tests that require managed example refresh on reinstall to
assert preservation instead.

## Part Two: Staged Sync Behavior

### Current problem

`SyncManager.preflight()` is not a preflight: it fetches and merges directly in
the active checkout. `SyncManager.push()` also merges directly after a rejected
push. Graph validation happens afterward. A textual merge conflict leaves
conflict markers and an in-progress merge in the active root, and a semantically
invalid clean merge changes active `HEAD` before the invalidity is reported.

The current caller then invokes sync-reconciler against that already modified
root. Repair can succeed, but model failure, process failure, or validation
failure can strand the active root in the invalid intermediate state.

### Synchronized update-review protocol

Every normal unified Update adds one
`update_reviews/review-<sha256(operation-id)>.md` document to the same commit as
its Memory or Pursuit change. Git therefore transports the review as part of
ordinary synchronized state, with no post-commit creation effect or separate
review store.

Human edits remain ordinary local working-tree drafts until the user checks
Ready. The scanner binds the normalized comment to the exact tracked review
commit and blob, publishes that evidence as a typed candidate in the existing
synchronized update queue, then restores the tracked review file so the local
draft cannot block sync. The queue candidate carries the submitted comment
between devices.

Review candidates are selected alone under the queue's singleton lease. Fenced
Git finalization rechecks the submitted commit and blob, consumes the candidate, and
atomically applies any prepared correction plus either deleting the resolved
review or writing a clarification and clearing Ready. Exact duplicates converge;
stale candidates are terminally consumed without touching the current review.
This gives retries and multiple devices one settlement point without another
queue or lease. When sync is disabled, correction and review settlement follow
the same rules but commit directly to local Git.

### Public method responsibilities

Rename methods so their names describe behavior:

- Replace mutating `preflight()` with `pull(repair=...)`.
- Replace `background_pull()` with `background_sync(repair=...)`, because it may
  push local commits as well as pull remote commits.
- Keep `push(repair=...)`, but make rejected-push reconciliation use the same
  staged pull machinery.

If compatibility requires temporary aliases, keep them private or deprecate
them explicitly. New implementation and tests should use the truthful names.

`SyncManager` should own `MemoryWriteLock` for active-root sync mutation. Remove
the outer sync-watch lock wrapper in `rightmemory/cli.py` and avoid nested
acquisition in `rightmemory/runtime.py`. Fetch and ordinary push network calls
may happen outside the lock; candidate creation, repair, validation, and
publication occur while it is held.

Because `rightmemory.sync` must not import the runtime, inject repair through a
narrow callback. A suitable conceptual interface is:

```python
SyncRepair = Callable[[Path, SyncResult, str], RepairOutcome]
```

The callback receives the candidate root, the candidate conflict or validation
diagnostic, and the operation ID. Runtime and CLI call sites provide the actual
sync-reconciler execution. Unit tests provide deterministic callbacks.

When no repair callback is available, an unresolved candidate returns
`conflict`; the active root remains unchanged.

### Pull sequence

Implement pull in this order:

```text
validate configuration
fetch remote refs without holding the memory-write lock
resolve and capture the exact upstream commit
acquire the active root memory-write lock
recheck repository root, active HEAD, local cleanliness, and local validity
compute ahead/behind against the captured upstream commit
return current when no incoming commit exists
create a leased candidate branch and worktree from the exact active HEAD
merge the exact upstream commit inside the candidate
repair the candidate when merge or semantic validation requires it
validate the complete candidate and its changed paths
recheck exact active HEAD and synchronized-file cleanliness
fast-forward the active checkout to the exact candidate commit
confirm active HEAD and cleanliness
record success and release the lock
clean up the settled candidate
```

The fetched symbolic upstream name is used only to resolve a commit. Every
later compare and merge uses that immutable commit hash. If the remote advances
again after fetch, the next sync cycle handles it.

### Candidate branch and worktree

Use a branch such as `rightmemory-sync-<operation-id>` and a worktree under the
existing ignored runtime worktree area. The candidate starts at the exact
active `HEAD` captured under the lock.

Reuse the first group's live-owner lease and safe cleanup rules. If that work
does not expose a neutral leased-worktree primitive, extract the smallest
shared primitive from its automatic-write supervisor. Do not make sync imitate
lease ownership using branch age, file modification time, or an independent
PID file.

Run:

```text
git merge --no-edit <captured-upstream-commit>
```

inside the candidate. This naturally produces one of these states:

- a fast-forwarded candidate;
- a clean merge commit when local and remote histories diverged compatibly;
- an uncommitted merge with conflict markers inside the candidate only.

If Git fails without producing synchronized conflict paths, such as for
unrelated histories or a conflict confined to a non-sync path, return `error`
without invoking the model. Before any repair or publication, reject a
candidate whose complete diff from the active starting commit includes a path
outside `MEMORY_SYNC_PATHS`.

Do not copy active files into the candidate and do not use a temporary index or
`git reset` rollback scheme. The Git worktree is the speculative state.

### When repair runs

Run sync-reconciler only when either condition holds:

- the candidate merge has unresolved synchronized paths;
- the candidate merge is textually clean but complete sync validation fails.

Do not run sync-reconciler merely because local and remote both changed. A
clean, valid Git merge is already a valid candidate.

Pre-existing dirty active state returns `dirty` before candidate creation.
Pre-existing committed but semantically invalid active state returns
`conflict` before fetch results are admitted. These are local repair concerns,
not incoming merge repairs.

### Running repair inside the candidate

Load executor configuration from the active root, then derive a nested runtime
configuration with:

- `memory_root` set to the candidate worktree;
- `state_root` set to the first group's isolated operation-state overlay rooted
  at the active root;
- sync disabled inside the nested runtime;
- a fresh provider conversation when CLI-agent mode requires speculative
  isolation;
- the sync-reconciler role and its existing sync-owned write permissions.

The candidate is already the isolation boundary. Invoke the shared inner role
execution path directly; do not create a nested Git worktree around it.

The repair turn may create at most one repair commit:

- For an unresolved Git merge, that commit completes the merge after resolving
  all synchronized conflict paths.
- For a clean but semantically invalid merge, that commit sits on top of the
  merged candidate and corrects it.

Record the candidate `HEAD` and merge state immediately before model execution.
Validate the repair as one new first-parent commit relative to that point. Do
not pass the candidate through a helper that counts every remote commit
reachable from the repaired merge as though the model created those commits.
For a conflicted merge, the repair commit must complete the expected merge with
the captured upstream commit as a parent. For a clean invalid merge, the repair
commit must be directly based on the pre-repair candidate tip.

After the turn:

- no synchronized path may remain unmerged;
- the candidate worktree must be clean;
- the repair commit may touch only sync-reconciler-owned paths;
- required root files and all changed files must remain regular files;
- complete validation must pass with updater correction capacity disabled.

The prompt should describe the candidate as speculative incoming state and
make clear that the role commits the repair but does not publish, push, abort,
or reset the active root. Preserve the current semantic rules for unioning
distinct corrections and validating the full graph.

### Durable repair and crash recovery

Use the first group's durable semantic-operation phases rather than adding a
sync journal.

Before invoking the model, create one operation record whose input includes:

- operation kind `sync-repair`;
- active starting commit;
- captured upstream commit;
- candidate branch identity;
- the pre-repair candidate tip and expected merge parent, when present;
- conflict paths or validation diagnostics;
- a hash of the bounded repair input;
- a repair-policy fingerprint that changes when the applicable prompt or
  validation contract changes.

The operation record owns the candidate lease. The branch name includes the
same operation ID.

Use a stable delivery key for the same active starting commit, upstream commit,
bounded repair input, and repair-policy fingerprint. Repeated watcher cycles
must find the existing operation rather than minting a random new operation and
rerunning the model. A changed local commit, changed upstream commit, changed
repair input, or changed repair policy creates a genuinely new operation.

When the model has committed a valid candidate, durably record a prepared
outcome containing the candidate commit and complete changed-path set before
publishing it. Do not mark the operation committed merely because the commit is
reachable from the temporary branch.

Recovery rules are:

- If the operation is still running and has no durable prepared outcome,
  inspect the candidate branch before retrying. If the candidate contains the
  completed repair commit, validate it and adopt it as the prepared outcome;
  do not run the model again. Retry is allowed only when neither a repair
  commit nor a durable no-change outcome exists.
- If the operation is prepared and the candidate branch still resolves to the
  recorded commit, resume validation and publication without running the model.
- If the active `HEAD` equals or descends from the candidate commit, publication
  already happened; mark the operation committed and continue only pending
  follow-up effects.
- If the active `HEAD` is still the recorded starting commit, publication may
  fast-forward it to the candidate after cleanliness checks.
- If the active `HEAD` changed incompatibly, do not overwrite it, rebase it, or
  rerun repair automatically. Leave a recoverable operation diagnostic for an
  explicit later decision.
- If a recorded candidate ref is missing or points to a different commit, fail
  closed and report operation corruption.
- Do not delete a prepared candidate branch or worktree until publication is
  confirmed or the operation is explicitly resolved.

If the repair returns no change and validation still fails, record the no-change
outcome and return `conflict`. A crash must not convert that result into another
automatic model run. Later watcher cycles with identical input return the same
conflict without rerunning. A new operation is allowed only after the local
state, upstream state, bounded evidence, or repair policy changes.

### Publication

Immediately before publication, while still holding the active memory-write
lock:

- verify active `HEAD` equals the recorded starting commit;
- verify no synchronized path is dirty or unmerged;
- verify the candidate ref still resolves to the recorded candidate commit;
- re-run complete candidate validation if recovery crossed a process boundary.

Then run an exact fast-forward in the active checkout:

```text
git merge --ff-only <candidate-commit>
```

The candidate commit descends from the starting commit, so publication should
not need a new merge decision. Afterward, verify active `HEAD` equals the
candidate commit and synchronized paths remain clean. Only then mark the
durable operation committed and update observational sync state.

Do not implement publication with file copies, cherry-picks, squash commits,
branch resets, or an update-ref that leaves the checked-out index and working
tree stale.

### Push sequence

Push should operate on exact commits rather than a moving `HEAD`:

1. Under the memory-write lock, verify local synchronized state is clean and
   valid, then capture the exact local tip.
2. Release the lock and push `<captured-tip>:<remote-branch>`.
3. If the push succeeds, record `pushed`.
4. If it fails because the remote advanced, fetch and run the staged pull flow.
5. After a candidate lands, push its exact commit.
6. If that push is offline or rejected again, keep the valid local publication
   and report the push failure. A later cycle retries push without repeating
   merge repair already represented in local history.

Do not roll back a valid local publication because remote transport failed.

### Result behavior

Keep the existing result statuses wherever their meanings remain accurate:

- `disabled`: sync is disabled.
- `unconfigured`: no usable upstream or push target exists.
- `fresh`: the background freshness policy intentionally skipped work.
- `dirty`: pre-existing local synchronized state is uncommitted; no candidate
  was created.
- `conflict`: the incoming candidate could not be repaired and validated; the
  active root is unchanged.
- `synced`: the active root is current or a validated candidate landed.
- `pushed`: the captured valid local commit reached the configured remote.
- `offline`: a required network operation failed.
- `error`: a non-conflict Git, validation, operation-record, or publication
  invariant failed.

Conflict messages and `repair_message()` must no longer tell users to inspect
conflict markers in the active root. They should say that incoming state could
not be admitted and that the active root was left unchanged. Candidate paths
may still be listed for diagnosis.

The existing `.runtime/sync/state.json` remains an observational record of last
status and timestamps. It is not an authority for candidate ownership,
prepared outcomes, or crash recovery. Generic pending-operation reporting from
the first group is sufficient; do not add fetched, merging, reconciling, or
ready-to-land public phases.

## Runtime and Watch Integration

### `rightmemory/runtime.py`

- Replace calls to `SyncManager.preflight()` with the staged `pull()` call.
- Provide a candidate-root repair callback that runs sync-reconciler through
  the shared prepared-worktree role execution path.
- Do not release the active root lock between discovering a candidate conflict
  and repairing that candidate; lock ownership moves inside `SyncManager`.
- Remove the old pattern in which sync returns `conflict` after changing main
  and runtime subsequently runs `_run_sync_reconciler()` against main.
- Preserve the separate automatic-writer handling for pre-existing dirty-main
  state according to the first group's final contract.
- Make post-write sync push use the same staged reconciliation callback when a
  remote update rejects the first push.
- Keep sync disabled inside the candidate runtime so a repair commit cannot
  recursively invoke sync.

### `rightmemory/cli.py`

- Remove the outer `MemoryWriteLock` around `background_pull()` after
  `SyncManager` owns lock scope.
- Pass the staged repair callback into `background_sync()`.
- Change `_run_sync_reconciler()` to accept the candidate root separately from
  the active configuration root.
- Load model/provider configuration from the active root, but execute tools and
  Git commands in the candidate root.
- Keep watcher stop and failure-limit behavior unchanged.
- Print ordinary sync result messages; do not expose internal worktree paths in
  normal success output.

### Worktree and operation helpers

- Reuse the first group's leased worktree cleanup proof.
- Reuse its durable operation store and prepared-outcome recovery.
- Reuse its isolated provider/session overlay and promotion rules.
- Extract a narrow helper only when the final first-group code otherwise forces
  sync to duplicate branch creation, owner identity, or cleanup logic.
- Do not make `SyncManager` depend on private serialized record fields. Add the
  smallest public operation methods needed for preparing and completing a
  staged outcome.

## File-by-File Implementation Guide

### `rightmemory/install_core.py`

- Add pure target inspection before all mutation.
- Split new-state bootstrap from existing-state preservation.
- Include canonical `update_reviews/*.md` paths in the root Git allowlist.
- Stop refreshing or migrating semantic examples in existing roots.
- Remove the existing-`HEAD` path that leaves newly created state uncommitted.
- Simplify or remove `new_managed_state_files` when unused.
- Update final install text to say reinstalls preserve semantic state and
  incomplete roots are refused.

### `rightmemory/sync.py`

- Rename mutating methods to `pull()` and `background_sync()`.
- Add the repair callback type and candidate orchestration.
- Resolve exact fetched commits before lock acquisition.
- Own active-root lock scope.
- Add candidate merge, validation, exact fast-forward publication, and cleanup.
- Push exact commits rather than symbolic `HEAD`.
- Keep `state.json` observational.
- Update conflict messages for unchanged active-root behavior.
- Admit and validate canonical update-review documents as synchronized protocol
  paths, and fail closed on their merge conflicts without model repair.

### `rightmemory/runtime.py`

- Replace post-conflict active-root repair with candidate repair callbacks.
- Build nested sync-reconciler configuration using candidate `memory_root` and
  isolated active `state_root`.
- Reuse the first group's operation phases and prepared recovery.
- Preserve automatic writer recovery and post-write push semantics.
- Add each generated update-review document to its normal Update commit rather
  than creating it as a later runtime effect.

### `rightmemory/cli.py`

- Update sync watch to call `background_sync()` without an outer memory lock.
- Run repair in the supplied candidate root.
- Keep watcher lifecycle, failure counting, and stop behavior unchanged.
- Publish Ready revisions as typed update-queue candidates and route claimed
  review work through fenced correction and review settlement.

### First-group operation/worktree modules

- Expose only the minimal prepared-worktree execution, lease, and recovery
  operations needed by sync.
- Do not fork or duplicate operation-record schemas.
- Ensure generic pending-operation status can identify `sync-repair` work.

### `rightmemory/prompts/sync-reconciler.md`

- Clarify staged-candidate behavior.
- Tell the role to resolve and commit the candidate but never publish, push,
  reset, or edit an original active root.
- Preserve correction-union and complete-validation rules.
- Keep any separate explicit dirty-main guidance coherent with the first
  group's final behavior.

### Documentation

- Update `README.md` to remove the promise that managed examples refresh during
  reinstall.
- Document that incomplete existing roots are refused before writes.
- Document validate-before-publish sync and unchanged active state on conflict.
- Update `DESIGN_NOTES.md` with the bootstrap-versus-reinstall boundary and the
  staged-sync rationale.
- Update `AGENTS.md` so future agents do not restore example refresh or direct
  active-checkout merge repair.
- No semantic upgrade note is required because this work does not change how
  existing Memory should be interpreted. It changes install and runtime safety.

## Sync Test Matrix

Rewrite mechanism-focused sync tests around observable active-root invariants.
Tests should record the active starting commit, active synchronized-file bytes,
and Git status before each operation.

### Ordinary pull

- Disabled and unconfigured behavior remains unchanged.
- A valid remote fast-forward lands and leaves the active root clean.
- A clean valid divergent merge lands its merge commit by fast-forwarding the
  active root to the candidate.
- No incoming commit returns current without creating a repair operation.
- A remote advance after fetch is not accidentally merged; the next cycle sees
  it.
- A canonical update-review document is transported and validated with its
  originating Update commit.

### Dirty and invalid local state

- Dirty synchronized files return `dirty`, create no candidate, and remain
  byte-for-byte unchanged.
- An already-invalid committed local graph blocks sync without admitting remote
  state.
- Runtime's separately specified bounded dirty-main path remains covered in its
  own focused tests.

### Candidate conflict and validation

- A textual merge conflict exists only in the candidate while the repair
  callback runs.
- A successful repair commit validates and lands; active files never contain
  conflict markers.
- A repair exception leaves active `HEAD`, files, index, and status unchanged.
- A no-change repair that remains invalid returns `conflict` and leaves active
  state unchanged.
- A clean Git merge that creates a duplicate graph ID is repaired in the
  candidate or rejected without changing active state.
- A repair touching a forbidden path is rejected before publication.
- A remote candidate introducing any tracked path outside `MEMORY_SYNC_PATHS`
  is rejected before repair and publication.
- A repair leaving unmerged entries, an uncommitted edit, a symlink, or an
  invalid graph is rejected before publication.
- Transporting correction entries above the updater-only ceiling remains
  allowed when the full sync validation profile otherwise passes.
- A malformed review document, noncanonical review filename, or review-path
  merge conflict is rejected without invoking `sync-reconciler`.

### Publication races

- An external active `HEAD` change before publication causes fail-closed
  refusal; it is never reset or overwritten.
- A new dirty synchronized-file edit before publication is preserved and blocks
  publication.
- A moved or missing candidate ref is treated as corruption.
- Publication uses the candidate commit exactly and preserves remote and local
  history rather than squashing it.

### Crash recovery

- Crash after model repair commit but before prepared-outcome persistence
  discovers and adopts that commit without invoking the model again.
- Crash after prepared-outcome persistence but before publication does not call
  the model again and lands the recorded candidate on recovery.
- Crash after active fast-forward but before operation completion is recognized
  from commit ancestry and does not call the model again.
- A live candidate lease is never removed by cleanup.
- A dead unfinished candidate without a prepared outcome may be cleaned and
  retried safely.
- A prepared candidate is retained until landing or explicit resolution.

### Push

- A direct push sends the exact captured commit.
- A rejected push stages remote reconciliation without changing active state
  until validation succeeds.
- Successful reconciliation lands once and then pushes.
- Offline push after local landing reports failure without rolling back local
  state.
- Retrying that push does not invoke sync-reconciler again.

### Watch and runtime integration

- Sync watch no longer wraps a lock around a manager that owns the same lock.
- Sync watch passes candidate root and active configuration root correctly.
- Automatic writer pre-sync uses staged admission.
- Post-write push rejection uses staged admission and does not invalidate the
  already durable writer outcome.
- A Ready review becomes one typed queue candidate; the global lease prevents
  two devices from processing it, and finalization applies correction plus
  review deletion or clarification in one fenced Git transaction.
- With sync disabled, review settlement is committed locally without queue
  publication.
- Watch failure counting, retry cadence, cooperative stop, and install-refresh
  re-exec remain unchanged.

## Suggested Implementation Order

### Task One: Land and inspect the automatic-write dependency

- Confirm the first group is committed and its tests pass.
- Identify its public operation store, prepared outcome, live worktree lease,
  isolated state overlay, pending-effect replay, and status interfaces.
- Record any naming differences in the implementation commit or update this
  specification before coding.

### Task Two: Change installer behavior first

- Add failing POSIX and Windows tests for incomplete-root refusal and
  zero-mutation behavior.
- Add tests for complete existing-root byte preservation.
- Implement target inspection and the bootstrap/preserve split.
- Remove existing-root example refresh and migration.
- Update installer-facing documentation.
- Run focused installer tests before starting sync work.

### Task Three: Introduce staged sync without model repair

- Add candidate worktree lifecycle using the first group's lease helper.
- Implement valid fast-forward and clean divergent merge in the candidate.
- Validate and publish by exact fast-forward.
- Make conflict and invalid-candidate paths fail with active state unchanged.
- Update pull, push-rejection, and background tests.

### Task Four: Run sync-reconciler in the candidate

- Add the injected repair callback.
- Derive nested runtime configuration from active config and candidate root.
- Update the prompt for staged behavior.
- Enforce one repair commit, allowed paths, clean worktree, and complete
  validation.
- Add repair success and failure tests.

### Task Five: Add durable prepared recovery

- Begin a first-group semantic operation before model repair.
- Persist the candidate commit as a prepared outcome before publication.
- Resume publication without rerunning a prepared model outcome.
- Recognize publication completed before a crash.
- Protect prepared candidates from cleanup.
- Add crash-injection and live-lease tests.

### Task Six: Integrate runtime, watcher, status, and push retry

- Move lock ownership into `SyncManager`.
- Replace active-root post-conflict repair call sites.
- Make push use exact commits and retry transport without repair duplication.
- Verify generic pending-operation status includes sync repair.
- Update runtime, config, CLI, watcher, and status tests.

### Task Seven: Documentation and full verification

- Update `README.md`, `DESIGN_NOTES.md`, and `AGENTS.md` coherently.
- Run focused install, sync, runtime, CLI, operation, worktree, and status tests.
- Run the complete unit test suite and compile check.
- Commit only intended files; do not add worktrees, `.runtime/`, the untracked
  current implementation review, or unrelated user files.

## Verification Commands

Run focused tests as the implementation progresses:

```bash
rtk python -m unittest tests.test_install
rtk python -m unittest tests.test_sync
rtk python -m unittest tests.test_config
rtk python -m unittest tests.test_cli
rtk python -m unittest tests.test_isolated_write
rtk python -m unittest tests.test_status
```

Run native Windows installer coverage on Windows:

```powershell
rtk python -m unittest tests.test_install_windows
```

Before completion, run:

```bash
rtk python -m compileall -q rightmemory tests
rtk python -m unittest discover -s tests
```

If the branch still has the previously diagnosed Windows source-checkout
watch-launch failure, verify whether the first group or intervening work fixed
it. Do not hide or reclassify an unrelated baseline failure as part of this
implementation.

## Completion Criteria

The work is complete only when all of the following are demonstrated by tests:

- A fresh install creates a complete clean root.
- An incomplete existing root causes a zero-mutation failed install.
- A complete reinstall leaves semantic files byte-identical.
- Incoming invalid or conflicted Git state never appears in the active checkout
  before successful repair and validation.
- Successful staged repair lands the exact candidate commit once.
- Prepared repair recovery never reruns the model.
- Failed repair leaves the active root unchanged.
- Push retry never rolls back valid local state or repeats completed repair.
- Live sync candidates survive cleanup, and settled candidates are removed.
- Documentation no longer promises existing-root example refresh or direct
  active-checkout conflict repair.

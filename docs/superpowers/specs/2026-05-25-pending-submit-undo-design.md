# Pending Submit Undo Design

## Goal

`rightmemory update submit` already records submitted candidates under the update session and reports their pending ids through `pull`/state output. This feature adds a small way to cancel a candidate that has not started processing yet.

The user should be able to submit a memory candidate, notice that it should not be processed, and remove that pending candidate by id without creating a semantic undo task or touching Git history.

## Command Shape

Add:

```bash
rightmemory update undo --session <session-id> <candidate-id>
```

The command is scoped to `update`, matching `submit` and `pull`.

## Behavior

The async update store gains a method that removes a matching candidate from `pending`.

If the candidate id is present in `pending`, the store writes the updated state and the CLI reports that the candidate was canceled. If the id is in `current_batch`, already processed, missing, or part of a finished historical result, the store leaves state unchanged and reports that the candidate is not pending.

The existing state shape remains the same. `AsyncUpdateJob` already has an integer id, and `AsyncUpdateState` already tracks `pending`, `current_batch`, and `next_id`, so no migration or receipt format is needed.

## Data Flow

1. User runs `rightmemory update undo --session agent-1 3`.
2. CLI validates that the command is under the `update` role and that the id is an integer.
3. `AsyncUpdateStore.cancel_pending(session_id, candidate_id)` reads the session state under the existing lock.
4. The store removes the matching pending job if present, writes the state, and returns `(state, canceled)` where `canceled` is true when a pending job was removed.
5. CLI prints a short human-readable result.

If removing the candidate leaves `pending` empty while the worker is waiting, no special worker control is required. The existing worker loop already turns a running waiting state with no pending work into idle when it wakes and reads the state.

## Error Handling

Invalid session state continues to use the existing async state validation errors.

Invalid candidate ids fail at CLI parsing. Missing ids or ids that are already running/processed are non-destructive no-op results, because the requested cancel target is no longer pending.

## Testing

Add focused tests for:

- canceling a pending candidate preserves the order of remaining pending candidates;
- canceling a missing or current-batch candidate leaves state unchanged;
- CLI accepts `rightmemory update undo --session <id> <candidate-id>` without building a runtime;
- non-update roles reject `undo`, matching `submit` and `pull`.

Documentation can be minimal: update the async submit paragraph in `README.md` to mention that pending candidates can be canceled by id.

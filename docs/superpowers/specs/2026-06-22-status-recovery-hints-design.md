# RightMemory Status Recovery Hints Design

## Context

`rightmemory status` is the read-only operational dashboard for the selected
memory root. It already summarizes Git state, managed watches, Dreamer and
Insight trigger state, async update queues, recent log previews, and recent
issues.

The current output can identify actionable problems, but often stops at the
error text. For example, async update manual recovery appears as
`manual recovery required`, but the dashboard does not show the recovery command
the operator should run. This makes status truthful but less useful when the
next step is already known.

## Goal

When `rightmemory status` reports an actionable error, it should also show a
short recovery hint. The hint should be specific when there is a safe command,
and cautious when the correct action depends on user intent.

The main user-facing outcome is that an operator can run `rightmemory status`,
see what is wrong, and see the next reasonable recovery step without opening
help text or remembering subsystem commands.

## Non-Goals

- Do not make `rightmemory status` repair anything automatically.
- Do not add a new doctor command or diagnostic workflow.
- Do not hide the original error messages.
- Do not suggest destructive Git or runtime-state operations.
- Do not make status output noisy when there are no issues.

## Current Actionable Issue Types

The status collector currently knows these actionable issue shapes:

- `dirty worktree: ...`
- `git unavailable: ...`
- `<watch>: stale pid <pid>`
- `<watch>: running outside manager`
- `<watch>: <failed or error log preview>`
- `sync config error: ...`
- `dreamer trigger error: ...`
- `insight trigger error: ...`
- `update worker: stale pid <pid>`
- `update worker: state error: ...`
- `update: state error: ...`
- `update: <session>: manual recovery required: ...`
- `update: <session>: retrying after error: ...`
- `update: <session>: error: ...`
- generic collector failures such as `managed watches: status error: ...`,
  `dreamer: status error: ...`, `insight: status error: ...`, and
  `update: status error: ...`

## Design

Add a `Recovery` section after `Recent Issues` when at least one recovery hint
is available. Keep `Recent Issues` unchanged so existing diagnostic text stays
visible and tests that assert issue text remain meaningful.

Recovery hints are derived from issue strings that status already produces.
This keeps the change small and read-only. The formatter should classify known
issue prefixes and patterns with ordered, explicit pattern matching. Each known
issue shape needs a focused formatter test, so future issue wording changes fail
loudly instead of silently dropping or misclassifying a recovery hint. The
formatter dedupes identical hints and prints them in issue order.

Example:

```text
Recent Issues
  update: codex-rightmemory-doc-positioning: manual recovery required: status_code: 402

Recovery
  update manual recovery: run `rightmemory update retry`
```

For per-session update errors, include the session id when the next command
needs it:

```text
Recovery
  update agent-1: inspect with `rightmemory update pull --session agent-1`
```

For managed watches, prefer existing manager commands:

```text
Recovery
  pruner: run `rightmemory watch restart pruner`
```

For uncertain cases, give an instruction rather than a command:

```text
Recovery
  git: inspect the configured memory root and repair Git before retrying
```

## Recovery Hint Mapping

- Dirty Git worktree: inspect with `git status --short`; resolve the local
  changes intentionally before automatic writes continue.
- Git unavailable: inspect the configured memory root and repair or initialize
  the Git repository.
- Stale managed watch pid: run `rightmemory watch restart <watch>`.
- Managed watch running outside the manager: stop the foreground process
  directly, then run `rightmemory watch start <watch>`.
- Managed watch failure preview: inspect the shown log path, then run
  `rightmemory watch restart <watch>` when appropriate.
- Sync config error: fix `rightmemory.toml`, then rerun `rightmemory status`.
- Dreamer trigger error: inspect or repair `.runtime/dreamer/trigger-state.json`.
- Insight trigger error: inspect or repair `.runtime/insight/trigger-state.json`.
- Async update worker stale pid: inspect async update state; run
  `rightmemory update retry` only for sessions already blocked in manual
  recovery.
- Async update worker state error: inspect or repair
  `.runtime/async/update/_worker/state.json`.
- Async update session state error: inspect or repair the malformed JSON under
  `.runtime/async/update/`.
- Async update manual recovery: run `rightmemory update retry`.
- Async update retrying after error: wait for automatic retry; use
  `rightmemory update pull --session <session>` for details.
- Async update session error: inspect with
  `rightmemory update pull --session <session>`.
- Generic collector status error: rerun `rightmemory status`; inspect the named
  subsystem if it persists.

## Formatting Rules

- Show `Recovery` only when hints exist.
- Deduplicate exact hint lines so repeated manual-recovery sessions do not spam
  the same command.
- Preserve issue order for the first occurrence of each hint.
- Keep hints to one line each.
- Use exact commands only when they are non-destructive and safe.
- Do not include commands that rewrite, delete, reset, or discard state.

## Implementation Shape

`rightmemory/status.py` should gain a small pure formatter helper, for example
`_recovery_hints(status: DashboardStatus) -> list[str]`. It should inspect the
same issue list that `format_status_dashboard()` already prints, including
`status.git.issue`.

The helper should stay independent from state collection. Collection functions
continue to report facts; formatting converts those facts into user-facing next
steps.

## Testing

Add focused status formatter tests that cover:

- every known issue shape listed in this spec;
- manual async update recovery emits `rightmemory update retry`;
- repeated manual recovery issues emit one deduped retry hint;
- per-session update errors include `rightmemory update pull --session <id>`;
- stale watch pid emits `rightmemory watch restart <watch>`;
- dirty worktree emits a non-destructive Git inspection hint;
- clean status output does not include a `Recovery` section.

Also run the existing status test module and compile check.

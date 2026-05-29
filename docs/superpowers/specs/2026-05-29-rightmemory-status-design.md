# RightMemory Operational Status Design

## Purpose

RightMemory needs one command that answers the daily operational question: "What is the memory system doing, and is anything stuck or failing?" The existing `rightmemory watch status` is a process-manager view for managed watches. It reports pid and log paths, but it does not summarize async update queues, dreamer trigger progress, recent failures, or useful last outcomes. Users currently have to inspect several logs and state files manually.

Add a new top-level command:

```bash
rightmemory status
```

This command is a read-only operational dashboard for the configured memory root. It summarizes managed watches, async update worker state, dreamer trigger progress, repository state, recent issues, and bounded last-message previews. Full agent output remains in existing log or state files, and the dashboard points to those files instead of dumping long messages.

## Command Surface

`rightmemory status` is the default human dashboard. It does not replace lower-level commands:

- `rightmemory watch status` remains the low-level managed-watch process view.
- `rightmemory update pull --session <id>` remains the detailed per-session async update view.
- Existing watch start, stop, and restart commands remain unchanged.

The first version should not add detail flags. The default output should be useful enough for routine inspection. Future extensions may add machine-readable output or target-specific detail, but they are out of scope for this design.

`rightmemory status` must not start workers, run agents, repair sync, restart watches, review transcripts, dream, prune, update memory, or otherwise mutate memory state.

## Output Shape

Use plain text with grouped sections, not a table-first layout. Groups are easier to scan when values include paths and previews.

Example shape:

```text
RightMemory
  root: /home/lztt/.rightmemory
  git: clean on main @ 59c321d

Managed Watches
  review: running pid 12345
    log: .runtime/watch/review.log
    last: reviewed 3 sessions
  dreamer: running pid 12346
    log: .runtime/watch/dreamer.log
    trigger: 37.5/50.0 points
    last: skipped, below threshold
  pruner: stopped
    log: .runtime/watch/pruner.log
    last: failed: RuntimeError: git command failed...
  sync: disabled

Async Update
  worker: idle
  pending: 5 candidates across 2 sessions
  next flush: 2026-05-29T10:20:00+00:00
  state: .runtime/async/update/
  last: succeeded: accepted 3 candidates

Recent Issues
  pruner: failed at 2026-05-29T08:57:38+00:00
```

Formatting rules:

- Show paths relative to the memory root when possible.
- Include the memory root path at the top.
- Show one short `last:` preview per component when useful.
- Show `Recent Issues` only when there is a recent failure, stale pid, external watch, malformed runtime state, or similar issue.
- Do not colorize output in the first version.
- Do not dump full agent messages by default.
- Do not add a daily report concept.

## Data Sources

### Git State

Read Git state from the memory root with bounded commands:

- `rev-parse --is-inside-work-tree`
- `branch --show-current`
- `rev-parse --short HEAD`
- `status --short`

If the memory root is not a Git repository, report that cleanly and continue with runtime sections.

### Managed Watches

Use the existing `managed_watch_status(memory_root, name)` helper for `review`, `dreamer`, `pruner`, and `sync`. For `sync`, load sync config so the dashboard can show `disabled` when sync is intentionally off.

For each managed watch, show:

- state: running, stopped, stale, or external
- pid when running or stale
- log path
- short last preview from the corresponding watch log, if available

### Watch Log Previews

Read `.runtime/watch/<name>.log` from the end of the file. Extract a bounded preview:

- Prefer the last failure-like line or block, such as text containing `failed`, `error`, or `stopping after`.
- Otherwise use the last meaningful non-empty line.
- Keep previews to at most three lines and at most 300 characters.
- If the log file is missing, show the log path as missing and omit `last:`.

The preview is an operational hint, not a full parser for role output.

### Dreamer Trigger Progress

Read dreamer trigger state through `DreamerTriggerStore(memory_root).read()`. Load dreamer watch config to display:

- current points
- configured trigger threshold
- check interval

Render trigger progress as a direct ratio such as `37.5/50.0 points`. Do not infer a daily schedule or daily report.

### Async Update Worker And Queues

Read async update state directly from `.runtime/async/update/`.

Global worker state:

- read `_worker/state.json` if present
- show worker status, pid, batch id, and active session ids when available
- classify missing or idle worker state as idle
- if the worker pid is dead, show a stale worker state without starting a new worker

Per-session queue state:

- read `*.json` state files under `.runtime/async/update/`, excluding `_worker`
- count pending candidates
- count sessions with pending candidates
- count current batch candidates
- show the earliest `next_flush_at`
- surface last `result` or `error` as a bounded preview
- show the async state directory path for full inspection

Malformed async JSON should affect only the async update section.

## Boundaries

`rightmemory status` is best-effort and section-local:

- A failure in one section should not hide the other sections.
- Config errors for one subsystem should print in that subsystem's section when possible.
- Missing files should be reported as missing, not fatal.
- Malformed runtime state should be surfaced clearly, not silently ignored.
- The command should exit successfully when it can render the dashboard with section-local errors.
- It should exit nonzero only for severe command or environment errors that prevent identifying the memory root or running the command at all.

## Implementation Shape

Create a small status aggregation module at `rightmemory/status.py`, rather than growing `cli.py`.

Suggested internal boundaries:

- collectors for Git state, managed watches, dreamer trigger, async update, and log previews
- dataclasses for section state
- a formatter that renders the grouped text output
- a thin CLI hook in `rightmemory/cli.py` for the top-level `status` command

This keeps the dashboard testable and prevents the CLI dispatcher from owning operational logic.

## Tests

Add focused tests for collectors and formatter output:

- all watches running
- stopped, stale, and external watch states
- sync disabled
- missing watch logs
- watch log preview selection for failure lines and ordinary last lines
- dreamer trigger progress
- async update idle worker
- async update running worker with active batch
- pending async candidates across multiple sessions
- malformed async state
- Git clean, dirty, and unavailable states

Add CLI coverage that `rightmemory status` prints the grouped dashboard and returns exit code $0$ for section-local errors.

## Out Of Scope

The first version should not add:

- a daily report
- a full-message dumping flag
- machine-readable JSON output
- watch manager control for update
- a new long-running update watch process
- automatic repair, restart, review, dream, prune, or update behavior

Full messages remain available through the log and state file paths shown in the dashboard.

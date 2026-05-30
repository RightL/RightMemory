# Insight Role Design

## Summary

RightMemory separates memory consolidation from reflective insight generation.
`dreamer` remains the memory cleanup and consolidation role. It edits active
memory files and uses Git commits as its durable audit trail.

`insight` becomes a first-class autonomous role that reads the memory root and
writes timestamped reflective essays under `insight_logs/`. Insight artifacts are
durable, committed, and synced with the memory repository, but Insight does not
edit active memory.

## Goals

- Keep Dreamer focused on active memory consolidation.
- Give the original reflective "dream log" idea its own role and artifact
  stream.
- Make Insight automatic from day one through the managed watch system.
- Keep Insight inside the memory root: active memory plus prior Insight logs.
- Preserve a narrow write boundary for Insight: committed artifacts under
  `insight_logs/*.md`.
- Treat manual Dreamer and Insight messages as operator hints, while scheduled
  operation remains cycle-driven.

## Non-Goals

- Insight does not read arbitrary project directories or filesystem paths.
- Insight does not edit `MEMORY.md` or `MEMORY_*.md`.
- Insight does not validate or repair memory graph/schema structure.
- `dream_logs/` is not migrated into `insight_logs/`.
- The main conceptual docs should describe the final role model directly, not
  the historical transition.

## Architecture

Dreamer reads active memory, consolidates stale or duplicated structure, manages
durable unresolved questions through memory content, validates memory, and
commits active memory changes. Dreamer no longer writes a separate report file.
If consolidation exposes durable unresolved questions, Dreamer may write or
refine them under `# Open Context Questions`. Cycle-specific notes can appear in
the final reply. Commit messages can summarize the consolidation, but they are
not the main user-facing surface for open questions.

Insight reads `MEMORY.md`, relevant `MEMORY_*.md`, and prior
`insight_logs/*.md`. When it finds useful reflection, it creates one timestamped
essay file such as `insight_logs/2026-05-30-143012.md` and commits it with an
`insight: ...` subject. The essay can include insights, strategy, reflection,
risks, recommendations, next-step ideas, or project-improvement ideas. Those are
scope cues, not required headings or a checklist. If there is no meaningful
reflection, Insight returns a concise no-op and writes no artifact.

Retired artifact directories outside the current model are left on disk when
present, but they are not part of normal role context, write validation, sync, or
dirty-file checks.

## Runtime And Triggering

Insight has an activity-triggered watcher from the first implementation. Update
and reviewer successes feed Dreamer and Insight trigger balances independently.
Dreamer keeps its current defaults:

- trigger threshold: `50` points
- successful update candidate: `1.0` point
- reviewed provider session: `1.5` points
- check interval: `3000` seconds

Insight uses the same point sources and check interval, with a default threshold
of `150` points. Dreamer cycles do not add Insight points.

Scheduled Insight cycles check `.runtime/insight/trigger-state.json`. If the
balance is below threshold, the cycle skips. If the role writes and commits a
valid Insight log, runtime consumes the threshold after the commit lands. If the
role completes with a valid no-op, runtime also consumes the threshold because
the role made a judgment for that cycle. Failed cycles preserve the balance for
retry.

Manual `rightmemory insight --session <id> "hint"` remains available. The hint
can focus the run, but the role's source of truth is the memory root and prior
Insight logs.

Dreamer and Insight scheduled runs should use explicit cycle entry points rather
than synthetic caller messages. Manual messages remain supported as operator
hints.

## Authority Boundaries

Insight's read boundary is memory-root scoped. It can read and search active
memory plus prior Insight logs. It does not receive project-directory or general
filesystem read tools.

Insight's write boundary is the Insight artifact stream. It may create and
commit `insight_logs/*.md`. Landed Insight commits are rejected if they touch
active memory, runtime state, config, retired artifacts, or any unrelated path.
This guard applies to standalone and CLI-agent modes through isolated-write
validation.

Dreamer's write boundary narrows to active memory files:

- `MEMORY.md`
- `MEMORY_*.md`

The active tracked surfaces become role-aware:

- memory-editing roles write active memory files;
- Insight writes Insight logs;
- retired artifact directories are preserved but excluded from normal automatic
  write and sync surfaces.

## Prompts And Artifacts

`rightmemory/prompts/dreamer.md` should receive a focused edit rather than a
broad rewrite. Keep the current consolidation model and remove the report-file
requirements. Replace report-based surfacing with the existing memory mechanism:
durable unresolved questions belong under `# Open Context Questions`, while
transient observations can appear in the final reply.

`rightmemory/prompts/insight.md` describes a reflective role that reads active
memory and prior Insight logs, then writes a timestamped essay when useful.
The prompt should preserve flexible prose and role judgment. It should avoid a
fixed report template.

Insight no-op behavior is explicit: if a run has no meaningful reflection, it
returns a concise no-op, creates no file, and makes no commit.

## Install, Layout, And Sync

Fresh installs create the current memory artifact layout:

- `MEMORY.md`
- optional `MEMORY_*.md`
- `insight_logs/`

The memory-root `.gitignore` is generated as the current RightMemory allowlist
for those artifacts. Reinstall refreshes the allowlist so upgraded roots use the
current artifact model. Existing files outside the current model are left alone
by the installer.

`insight_logs/*.md` belongs to the memory Git repository and sync surface.
Insight commits are pushed by the same automatic sync flow used by other
automatic semantic roles when sync is enabled.

## Watch And Status

Managed watch gains an `insight` target. `rightmemory watch start` starts
review, dreamer, pruner, insight, and sync when sync is enabled. Targeted watch
commands such as `rightmemory watch start insight`, `stop insight`, and
`restart insight` behave like the existing managed targets.

`rightmemory status` includes Insight watch state and trigger progress: current
points, threshold, check interval, and enough last-run signal to tell whether a
due cycle wrote a log or no-oped. The dashboard should not become a viewer for
Insight log content.

## Error Handling

Insight follows the existing isolated automatic-write pattern. If a scheduled
cycle fails, leaves dirty temporary files, or tries to commit outside
`insight_logs/*.md`, the temporary worktree is discarded and the trigger balance
is preserved for retry.

If sync is enabled, runtime preflights before an automatic Insight cycle and
pushes after landed Insight commits. Sync repair remains a runtime/reconciler
responsibility; Insight does not repair dirty or conflicted memory state.

Manual Insight failures return to the caller and do not imply that an artifact
was written.

## Test Coverage

Focused tests should cover:

- Dreamer prompt assembly no longer asks for `dream_logs/`.
- Insight appears in role/config/prompt/runtime sets.
- Watch manager includes Insight by default and supports targeted Insight watch
  commands.
- Insight trigger increments from update/review success and defaults to `150`
  points.
- Isolated-write validation accepts Insight commits touching
  `insight_logs/*.md` and rejects other paths.
- Standalone commit tools enforce role-aware staging and commit boundaries.
- Installer creates and refreshes the current memory artifact allowlist.
- Sync and dirty checks use current role-owned surfaces, not retired artifacts.
- Manual and scheduled Dreamer/Insight entry points treat messages as operator
  hints.

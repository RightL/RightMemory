# Pruner Generation Forgetting Design

## Problem

RightMemory's active memory surface can accumulate facts, preferences, lessons,
and stale traces that once helped but later add noise. Ordinary retrieval then
has more text to inspect and a higher chance of returning old guidance that no
longer matters.

The desired behavior is intentionally sharp: current `MEMORY*.md` files should
forget. Git history remains the archaeology layer, but ordinary retrieval should
stay focused on the active surface.

Time-based forgetting is not a good fit because idle periods, travel, or breaks
should not make untouched memory age in the same way as seventy active memory
commits. Forgetting should follow repository activity rather than wall time.

## Goals

- Add a `pruner` role and `rightmemory prune` command for active-memory
  forgetting.
- Add a read-only `historian` role and `rightmemory history` command for
  explicit archaeology of pruned memory.
- Use Git commit generations as the pruning rhythm.
- Default one generation to `70` commits since the latest `prune:` commit.
- Use `prune:` commits as generation boundaries and as the ledger for what was
  removed or preserved by revival grace.
- Keep Git history as the explicit archaeology layer.
- Keep update, reviewer, retrieve, and dreamer focused on their current duties.
- Avoid separate prune log files, runtime history indexes, lifecycle metadata in
  node text, or durable exemption tags.
- Give a memory item that was pruned and later written back one extra generation
  of grace before it can be pruned again.

## Non-Goals

- Do not scan provider transcripts during pruning.
- Do not make update or reviewer search historical prune commits.
- Do not add 30-week, 300-week, or permanent memory tiers in this design.
- Do not purge or rewrite Git history.
- Do not use wall-clock age to decide whether a generation is due.
- Do not fold pruned-memory archaeology into ordinary `retrieve`.

## Core Semantics

`MEMORY.md` and sibling `MEMORY_*.md` files are the active work surface. They do
not promise permanent retention. When memory leaves that surface, the
corresponding Git commit remains available for explicit archaeology.

Pruner works at generation boundaries:

1. Find the latest commit whose subject starts with `prune:`.
2. Count commits from that boundary to `HEAD`.
3. If the count is below `generation_commits`, report that pruning is not due.
4. If the count reaches the threshold, compare the boundary snapshot with the
   current memory files.
5. Remove current memory entries that crossed the generation without semantic
   change, except entries currently covered by revival grace.
6. Commit the generation result with a `prune:` subject and a body that records
   the boundary, removed entries, grace state, and skipped entries.

On a memory root with no prior `prune:` commit, the first due boundary is
`HEAD~generation_commits` when that ancestor exists. If the repo has reached the
threshold but the exact ancestor is not available, use the oldest available
commit as the first boundary. If the repo has fewer commits than the threshold,
pruning is not due.

## Revival Grace

Revival grace handles the simple durable-looking case without creating durable
memory tiers.

If a prune commit removed an item and a later update, review, or dream cycle
writes a semantically matching item back into current memory, pruner treats that
item as revived. A revived item gets one extra generation of grace:

- At the first due prune after revival, pruner keeps the item and records
  `grace 1/2` in the new `prune:` commit body.
- At the next due prune, if the item is still unchanged, pruner keeps it again
  and records `grace 2/2`.
- At the following due prune, if the item is still unchanged, grace is spent and
  the item can be removed.

If a revived item is semantically changed before grace is spent, pruner treats
the changed item as active memory for the new generation. The prior grace state
does not need to continue because the memory has received fresh semantic work.

Revival matching is conservative. Same id is the strongest signal. Same heading
path plus closely matching meaning can also count. If pruner is not confident,
it keeps the item for that generation and records the uncertainty in the commit
body.

## Commit Subjects And Ledger

Pruner uses a distinct subject prefix so Git archaeology can search pruning
events cleanly:

```text
prune: expired active memory
prune: checkpoint
```

Use `prune: expired active memory` when memory files changed. Use
`prune: checkpoint` when a generation is due and the boundary should advance,
but no memory file changes were needed. A checkpoint can be an empty Git commit.

Commit bodies are the ledger. They should be compact, stable enough for the next
pruner run to parse, and readable in ordinary `git log` output:

```text
Boundary: <commit>
Generation commits: 70

Removed:
- `<id>` path: <heading path>; topic: <short keywords>; summary: <short summary>

Revival grace:
- `<id>` grace 1/2; revived from: <prune commit>; path: <heading path>

Skipped:
- `<id>` reason: <short reason>
```

The ledger is not a new memory store. It is the explanatory body of the Git
commit that changed or checkpointed the active surface.

## Configuration

Pruner has role-local configuration:

```toml
[pruner]
generation_commits = 70
revival_grace_checkpoints = 2

[pruner.model]
model_id = "..."

[historian.model]
model_id = "..."
```

`generation_commits` is a positive integer. `revival_grace_checkpoints` is a
positive integer and defaults to `2`, which means two due prune checkpoints can
preserve a revived item before it becomes eligible again. This gives a revived
item one extra generation compared with an ordinary newly written item.

Standalone and cli-agent execution should mirror existing write-capable roles.
Pruner can run manually through `rightmemory prune`; watch integration can call
the same command opportunistically because the command itself decides whether a
generation is due.

Historian uses the same executor configuration shape as retrieve. It is
read-only and receives Git history read tools, not memory edit or commit tools.

## Comparison Model

Pruner parses addressable headings and nodes from the boundary snapshot and the
current memory files. Each parsed item carries:

- id;
- file path;
- heading path;
- addressable line text;
- direct heading body when applicable;
- edges.

The comparison starts structurally and adds semantic judgment where it helps:

- Same id with unchanged addressable text and unchanged relevant body is
  unchanged.
- Same id with materially changed description, heading title, body, placement,
  or edges is active for the current generation.
- Current items absent from the boundary are new for the current generation.
- Items removed from the current surface need no action unless they matter for
  edge cleanup.
- Revived items are detected by comparing current new items with the prior
  prune ledger's `Removed` entries and current grace ledger entries.

When uncertain, pruner should skip deletion for that item and explain the skip in
the prune commit body. Skips are acceptable because the next generation gives
the system another chance with more evidence.

## File Edits

Pruner may delete stale nodes, remove stale anchored headings, and clean nearby
structure left empty by those deletions. It should keep the surrounding Markdown
coherent rather than leaving orphan headings or dangling edges.

Before committing, pruner runs memory validation. Dangling edges caused by
deleted items should be removed or the deletion should be skipped and recorded.

Pruner commits touched `MEMORY.md` and `MEMORY_*.md` files. A `prune:
checkpoint` commit may be empty when no file changes are needed but the
generation boundary or grace ledger needs to advance.

## Error Handling

- Dirty memory files stop pruning before semantic work begins.
- A missing or too-shallow history means pruning is not due.
- A boundary snapshot that cannot be read safely causes a checkpoint with no
  memory edits, or a clear failure if even checkpointing would be misleading.
- Validation failure prevents the commit and leaves the worktree clean.
- Ambiguous semantic matches are skipped rather than guessed.
- If the command is invoked before the generation threshold, it exits with a
  status message and does not create a checkpoint.

## Historical Retrieval

Ordinary `retrieve` reads the active memory surface and does not inspect old
prune commits by default.

Historical recovery is explicit through `rightmemory history`, backed by the
read-only `historian` role. Historian searches the Git archaeology layer without
changing current memory.

Historian's retrieval flow:

1. Search `prune:` commit subjects and bodies for ids, heading paths, topics,
   summaries, and query terms.
2. Inspect matching prune commit bodies to identify removed entries and their
   source files.
3. Use Git snapshots such as `<prune-commit>^:<path>` to recover the original
   addressable line and nearby heading context from before removal.
4. Return matches clearly labeled as historical/pruned memory rather than active
   memory.
5. When a historical item looks useful again, report enough detail for the caller
   to submit an ordinary update candidate. Historian does not write the recovered
   item back itself.

Historian can search beyond prune commit bodies when the query names a specific
id, file path, or phrase, but prune commits remain the primary index because
their bodies were written for archaeology. No separate history index is added.

## Tests

Focused tests should cover:

- config defaults and validation for `[pruner]`;
- role registration and prompt assembly for `pruner` and `historian`;
- first-run threshold behavior with fewer than `generation_commits` commits;
- first-run pruning from `HEAD~generation_commits`;
- subsequent pruning from the latest `prune:` commit;
- no pruning before 70 commits since the latest `prune:` commit;
- removal of unchanged addressable nodes across a generation;
- preservation of current-generation new or changed nodes;
- `prune: expired active memory` body includes removed ids, paths, and boundary;
- `prune: checkpoint` can be an empty commit;
- revived item receives `grace 1/2`, then `grace 2/2`, then becomes eligible;
- semantic changes clear revival grace by making the item active again;
- dirty memory files block pruning;
- edge cleanup keeps validation passing after deletions;
- retrieve, update, reviewer, and dreamer prompts do not inherit prune duties;
- ordinary retrieve does not inspect `prune:` commits;
- `rightmemory history` uses historian and read-only Git history tools;
- historian recovers a pruned addressable line from a prune ledger and prior Git
  snapshot;
- historian labels returned items as historical/pruned memory and does not write
  them back.

## Upgrade Impact

Existing memory roots do not need migration. The first `rightmemory prune` run
will either report that the repository has not reached the commit threshold or
establish the first prune generation from existing Git history.

The implementation should update README, design notes, role prompt inventory,
configuration docs, and tests. A semantic upgrade note is not needed for
existing memory content because this feature changes future lifecycle behavior
and adds an explicit archaeology path rather than changing the schema meaning of
existing nodes.

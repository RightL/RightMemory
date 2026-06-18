# Retrieve Prefix Cache Design

## Purpose

RightMemory retrieve currently spends too much time in model tool rounds. The
retriever is instructed to read `MEMORY.md`, then uses broad read/search tools
to reconstruct context that can be supplied directly. The new design makes
ordinary retrieve prefix-cache-first: runtime supplies a stable daily memory
snapshot before the query, and retrieve answers from that supplied context
without tools on the normal path.

## Goals

- Put active memory in a byte-stable daily context prefix.
- Keep system instructions static and separate from memory data.
- Keep the user query last.
- Preserve resumed-session cache hits by appending only new volatile context.
- Support same-day memory changes with incremental diffs.
- Keep pending submitted memory visible until update consolidates it.
- Preserve `S#`, `MF#`, and `MQ#` progressive disclosure without broad tools.
- Avoid reintroducing provider-question calls into retrieve.

## Non-Goals

- No structured graph-query tool redesign in this iteration.
- No inline expansion of memory skill files in the daily snapshot.
- No inline expansion of `MF#` mirrored imports in the daily snapshot.
- No retrieve-side `MQ#` ask command.
- No compatibility layer for old `M#` shared-view behavior.

## Current State

The merged branch uses schema-defined `MF#` and `MQ#` shared-view headings.
Retrieve silently pulls accepted `MF#` file views before model start, stores
mirrored files under `.runtime/shared_views/imports/<mf-id>/`, and does not add
pull results to session history. Retrieve does not expose `retrieve_shared_view`.
For `MQ#`, retrieve reports that provider-question context may help, including
the local relationship context, but it does not call provider ask commands or
invent questions.

The current retrieve prompt still says to read `MEMORY.md` before retrieval.
That instruction conflicts with the prefix-cache-first direction and should be
replaced.

## Prompt Shape

System prompt remains static:

```text
RightMemory retrieve instructions
schema
rewritten retrieve role prompt
```

Memory data is supplied as context, not as system authority:

```text
synthetic daily context:
  daily canonical active-memory snapshot

saved session history:
  prior real query and answer turns only

current appended message:
  memory diff since this retrieve session last saw memory HEAD, if any
  newly delivered recent submitted memory candidates, if any
  current query
```

The daily context is not saved into retrieve session history. The volatile
blocks are appended per turn so earlier bytes remain stable.

## Daily Snapshot

The daily snapshot contains canonical local active memory:

- `MEMORY.md`
- ordinary `MEMORY_<slug>.md` detail files, excluding
  `MEMORY_SKILL_*.md`
- `S#` heading lines and direct heading body paragraphs
- `MF#` heading lines and direct heading body paragraphs
- `MQ#` heading lines and direct heading body paragraphs
- open context question nodes

The snapshot does not contain:

- `MEMORY_SKILL_*.md` full bodies
- `.runtime/shared_views/imports/` mirrored file content
- provider-owned `shared_views/<view-id>/` files
- runtime config, traces, credentials, or logs
- snapshot date or id inside the model-visible text

Snapshot metadata can record the day, base commit, content hash, and file list
outside the prompt.

## Same-Day Diffs

Committed memory changes after the daily snapshot are supplied as Git diffs.
Diffs are appended only when the retrieve session has not already seen the
current memory commit. If there is no diff to deliver, runtime omits the diff
block entirely.

The diff block says that the model should mentally apply the patch to the daily
snapshot. Added lines are newer memory. Removed lines are obsolete.

The diff scope is active memory only:

- `MEMORY.md`
- ordinary `MEMORY_<slug>.md` detail files, excluding
  `MEMORY_SKILL_*.md`

If a skill body changes, retrieve should rely on `read_skill` when the matching
`S#` heading is relevant instead of appending full skill-body diffs by default.

## Pending Submitted Memory

Recent submitted memory remains a volatile block. It includes submitted update
candidates that are not yet consolidated into active memory, including jobs
currently in an update worker batch.

The existing per retrieve-session delivered-candidate tracking remains useful:
only newly delivered candidates are appended. Failed retrieve turns do not mark
candidates as delivered. If there are no newly delivered candidates, runtime
omits the recent submitted memory block entirely.

## Session State

Retrieve needs two delivery cursors:

- Delivered recent submitted memory keys, already implemented.
- Last delivered memory commit for same-day diffs, new.

After a successful retrieve response, runtime records the current memory commit
as delivered for that retrieve session. If the model turn fails, runtime does
not advance either cursor.

Session history stores only real conversation turns. It does not store the
daily snapshot, MF pull status, generated diffs, or pending candidate blocks.

## Progressive Tools

Ordinary retrieve should answer from supplied context without tools. Tools are
reserved for progressive disclosure when the supplied snapshot indicates that
extra material is needed.

### `read_skill(skill_id)`

Reads the full body for `MEMORY_SKILL_<skill_id>.md`.

Use only when a relevant `S#` heading matched and the caller needs the full
instruction body.

Failure output is id-only:

```text
Skill not found: <skill-id>

Available skills:
- <id>
```

### `read_mf(mf_id)`

Reads the whole mirrored `MF#` import package for the given id, including clear
file separators and external provenance.

Use only when a relevant `MF#` heading matched and the caller needs mirrored
provider context.

Failure output is id-only:

```text
MF import not found: <mf-id>

Available MF imports:
- <id>
```

There is no path argument in the model-facing tool contract.

## `MQ#` Behavior

`MQ#` remains recommendation-only in retrieve. When an `MQ#` heading is
relevant, retrieve returns the local heading and relationship context, and says
that provider-question context may help. It must not call ask commands, suggest
a made-up question, or imply that provider context is already known.

## Tool Surface

The target retrieve tool surface is:

- `read_skill`
- `read_mf`

The older `read`, `grep`, `glob`, `outline`, `validate_memory`, and
`read_command` tools may remain temporarily as fallback during migration, but
the rewritten prompt should make clear that ordinary retrieval does not use
them. `read_command` should not be part of the final retrieve hot path.

## Runtime Responsibilities

Runtime owns the context assembly:

1. Pull accepted `MF#` file views before model start, as current code already
   does.
2. Load or rebuild the daily active-memory snapshot.
3. Add the daily snapshot as synthetic context outside saved conversation
   history.
4. Append a memory diff from the session delivery cursor to current memory
   HEAD.
5. Append newly delivered recent submitted memory candidates.
6. Append the current query last.
7. On success, save only the real query and answer to session history and
   advance delivery cursors.

## Testing

Tests should cover:

- `prompts/retrieve.md` no longer tells retrieve to read `MEMORY.md`.
- Snapshot text is not stored in session history.
- New sessions receive daily snapshot, diff, pending candidates, then query.
- Existing sessions append only new diff and new pending candidates.
- Failed turns do not advance memory-commit or pending-candidate cursors.
- `read_skill` returns a full skill body by id.
- `read_skill` failure lists available skill ids without paths.
- `read_mf` returns mirrored package content by id.
- `read_mf` failure lists available MF ids without paths.
- Retrieve prompt does not expose `retrieve_shared_view` or `MQ#` ask behavior.
- If the assembled provider request is easy to inspect in tests, repeated
  retrieves on the same daily snapshot should have an identical byte prefix up
  to the first volatile block.
- Full `python -m compileall -q rightmemory tests` and
  `python -m unittest discover -s tests` pass.

## Open Implementation Choice

The implementation plan should decide the exact representation of synthetic
context for standalone Pydantic AI and CLI-agent mode. The behavior must remain
the same: daily memory snapshot is context data, not system instructions, and
it is not persisted as ordinary retrieve session history.

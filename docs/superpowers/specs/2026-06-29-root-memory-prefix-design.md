# Root Memory Prefix Design

## Purpose

The retrieve prefix-cache design currently preloads `MEMORY.md` plus ordinary
`MEMORY_<slug>.md` detail files. That makes every retrieve call pay for all
detail memory, even when the caller only needs a small part of it.

This design narrows the stable retrieve prefix to `MEMORY.md` and moves
ordinary detail files behind a typed progressive-disclosure tool.

## Goals

- Keep prefix-cache-first retrieve behavior.
- Preload only `MEMORY.md` in the daily snapshot.
- Keep ordinary `MEMORY_<slug>.md` files available on demand.
- Preserve the existing typed tool style used by `read_skill` and `read_mf`.
- Avoid broad filesystem, shell, search, or path-style tools in retrieve.
- Keep same-day automatic diffs focused on the preloaded root memory.

## Non-Goals

- No return to structured-tool-first retrieval.
- No generic retrieve filesystem read tool.
- No inline expansion of `MEMORY_SKILL_*.md` bodies.
- No inline expansion of `MF#` mirrored imports.
- No retrieve-side `MQ#` ask behavior.
- No change to update, insight, dreamer, or review memory-writing flows.

## Current State

`rightmemory/retrieve_context.py` selects active memory with
`active_memory_paths()`. Today that selector includes `MEMORY.md` and ordinary
`MEMORY_<slug>.md` files, while excluding `MEMORY_SKILL_*.md`.

Retrieve exposes only two model-facing tools:

- `read_skill(skill_id)`, which reads `MEMORY_SKILL_<skill_id>.md`.
- `read_mf(mf_id)`, which reads a pulled `.runtime/shared_views/imports/<id>/`
  package.

There is no dedicated tool for ordinary detail files because those files are
currently preloaded in the daily snapshot.

## Target Behavior

The daily snapshot should contain only:

- `MEMORY.md`

The daily snapshot should not contain:

- ordinary `MEMORY_<slug>.md` detail file bodies
- `MEMORY_SKILL_*.md` skill bodies
- `.runtime/shared_views/imports/` mirrored file content
- provider-owned `shared_views/<view-id>/` files
- runtime config, traces, credentials, or logs

The retriever first answers from `MEMORY.md`. If `MEMORY.md`, the user query,
or same-day root-memory diff indicates that a detail file is relevant, the
retriever calls `read_memory_file(slug)` to fetch that file.

## New Tool

Add a retrieve-only model-facing tool:

```text
read_memory_file(slug)
```

The tool resolves `slug` to exactly `MEMORY_<slug>.md` under the memory root.
It does not accept paths. It rejects absolute paths, path separators, parent
traversal, hidden-file names, empty slugs, and names that would resolve to
`MEMORY_SKILL_*.md`.

Successful output should mirror the existing progressive tools:

```text
===== MEMORY_<slug>.md =====
<file text>
```

Missing-file output should be clear and id-oriented:

```text
Memory file not found: <slug>

Available memory files:
- <slug>
```

The available list is derived from ordinary `MEMORY_<slug>.md` files only. It
must not include `MEMORY.md`, `MEMORY_SKILL_*.md`, runtime files, or imported
shared-view files.

## Same-Day Diffs

Same-day automatic diffs should follow the same visibility rule as the daily
snapshot. Runtime should append committed diffs for `MEMORY.md` only.

Detail-file changes are delivered through `read_memory_file(slug)` when the
detail file is relevant. If a detail-file change matters, the corresponding
`MEMORY.md` summary or pointer should be updated so the root memory can route
retrieve toward the detail file.

## Prompt Contract

The retrieve prompt should say:

- Runtime supplies a daily root-memory snapshot before the caller query.
- The snapshot contains `MEMORY.md`, not all memory files.
- `read_memory_file(slug)` reads ordinary `MEMORY_<slug>.md` detail files.
- `read_skill(skill_id)` remains the tool for `MEMORY_SKILL_*.md`.
- `read_mf(mf_id)` remains the tool for pulled `MF#` imports.

The prompt should keep the user query last and keep memory data as context, not
system authority.

## Runtime Responsibilities

Runtime owns the context assembly:

1. Pull accepted `MF#` file views before model start, as it already does.
2. Load or rebuild the daily root-memory snapshot.
3. Add the daily snapshot outside saved conversation history.
4. Append a `MEMORY.md` diff from the session delivery cursor to current memory
   `HEAD`, if needed.
5. Append newly delivered recent submitted memory candidates.
6. Append the current query last.
7. On success, save only the real query and answer to session history and
   advance delivery cursors.

## Data Boundaries

This design keeps three memory disclosure levels:

- `MEMORY.md`: preloaded root context and routing surface.
- `MEMORY_<slug>.md`: ordinary detail context, read by `read_memory_file`.
- `MEMORY_SKILL_<slug>.md` and `MF#` imports: specialized context, read by the
  existing `read_skill` and `read_mf` tools.

The runtime should keep these levels separate in both implementation and tests.

## Testing

Tests should cover:

- Daily snapshot renders `MEMORY.md` and excludes ordinary `MEMORY_<slug>.md`
  detail files.
- Daily snapshot still excludes `MEMORY_SKILL_*.md` and runtime import files.
- Same-day diff generation includes `MEMORY.md` changes and excludes ordinary
  detail-file changes.
- Retrieve runtime exposes exactly `read_memory_file`, `read_skill`, and
  `read_mf`.
- `read_memory_file` returns a valid ordinary detail file by slug.
- `read_memory_file` missing-file output lists available ordinary detail slugs.
- `read_memory_file` rejects path traversal, path separators, absolute paths,
  empty slugs, and `MEMORY_SKILL_*.md` targets.
- Existing `read_skill` and `read_mf` behavior remains unchanged.
- Retrieve prompt mentions the root-memory snapshot and `read_memory_file`.
- Full `python -m compileall -q rightmemory tests` passes.
- Full `python -m unittest discover -s tests` passes.

## Migration Notes

Existing cached daily snapshots may contain old detail-file content until they
are rebuilt. The implementation can either tolerate that for the current day or
change the snapshot metadata shape so old cached snapshots are invalidated when
the root-memory-only selector lands. Invalidating by metadata shape is cleaner
because tests and real retrieve behavior converge immediately.

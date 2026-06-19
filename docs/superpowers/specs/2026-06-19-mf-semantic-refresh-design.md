# MF Semantic Refresh Design

## Context

`MF#` mirrored file views currently have one extractive implementation. The
shared-view builder receives a natural-language request, chooses concrete
memory ids, and writes `shared_views/<view-id>/recipe.toml`. Automatic publish
then rebuilds `dist/MEMORY.md` from that recipe after provider memory writes.

That works mechanically, but the fixed id recipe is too rigid as provider
memory evolves. Headings and nodes can be renamed, split, merged, or pruned.
Some source nodes can also mix private details with shareable facts, which makes
id extraction too coarse.

The product should keep the current cheap recipe render path, but add periodic
semantic refresh from a durable refined intent. It should also support a
generative `MF#` render mode for cases where sanitized shared memory must be
rewritten instead of extracted.

## Goals

- Keep one durable file-view source: `shared_views/<view-id>/recipe.toml`.
- Have the builder refine the initial user request into a durable `intent`.
- Add two file-view render modes: `extractive` and `generative`.
- Rebuild recipes semantically on a simple time-based cadence.
- Preserve approval and publish settings after successful semantic refresh.
- Avoid relevance detection, manual proposals, or extra derived source files.
- Keep generated `dist/` output uncommitted.
- Prevent shared-view maintenance commits from advancing active-memory pruning.

## Non-Goals

- No per-write semantic relevance classifier.
- No proposal or manual review loop for every refresh.
- No committed `derived.md` or second public memory source.
- No change to `MQ#` question views.
- No attempt to solve detailed privacy classification in this design.
- No backwards compatibility for the old file-view recipe render value.

## Architecture

`recipe.toml` remains the source of truth for `MF#` provider views.
`view.md` may still exist for package compatibility and human display, but it
is derived from `recipe.toml` title and intent. It must not carry independent
file-view semantics.

Common file-view fields:

- `version`
- `view_id`
- `kind = "file"`
- `title`
- `intent`
- `approved`
- `render`
- `[publish]`
- `semantic_refresh_days`
- `last_semantic_refresh_at`
- `last_semantic_refresh_memory_commit`

`render` is one of:

- `extractive`
- `generative`

The existing `render = "expanded-heading-subtrees"` recipe value should be
removed. This design intentionally does not preserve old recipe compatibility;
the product should use the new clean render mode directly.

For `render = "extractive"`, the recipe includes the existing selection fields:

- `include_headings`
- `include_nodes`
- `include_files`
- `exclude_ids`

For `render = "generative"`, selection fields are absent. The builder uses the
stored `intent` and current provider memory to write the final
`dist/MEMORY.md` directly. `dist/MEMORY.md` is still generated runtime output,
not committed provider source.

## Initial Build Flow

`shared-view build-file` remains agent-backed. The caller supplies a rough
intent, title, hub URL, and credential id.

The shared-view builder now does two decisions:

1. Refine the rough request into a durable `intent`.
2. Choose `render = "extractive"` or `render = "generative"`.

Use `extractive` when clean headings, nodes, or files can represent the view.
Use `generative` when source memory mixes private details with useful
shareable facts, or when the consumer-facing memory should be rewritten for
clarity.

The builder must use model-facing tools rather than hand-writing machine
config. These tools are internal to the shared-view-builder role; they are not
extra user prompts or user-visible ceremonies.

Use `create_extractive_file_view` for extractive views. It accepts the refined
intent, selection ids/files, exclusions, and publish settings. It writes
canonical `recipe.toml`, creates `view.md`, and renders preview
`dist/MEMORY.md`.

Use `create_generative_file_view` for generative views. It accepts the refined
intent, the agent-written `published_context` body, and publish settings. The
tool, not the agent, wraps that body in the canonical shared-view
`dist/MEMORY.md` structure. It writes canonical `recipe.toml`, creates derived
`view.md`, and writes canonical `dist/MEMORY.md`.

New file views default to `semantic_refresh_days = 7`. The first
implementation does not need a CLI or Web Studio control for this value. Users
may edit `recipe.toml` directly, and validation should reject invalid cadence
values.

Initial build writes `last_semantic_refresh_at` and
`last_semantic_refresh_memory_commit`, because the builder has just
materialized the view from current active memory.

## Normal Publish Flow

Approved file views keep the existing automatic publish behavior.

For `extractive` views:

- render `dist/MEMORY.md` from current provider memory and recipe selection;
- export and publish the package.

For `generative` views:

- publish the existing generated `dist/MEMORY.md`;
- do not rerun the model on every provider memory write.

This keeps ordinary memory writes cheap and predictable.

If a generative view is approved but `dist/MEMORY.md` is missing, normal publish
must fail closed with an operator-visible event. It should not silently publish
an empty package or invoke a builder agent from the publish path.

## Semantic Refresh Flow

Semantic refresh is time-based per view.

Each recipe stores:

- `semantic_refresh_days`
- `last_semantic_refresh_at`
- `last_semantic_refresh_memory_commit`

`last_semantic_refresh_memory_commit` is the latest commit that touched active
memory paths when the refresh succeeded. It is not the raw repository `HEAD`,
because shared-view refresh commits should not make a view look stale again.

Semantic refresh is not part of the ordinary auto-publish call stack. It runs
through a separate maintenance entry point, such as a future
`shared-view refresh-file <view-id>` command or shared-view refresh watch. That
entry point may run before normal publishing in an operator workflow, but
`publish_approved_file_views` itself should not start a builder agent.

The maintenance entry point checks whether semantic refresh is due. A view is
due when:

- `semantic_refresh_days` is positive;
- enough wall-clock days have passed since `last_semantic_refresh_at`, or the
  field is missing;
- the current active-memory commit differs from
  `last_semantic_refresh_memory_commit`.

If the view is due, RightMemory reruns the shared-view builder from:

- existing `view_id`;
- existing `title`;
- stored refined `intent`;
- existing publish settings;
- current provider memory.

On success:

- replace the recipe with the new canonical recipe;
- preserve `approved = true` if the old recipe was approved;
- preserve publish settings;
- update `last_semantic_refresh_at`;
- update `last_semantic_refresh_memory_commit`;
- render or generate `dist/MEMORY.md`;
- commit changed shared-view source files such as `recipe.toml`;
- optionally publish if the maintenance entry point requested publish after
  refresh.

The refresh maintenance entry point owns the deterministic commit. The
model-facing builder tools write and validate artifacts, but they do not decide
what to stage or commit. After validation passes, the maintenance code stages
only allowed shared-view source files and creates the refresh commit.

On failure:

- keep the previous recipe;
- keep the previous `dist/MEMORY.md`;
- record a publish or refresh failure event;
- do not publish a broken replacement.

## Generative Rendering

A generative file view has no selection fields. Its recipe describes the
semantic writing contract through `intent` and `render = "generative"`.

The builder calls `create_generative_file_view` with a `published_context` body.
The tool writes `dist/MEMORY.md` in canonical wrapper form. The output must:

- be non-empty;
- include the standard shared-view title and intent wrapper;
- include `## Published Context`;
- contain only shareable provider context;
- avoid private details even when they appear in the same source node as useful
  facts.

The generated file is not committed. Rebuilding it requires a semantic refresh.

The first implementation should not add hidden provenance for generative
outputs. `dist/manifest.toml` should continue to describe the package, not the
source memory evidence used by the model.

## Trigger Hygiene

Semantic refresh may modify committed shared-view source files such as
`shared_views/<view-id>/recipe.toml`. Those commits must not accidentally age
active memory for pruning.

Semantic refresh commits should stage only shared-view source files. They
should not stage generated `dist/` output. A successful semantic refresh updates
recipe metadata, so it normally has a source change to commit. If some future
maintenance path only regenerates missing generated output without semantic
refresh, that path should not create a commit.

Dreamer and Insight remain point-based and do not need to change for this
feature.

Pruner should keep its commit-generation model, but count only commits that
touch active memory paths:

- `MEMORY.md`
- `MEMORY_*.md`

Pruner boundary selection should use the same active-memory path filter.
Commits that only touch `shared_views/`, `shared_views.toml`, `shares.toml`,
`insight_logs/`, or other non-active-memory metadata should not advance prune
generations.

This keeps each trigger aligned with its domain:

- semantic consolidation uses points;
- pruning uses active-memory commits;
- shared-view semantic refresh uses per-view time cadence.

## Validation And Failure Behavior

Recipe validation should enforce:

- `render` is required for file views;
- `render = "expanded-heading-subtrees"` is rejected;
- `extractive` recipes include at least one selection source;
- `generative` recipes do not include selection fields;
- publish settings remain valid when present;
- semantic refresh metadata has valid types.

Refresh failure is fail-closed:

- do not replace the old recipe;
- do not delete the old generated output;
- do not publish the failed output;
- log the failure for operators.

If a previously approved recipe refreshes successfully, approval remains true.

## Testing

Focused coverage should include:

- loading and rendering extractive recipes with `render = "extractive"`;
- rejecting extractive recipes without selections;
- loading generative recipes without selection fields;
- rejecting generative recipes with selection fields;
- rejecting old `render = "expanded-heading-subtrees"` recipes;
- builder tool tests for refined intent and render mode;
- `create_extractive_file_view` and `create_generative_file_view` tool tests;
- semantic refresh due and not-due cases;
- successful refresh preserving approval and publish settings;
- failed refresh preserving the previous recipe and output;
- automatic publish failing closed when an approved generative view has no
  generated output;
- semantic refresh maintenance not running from the normal auto-publish call
  stack;
- pruner counting only active-memory path commits.

# Structured Retrieve Selection Design

## Purpose

RightMemory retrieve currently asks the model to choose relevant context and
also write the final response. Even when retrieval finds the right items, this
lets explanatory prose, repeated headings, weak matches, and other model-made
text leak into the caller-facing result.

This design separates semantic selection from rendering. The model's only
final action is one structured selection. RightMemory validates that selection
and renders the authoritative source content itself.

## Goals

- Keep model judgment for semantic relevance.
- Prevent model-written summaries or commentary from entering retrieve output.
- Support durable Memory, live Pursuit, F# detail files, M# evidence, S# skills,
  MF# mirrored views, MQ# question-view references, and recent updater
  candidates.
- Preserve progressive disclosure and the existing daily root-prefix design.
- Use graph ids wherever content is addressable and line ranges only where it
  is not.
- Produce the same caller-visible behavior in standalone and CLI-agent modes.
- Preserve session-level omission of previously returned content, with an
  explicit per-call override.

## Non-Goals

- No replacement of model-based relevance judgment with lexical or embedding
  search.
- No new ids inside free-form Markdown merely to support retrieval.
- No automatic expansion of graph edges.
- No automatic MQ# provider question call.
- No ordinary retrieval access to updater-only `corrections.md`.
- No change to Memory, Pursuit, M#, S#, MF#, or MQ# stored-document semantics.
- No implementation work in this design phase.

## Existing Context Boundary

The runtime continues to supply the stable daily root snapshot and volatile
turn context before the caller query. In the pre-09:16 implementation, the
root snapshot contains `MEMORY.md` and `PURSUITS.md`; relevant F#, M#, S#, and
MF# content remains behind typed progressive reads. Recent updater candidates
remain volatile evidence rather than settled graph content.

The new selection contract changes the retriever's output boundary, not the
prefix-cache architecture. The current query remains last, snapshot data
remains outside system authority, and synthetic context is not saved as an
ordinary conversation turn.

This design intentionally supersedes the older prefix-cache specs' scoped
non-goal against structured selection. It preserves their context assembly and
progressive-read boundaries while replacing only model-authored retrieval
output.

## Architecture

Retrieve becomes a select-then-render pipeline:

1. Runtime assembles the existing root snapshot, same-day diff, newly visible
   recent candidates, prior retrieve context, and current query.
2. The model inspects the supplied graph and uses typed reads when progressive
   content is needed.
3. The model performs exactly one terminal selection as its final action.
4. Runtime validates and resolves the selection against authoritative source
   content.
5. Runtime applies session omission, merges overlapping selections, reconstructs
   useful hierarchy, and renders clean Markdown.
6. Runtime records the real query, rendered answer, and delivered selection
   coverage only after successful rendering.

The model does not provide titles, descriptions, reasons, confidence values,
summaries, or final prose. Those fields are absent from the selection schema.

## Selection Contract

One provider-neutral contract carries every selection kind. Its logical shape
is:

```json
{
  "ids": ["local-graph-id"],
  "sources": [
    {
      "source_id": "M#agent-corrections-writing",
      "ids": [],
      "ranges": [{"start": 12, "end": 24}]
    },
    {
      "source_id": "MF#auth-api",
      "ids": ["token-refresh"],
      "ranges": []
    },
    {
      "source_id": "S#two-side-review",
      "ids": [],
      "ranges": []
    }
  ],
  "recent_candidates": ["session-id:3"]
}
```

The exact Python and JSON type definitions may use typed variants rather than
optional fields, but they must preserve these semantics:

- `ids` contains globally unique ids from the local Memory + Pursuit graph.
- A linked `source_id` includes its marker so resolution is unambiguous.
- MF# addressable ids are scoped to their MF# source and never enter the local
  global graph namespace.
- Inclusive line ranges apply only to source text that lacks an addressable id.
- Selecting an S# source means selecting its complete skill body; S# ranges are
  invalid.
- Recent candidates use their existing session-and-candidate composite identity.
- An empty valid selection means `no strong match`.

## Graph Selection Semantics

Memory and Pursuit form one parsed graph with globally unique ids. F# backing
files are part of that graph and follow the same selection behavior.

Selecting an addressable heading returns:

- the minimal ancestor heading path needed to locate it;
- the selected heading line and direct body;
- its entire logical subtree, including F# detail content when the selected
  heading owns that subtree.

Selecting a node returns its minimal ancestor heading path and the node itself.
When a selected Pursuit is referenced by `## Focus`, runtime also includes its
Focus entry because resume order is meaningful live context outside the Pursuit
subtree.

When several selections overlap, runtime renders each source item once and
merges them into the smallest coherent tree. Ancestor headings included only as
context do not implicitly select their bodies or sibling subtrees. Graph edges
remain verbatim references; their targets appear only when explicitly selected.

## Linked-Resource Semantics

Selecting an M#, S#, MF#, or MQ# heading through its local graph id returns the
local heading according to normal heading semantics. It does not by itself
select linked content.

### M# Evidence

M# backing files are free-form Markdown. Typed reads return line-numbered text
to the model. The model selects one or more inclusive line ranges. Runtime
returns the original source text without line-number prefixes.

The fixed Writing and Design agent-correction M# collections follow this path.
Root-level `corrections.md` remains updater-only and is neither readable nor
selectable by ordinary retrieve.

### S# Skills

S# backing files are coherent executable instructions. A typed read shows line
numbers for inspection consistency, but selecting an S# source always
returns the complete original skill. Partial skill ranges are rejected because
they could omit constraints or change the instruction's meaning.

Broad retrieval may select only the local S# heading. The model expands the S#
source only when the caller asks for the skill or its full instruction is
needed.

### MF# Mirrored Views

An MF# connection resolves to the mirrored view's canonical `dist/MEMORY.md`.
Package metadata such as `view.md`, `recipe.toml`, and manifests is not retrieval
content and is not exposed through the selection contract.

When the mirrored view contains addressable items, the model selects those ids
inside the MF# source namespace. When relevant text is not addressable, the
model selects line ranges from line-numbered view content. MF# ids never collide
with local graph ids because resolution always includes the MF# source id.

### MQ# Provider Questions

MQ# has no linked content selection. Selecting its local heading returns the
local relationship context plus a fixed runtime-rendered indication that
provider question context is available. Retrieve does not call the provider,
invent a question, or imply that a provider answer is known.

### Recent Candidates

A selected recent updater candidate is returned whole because candidates are
already concise evidence units. Runtime labels it as recent submitted evidence,
not settled Memory or Pursuit.

## Line-Range Rendering

Line numbers are model-facing selection coordinates only. Final output never
shows them.

For each selected range, runtime:

- validates inclusive bounds against the exact content read by the model;
- includes the enclosing Markdown heading for context;
- expands a boundary that cuts through a fenced code block to include the
  complete fence;
- merges overlapping or adjacent resolved ranges when doing so does not change
  their enclosing-heading context;
- preserves the original source text exactly.

No synthetic chunk ids are created. A line range is used only when the source
text has no usable addressable id.

## Deterministic Output

Runtime, not the model, renders the final Markdown. Output uses compact source
labels such as `M#agent-corrections-writing`, `S#two-side-review`, or
`MF#auth-api`, without filesystem paths or line numbers.

Content preserves source order rather than model selection order:

- local graph content follows canonical tree traversal;
- linked resources follow the order of their owning headings in the local
  graph, then original order inside each source;
- recent candidates preserve their submitted order after settled graph and
  linked-resource results.

Repeated ancestors, Focus entries, source labels, and overlapping content are
deduplicated while retaining enough repeated structural context for each
rendered section to remain understandable.

## Session Omission And CLI Override

By default, retrieve omits content already returned in the same RightMemory
session. Runtime tracks resolved delivery coverage rather than asking the model
to infer it from prior prose:

- local graph item ids and the authoritative content version delivered;
- source-scoped MF# item ids;
- delivered source intervals for free-form ranges;
- complete delivered S# source versions;
- recent candidate composite ids.

Changed content is not suppressed merely because it retains the same id. The
delivery identity includes enough source-version information to allow a revised
item to be returned.

Add a per-call CLI option:

```text
rightmemory retrieve --include-returned --session <id> "<query>"
```

The option bypasses omission for that call only. It does not clear stored
delivery state. Returned content is still recorded normally.

Existing retrieve-session state that predates structured coverage loads with
empty coverage. RightMemory preserves its prior query/answer history but does
not attempt to infer selections from old model prose, so an old session may
return an item once before structured omission takes over.

## Runtime-Mode Adapters

The selection contract and renderer are shared across runtime modes.

### Standalone

Standalone exposes the contract as a native terminal model tool. Progressive
read tools may be called multiple times, but a successful retrieve turn ends
with exactly one selection call. A free-text final answer is not accepted.

### CLI-Agent

CLI-agent cannot receive RightMemory's in-process Python tool. Its canonical
prompt requires the same contract as a strict JSON final response. RightMemory
parses that response into the shared selection type and rejects surrounding
prose or unknown fields.

This is a transport difference only. Resolution, omission, ordering, rendering,
and caller-visible output are identical in both modes.

## Validation And Retry

Selection validation rejects:

- unknown or malformed local ids;
- a source marker that does not match the resolved heading kind;
- unknown MF# source-scoped ids;
- ranges on S# or MQ# sources;
- ranges with invalid or stale bounds;
- recent candidate ids not present in supplied volatile context;
- additional model-authored fields or prose;
- selections whose fully rendered output exceeds the configured output-size
  safety limit.

Validation failures are returned to the model through a bounded retry path.
The model may inspect additional context and submit a replacement selection.
Oversized output is never silently truncated; the model must narrow its
selection. Exhausted retries fail the retrieve turn without advancing delivery
cursors.

## Prompt Changes

The retrieve prompt should retain semantic relevance judgment and progressive
disclosure rules but remove instructions that ask the model to compose the
caller-facing answer. It should state:

- use ids for addressable local graph items;
- use source-scoped ids for addressable MF# view items;
- use line ranges only for unaddressable free-form source text;
- expand S# only as a whole skill;
- do not automatically follow graph edges or call MQ# providers;
- finish with exactly one terminal selection;
- select all strongly relevant content without a fixed item-count quota.

The output-size safety limit is enforced after resolution. There is no hard cap
on the number of selected ids.

## Testing

Focused tests should cover:

- standalone exposes one terminal selector alongside the typed progressive
  read tools;
- CLI-agent accepts only strict JSON matching the same selection schema;
- both runtime modes render byte-identical output for the same selection;
- heading selection includes ancestor path, body, and complete logical subtree;
- F# selection traverses Memory and Pursuit detail files correctly;
- node selection includes only its ancestor path and node;
- selected Pursuits include matching Focus entries;
- graph edges do not expand unselected targets;
- M# ranges include enclosing headings and remove line-number prefixes;
- S# expansion returns the complete skill and rejects ranges;
- MF# selects canonical view content by source-scoped id or line range and does
  not expose package metadata;
- MQ# produces only deterministic local availability context;
- recent candidates resolve by composite id and remain labeled as unsettled;
- overlapping selections merge without duplicate content;
- final output preserves deterministic source order and compact provenance;
- previously delivered unchanged content is omitted by default;
- revised content with the same id can be returned;
- `--include-returned` bypasses omission for one call without clearing state;
- malformed, stale, or oversized selections retry without advancing delivery
  state;
- empty selection returns exactly `no strong match`;
- old retrieve-session state loads safely with empty structured coverage;
- existing daily snapshot, diff, recent-candidate, and MF# pull behavior remains
  intact;
- `python -m compileall -q rightmemory tests` passes;
- `python -m unittest discover -s tests` passes.

## Compatibility And Upgrade Impact

The feature changes retrieve output and retrieve-session runtime state, but it
does not rewrite Memory, Pursuit, linked-resource files, or user-authored
corrections. Structured delivery coverage is runtime-only and defaults safely
when absent.

The current read_mf behavior exposes a whole import package; implementation of
this design intentionally narrows it to the canonical mirrored view content.
Tests and prompts that expect package metadata in model context must be updated.
No semantic upgrade note is required because stored Memory and Pursuit schema
meaning does not change.

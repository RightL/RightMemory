# Canonical Document Index and Schema-Valid MF Views Specification

## Status

This document specifies the implementation group that should follow the safe
and recoverable automatic-writes work and the safe existing-root install and
staged-sync work.

The group contains two connected changes:

1. RightMemory gets one canonical parser and in-memory document index for its
   Memory and Pursuit documents. Validation, graph-aware tools, retrieval, sync
   validation, and shared-view rendering consume that same result instead of
   rebuilding document structure independently.
2. The canonical `dist/MEMORY.md` published by an `MF#` file view becomes a
   schema-valid, self-contained RightMemory Memory document. Its graph is
   parsed by the same canonical parser in an MF-local namespace. It may use
   ordinary headings and nodes plus `F#`, `M#`, and `S#` resources whose
   required backing files are included in the package.

Implementation should begin from the branch containing the two preceding
safety groups. The implementing agent must inspect their final interfaces and
adapt names as necessary, but must preserve the behavior and invariants in this
document. This specification is self-contained and must not depend on
untracked implementation notes being present.

## Summary

The design has one central rule:

> RightMemory syntax is interpreted once, and every consumer uses that same
> interpretation.

Local Memory and Pursuit remain one global graph. Each `MF#` import is a
separate, read-only Memory graph with its own ID namespace. The local `MF#`
heading remains the connection point in the consumer graph; the mirrored graph
does not merge its IDs into the consumer's global namespace.

An MF file view is no longer arbitrary Markdown stored in a file named
`MEMORY.md`. The provider must publish a valid Memory document and all backing
files required by its typed headings. Providers validate before publishing;
consumers validate a downloaded candidate before replacing a previously valid
import; retrieval fails closed if no valid import is available.

No database, parser cache, background indexer, generalized schema registry, or
second persistent graph is introduced. The index is rebuilt in memory from the
authoritative files for each operation that needs it.

## Goals

- Make `rightmemory/graph.py` the only owner of RightMemory document grammar.
- Represent physical document hierarchy, logical F# expansion, source spans,
  source order, and content hashes in one in-memory result.
- Remove the graph and F# hierarchy reconstruction currently implemented in
  `rightmemory/retrieve_selection.py`.
- Remove schema validation logic from `MemoryTools` that duplicates document
  parsing, while retaining tool-specific raw Markdown operations where they do
  not interpret RightMemory schema.
- Ensure validation, retrieval, shared-view extraction, and sync agree on the
  same IDs, hierarchy, backing files, edges, and diagnostics.
- Make every published or imported MF canonical document schema-valid before
  it becomes available to retrieval.
- Keep MF IDs scoped to their view so two providers may use the same ID without
  colliding with each other or with local memory.
- Support ordinary `#`, `F#`, `M#`, and `S#` content inside an MF document.
- Preserve progressive disclosure for F#, free-form M# evidence, and complete
  S# instructions inside an MF package.
- Keep invalid downloaded packages from replacing a previously valid import.
- Preserve deterministic, source-authored retrieve output.

## Non-Goals

- Do not change the Memory-versus-Pursuit lifecycle model.
- Do not change local marker meanings, edge types, Focus behavior, or Pursuit
  fields except where the canonical parser starts enforcing an already-written
  schema rule consistently.
- Do not redesign local map IDs or add linked-source syntax.
- Do not replace model-based relevance judgment.
- Do not introduce opaque retrieval handles in this group.
- Do not redesign session-level duplicate suppression beyond adapting its keys
  to canonical index hashes.
- Do not centralize every filesystem path or role permission in a new
  `StateLayout` or `RolePolicy` framework. Those may be considered later if
  meaningful duplication remains after the safety groups land.
- Do not make MF IDs globally visible to local graph edges.
- Do not allow an MF document to import another live `MF#` or `MQ#` connection.
- Do not add a Pursuit document to an MF package in this version. MF remains a
  mirrored Memory-document surface.
- Do not execute or install an imported S# instruction automatically. It is
  retrievable source content until a caller explicitly chooses to use it.
- Do not automatically migrate or rewrite user-authored local Memory files.
- Do not preserve arbitrary free-form MF `dist/MEMORY.md` packages as valid
  version-two packages.

## Terminology

- **Canonical document parser**: the code in `rightmemory/graph.py` that reads
  RightMemory Markdown and produces the in-memory document index and
  diagnostics.
- **Document index**: the complete in-memory result for one local root or one
  MF package, including files, blocks, addressable items, hierarchy, backing
  references, source spans, hashes, and validation errors.
- **Physical hierarchy**: headings, nodes, and body text as they physically
  appear in one Markdown file.
- **Logical hierarchy**: the graph tree after an F# backing document is attached
  beneath its owning F# heading.
- **Local graph**: the combined Memory and Pursuit graph rooted at `MEMORY.md`
  and `PURSUITS.md`, with one globally unique local ID namespace.
- **MF graph**: the memory-only graph rooted at an imported or provider-owned
  `dist/MEMORY.md`, with an ID namespace scoped to one MF view ID.
- **Direct MF document**: `dist/MEMORY.md`, the canonical document directly
  exposed through an `MF#` connection.
- **MF backing file**: a file under `dist/` referenced by an F#, M#, or S#
  heading in the direct MF document or an F# detail document.
- **Qualified MF source**: a terminal selection source identifying a linked
  M# or S# resource inside one MF namespace, such as
  `MF#auth-api/M#incident-evidence`.
- **Package candidate**: provider output awaiting publication or a downloaded
  consumer import awaiting validation and atomic promotion.

## Current Problems

### The local graph is interpreted more than once

`rightmemory/graph.py` currently discovers graph files, IDs, edges, Focus
references, and backing files. It does not retain a full document tree, item
end spans, or the logical F# hierarchy needed by retrieval.

`rightmemory/retrieve_selection.py` therefore implements `_TreeParser` and
`_LogicalGraph`. That code reparses headings, nodes, fences, Focus entries, and
F# detail files to recover hierarchy and source spans. It uses `graph.py` for
some identity information but independently decides the tree shape.

`MemoryTools._structure_errors()` parses headings, fences, terminal references,
and node lines a third time. Other tool helpers independently classify and
locate pieces of the same document.

This is observable, not merely internal duplication. For example, the graph
parser currently accepts a broader node-ID shape than structured retrieval can
represent. A root can therefore validate while a retrieve selection cannot
name one of its IDs. A syntax change can also be added to one parser without
updating the others.

### MF content is currently free-form

The local `{MF#id}` heading is validated as part of the consumer graph, but its
imported `dist/MEMORY.md` is not validated as a RightMemory graph document.

Current provider behavior wraps extracted or generated prose under unanchored
`# <title> Shared View` and `## Published Context` headings. Generative output
is considered valid when the Published Context body merely exists and is
nonempty.

Current consumer behavior checks that a downloaded archive contains required
filenames and then promotes it. Git transport similarly copies a package after
checking only required paths. Neither path validates the mirrored Memory
document, its IDs, its edges, or typed backing files.

Retrieval compensates by reparsing any recognizable items inside the imported
file and allowing arbitrary line ranges for everything else. This behavior
made sense for free-form Markdown, but it contradicts the intended product
meaning of an MF view as a mirrored RightMemory Memory document.

## Required Invariants

### Canonical parser invariants

- All code that decides whether RightMemory syntax is valid uses the canonical
  document parser.
- A graph-aware consumer does not re-match headings, nodes, IDs, markers,
  edges, Focus entries, or F# ownership from raw text.
- One parser invocation reads each participating graph file once and records
  the text needed by downstream rendering.
- The parser is fence-aware. Markdown-looking text inside fenced code blocks is
  never treated as graph structure.
- Addressable heading IDs, node IDs, edge targets, Focus IDs, and structured
  selection IDs use one shared ID grammar.
- Source spans are inclusive, stable line coordinates into the exact text read
  by the parser.
- Logical F# expansion is represented by the index. Retrieval and shared-view
  rendering do not independently splice detail files into a second tree.
- Validation diagnostics identify the source namespace, relative file, and
  line when applicable.
- The document index is ordinary in-memory data. It is not serialized into
  `.runtime/` and is not synchronized.

### Local graph invariants

- `MEMORY.md` and `PURSUITS.md` remain the two required local roots.
- Reachable F# files are graph files; M# and S# files are linked resources and
  are not parsed as graph structure.
- MF# and MQ# headings remain local graph objects whose resolver details live
  outside memory prose.
- Local IDs remain globally unique across Memory and Pursuit.
- Focus references continue to target Pursuit headings in the local namespace.

### MF document invariants

- A version-two MF package contains a regular `dist/MEMORY.md` that passes the
  canonical Memory-document validation profile.
- MF package graph files and backings are regular, non-symlink files under the
  package's `dist/` directory.
- MF IDs are unique within one view and are not compared with local IDs or IDs
  in another MF view.
- Edges inside an MF package target IDs in that same MF namespace. They cannot
  target consumer-local IDs or IDs in another view.
- The local outer MF# heading is the only supported place for relationships
  between imported context and the consumer graph.
- Ordinary headings and nodes plus F#, M#, and S# headings are supported inside
  an MF package.
- MF# and MQ# headings inside an MF package are invalid because a package does
  not carry transitive resolver authority.
- Every F#, M#, and S# heading has the required package-local backing file.
- An MF package contains no `PURSUITS.md`, Focus list, credentials, provider
  runtime state, or resolver registry.
- Provider build, provider publish, HTTP import, Git import, and retrieve-time
  loading all fail closed on invalid MF documents.
- A failed import never replaces the last valid imported package.

## Part One: Canonical Document Index

### Keep one parser owner

Extend `rightmemory/graph.py`; do not add a new parser beside it. Existing
callers may continue using `build_graph_manifest()` while the data it returns
is deepened. A later mechanical rename from `GraphManifest` to `GraphIndex` is
optional and must not be required for this work.

The implementation may split private parsing helpers into a nearby module if
`graph.py` becomes unwieldy, but there must still be one public construction
path and one grammar implementation.

Provide two public builders:

```text
build_graph_manifest(memory_root) -> GraphManifest
build_mf_manifest(package_root, view_id) -> GraphManifest
```

Both call the same internal document parser. Their small profiles define root
documents, family rules, allowed marker kinds, backing paths, and namespace.
Do not build a generic plugin or schema-registration system.

### Required index information

The exact Python names may follow the surrounding code, but the returned index
must make the following information available without reparsing raw Markdown:

- Namespace identity: local or `MF#<view-id>`.
- Every participating graph document's path, exact text, split lines, family,
  and source order.
- Every physical heading, including unanchored grouping headings.
- Every addressable heading and node.
- Heading depth and physical parent/child relationships.
- Each node's containing heading.
- Start and inclusive end line for every heading subtree and addressable item.
- Direct heading body span, distinct from the whole subtree span.
- Marker kind, family, edges, and malformed edge fragments.
- F#, M#, and S# backing references and their owning headings.
- Logical parent/child relationships after F# expansion.
- Focus entries and their source locations for the local profile.
- Deterministic traversal rank in source order.
- A content hash for the exact logical block returned when an item is selected.
- Structured diagnostics.

It is acceptable to introduce plain data classes such as `ParsedDocument`,
`DocumentHeading`, or `SourceSpan`. Avoid cyclic object graphs and behavior-rich
service objects. Stable keys such as `(relative_path, start_line)` are
sufficient for connecting parents and children.

### One ID grammar

Define one shared item-ID validator in `rightmemory/graph.py`. It must be used
for:

- anchored heading IDs;
- node IDs;
- edge targets;
- Focus references;
- shared-view recipe IDs that select graph items;
- structured retrieve local IDs;
- structured retrieve MF-scoped IDs.

The accepted grammar is the existing short-slug character set:

```text
[A-Za-z0-9_.-]+
```

A backtick node line containing an ID outside that grammar is a validation
error rather than an addressable item that some consumers cannot name. The
diagnostic must show the invalid ID and source location.

Node lines without edges must continue to follow the written schema and include
an empty edge list. A node shaped like an addressable node but missing the edge
suffix is diagnosed rather than silently accepted. This closes the current
parser/schema mismatch.

### Structural parsing

Move these schema decisions into the canonical parser:

- maximum heading depth;
- allowed marker kinds;
- terminal `####` placement and allowed kinds;
- terminal-heading prohibition on child headings and node lines;
- F# prohibition on child graph content left in the owning file;
- Memory-only marker restrictions;
- reserved Pursuit IDs;
- fence handling;
- duplicate IDs;
- duplicate, malformed, self, unknown-type, and dangling edges;
- backing ownership and file safety;
- Focus duplicates, target existence, and target family.

`MemoryTools._structure_errors()` should disappear after its tests have moved to
the canonical parser. Generic Markdown helpers may still parse arbitrary files
for operations such as `outline_file`; they must not be used to decide
RightMemory graph validity.

### Physical and logical hierarchy

The parser first records the physical tree of each file. It then connects each
valid F# heading to the root blocks of its backing file to expose a logical
tree.

The logical connection does not copy or rewrite source text. Every logical item
retains its original physical source span. A caller can therefore render a
coherent logical subtree while preserving exact source-authored Markdown.

The parser must detect recursive backing paths and report a diagnostic. It must
not rely only on duplicate IDs to make recursion impossible.

### Content hashes

The index owns the canonical hash input for addressable selections. The hash is
computed from the exact source-authored logical block that retrieval would
return, including required ancestor or F# content according to the selection
kind. Retrieval-delivery state stores this hash but does not define it.

Hash algorithm choice remains an implementation detail; use the existing
SHA-256 behavior unless there is a concrete reason to change it. Do not persist
the full document index.

### Validation consumer

`MemoryTools.validate_memory()` should become a thin composition:

1. Build the local manifest.
2. Report its diagnostics.
3. Validate `corrections.md` using its separate correction format.

Sync validation continues to call the same high-level validation operation and
therefore automatically receives canonical graph behavior. Sync must not add a
special parser for candidate worktrees.

### Retrieval consumer

Replace `_TreeParser` and `_LogicalGraph` in
`rightmemory/retrieve_selection.py` with canonical-index traversal.

Preserve current local selection behavior:

- selecting a heading returns its logical subtree with minimal ancestor
  context;
- selecting a node returns the node with minimal ancestor headings;
- selecting a Pursuit also returns its matching Focus entry;
- overlapping selections are emitted once in source order;
- F# detail content is reattached logically;
- the model cannot select unknown or duplicate IDs;
- deterministic output and maximum-length checks remain;
- session delivery suppression uses canonical item hashes.

The retrieval renderer remains responsible for deciding what selected context
to display. The document index supplies structure and source spans; it does not
become a presentation layer.

### Graph-aware tools

Typed tools such as `read_detail`, `read_markdown`, `read_skill`, shared-view
recipe selection, and validation should resolve IDs and backing files through
the canonical index.

Raw file reads, grep, Git history, and editing primitives may continue using
filesystem paths. They are not alternate graph parsers merely because they
read Markdown text.

### Shared-view extraction consumer

Extractive MF rendering must select headings, nodes, source files, exclusions,
ancestor context, F# detail content, and backing resources from the canonical
local index. `shared_view_files.py` must not independently infer item boundaries
from line matching.

The renderer produces a candidate package, then validates that package through
`build_mf_manifest()` before replacing provider `dist/` output.

## Part Two: Schema-Valid MF Views

### Product boundary

The local heading:

```md
### Auth API Files {MF#auth-api} → [rel:frontend-login]
```

remains an item in the consumer's local graph. Its body explains why the view
matters locally. `shared_views.toml` continues to hold resolver metadata.

The connection resolves to a provider-published package. Only files under that
package's `dist/` directory form the mirrored Memory surface. Package recipe,
view metadata, transport manifests, credentials, and provider-private data are
not graph or retrieval content.

### Version-two package layout

A file-view package has this shape:

```text
view.md
recipe.toml
rightmemory-shared-view.toml
dist/
  MEMORY.md
  MEMORY_<id>.md
  MEMORY_SKILL_<id>.md
  manifest.toml
```

Only `dist/MEMORY.md` and `dist/manifest.toml` are always required. Detail,
Markdown-evidence, and skill files appear only when referenced.

Allowed graph-resource paths under `dist/` are exactly:

- `MEMORY.md`;
- `MEMORY_<short-slug>.md`;
- `MEMORY_SKILL_<short-slug>.md`.

All are regular non-symlink files. Directories, absolute paths, traversal,
duplicate archive entries, case-folded path collisions, and unexpected files
under `dist/` are rejected. Existing package metadata files outside `dist/`
retain their current allowlist and are never exposed to retrieve.

Set `version = 2` in `rightmemory-shared-view.toml` and
`dist/manifest.toml`. The dist manifest also records:

```toml
version = 2
view_id = "auth-api"
document_kind = "rightmemory-memory"
```

The hub may continue treating packages as opaque archives. Provider and
consumer code enforce the version and document contract.

`recipe.toml` does not need a version bump solely because generated output is
now validated. Existing extractive and generative recipes can regenerate
version-two derived output.

### Direct MF document profile

`dist/MEMORY.md` is parsed as a Memory document, not as a complete local
RightMemory root. It therefore:

- uses the ordinary Memory heading, node, edge, and backing rules;
- does not require `PURSUITS.md`;
- does not allow Focus entries or Pursuit control fields as special syntax;
- resolves F# and M# backing names relative to `dist/`;
- resolves S# backing names relative to `dist/`;
- allows ordinary `{#id}`, `{F#id}`, `{M#id}`, and `{S#id}` headings;
- rejects `{MF#id}` and `{MQ#id}` headings;
- validates all IDs and edges inside one MF-local namespace.

Plain unanchored headings may organize the document, as they can locally, but
they must not carry the only copy of retrievable semantic content. In the MF
profile, nonblank body content must belong to an addressable heading or be
inside an M# backing resource. An unanchored heading may group addressable
descendants, but free-form prose placed only under that unanchored heading is a
validation error. This removes the need for arbitrary direct-document line
ranges.

### Namespace behavior

Treat each MF graph as if all of its IDs were qualified by its owning view ID:

```text
MF#auth-api :: token-expiry
MF#billing-api :: token-expiry
```

The qualifier is a runtime identity and does not need to be written into the
provider's Markdown. The provider continues writing natural local IDs such as
`token-expiry`.

Consequences:

- The same ID may exist in local Memory and any number of MF views.
- The same ID may exist in two different MF views.
- Duplicate IDs inside one MF view are invalid.
- An edge inside `MF#auth-api` may target only another ID inside
  `MF#auth-api`.
- Local graph edges target the outer local heading ID `auth-api`, not imported
  item IDs.
- Imported IDs never enter `build_graph_manifest(local_root).items`.

### Marker behavior inside MF

| Marker | MF behavior |
| --- | --- |
| `{#id}` | Ordinary addressable heading in the MF-local graph. |
| `{F#id}` | Parsed detail subtree in `dist/MEMORY_<id>.md`; included in the MF-local logical graph. |
| `{M#id}` | Free-form Markdown resource in `dist/MEMORY_<id>.md`; not parsed as graph structure. |
| `{S#id}` | Complete instruction resource in `dist/MEMORY_SKILL_<id>.md`; not parsed as graph structure and never auto-executed. |
| `{MF#id}` | Invalid in an MF package; transitive resolver behavior is not defined. |
| `{MQ#id}` | Invalid in an MF package; provider-question authority is not transferable through a file package. |

F# detail files may recursively contain ordinary, F#, M#, and S# headings.
All reachable backing files must be packaged. Unreferenced `MEMORY_*.md` and
`MEMORY_SKILL_*.md` files under `dist/` are invalid rather than silently
published.

### No wrapper prose in the direct document

Stop generating the current `# <title> Shared View` and `## Published Context`
wrapper around arbitrary text.

`view.md` and `recipe.toml` already own the title and durable sharing intent.
`dist/MEMORY.md` should contain only the schema-valid projected Memory
document. The consumer's local MF# heading already owns local relationship
meaning.

For generative views, rename the model-facing tool argument from
`published_context` to `memory_document` or an equally explicit name. The agent
must provide the complete schema-valid body of `dist/MEMORY.md`; the tool
validates it before atomically replacing `dist/`.

### Extractive rendering

An extractive recipe still selects headings, nodes, files, and exclusions. The
renderer uses the canonical local index and produces the smallest coherent
Memory projection:

- Selected headings include their logical subtree.
- Selected nodes include required ancestor headings.
- Overlapping selections are deduplicated in source order.
- A selected F# owner keeps its F# marker and writes the selected logical child
  content to the corresponding `dist/MEMORY_<id>.md`.
- A selected M# or S# owner copies its complete backing resource.
- Selecting a graph file includes its reachable F#, M#, and S# resources.
- Excluded items and their owned subtrees are omitted.
- M# and S# resources are indivisible for extraction. If they contain private
  material, the builder must use generative mode rather than partially copying
  them.

The renderer must not automatically include an omitted edge target because
doing so could publish context the recipe did not authorize. It must not
silently remove an edge because that changes source-authored meaning. If the
projection contains a dangling edge, package validation fails and the builder
must refine the recipe or use generative mode.

Provider `dist/` replacement is atomic: render into a sibling temporary
directory, validate the complete candidate, then rename it into place. On
failure, retain the previous valid `dist/` unchanged.

### Generative rendering

The shared-view-builder prompt must require generative output to follow the
Memory-document profile. The builder may rewrite or sanitize provider context,
but the final document must use addressable headings or nodes, valid edges, and
valid typed resources.

The first implementation may support only ordinary headings and nodes in the
single `memory_document` tool argument. If generative F#, M#, or S# output is
supported, the tool interface must accept each backing file as a separate
explicit argument and validate the full candidate together. It must never
infer hidden backing files from prose or accept dangling typed headings.

The builder tool writes recipe/view source files according to current ownership
rules, constructs `dist/` in a temporary directory, validates it, and promotes
it atomically. A failed generated document does not replace an earlier valid
preview.

### Provider approval and publishing

Approval requires both a valid recipe and a currently valid version-two dist
candidate. Publish re-renders extractive views, validates generative output,
and validates the exported package immediately before upload.

If provider source Memory changed so that an extractive projection becomes
invalid, automatic publish records a failure and leaves the last published
remote version unchanged. It does not publish a broken package and does not
silently widen the recipe.

### Consumer import validation

HTTP and Git imports use the same package validator.

For HTTP:

1. Download the archive.
2. Extract it into the existing temporary import directory with path-safety
   checks.
3. Validate package metadata and version.
4. Build the MF manifest from temporary `dist/`.
5. Reject any manifest diagnostic or unexpected file.
6. Atomically replace the previous import only after validation passes.

For Git:

1. Copy the provider package to the existing temporary import directory.
2. Run the same package and MF-manifest validation.
3. Atomically replace the previous import only after validation passes.

Do not validate in the provider checkout and then copy unchecked files; the
temporary consumer candidate is the exact object that must pass validation.

If validation fails and a previously validated version-two import exists,
return `stale` and continue using it. If no valid import exists, return
`unavailable`. A mere `dist/MEMORY.md` file is no longer enough for
`_import_exists`; the helper must recognize a complete previously validated
version-two package.

### Retrieve selection semantics

The local selection of an outer MF# heading remains unchanged: selecting its
local graph ID returns local relationship context and does not automatically
return imported content.

Selecting imported graph content uses the existing `sources` list with the
top-level MF source:

```json
{
  "source_id": "MF#auth-api",
  "ids": ["token-refresh"],
  "ranges": []
}
```

The canonical MF index resolves IDs across `dist/MEMORY.md` and all reachable
F# detail files. Selecting an MF heading or node follows the same hierarchy
rules as local graph selection, but its identity and delivery hash remain
qualified by `MF#auth-api`.

Direct MF ranges are invalid in version two. All semantic content in the
direct graph must be addressable.

Free-form M# evidence inside an MF package uses a qualified source:

```json
{
  "source_id": "MF#auth-api/M#incident-evidence",
  "ids": [],
  "ranges": [{"start": 12, "end": 24}]
}
```

A complete S# instruction inside an MF package uses:

```json
{
  "source_id": "MF#auth-api/S#review-checklist",
  "ids": [],
  "ranges": []
}
```

Qualified source parsing must verify all of the following:

- the outer local heading exists and uses `MF#`;
- a valid imported package exists for that exact view;
- the nested ID exists in that MF namespace;
- the nested heading uses the marker written in the qualified source;
- M# accepts ranges and rejects `ids`;
- S# accepts neither ranges nor `ids` and returns the complete file.

No deeper nesting is supported. F# is part of the MF graph and does not form a
qualified linked source. MF# and MQ# cannot occur inside the package.

Session delivery keys use qualified identities:

```text
MF#auth-api:item:token-refresh
MF#auth-api:M#incident-evidence:range:12-24:<source-hash>
MF#auth-api:S#review-checklist:<content-hash>
```

The exact stored encoding may differ, but two views with the same inner ID must
never suppress each other's output.

### Model-facing MF reads

Extend the typed MF read surface so the model can inspect the schema-valid
package progressively without general runtime-directory access.

One acceptable compact interface is:

```text
read_mf(mf_id, resource_id=None)
```

Behavior:

- no `resource_id`: return line-numbered `dist/MEMORY.md` and identify the
  available typed backing IDs;
- `F#<id>`: return the line-numbered F# detail document;
- `M#<id>`: return the line-numbered free-form Markdown backing;
- `S#<id>`: return the complete instruction without line-number prefixes;
- unknown, mismatched, unsafe, or unreferenced resources fail closed.

The implementer may retain separate typed tool names if that produces a
clearer model interface, but ordinary raw reads must not expose package
metadata or arbitrary files under the import directory.

### Retrieval failure behavior

Retrieve-time loading performs a defensive canonical parse even though import
already validated the package. If the package was modified locally, is
incomplete, or no longer validates, imported content is unavailable for that
turn. Retrieval may still return the local MF# relationship heading.

Do not fall back to free-form line ranges over an invalid package. Do not expose
validation diagnostics as retrieved memory content. Status and explicit
shared-view commands may report the operator-facing reason.

## Package Compatibility and Existing State

Version-one packages are not interpreted as schema-valid MF graphs. The
feature was still under development, and preserving arbitrary Markdown would
retain the design error this group exists to remove.

Upgrade behavior is:

- Existing provider `recipe.toml` and `view.md` source remain untouched.
- The next provider build, refresh, export, or publish regenerates derived
  `dist/` output as version two.
- A generative recipe must rerun its builder because old arbitrary generated
  prose cannot be certified as schema-valid without model judgment.
- Existing consumer imports are runtime cache. Version-one imports are ignored
  and replaced on the next successful pull.
- If the provider still serves version one, the consumer reports stale only if
  it already has a previously validated version-two import; otherwise it
  reports unavailable.
- Local MF# headings, `shared_views.toml`, credentials, invitations, and share
  relationships do not need rewriting solely for this change.
- No committed user Memory or Pursuit file is silently rewritten.

Update the canonical schema and shared-view usage documentation so future
agents do not recreate the free-form interpretation. Add a semantic-upgrade
note only if the final implementation changes user-authored Memory organization
or interpretation beyond the MF package boundary; derived MF cache invalidation
alone does not require Dreamer to rewrite local Memory.

## File-by-File Implementation Guide

### `rightmemory/graph.py`

- Add shared item-ID validation.
- Retain exact document text and source lines.
- Represent physical heading hierarchy and item spans.
- Represent logical F# hierarchy and traversal order.
- Move terminal-heading and remaining structural diagnostics here.
- Add the MF Memory-document parse profile.
- Expose `build_mf_manifest()`.
- Keep local and MF namespaces separate.

### `rightmemory/tools.py`

- Delete `_structure_errors()` after equivalent canonical diagnostics pass.
- Make typed local backing reads resolve through the manifest.
- Extend typed MF reads for F#, M#, and S# package resources.
- Update generative file-view tool input from arbitrary Published Context prose
  to a complete Memory document.
- Keep generic raw Markdown and Git tools independent where they do not
  interpret graph syntax.

### `rightmemory/retrieve_selection.py`

- Delete `_TreeParser` and `_LogicalGraph`.
- Render local and MF graph selections from canonical index hierarchy/spans.
- Remove direct MF line-range selection.
- Parse qualified MF M#/S# source IDs.
- Qualify MF delivery keys and hashes.
- Preserve current M#, S#, local graph, Pursuit, MQ#, and recent-candidate
  behavior outside the explicitly changed MF contract.

### `rightmemory/shared_view_files.py`

- Render schema-valid extractive package candidates from the canonical index.
- Copy reachable F#, M#, and S# resources into `dist/`.
- Replace arbitrary wrapper rendering with direct Memory-document output.
- Validate generative candidates before promotion.
- Write and enforce version-two package manifests.
- Validate the exact temporary HTTP import before atomic replacement.
- Treat only validated version-two imports as usable stale fallback.

### `rightmemory/git_share_transport.py`

- Use the common package validator on the exact temporary Git import.
- Support safe package-local backing files.
- Reject version-one, unexpected, unsafe, symlinked, or invalid graph content
  before replacing the current import.

### `rightmemory/shared_view_builder.py`

- Require valid package output after both initial build and semantic refresh.
- Restore prior source and prior valid `dist/` when a builder or validation
  failure occurs.
- Preserve approval and publish settings under current rules.

### `rightmemory/prompts/shared-view-builder.md`

- Teach generative builders to produce a complete schema-valid Memory document.
- Explain that ordinary/F#/M#/S# are allowed and MF#/MQ# are forbidden inside
  the package.
- Require the compiler tool to report success before the role finishes.
- Remove the instruction to produce only a `## Published Context` body.

### `rightmemory/prompts/retrieve.md` and prompt assembly

- State that direct MF content is selected only through source-scoped IDs.
- Explain qualified MF M#/S# sources.
- Remove direct MF range guidance.
- Keep local M# range and complete S# behavior unchanged.

### `skills/rightmemory-schema.md`

- Replace the statement that MF linked content is not parsed as graph structure
  with the version-two MF document rule.
- Document the MF-local namespace and permitted inner marker kinds.
- Document that nested MF#/MQ# connections are forbidden.
- Keep local MF# heading and resolver behavior unchanged.

### `docs/shared-views-usage.md`

- Describe the schema-valid package layout.
- Explain provider and consumer validation.
- Replace direct MF range examples with scoped ID examples.
- Document version-one regeneration and cache replacement behavior.

### Installed skill text

- Update checked-in retriever and orchestrator skill text that currently says
  MF uses source-scoped IDs or ranges.
- Preserve generated/canonical prompt ownership: edit canonical role prompts
  first and keep installed skills as thin command guidance.

## Test Matrix

### Canonical local parsing

- Existing valid Memory/Pursuit fixtures produce the same item IDs, edges,
  backing references, and Focus results.
- Every item reports correct physical start/end span and source order.
- Heading direct-body and whole-subtree spans are distinct.
- F# logical children attach under the owning heading while retaining backing
  file spans.
- Nested F# details work and cycles fail with a source diagnostic.
- Fenced headings, nodes, and edges remain plain text.
- Terminal `####` rules are enforced by the canonical parser.
- Invalid node ID characters fail validation and structured selection uses the
  same ID validator.
- A node without `→ []` or another edge list fails consistently.
- Duplicate IDs and malformed, duplicate, self, unknown, or dangling edges
  preserve useful diagnostics.
- Local M#/S# resources remain non-graph files.
- No graph-aware test needs `MemoryTools._structure_errors()`.

### Local retrieval parity

- Golden local selection fixtures render byte-for-byte equivalent Markdown
  before and after the parser migration.
- Heading, node, overlapping, F#, Focus, M#, S#, MQ#, and recent-candidate
  behavior remains intact.
- Delivery suppression uses canonical hashes and `--include-returned` still
  overrides suppression.
- Unknown and duplicate IDs fail closed.
- Output limits remain enforced.
- Tests assert `_TreeParser` and `_LogicalGraph` no longer exist.

### Valid MF documents

- A simple addressable `dist/MEMORY.md` validates.
- The same inner ID may exist locally and in two MF packages.
- Duplicate IDs inside one MF package fail.
- Edges resolve only within the same MF namespace.
- Ordinary headings/nodes and F#/M#/S# resources validate with their expected
  backing files.
- Nested F# files are included in the logical graph.
- Missing, unreferenced, symlinked, escaped, or case-colliding backing files
  fail.
- MF#/MQ# headings inside the package fail.
- Pursuit/Focus files are rejected from the MF package profile.
- Unaddressable direct-document semantic prose fails.
- Invalid terminal headings, node syntax, IDs, and edges fail identically to
  local Memory.

### Provider rendering

- Extractive heading and node selections produce a valid version-two package.
- Overlapping extraction is deduplicated.
- F# detail files and reachable M#/S# resources are copied correctly.
- Exclusions that create dangling edges fail rather than widening or rewriting
  the projection.
- A failed render or validation keeps the previous valid `dist/` byte-for-byte.
- Generative ordinary graph output succeeds.
- Arbitrary prose, missing node edge lists, dangling markers, and invalid
  generated IDs fail.
- Approval and publish refuse invalid or version-one dist output.
- Automatic publish failure leaves the last remote package unchanged.

### Consumer import

- A valid HTTP version-two package replaces the import atomically.
- A valid Git version-two package uses the same validator and replacement
  behavior.
- Invalid metadata, version, graph syntax, edge, backing, path, or symlink
  leaves the previous valid import unchanged and reports stale.
- The same invalid package reports unavailable when no valid version-two import
  exists.
- A version-one import is not considered valid stale fallback.
- Package metadata and recipe files remain unreadable to retrieve.
- Local modification of an imported package causes retrieve-time fail-closed
  behavior.

### MF retrieval

- `MF#view` plus an ID selects addressable content from the direct document.
- IDs inside F# details are selectable in the same MF namespace.
- Direct MF line ranges are rejected.
- Qualified MF M# ranges return exact free-form source text.
- Qualified MF S# selection returns the complete instruction.
- Marker mismatch, unknown nested ID, unsafe backing, or deeper qualification
  fails closed.
- Selecting the outer local MF heading alone does not expand imported content.
- Identical inner IDs in different views have independent delivery coverage.
- Imported S# content is returned as source text and is not installed or
  executed.

### Full integration

- Retrieve silently pulls a valid current package and selects from its MF-local
  index.
- Pull failure retains and uses the last valid version-two package.
- Invalid new provider output cannot replace a valid consumer import.
- Sync validation and isolated semantic-write validation use the canonical
  local parser after the migration.
- Full install and prompt tests reflect the new MF rules without pinning exact
  prose.

## Suggested Implementation Order

### Task One: Lock the behavior with tests

- Add canonical span/hierarchy tests around current `GraphManifest` behavior.
- Add failing consistency tests for node-ID grammar and missing node edge lists.
- Add failing MF tests proving arbitrary Markdown is currently accepted and
  must be rejected.
- Add retrieval parity fixtures before deleting the shadow parser.

### Task Two: Deepen the canonical parser

- Store document text, blocks, spans, physical hierarchy, and source order.
- Move structural diagnostics from tools.
- Add logical F# connections and canonical content hashes.
- Keep existing public local manifest calls working.

### Task Three: Migrate local consumers

- Make validation use only manifest diagnostics.
- Migrate typed local reads and shared-view recipe selection.
- Replace local `_TreeParser`/`_LogicalGraph` retrieval with index traversal.
- Run local parity tests before changing MF behavior.

### Task Four: Add the MF parse profile and package validator

- Implement `build_mf_manifest()`.
- Implement allowed package paths and version-two manifests.
- Add ordinary/F#/M#/S# validation and namespace tests.
- Share one provider/consumer package validator.

### Task Five: Produce valid provider packages

- Refactor extractive projection around canonical spans and logical hierarchy.
- Package reachable backing files.
- Change generative tools and prompts to accept full Memory documents.
- Validate and atomically promote provider candidates.

### Task Six: Validate imports

- Validate HTTP temporary imports before rename.
- Validate Git temporary imports through the same function.
- Tighten stale-import recognition to validated version two.
- Add package-version and unsafe-path tests.

### Task Seven: Migrate MF retrieval

- Parse imported MF packages through the canonical index.
- Remove direct MF ranges.
- Add qualified M#/S# source selection and typed reads.
- Qualify delivery coverage.
- Delete the remaining MF shadow tree parsing.

### Task Eight: Documentation and full verification

- Update schema, prompts, installed skill guidance, and shared-view usage docs.
- Remove obsolete tests that pin free-form MF behavior or duplicate parser
  internals.
- Run focused tests, the complete suite, compile checks, and installer checks.
- Confirm no generated `dist/`, runtime import, or unrelated untracked file is
  committed.

## Verification Commands

Run from the implementation worktree:

```bash
python -m unittest tests.test_graph
python -m unittest tests.test_tools
python -m unittest tests.test_retrieve_selection
python -m unittest tests.test_shared_views
python -m unittest tests.test_git_share_transport
python -m unittest tests.test_sync
python -m unittest tests.test_config
python -m unittest tests.test_cli
python -m unittest discover -s tests
python -m compileall -q rightmemory tests
```

Use the project's required `rtk` prefix when executing these commands through
the agent shell.

If the final implementation changes installed files or prompts, also run the
focused installer tests for both standalone and CLI-agent modes. Do not assume
the known Windows source-checkout watcher failure is related to this group;
report it separately if it remains the only failure.

## Completion Criteria

This group is complete only when all of the following are true:

- RightMemory schema interpretation has one implementation owner.
- `retrieve_selection.py` no longer contains a second graph/F# tree parser.
- Tool validation no longer reparses graph structure independently.
- Local validation and retrieval accept exactly the same ID grammar.
- Current local retrieval behavior passes parity tests.
- Every new provider MF package contains a valid version-two Memory document.
- F#, M#, and S# inside MF packages work with package-local backing files.
- MF#/MQ# nesting is rejected.
- HTTP and Git consumers validate the exact temporary package before promotion.
- Invalid imports preserve the last valid version-two cache.
- Direct MF ranges are gone; qualified MF M# ranges and complete S# selection
  work.
- MF delivery suppression is namespace-safe.
- Schema, prompts, installed skill guidance, and user documentation describe
  the same MF contract.
- The full verification results are recorded, including any unrelated known
  environment-specific failure.

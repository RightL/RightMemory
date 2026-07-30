# RightMemory Schema

## Stored Model

RightMemory is one addressable graph organized into two Markdown document trees. `MEMORY.md` stores durable context; `PURSUITS.md` stores live intent and continuity. Their headings and nodes share one globally unique id namespace, and typed edges may cross between the trees. Item ids use the short-slug grammar `[A-Za-z0-9_.-]+`.

The local graph begins at those two roots and recursively follows F# detail references. Filename globs do not define graph membership. One canonical parser builds the in-memory document index used by validation, retrieval, graph-aware tools, sync, and shared-view extraction. M# and S# are Memory-only linked-resource forms whose backing content is not parsed as local graph structure. MF# and MQ# are Memory-only connection headings; an imported MF# version-two package is parsed separately as a schema-valid Memory graph in its own view-local namespace.

## Addressable Items

Each graph item is either an addressable heading or a node.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
### Markdown Evidence {M#heading-id} → [edge1, edge2, ...]
### Skill Title {S#heading-id} → [edge1, edge2, ...]
### Mirrored File View {MF#heading-id} → [edge1, edge2, ...]
### Provider Question View {MQ#heading-id} → [edge1, edge2, ...]
```

Plain tree headings without graph edges may omit the anchor and edge list.

Addressable `#`, `##`, and `###` headings may have body paragraphs directly under the heading. Those paragraphs describe the heading node itself, even when the heading also has a sibling detail file. Use a heading body when text explains the whole heading concept; use child nodes when text should stand as its own memory under that concept.

Nodes use:

```md
- `<node-id>` <free-form description> → [edge1, edge2, ...]
```

- `heading-id` and `node-id` share one namespace across Memory and Pursuit. Do not reuse an id anywhere in the parsed graph.
- `F#` marks a heading whose child graph content lives in a sibling detail file. Memory F# maps to `MEMORY_<heading-id>.md`; Pursuit F# maps to `PURSUIT_<heading-id>.md`. The graph id remains `heading-id`.
- `M#` marks a Memory heading backed by free-form Markdown evidence in `MEMORY_<heading-id>.md`. The backing file is addressable through its heading but is not parsed as graph content.
- `S#` marks a Memory heading backed by reusable instruction in `MEMORY_SKILL_<heading-id>.md`. The backing file is addressable through its heading but is not parsed as graph content.
- `MF#` marks a mirrored file shared-view connection. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:MF#heading-id`. The heading body records the local relationship and collaboration meaning; HTTP resolver details live outside memory prose.
- `MQ#` marks a provider question shared-view connection. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:MQ#heading-id`. The heading body records when provider-side questions may help; prompts, HTTP resolver details, and credentials live outside memory prose.
- Edges may connect heading to heading, heading to node, node to heading, or node to node.
- Node lines with no edges write `→ []`; heading lines with no edges may omit `→ []`.
- Useful but unsettled memory uses `Uncertain:` at the start of the node description, for example ``- `<node-id>` Uncertain: <tentative memory claim with its scope or doubt> → [...]``. Revise it into ordinary declarative memory when it becomes settled, or remove it when it is contradicted or no longer useful.

Focus entries in `PURSUITS.md` are ordered references to Pursuit heading ids, not graph nodes. They use ``- `<pursuit-id>` ...`` under `## Focus`; the referenced id must name an addressable Pursuit heading. The ``- `do|ask|wait` ...`` bullets inside a Pursuit's `**Next:**` block are ordered control entries, not graph nodes.

## Reading Context And Referential Clarity

An item's local reading context is the item itself together with the titles and
bodies of its logical ancestor headings. The logical ancestor chain continues
across F# detail-file boundaries. Ancestor headings may establish shared
subject, scope, viewpoint, and reference points for descendants; descendants
need not restate that context.

A reference that materially affects interpreting or applying an item is clear
when its intended referent is determined by that local reading context or is
explicitly identified in the item, including by a graph id or typed edge. Typed
edges express graph relationships; they do not make their targets' prose part
of the item's ancestor context.

Context supplied only by the current reader or executing agent is not stored
context. Perspective-dependent language and relative references are valid only
when stored context uniquely establishes their viewpoint or base.

Items are judged within their local reading context, not as globally
self-contained fragments. Eliminating all repeated wording is not a schema
goal; repetition that preserves necessary scope or independent meaning is not
duplication.

## Memory Quality

Good Memory is scoped, durable context that helps future agents act, decide, interpret, or retrieve. Live intent, current task state, and next actions belong in Pursuit and follow the package-owned Pursuit rules supplied by the runtime.

Project artifacts are the primary home of project-specific details and guidance.
Compact, durably useful context is also good Memory when recovering it from
project artifacts would require substantial search or reasoning.
A compact, stable lookup rule for a recurring family of project artifacts is
good Memory; the artifact details remain in the project.

### Item Quality

A good memory item states one reusable fact, preference, rule, decision,
conclusion, or unresolved question at the right scope.

Clearly not good memory:

- Raw chat transcripts.
- Turn-by-turn session narration.
- Completed-task progress notes.
- Raw experiment tables or parameter dumps.
- Generated artifact inventories.
- Git log or commit-by-commit history.
- Copied project documentation or source content.
- Repeated examples that do not change the rule or conclusion.
- Live task state preserved only because work is incomplete.

### Structure Quality

Good memory structure makes domain, topic, and scope obvious.

Clearly not good structure:

- Duplicate memory.
- Fake hub nodes.
- Overloaded headings that mix unrelated topics or scopes.
- Child nodes that only restate their parent heading.
- Detail that hides the main reusable point.

### Graph Quality

Good graph edges express useful semantic relationships beyond tree placement.

Clearly not good graph structure:

- Edges that only repeat parent-child containment.
- `rel:` edges where a specific schema edge type clearly applies.

### Behavior Memory Quality

Good behavior memory is scoped operating guidance for future agents.

Clearly not good behavior memory:

- Transcript review notes.
- A growing incident history.
- Frustration context preserved as the memory itself.
- Project-specific workflow stored as a global default.
- Examples repeated after the rule is already clear.

## Agent Correction Memory

Agent Correction Memory is a fixed Memory module consisting of the M# collections `agent-corrections-writing` and `agent-corrections-design`. The package-owned Agent Correction Memory rules supplied by the runtime define their semantics.

## Memory Skills

A memory skill is a reusable instruction asset. It can be a workflow, a judgment
playbook, a prompt-shaped instruction the user would otherwise repeat, or a
bounded operating style for a recurring situation.

Ordinary memory records durable facts, context, and preferences. Skill memory
tells a future agent how to act when the relevant situation comes up.

Use an `S#` heading when the heading body can give a compact description and the
full instruction belongs in `MEMORY_SKILL_<slug>.md`. The skill file should be
clear enough for an agent to apply after reading it, but it should not follow a
rigid section template.

Skill files are free-form instruction Markdown, not graph-bearing memory files.
Put graph ids, edges, and placement context on the `S#` heading or nearby
ordinary memory. Inside `MEMORY_SKILL_<slug>.md`, Markdown headings, bullets, and
backticked labels are treated as instructional text rather than memory nodes.

## Shared Views

Shared views connect a local memory root to collaboration context owned by another person, project, team space, or agent root. Use an `MF#` heading when this root should use provider-published mirrored files as external read context. Use an `MQ#` heading when this root may ask a provider-side retriever a synchronous question. In both cases, the heading body explains the relationship in local terms: what the view represents, when it helps, and how it relates to nearby work. Store resolver details in `shared_views.toml`, not in memory prose.

A published version-two file-view package has this layout:

```text
shared_views/<view-id>/
  view.md
  recipe.toml
  rightmemory-shared-view.toml
  dist/
    MEMORY.md
    MEMORY_<id>.md
    MEMORY_SKILL_<id>.md
    manifest.toml
```

Every included graph item must be interpretable from local reading context
contained in the package. Provider-only ancestry and resolver state outside
the package are not implicit consumer context.

`view.md` and `recipe.toml` are provider-owned file-view source files. The recipe records the approved source headings, nodes, or files chosen by the builder. The generated `dist/` directory is preview or publishing output; it is not active provider memory. A version-two `dist/MEMORY.md` is a schema-valid, Memory-only RightMemory document, not arbitrary Markdown. It has an id namespace local to that view, so its ids do not collide with local ids or ids in another view, and its edges remain inside that namespace.

An MF document may use ordinary headings and nodes plus F#, M#, and S# headings. Its F# and M# files live at `dist/MEMORY_<id>.md`; its S# files live at `dist/MEMORY_SKILL_<id>.md`. F# content participates in the MF graph, M# remains free-form evidence, and S# remains a complete instruction resource that is never installed or executed automatically. Every typed heading needs its package-local backing, and unreferenced backing files are invalid. Plain headings may group addressable descendants, but arbitrary prose under an unaddressable wrapper is invalid. Nested MF# and MQ# headings are invalid because an imported package has no authority to resolve another live connection.

Selecting the local outer MF# heading returns its local relationship context only. Imported graph items are selected by id within the `MF#<view-id>` source, including items reached through F#. Direct ranges over `dist/MEMORY.md` are invalid. Imported M# ranges use a qualified source such as `MF#auth-api/M#incident-evidence`; imported S# uses a qualified source such as `MF#auth-api/S#review-checklist` and returns the complete instruction.

A provider root may define question views under `shared_views/<view-id>/`:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  question.toml
```

`retriever.md` belongs only to provider question views. It is the provider-side retrieval prompt used when an accepted consumer asks an `MQ#` question.

Consumers pull accepted `MF#` file views into `.runtime/shared_views/imports/<view-id>/` before retrieve runs, then inspect the validated graph and backing files through typed MF reads and structured selection. Consumers call `MQ#` question views through explicit ask commands or UI actions outside retrieve. Record local consequences in ordinary memory only when those consequences become durable.

Providers validate the complete version-two package before approval or publication. Consumers validate the exact downloaded candidate before atomically replacing a prior import. Invalid or version-one packages are unavailable unless a previously validated version-two import can remain as stale context.

## Memory Domains

Memory domains are ordinary headings. Use `# User Context` for durable context about the user as a person with ongoing life, work, and direction: relevant background, longer-term direction, important responsibilities or circumstances, and why those things matter. It is a compact context profile grounded in evidence. A commitment that still needs continuation belongs in Pursuit even when it also reflects a longer-term direction.

Use `Cross-Session Agent Behavior` for future-facing guidance about how agents should work with this user across coding sessions. This includes both broadly applicable user-level guidance and project-scoped guidance that should persist across sessions for a specific repository or codebase, such as communication style, workflow expectations, tool or process preferences, and repeated agent mistakes that future agents can avoid. Express narrower scope through heading nesting or an explicit scope in the heading/local context. These entries should read like operating instructions, not transcript review notes.

Use global `# Open Context Questions` for loose ends in memory. These are short questions for future agents, not declarative memory facts. Use them when a memory area feels incomplete, unclear, or hard to apply because something still needs to be pinned down. Question nodes use normal node syntax and link to related memory with `todo:`. When the answer becomes clear, write the answer into the appropriate declarative memory section, then remove or revise the question.

When durable context could fit either domain, place it by what it is about. Facts that describe the user's background, direction, motivations, responsibilities, or circumstances belong under user context. Guidance that tells an agent how to respond, decide, use tools, write, or collaborate belongs under agent behavior. Live commitments and their current movement belong in Pursuit rather than either Memory domain.

## Edge Types

- `dep:` A depends on B.
- `emb:` A embeds a copy of B.
- `bak:` A is a backup or snapshot of B.
- `agg:` A aggregates B's outputs.
- `ver:` A verifies or tests B.
- `ext:` A extends or enhances B.
- `up:` A is upstream of B.
- `rel:` general relation; use only when no specific type fits.
- `loc:` A is located inside B.
- `run:` A runs or launches through B.
- `cfg:` A uses B as configuration.
- `out:` A outputs B.
- `in:` A consumes B as input.
- `doc:` A documents B.
- `todo:` A is a todo or blocker for B.

Written edges may be one-way or reciprocal [stored on both records so either side visibly points to the other]. The agent should choose based on whether the reverse edge improves future retrieval or understanding without making the relationship misleading.

## Heading Rules

- `#`, `##`, and `###` are normal tree layers and may contain Memory or Pursuit content.
- `#`, `##`, and `###` may have `{#short-slug}` anchors and edges when the whole subtree is a graph target.
- A file-backed `#`, `##`, or `###` heading uses `{F#short-slug}`. It maps to `MEMORY_<short-slug>.md` in the Memory tree and `PURSUIT_<short-slug>.md` in the Pursuit tree.
- A Markdown-evidence `#`, `##`, or `###` heading uses `{M#short-slug}` and maps to free-form `MEMORY_<short-slug>.md`. M# is valid only in Memory.
- A skill-backed `#`, `##`, or `###` heading uses `{S#short-slug}` and maps to sibling skill file `MEMORY_SKILL_<short-slug>.md`.
- A mirrored file shared-view `#`, `##`, or `###` heading uses `{MF#short-slug}` and points to an external file view through an out-of-band resolver entry.
- A provider question shared-view `#`, `##`, or `###` heading uses `{MQ#short-slug}` and points to an external question view through an out-of-band resolver entry.
- When a heading's child content moves into its detail file, keep only the heading line and any heading body paragraphs in the current file. Do not leave child node lines or child headings under that heading in the current file.
- `####` is the deepest heading level allowed in a graph file and is a terminal reference heading under an existing `###` topic.
- `#### Human Title {F#short-slug}` points to the containing tree's F# detail file.
- `#### Human Title {M#short-slug}` points to free-form Memory evidence in `MEMORY_<short-slug>.md`.
- `#### Human Title {S#short-slug}` points to sibling skill file `MEMORY_SKILL_<short-slug>.md`.
- `#### Human Title {MF#short-slug}` points to a mirrored file shared-view connection.
- `#### Human Title {MQ#short-slug}` points to a provider question shared-view connection.
- A `####` terminal reference may have body paragraphs directly under it when they summarize or explain the reference.
- Do not write child node lines or child headings under a `####` terminal reference in the current file.
- Create `####` terminal references only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
- Detail files use the same schema recursively.

## Placement Rules

- Use F# detail files to keep large headings readable. As a guide, consider moving child graph content into a detail file when a `#`, `##`, or `###` heading has more than about 15 direct node lines, then mark the parent heading with `F#`.
- Count only direct node lines shaped like ``- `<node-id>` ...``; do not count child headings or `####` pointers.
- Tree nesting expresses reading context and containment; do not add child-to-containing-heading edges merely to say a node belongs under a heading.
- Use heading edges when a relation applies to the whole subtree.
- Use node edges when a relation applies only to one fact.
- Do not create a child node merely to summarize an addressable heading. Prefer a heading body for whole-heading description.
- Put new nodes in the closest existing `##` or `###` group of the matching `#` domain.
- When a topic has multiple closely related facts, prefer a meaningful anchored `##` or `###` heading over a fake hub node.
- Place information where it keeps the tree/graph clear: update, add, split, merge, or move headings and nodes as needed. Avoid duplicate, fake-hub, or overloaded structure.

## File Set

- The parsed Memory tree begins at `MEMORY.md` and follows Memory F# references into `MEMORY_<id>.md`.
- The parsed Pursuit tree begins at `PURSUITS.md` and follows Pursuit F# references into `PURSUIT_<id>.md`.
- M# files use the same `MEMORY_<id>.md` filename shape as Memory F# detail, but the referring heading determines that the file is free-form and excluded from graph parsing.
- S# files use `MEMORY_SKILL_<id>.md` and are excluded from graph parsing.
- An imported MF# graph begins at that package's `dist/MEMORY.md`, follows only its F# files, and resolves its M# and S# backing resources within the same `dist/` directory.
- Both root files remain useful documents rather than routing-only indexes.

# RightMemory Model And Schema

## Modules

RightMemory has three semantic modules:

- **Memory** stores durable context that should remain useful beyond the current task or session.
- **Pursuit** is a user-owned hierarchical map of ongoing directions, with the context needed to understand or enter them.
- **Agent Corrections** stores bounded, reusable cases in which the user redirected prior agent work.

Memory and Pursuit form one addressable graph organized into two Markdown document trees. Their headings and nodes share one globally unique id namespace, and typed edges may cross between the trees.

Agent Corrections is a non-graph case library. Its two fixed collections are `MEMORY_agent-corrections-writing.md` and `MEMORY_agent-corrections-design.md`.

Root `corrections.md` contains RightMemory Edit Feedback. It is neither semantic RightMemory state nor part of Agent Corrections.

The canonical module rules define what belongs in each module. This document defines representation and validity.

## Graph Roots And Membership

The local graph begins at:

- `MEMORY.md` for Memory;
- `PURSUITS.md` for Pursuit.

It recursively follows `F#` detail references from those roots. Filename patterns alone do not establish graph membership.

Item ids use the grammar `[A-Za-z0-9_.-]+`.

## Addressable Items

A graph item is either an addressable heading or a node.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
### Markdown Evidence {M#heading-id} → [edge1, edge2, ...]
### Skill Title {S#heading-id} → [edge1, edge2, ...]
### Mirrored File View {MF#heading-id} → [edge1, edge2, ...]
### Provider Question View {MQ#heading-id} → [edge1, edge2, ...]
```

Plain tree headings without graph relationships may omit the anchor and edge list. New Pursuit map items use addressable headings; their bodies are ordinary optional Markdown, with no dedicated task fields or node types.

Addressable `#`, `##`, and `###` headings may have body paragraphs directly below them. The body describes the heading concept as a whole. Use child nodes only for independently useful items.

Nodes use:

```md
- `<node-id>` <free-form description> → [edge1, edge2, ...]
```

Rules:

- Heading ids and node ids share one namespace across Memory and Pursuit.
- A node with no edges writes `→ []`. A heading with no edges may omit the edge list.
- Edges may connect any two addressable graph items.
- Useful but unsettled Memory begins its description with `Uncertain:`.
- Focus entries in `PURSUITS.md` reference Pursuit heading ids; they are not graph nodes.

## Backing Forms

### F#: Graph Detail

`F#` moves child graph content into a sibling detail file while preserving the heading as the graph item.

- Memory: `MEMORY_<id>.md`
- Pursuit: `PURSUIT_<id>.md`

The logical ancestor chain continues recursively across detail-file boundaries. The Pursuit editor presents this as one tree and hides the physical document split; each graph file still obeys the heading and terminal-reference rules below.

### M#: Markdown Evidence

`M#` is valid only in Memory. It points to free-form Markdown evidence in `MEMORY_<id>.md`. The backing file is retrievable through the heading but is not parsed as graph structure.

### S#: Instruction Asset

`S#` is valid only in Memory. It points to a complete reusable instruction in `MEMORY_SKILL_<id>.md`. The backing file is not parsed as graph structure.

### MF# And MQ#: Shared-View Connections

`MF#` represents a mirrored file view. `MQ#` represents a provider question view. Both are valid only in Memory. In both cases, the graph id is the unprefixed heading id, so edges target `type:<heading-id>`, never `type:MF#<heading-id>` or `type:MQ#<heading-id>`. The heading body records the local relationship. Resolver details and credentials remain outside graph prose.

`SHARED_VIEW_RULES.md` defines shared-view package and relationship semantics. The Retrieve runtime contract defines selector syntax.

## Reading Context And Referential Clarity

An item's local reading context consists of the item plus the titles and bodies of its logical ancestor headings. This context continues across `F#` boundaries. Ancestor headings may establish shared subject, scope, viewpoint, and reference points for descendants; descendants need not restate that context.

A reference that materially affects interpretation or application is clear when its referent is determined by local reading context or explicitly identified in the item, including by graph id or typed edge.

Typed edges express relationships. They do not make the target's prose part of the source item's ancestor context.

Context known only to the current reader or executing agent is not stored context. Relative or perspective-dependent language is valid only when stored context uniquely establishes its base.

Items are judged within local reading context, not as isolated fragments. Repetition needed to preserve scope or independent meaning is not duplication.

## Graph Invariants

A valid local graph has:

- globally unique ids across Memory and Pursuit;
- no dangling edges;
- no self-edges;
- no duplicate edges on one item;
- no child-to-parent edge used only to repeat containment;
- no graph content under an `F#` heading in its containing file;
- exactly the backing resource required by each typed heading;
- no unreferenced typed backing resource treated as graph state.

## Edge Types

- `dep:` A depends on B.
- `emb:` A embeds a copy of B.
- `bak:` A is a backup or snapshot of B.
- `agg:` A aggregates B's outputs.
- `ver:` A verifies or tests B.
- `ext:` A extends or enhances B.
- `up:` A is upstream of B.
- `rel:` A has a general relationship to B; use only when no specific type fits.
- `loc:` A is located inside B.
- `run:` A runs or launches through B.
- `cfg:` A uses B as configuration.
- `out:` A outputs B.
- `in:` A consumes B as input.
- `doc:` A documents B.
- `todo:` A is a question, todo, or blocker concerning B.

An edge may be one-way or reciprocal. Store the reverse edge only when it improves retrieval or understanding without misrepresenting the relationship.

## Heading And Placement Rules

- `#`, `##`, and `###` are normal tree layers and may be addressable.
- `####` is the deepest level allowed in a graph file. It is a terminal reference under an existing `###` topic and may use `F#`, `M#`, `S#`, `MF#`, or `MQ#`.
- A `####` terminal reference may have body paragraphs directly below it when they summarize or explain the reference.
- Do not place child headings or graph nodes under a `####` terminal reference.
- Create a `####` terminal reference only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
- Detail files follow this schema recursively.
- When child graph content moves behind `F#`, retain only the heading line and any body describing the heading itself in the containing file.
- Tree nesting expresses containment and reading context. Do not add an edge merely to repeat that relationship.
- Use heading edges for relationships that apply to the whole subtree and node edges for relationships that apply to one item.

## File Set

Semantic state:

```text
MEMORY.md
MEMORY_<id>.md              # Memory F# detail or M# evidence, determined by its heading
MEMORY_SKILL_<id>.md        # S# instruction
PURSUITS.md
PURSUIT_<id>.md             # Pursuit F# detail
MEMORY_agent-corrections-writing.md
MEMORY_agent-corrections-design.md
```

Operational or supporting files such as `corrections.md`, shared-view metadata, generated imports, share metadata, and insight logs are not local Memory/Pursuit graph items merely because they live in the same root.

Both graph roots remain useful documents rather than routing-only indexes.

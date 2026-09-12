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

## Document Structure And Addressability

Headings contain their own Markdown body, leaf items, and subheadings. Only
headings branch. A leaf item owns its complete Markdown content; lists or headings
inside that content are formatting, not RightMemory children or graph items.

Heading titles and ids are optional. These headings are valid:

```md
## Aesthetic quality
## {#aesthetics}
##
```

Text before the first structural heading belongs to the document root. Titles
and body previews are display text, not identity. Anonymous elements participate
in the tree; only explicitly addressed elements participate in the graph. Add an
id when graph relationships, saved references, Focus, or a typed backing need
it. Reading and editing do not require stored ids. Retrieve supplies missing
ids in its reading copy; editing tools may use snapshot-local handles. Neither
kind of temporary address is a stored graph identity.

Protocol-defined sections such as `## Focus` retain their specified identifying syntax.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
### Markdown Evidence {M#heading-id} → [edge1, edge2, ...]
### Skill Title {S#heading-id} → [edge1, edge2, ...]
### Mirrored File View {MF#heading-id} → [edge1, edge2, ...]
### Provider Question View {MQ#heading-id} → [edge1, edge2, ...]
```

Pursuit map entries remain headings; their titles and ids may be omitted, and
there are no dedicated task fields.

Leaf items use ordinary Markdown list syntax. Addressed items retain the existing
backtick-id notation; an unaddressed item needs neither an id nor an edge list:

```md
- A leaf without an id.
- `spacing` An addressed leaf. → []

  Another paragraph in the same leaf.

  - This nested list is Markdown content, not a RightMemory child.
```

Metadata is recognized only on a structural heading or leaf's opening line.
A leaf's leading backtick id is metadata when followed on that line by an
unescaped `→ [` or `-> [` outside inline code; otherwise it is ordinary Markdown.
An attempted declaration with an invalid id or malformed edge list is an error.
Heading anchors occupy the existing trailing declaration position. Code spans
and escaped markers are literal text, not metadata; escaping `[` disambiguates
a literal edge-list marker.

- Heading ids and leaf ids share one namespace across Memory and Pursuit.
- An addressed leaf with no edges writes `→ []`; a heading may omit the empty edge list.
- Existing ids remain stable when titles or bodies change.
- Edges may connect any two addressable graph items.
- Useful but unsettled Memory begins its description with `Uncertain:`.
- Focus entries in `PURSUITS.md` reference Pursuit heading ids; they are not graph nodes.

## Markdown Bodies And Boundaries

Parse CommonMark block structure with pipe tables. Outside a body fence,
document-level ATX headings and outer list items establish RightMemory structure.
List indentation and continuation follow Markdown. Consume each leaf's entire
list-item block without interpreting its interior as RightMemory structure.
Other blocks, including quotes, code, HTML, tables, and Setext headings, belong
to the surrounding heading or document body.

Use an optional body fence when a heading body itself needs headings or lists:

```md
:::body
### A heading within the body, not a RightMemory subheading

- A list within the same body, not a RightMemory item.
:::
```

A body fence is one owned Markdown block, not a tree node. Its opener is a
standalone, unindented line of at least three colons followed by `body`; its
closer is a standalone, unindented line with exactly the same number of colons.
Fence lines may have trailing spaces. The enclosed text is opaque to RightMemory
structure and metadata. Fences do not nest; use more colons when the payload
contains a matching closing line, including inside a code example. Unclosed
fences and unmatched closing fences are errors. Outside a body fence, recognize
markers only at document block level, not within a leaf, quote, code block, or
HTML block.

A heading's own body excludes its leaf items and subheadings. Preserve the source
order of body blocks and children, including body text between leaf items. An
own-body view returns only that body; a subtree view also includes the children.
Keep fence delimiters in stored/source-editable Markdown; omit them when rendering
body content. Preserve original source spans and Markdown resource resolution
when extracting or moving content.

## Backing Forms

### F#: Graph Detail

`F#` moves child headings and leaf items, addressed or anonymous, into a sibling detail file while preserving the heading as the graph item.

- Memory: `MEMORY_<id>.md`
- Pursuit: `PURSUIT_<id>.md`

The logical ancestor chain continues recursively across detail-file boundaries. The Pursuit editor presents this as one tree and hides the physical document split; each graph file still obeys the heading and terminal-reference rules below.

A split retains the heading and its leading own-body content, then moves the
remaining ordered content to the detail file. Document-root body blocks there
belong to the owning `F#` heading. Splitting or joining preserves logical order
and ownership, including body text between leaf items.

### M#: Markdown Evidence

`M#` is valid only in Memory. It points to free-form Markdown evidence in `MEMORY_<id>.md`. The backing file is retrievable through the heading but is not parsed as graph structure.

### S#: Instruction Asset

`S#` is valid only in Memory. It points to a complete reusable instruction in `MEMORY_SKILL_<id>.md`. The backing file is not parsed as graph structure.

### MF# And MQ#: Shared-View Connections

`MF#` represents a mirrored file view. `MQ#` represents a provider question view. Both are valid only in Memory. In both cases, the graph id is the unprefixed heading id, so edges target `type:<heading-id>`, never `type:MF#<heading-id>` or `type:MQ#<heading-id>`. The heading body records the local relationship. Resolver details and credentials remain outside graph prose.

`SHARED_VIEW_RULES.md` defines shared-view package and relationship semantics. The Retrieve runtime contract defines selector syntax.

## Reading Context And Referential Clarity

An item's local reading context consists of the item plus the titles and own bodies of its logical ancestor headings, excluding their other leaf items and subheadings. This context continues across `F#` boundaries. Ancestor headings may establish shared subject, scope, viewpoint, and reference points for descendants; descendants need not restate that context.

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
- no inline child headings or leaf items under an `F#` heading in its containing file;
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

These rules apply to structural headings, not Markdown inside a leaf or body fence.

- `#`, `##`, and `###` are normal tree layers and may be addressable.
- `####` is the deepest level allowed in a graph file. It is a terminal reference under an existing `###` topic and may use `F#`, `M#`, `S#`, `MF#`, or `MQ#`.
- A `####` terminal reference may have its own Markdown body, including body-fenced content.
- Do not place child headings or leaf items under a `####` terminal reference, addressed or anonymous.
- Create a `####` terminal reference only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
- Detail files follow this schema recursively.
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

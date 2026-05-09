# RightMemory Schema

## Addressable Lines

Each memory entry is either an addressable heading or a node.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
```

Plain tree headings without graph edges may omit the anchor and edge list.

Addressable `#`, `##`, and `###` headings may have body paragraphs directly under the heading. Those paragraphs describe the heading node itself. Use a heading body when text explains the whole heading concept; use child nodes when text should stand as its own memory under that concept.

Nodes use:

```md
- `<node-id>` <free-form description> → [edge1, edge2, ...]
```

- `heading-id` and `node-id` share one namespace; do not reuse an id between a heading and a node.
- Edges may connect heading to heading, heading to node, node to heading, or node to node.
- Node lines with no edges write `→ []`; heading lines with no edges may omit `→ []`.

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

- `#`, `##`, and `###` are normal tree layers and may contain memory content.
- `#`, `##`, and `###` may have `{#short-slug}` anchors and edges when the whole subtree is a graph target.
- `#### Human Title {#short-slug}` is title-only and points to sibling detail file `MEMORY_<short-slug>.md`.
- Do not write body content under a `####` pointer in the current file.
- Create `####` pointers only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
- Detail files use the same schema recursively.

## Placement Rules

- Tree nesting expresses reading context and containment; do not add child-to-containing-heading edges merely to say a node belongs under a heading.
- Use heading edges when a relation applies to the whole subtree.
- Use node edges when a relation applies only to one fact.
- Do not create a child node merely to summarize an addressable heading. Prefer a heading body for whole-heading description.
- Put new nodes in the closest existing `##` or `###` group of the matching `#` domain.
- When a topic has multiple closely related facts, prefer a meaningful anchored `##` or `###` heading over a fake hub node.
- Place information where it keeps the tree/graph clear: update, add, split, merge, or move headings and nodes as needed. Avoid duplicate, fake-hub, or overloaded structure.

## File Set

- The memory file set is `MEMORY.md` plus optional sibling `MEMORY_*.md` detail files.
- `MEMORY.md` is the root memory file, but it remains normal memory and may contain real nodes.
- Do not edit `# User Pending Task and Thoughts`; that section is user-edited only.

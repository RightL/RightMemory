# RightMemory Schema

## Addressable Lines

Each memory entry is either an addressable heading or a node.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
```

Plain tree headings without graph edges may omit the anchor and edge list.

Addressable `#`, `##`, and `###` headings may have body paragraphs directly under the heading. Those paragraphs describe the heading node itself, even when the heading also has a sibling detail file. Use a heading body when text explains the whole heading concept; use child nodes when text should stand as its own memory under that concept.

Nodes use:

```md
- `<node-id>` <free-form description> → [edge1, edge2, ...]
```

- `heading-id` and `node-id` share one namespace; do not reuse an id between a heading and a node.
- `F#` marks a heading as file-backed; the graph id is still `heading-id`, so edges target `type:heading-id`, not `type:F#heading-id`.
- Edges may connect heading to heading, heading to node, node to heading, or node to node.
- Node lines with no edges write `→ []`; heading lines with no edges may omit `→ []`.
- Useful but unsettled memory uses `Uncertain:` at the start of the node description, for example ``- `<node-id>` Uncertain: <tentative memory claim with its scope or doubt> → [...]``. Revise it into ordinary declarative memory when it becomes settled, or remove it when it is contradicted or no longer useful.

## Memory Domains

Memory domains are ordinary `#` headings. Use `# User Context` for durable context about the user as a person with an ongoing life, work, and direction: relevant background, active pursuits, longer-term goals, important responsibilities or life/work circumstances, and why those things matter. It is a compact context profile grounded in evidence.

Use `# Cross-Session Agent Behavior` for guidance about how agents should work with this user when they use coding agents: communication style, workflow expectations, tool or process preferences, and repeated agent mistakes that future agents can avoid.

Use global `# Open Context Questions` for loose ends in memory. These are short questions for future agents, not declarative memory facts. Use them when a memory area feels incomplete, unclear, or hard to apply because something still needs to be pinned down. Question nodes use normal node syntax and link to related memory with `todo:`. When the answer becomes clear, write the answer into the appropriate declarative memory section, then remove or revise the question.

When a fact could fit either domain, place it by what the memory is about. Facts that describe the user's background, pursuits, direction, motivations, responsibilities, or circumstances belong under user context. Guidance that tells an agent how to respond, decide, use tools, write, or collaborate belongs under agent behavior.

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
- A file-backed `#`, `##`, or `###` heading uses `{F#short-slug}` and maps to sibling detail file `MEMORY_<short-slug>.md`.
- When a heading's child content moves into its detail file, keep only the heading line and any heading body paragraphs in the current file. Do not leave child node lines or child headings under that heading in the current file.
- `#### Human Title {F#short-slug}` is the deepest heading level allowed in a memory file and points to sibling detail file `MEMORY_<short-slug>.md`.
- A `####` pointer may have body paragraphs directly under it when they summarize the detail file or explain the pointer.
- Do not write child node lines or child headings under a `####` pointer in the current file.
- Create `####` pointers only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
- Detail files use the same schema recursively.

## Placement Rules

- Use detail files to keep large headings readable. As a guide, consider moving child content into a detail file when a `#`, `##`, or `###` heading has more than about 15 direct node lines, then mark the parent heading with `F#`.
- Count only direct node lines shaped like ``- `<node-id>` ...``; do not count child headings or `####` pointers.
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

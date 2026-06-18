# RightMemory Schema

## Addressable Lines

Each memory entry is either an addressable heading or a node.

Addressable headings use:

```md
### Human Title {#heading-id} → [edge1, edge2, ...]
### File-Backed Title {F#heading-id} → [edge1, edge2, ...]
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

- `heading-id` and `node-id` share one namespace; do not reuse an id between a heading and a node.
- `F#` marks a heading as backed by an ordinary detail file. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:F#heading-id`.
- `S#` marks a heading as backed by a memory skill file. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:S#heading-id`.
- `MF#` marks a mirrored file shared-view connection. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:MF#heading-id`. The heading body records the local relationship and collaboration meaning; HTTP resolver details live outside memory prose.
- `MQ#` marks a provider question shared-view connection. The graph id is still `heading-id`, so edges target `type:heading-id`, not `type:MQ#heading-id`. The heading body records when provider-side questions may help; prompts, HTTP resolver details, and credentials live outside memory prose.
- `S#heading-id` maps to sibling skill file `MEMORY_SKILL_heading-id.md`.
- Edges may connect heading to heading, heading to node, node to heading, or node to node.
- Node lines with no edges write `→ []`; heading lines with no edges may omit `→ []`.
- Useful but unsettled memory uses `Uncertain:` at the start of the node description, for example ``- `<node-id>` Uncertain: <tentative memory claim with its scope or doubt> → [...]``. Revise it into ordinary declarative memory when it becomes settled, or remove it when it is contradicted or no longer useful.

## Memory Entry Shape

A memory entry is durable, reusable knowledge for future agents. It should
preserve what future agents need to know, rather than narrate how that knowledge
appeared. Rewrite session evidence, user quotes, correction stories, and
"observed during" provenance into the durable rule or conclusion; keep exact
phrasing only when the wording itself is a reusable trigger or artifact.

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

A provider root may define file views under `shared_views/<view-id>/`:

```text
shared_views/<view-id>/
  view.md
  recipe.toml
  dist/
```

`view.md` and `recipe.toml` are provider-owned file-view source files. The recipe records the approved source headings, nodes, or files chosen by the builder. The generated `dist/` directory is preview or publishing output; it is not active provider memory.

A provider root may define question views under `shared_views/<view-id>/`:

```text
shared_views/<view-id>/
  view.md
  retriever.md
  question.toml
```

`retriever.md` belongs only to provider question views. It is the provider-side retrieval prompt used when an accepted consumer asks an `MQ#` question.

Consumers pull accepted `MF#` file views into `.runtime/shared_views/imports/<view-id>/` before retrieve runs, then read those imported files with ordinary read/search tools. Consumers call `MQ#` question views through explicit ask commands or UI actions outside retrieve. Record local consequences in ordinary memory only when those consequences become durable.

## Memory Domains

Memory domains are ordinary headings. Use `# User Context` for durable context about the user as a person with an ongoing life, work, and direction: relevant background, active pursuits, longer-term goals, important responsibilities or life/work circumstances, and why those things matter. It is a compact context profile grounded in evidence.

Use `Cross-Session Agent Behavior` for future-facing guidance about how agents should work with this user across coding sessions. This includes both broadly applicable user-level guidance and project-scoped guidance that should persist across sessions for a specific repository or codebase, such as communication style, workflow expectations, tool or process preferences, and repeated agent mistakes that future agents can avoid. Express narrower scope through heading nesting or an explicit scope in the heading/local context. These entries should read like operating instructions, not transcript review notes.

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
- A skill-backed `#`, `##`, or `###` heading uses `{S#short-slug}` and maps to sibling skill file `MEMORY_SKILL_<short-slug>.md`.
- A mirrored file shared-view `#`, `##`, or `###` heading uses `{MF#short-slug}` and points to an external file view through an out-of-band resolver entry.
- A provider question shared-view `#`, `##`, or `###` heading uses `{MQ#short-slug}` and points to an external question view through an out-of-band resolver entry.
- When a heading's child content moves into its detail file, keep only the heading line and any heading body paragraphs in the current file. Do not leave child node lines or child headings under that heading in the current file.
- `####` is the deepest heading level allowed in a memory file and is a terminal reference heading under an existing `###` topic.
- `#### Human Title {F#short-slug}` points to sibling detail file `MEMORY_<short-slug>.md`.
- `#### Human Title {S#short-slug}` points to sibling skill file `MEMORY_SKILL_<short-slug>.md`.
- `#### Human Title {MF#short-slug}` points to a mirrored file shared-view connection.
- `#### Human Title {MQ#short-slug}` points to a provider question shared-view connection.
- A `####` terminal reference may have body paragraphs directly under it when they summarize or explain the reference.
- Do not write child node lines or child headings under a `####` terminal reference in the current file.
- Create `####` terminal references only under an existing or newly created `###` topic; do not jump directly from `#` or `##` to `####`.
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

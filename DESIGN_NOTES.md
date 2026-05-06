# DESIGN_NOTES

## Project

### Multi-file memory tree

`MEMORY.md` remains normal memory instead of becoming a routing-only index, because the root file should still carry useful high-level graph nodes and readable context. `#`, `##`, and `###` are normal tree layers; `####` is reserved as a title-only external child pointer so deeper detail can move out without pretending that a broad section is a detail file.

### Addressable headings

`#`, `##`, and `###` headings may carry `{#slug}` anchors and graph edges because some relations apply to an entire subtree, not one fact node. Heading slugs and node ids share one namespace so edges can target either form without fake hub nodes or duplicate identifiers.

### Containment is tree structure

Child nodes should not point to their containing heading merely to say they belong there, because Markdown nesting already encodes that context. Edges are reserved for cross-links and semantic relations that are not obvious from position, which keeps reverse-edge maintenance from drowning useful graph signal.

### Detail file naming

Detail files use short explicit slugs from `#### Topic {#slug}` and map to `MEMORY_<slug>.md`. This keeps filenames stable and short while preserving the visible Tree+graph model in the Markdown content; filenames are storage details, not graph nodes.

### Schema ownership

Schema rules live in `skills/rightmemory-schema.md` instead of at the top of every `MEMORY.md`, because memory files should stay focused on user memory while prompt/schema changes remain single-source and installable with the skills.

### Curator baseline commits

The curator makes a baseline commit only before its first write when the memory repo is already dirty, because pre-existing memory edits should not be mixed with curator-created routine changes. Routine curator writes remain uncommitted so users can batch or review them, while dreamer remains the commit-oriented consolidation path.

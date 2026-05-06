# DESIGN_NOTES

## Project

### Multi-file memory tree

`MEMORY.md` remains normal memory instead of becoming a routing-only index, because the root file should still carry useful high-level graph nodes and readable context. `#`, `##`, and `###` are normal tree layers; `####` is reserved as a title-only external child pointer so deeper detail can move out without making ordinary headings special.

### Detail file naming

Detail files use short explicit slugs from `#### Topic {#slug}` and map to `MEMORY_<slug>.md`. This keeps filenames stable and short while preserving the visible Tree+graph model in the Markdown content; filenames are storage details, not graph nodes.

### Curator baseline commits

The curator makes a baseline commit only before its first write when the memory repo is already dirty, because pre-existing memory edits should not be mixed with curator-created routine changes. Routine curator writes remain uncommitted so users can batch or review them, while dreamer remains the commit-oriented consolidation path.

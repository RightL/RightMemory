# Retrieve Runtime Contract

This contract owns Retrieve's input envelope and terminal-selection mechanics. The Retrieve role prompt does not redefine them.

## Input Context

The conversation begins with a stable snapshot of:

- `MEMORY.md`;
- `PURSUITS.md`;
- any present fixed Agent Correction collections.

Root `corrections.md` is excluded from ordinary retrieval context.

In CLI-agent mode, RightMemory keeps one internal prefix-base provider conversation for each exact combination of the canonical Retrieve instructions, this snapshot, and the effective provider configuration. It bootstraps that base once with an empty terminal selection. Each new logical Retrieve session forks the base through Codex or Claude and owns independent provider history and delivery coverage; later calls resume that fork.

If base initialization or forking fails, retire that base and start the request in a fresh provider conversation with the complete prefix.

Later context may contain:

- diffs that update those snapshot files;
- notices about previously returned sources that changed;
- pending updater candidates;
- an inventory of learning sources, including raw evidence, guidance, experiences, and skills;
- the current retrieval query, placed last.

This volatile context follows the inherited snapshot in each CLI-agent session fork. Apply each diff to the earlier snapshot. Added lines are current; removed lines are obsolete. Standalone mode builds the same logical context without a provider prefix base or fork.

## Terminal Selection

The logical terminal value is:

```json
{
  "ids": ["local-graph-id"],
  "sources": [
    {
      "source_id": "M#linked-source-id",
      "ids": [],
      "ranges": [{"start": 12, "end": 24}]
    },
    {
      "source_id": "AC#design",
      "ids": ["4"],
      "ranges": []
    }
  ],
  "recent_candidates": ["update-session:3"]
}
```

Return exactly one object with only `ids`, `sources`, and `recent_candidates`. Use empty arrays when no relevant content exists. Do not add reasons, confidence, summaries, or prose.

Standalone mode supplies this object as the terminal output type. CLI-agent mode emits strict JSON without a code fence or surrounding text.

## Local Graph Selection

- Top-level `ids` contains globally unique ids from the local Memory and Pursuit graph, including graph items reached through `F#` details.
- Selecting a graph heading does not automatically select its linked `M#`, `S#`, `MF#`, or `MQ#` content.

## Linked Sources

A linked graph `source_id` includes its marker:

- `M#<id>`: free-form Markdown evidence; select inclusive one-based line ranges.
- `S#<id>`: complete instruction; use empty `ids` and `ranges`.
- `MF#<view-id>`: imported graph items; place view-local graph ids in that source's `ids`. Direct ranges are invalid.
- `MF#<view-id>/M#<id>`: imported free-form evidence; select inclusive one-based line ranges.
- `MF#<view-id>/S#<id>`: complete imported instruction; use empty `ids` and `ranges`.

`MQ#` has no linked-content selection. Its local graph heading may be selected through top-level `ids` when the relationship context is relevant.

## Agent Correction Sources

`AC#writing` and `AC#design` are fixed retrieval-only sources. They are not graph headings or stored markers.

- Their `ids` are one-based entry positions encoded as strings.
- Their `ranges` must be empty.
- Each selected id returns the complete current entry.
- The runtime exposes entry positions unambiguously in model context rather than requiring the model to count headings.

The correction collections are part of ordinary retrieval context. There is no separate correction retrieval pass.

## Learning Sources

Learning sources are independent of graph ids. The inventory supplies a source id, path, kind, and brief excerpt. The inventory may be compact. Search further when it is insufficient: standalone Retrieve uses `search_learning(query, offset=0, limit=20)` over all learning sources, with an empty query for paged browsing; command-backed and native agents can search the real source directories. Read relevant content with `read_learning(source_id, offset=0, limit=12000)` or its listed file before judging its meaning. The reader pages by character offset while preserving source line numbers, so oversized evidence and skills can be inspected incrementally.

Source ids begin with `L#` followed by the path relative to `learning/`, such as `L#candidates/<id>.json`, `L#guidance/collaboration.md`, `L#skills/report/SKILL.md`, or `L#experiences/report.md`. Retained legacy inbox entries use `L#legacy/<GI-id>`.

Use empty `ids` for all learning sources. Raw candidate evidence, legacy inbox evidence, and `SKILL.md` are complete sources with empty `ranges`, even when inspected in pages; other learning Markdown may be selected whole or by inclusive one-based line ranges. Processing receipts are not retrieval sources.

Raw evidence remains distinct from curated guidance even after its candidates have been processed. Select relevant material rather than injecting every candidate into every result. When a skill is needed as an instruction, return it in full.

## Recent Candidates

`recent_candidates` contains exact `selection_id` values supplied in volatile context.

Candidates are unsettled evidence, not stored RightMemory state. Select one only when both its status and content are relevant to the current query.

## Delivery Behavior

The runtime owns hierarchy expansion, Focus rendering, source ordering, overlap removal, delivery hashes, unchanged-result suppression, `--include-returned`, and final source formatting.

The Retrieve role must not compensate for those behaviors with model-authored prose.

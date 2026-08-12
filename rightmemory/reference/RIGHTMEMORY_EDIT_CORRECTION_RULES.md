# RightMemory Edit Feedback

RightMemory Edit Feedback records concrete user feedback on proposed edits to Memory, Pursuit, Agent Corrections, or linked RightMemory content.

The optional root file is `corrections.md`. It is operational curation feedback, not semantic RightMemory state, part of the Memory/Pursuit graph, or part of Agent Corrections.

## Entry Form

Each `##` entry contains, in order:

```md
## <Edit pattern title>

### Candidate
<The candidate evidence that materially shaped the proposed edit.>

### Proposed edit
<The smallest self-contained RightMemory fragment needed to show the rejected edit.>

### Accepted edit
<The corresponding accepted state, or [no change].>
```

## Rules

- Record only proposed RightMemory edits that the user corrected, rejected, or replaced. Do not record ordinary accepted edits.
- Preserve relevant candidate text exactly. Include every candidate that materially shaped the proposal, but omit unrelated candidates from the same batch.
- Preserve the smallest self-contained fragments needed to compare the proposed and accepted edits.
- Use `[no change]` when a proposed edit was rejected entirely.
- Keep evidence exact. Do not derive general behavior lessons inside this collection.
- Keep at most 10 distinct, reusable entries.
- Treat the collection as a bounded priority set, not a log or FIFO window. Retain the examples most likely to prevent costly repeated curation mistakes.

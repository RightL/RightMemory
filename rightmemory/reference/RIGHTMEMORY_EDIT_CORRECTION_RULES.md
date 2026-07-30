# RightMemory Edit Correction Rules

RightMemory Edit Corrections preserve user corrections to edits of Memory, Pursuit, or their linked content. The active collection is the optional root `corrections.md`. It is editing feedback, not semantic RightMemory state or Agent Correction Memory.

Read relevant entries only after forming a tentative edit.

## Entries

- Record only proposed RightMemory edits that the user corrected, rejected, or replaced; do not record ordinary accepted edits.
- Each `##` entry contains, in order, `### Candidate`, `### Proposed edit`, and `### Accepted edit`.
- `Candidate` preserves the relevant candidate text verbatim. Include every candidate that materially shaped the proposed edit, but omit unrelated candidates from the same batch. Do not substitute record paths, ids, or summaries.
- Preserve the smallest self-contained RightMemory fragment needed to compare the proposed and accepted edits. Use `[no change]` for a file whose proposed edit was rejected entirely.
- Keep evidence exact and omit derived lessons or general behavior guidance.
- Keep at most 15 distinct, reusable entries. This is a bounded priority set, not an append-only log or FIFO window; retain the entries most likely to prevent costly repeated edit errors.

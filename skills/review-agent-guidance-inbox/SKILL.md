---
name: review-agent-guidance-inbox
description: "Use when the user explicitly asks to review pending agent guidance and decide what should enter RightMemory."
---

# Review Pending Agent Guidance

This is a user-reviewed direct-curation workflow. Do not route inbox entries through Update.

## Inspect

- Use the user-selected RightMemory root or profile. Otherwise run `rightmemory status` and use its reported root; never guess.
- Run `rightmemory validate --root <root>`.
- Read root `AGENT_GUIDANCE_INBOX.md`. If it contains no pending entries, report that and stop.
- Read the canonical Memory and Agent Corrections rules, the relevant current Cross-Session Agent Behavior and Agent Corrections, and any other state needed to judge overlap.
- Form a tentative judgment first, then read root `corrections.md`, when present, as a late check on the proposed curation.
- Consider related inbox entries together rather than reviewing each mechanically in isolation.

## Propose

For each proposal, identify the supporting inbox entries and state:

- whether to add, merge, replace, or remove formal guidance;
- whether its destination is Cross-Session Agent Behavior, Agent Corrections, or both;
- the proposed meaning and scope;
- which inbox entries would be removed.

Also identify entries that should be rejected, treated as already covered, or left pending for more evidence.

Do not present exact final wording, graph ids, or a patch before approval. Apply only explicitly approved proposals.

## Edit And Land

- Require the active root to be clean. Capture its branch and HEAD, then create a temporary Git worktree from that HEAD.
- Apply the approved semantic changes and inbox removals together. Leave deferred and unreviewed entries unchanged.
- Run `rightmemory validate --root <worktree>` immediately before committing.
- Before landing, require the active root to remain clean and at the captured HEAD.
- Land the exact validated commit, validate the active root again, and remove the temporary worktree.

Report the applied proposals, removed and remaining inbox entries, validation result, and commit hash, or `no commit`.

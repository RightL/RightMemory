---
name: review-rightmemory-session
description: "Use when the user explicitly asks to review a saved agent session or transcript and directly curate RightMemory from it."
---

# Review A RightMemory Session

This is an explicit direct-curation workflow. Do not route the transcript through Update or another RightMemory model role.

## Resolve The Review

- Use the user-selected RightMemory root or profile. Otherwise run `rightmemory status` and use its reported root; never guess. Keep that root or profile consistent for every RightMemory command in this workflow.
- Resolve the requested regular transcript through provider tooling or configured transcript locations. Require one unambiguous transcript when given an id; otherwise ask for its path.
- Review the full available session, including visible conversation and any compacted or summarized earlier context.
- Derive the provider and stable session id from the resolved transcript. If either cannot be determined reliably, ask rather than inventing it. Then run `rightmemory review status <provider>:<session-id>`.
- Stop when it is already marked reviewed unless the user explicitly requests another review.

## Form Proposals

1. Run `rightmemory validate --root <root>`.
2. Read the canonical RightMemory schema, Memory rules, Agent Corrections rules, and `RIGHTMEMORY_EDIT_CORRECTION_RULES.md` (RightMemory Edit Feedback).
3. Read the complete reachable Memory state relevant to the session.
4. Form an independent tentative judgment from the transcript and express it as tentative proposals.
5. Then read root `corrections.md`, when present, as a late check against repeated RightMemory curation mistakes.
6. Then read `MEMORY_agent-corrections-writing.md` (Expression) and `MEMORY_agent-corrections-design.md` (Substance), when present, to merge, replace, or reject duplicates.

Consider only:

- ordinary durable Memory;
- Cross-Session Agent Behavior;
- Agent Corrections.

Do not edit Pursuit or root `corrections.md` in this workflow.

Apply the canonical admission, scope, classification, and retention rules. Do not reproduce those rules locally in this skill.

## Approval

Unless the user explicitly requests direct editing, present proposal summaries under `Strongly recommended` and `Optional`.

For each proposal, state its destination, scope, substance, and reason. Do not present exact final wording, graph ids, or a patch before approval.

Apply only explicitly approved proposals. When direct editing was explicitly requested, skip the approval pause but keep every validation and landing safeguard below.

## Edit And Land

- Require the active root to be clean. Capture its branch and HEAD.
- Create a temporary Git worktree from that HEAD. Do not stash, discard, or absorb unrelated changes.
- Apply the authorized changes directly while preserving the complete Memory graph and relevant backing files.
- Run `rightmemory validate --root <worktree>` immediately before committing.
- Before landing, require the active root to remain clean and at the captured HEAD.
- Land the exact validated commit, validate the active root again, and remove the temporary worktree.

After every successful review—including an authorized no-change review—run:

`rightmemory review mark <provider>:<session-id>`

Never mark a failed, interrupted, or deferred review.

Report the applied proposal summaries, validation result, commit hash or `no commit`, and review-record result.

---
name: maintain-pursuit-map
description: "Use when the user explicitly asks to inspect, create, rename, move, remove, focus, simplify, or reorganize the RightMemory Pursuit map."
---

# Maintain The Pursuit Map

Pursuit is a user-owned map of ongoing directions. Use this workflow for an explicit request concerning that map. It edits canonical Markdown directly; it does not submit to Update or invoke another RightMemory role or maintenance skill.

- Use the exact root supplied by the user. Otherwise run `rightmemory status`, or `rightmemory --profile <name> status` for a requested profile, and use the reported root. If no root is reported, ask rather than guessing.
- Run `rightmemory validate --root <root>` before reading state. Do not read paths reported as non-regular. Read `rightmemory reference schema` and `rightmemory reference pursuit`, the complete affected subtree, its ancestors, Focus, and relevant graph references. Read additional Memory or project context when it is needed to understand the requested change.
- A request to modify the map authorizes the requested change; an inspection request alone does not. Resolve ordinary local details from canonical state, and ask only when a missing target or materially different interpretation prevents a safe edit. Do not add a routine proposal-and-approval step.
- Require a clean active root, capture its branch and HEAD, and create a dedicated task branch and Git worktree from that HEAD, following the repository's worktree rules. Preserve unrelated changes; do not stash or discard them to proceed.
- Edit in the worktree. Preserve stable ids across rename and move, and generate globally unique ids for new headings without asking the user. Keep each node to its title, tree position, optional free-form body, and useful graph relationships. Execution detail and completed history belong outside the map.
- Update Focus, `F#` backing placement, and graph references together. Read Memory as context; change it here only for mechanically required reference repair unless the user separately requested Memory maintenance. Preserve existing content outside the requested change, including old body text that has not been selected for cleanup.
- Run `rightmemory validate --root <worktree>` immediately before committing and require success. Before landing, require the active root to remain clean, on the captured branch, and at the captured HEAD; otherwise stop and reassess against the new state. Land only the validated change, validate the active root again, and remove the temporary worktree after a successful landing. Push when sync is configured and the branch is published.

Report the changed map structure, validation result, landed commit, and push result, or `no commit` for inspection or an unchanged map.

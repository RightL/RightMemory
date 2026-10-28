# Updating an Existing RightMemory Root

RightMemory develops against its current model. An upgrade may change commands, content ownership, or stored formats. When old state needs attention, ask an agent to inspect that root using this explanation and the current package references. Installation does not reinterpret or rewrite the user's memory. Cleanup follows the actual content and intended result rather than a version-by-version conversion script.

## Current Content Ownership

| Content | Current home |
| --- | --- |
| Context about the user, other people, projects, choices, and their reasons | The ordinary Memory graph |
| Guidance about how agents should work with the user | `learning/guidance/` |
| Reusable contrasts between prior agent work and a user redirection | The two fixed Agent Correction collections |
| Useful work cases with partial or contextual lessons | `learning/experiences/` |
| Reusable methods and their supporting resources | `learning/skills/<slug>/SKILL.md` and its assets |

Pursuit remains the user's direction map. Root `corrections.md` remains feedback about RightMemory's curation. Read current definitions with `rightmemory reference <name>`, using `schema`, `memory`, `guidance`, `work-skills`, or `agent-correction` as needed.

## Changes Relevant to This Release

Ordinary Memory Update now owns ordinary context. Behavioral guidance, user redirections, and reusable work use `rightmemory learning ... submit` or `rightmemory_submit_learning`, including explicit requests to remember that material. The former `rightmemory guidance submit` and `rightmemory_capture_guidance` calls are no longer supported. The current daily entry and domain maintenance skills replace the old inbox-review workflow.

An older root may contain guidance in Cross-Session Agent Behavior, work instructions behind `S#` headings, or unresolved interactions in `AGENT_GUIDANCE_INBOX.md`. Inspect their meaning before deciding what still deserves retention. Reconcile useful content into the appropriate current home, preserve valuable concrete context, and remove duplication or obsolete material within the requested cleanup scope. Copying every old item would carry the same clutter into new directories.

`S#` remains a supported graph resource, including in shared views. Its presence alone is not an error. Preserve working references and package-owned imports; relocate an instruction because its ownership or use needs to change, not merely because it uses that marker. Existing Agent Corrections already have a current home and need no second case library.

Review/Reviewer and Dreamer execution have been removed. Stop an older installation's background manager with its `rightmemory watch stop` and let active work finish before upgrading. Old configuration may contain `[review]`, `[reviewer]`, `[dreamer]`, or `insight.watch.review_session_points`. Remove obsolete configuration while preserving unrelated settings. Resolve pending deliveries with that installation or preserve their evidence before retiring the old runtime files. Package references now live with the package; preserve any useful local edits before removing obsolete root copies such as `PURSUIT_RULES.md` or `AGENT_CORRECTION_MEMORY_RULES.md`. Disable or remove obsolete installed skills, including `review-agent-guidance-inbox`, after inspecting any local customizations.

## Working on a User's Root

Use the root the user selected and inspect the current validation or installation error. Preserve unrelated edits and retain a recoverable starting point. Read the relevant current references and actual stored content, then make the requested cleanup as a coherent change. Current candidate identities and processing receipts are program-owned; use their normal recovery and curation commands rather than fabricating completed work.

Run `rightmemory validate --root <root>` on the result and report what changed and any unresolved evidence. If the package is not yet installed, the repository's schema and domain cores provide the same definitions. Retry installation after resolving the reported incompatibility. No general conversion of unaffected data is required.

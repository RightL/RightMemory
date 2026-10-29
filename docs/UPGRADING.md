# Updating an Existing RightMemory Root

RightMemory develops against its current model. When an upgrade changes commands, ownership, or stored formats, ask an agent to inspect the actual root using this explanation and the current package references. The product does not carry historical readers, automatic conversion, or template cleanup for older installations. Ordinary installation preserves user data and validates the current format; cleanup is a separate requested task.

## Current Content Ownership

| Content | Current home |
| --- | --- |
| Context about the user, other people, projects, choices, and their reasons | The ordinary Memory graph |
| Guidance about how agents should work with the user | `learning/guidance/` |
| Reusable contrasts between prior agent work and a user redirection | The two fixed Agent Correction collections |
| Useful work cases with partial or contextual lessons | `learning/experiences/` |
| Reusable methods and their supporting resources | `learning/skills/<slug>/SKILL.md` and its assets |

Pursuit remains the user's direction map. Root `corrections.md` remains feedback about RightMemory curation. Read current definitions with `rightmemory reference <name>`, using `schema`, `memory`, `guidance`, `work-skills`, or `agent-correction` as needed.

## What Changed

There is one submission route: `rightmemory submit` or the MCP tool `rightmemory_submit`. Memory, guidance, and skills independently process the same candidate. All domains use `rightmemory curate` for preparation, completion, status, and recovery, with `update` or `maintain` selecting the task scope. The former `update submit/pull/undo`, `assistant submit/update`, `learning ...`, and `guidance submit` interfaces are removed. Update is still the internal Memory role and default writer configuration name.

Ordinary `rightmemory mcp` now exposes exactly `rightmemory_retrieve` and `rightmemory_submit`. Calls to the removed `rightmemory_begin_curation`, `rightmemory_complete_curation`, `rightmemory_curation_status`, `rightmemory_withdraw_candidate`, and `rightmemory_run_curation` tools use the corresponding `begin`, `complete`, `status`, `withdraw`, and `run` subcommands of `rightmemory curate`. Native `assistant mcp` retains the model-free begin, complete, status, and withdrawal tools for native worker coordination. Successful ordinary MCP submission returns empty success; retrieve returns context and actionable warnings without routine curation notices. Use the CLI to inspect accepted evidence or recover curation. Configured worker scheduling and command-backed maintenance skills are unchanged.

`learning/candidates/` now holds pending evidence. After all three domains finish, the program removes the raw source. Curators preserve useful context, concrete cases, and work experiences in their current homes. Processing records retain identities and hashes rather than evidence bodies. Maintenance works on existing curated material; it does not manually retire raw candidates or edit receipts. Current Git history is not rewritten, so earlier evidence may remain in existing commits and remotes.

Older installations may have `AGENT_GUIDANCE_INBOX.md`, a separate `update_queue/`, immutable `update_records/`, or local async outboxes. These are no longer read, processed, or synchronized as active product state. Inspect their actual value and unfinished work before retiring them. Finish work with the old executor when practical, or preserve useful evidence and submit it through the current interface during the requested cleanup. Do not manufacture current processing receipts to make old state look completed.

Review/Reviewer and Dreamer execution, and the separate asynchronous Update worker, are removed. Stop the old background processes before replacing that installation. Remove obsolete `[review]`, `[reviewer]`, `[dreamer]`, `[update.async]`, and `insight.watch.review_session_points` settings while preserving unrelated configuration. Current validation reports incompatible state; it does not recognize or convert historical `Next` fields or old evidence formats.

Pursuit graph-node bullets with an arrowed edge list are no longer accepted as map leaves. During requested cleanup, represent a retained direction as a heading or keep contextual material as ordinary Markdown, preserving meaningful ids and references. Ordinary lists, plain grouping headings, and standalone Focus references remain supported. Validation reports incompatible lines without converting or deleting them; Memory nodes and imported Memory graphs keep their current syntax.

The Hub's local database also uses the current schema without automatic historical upgrades. If required tables or fields are missing, have the cleanup agent inspect that database and preserve useful data while bringing it to the current schema; normal startup does not rewrite existing data or backfill historical rows.

Inspect local customizations before disabling obsolete installed skills or deleting old package-reference copies. Current daily entries and the three maintenance skills replace earlier review workflows. The installer only handles the two current daily entries when switching modes; it does not identify historical managed copies by hashes or sweep old role directories.

## Cleanup Judgment

An older root may contain guidance in a Behavior section, instructions behind `S#` headings, or unsettled interactions in an inbox. Reconcile useful content into its current home according to meaning and intended use, preserving valuable concrete context and working references. Copying every old item would carry the same clutter into new directories.

`S#` and `MF#` remain supported graph resources, including imported instructions. Their presence is not a compatibility error. Preserve package-owned imports and relocate an instruction only when its ownership or use needs to change. Fixed Agent Corrections already have a current home and need no second case library.

Use the user-selected root, preserve unrelated edits, and retain a recoverable starting point. Inspect current validation findings and actual content, make the requested cleanup as a coherent change, then run `rightmemory validate --root <root>`. Report what changed and any unresolved evidence before retrying installation. Current active claims and unfinished tasks still use their normal recovery controls; historical cleanup is not permission to replace live work.

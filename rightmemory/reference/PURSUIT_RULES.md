# Pursuit Rules

## Purpose

A Pursuit is an ongoing direction the user wants represented in a hierarchical map. It preserves the direction's identity, place in the map, and the context needed to understand or enter it.

The map is user-owned. An unfinished task, newly mentioned idea, or available next step does not by itself establish a Pursuit. Project execution details belong in project documents, Git, tests, issues, experiments, and task conversations.

Pursuit is not a backlog, work log, task execution store, or record of completed work.

## Structure

```md
## <Title> {#<stable-id>} → [<optional typed edges>]

<Optional Markdown explaining meaning, scope, or stable entry context.>

### <Child direction> {#<stable-id>}
```

Every new map item is an addressable heading with a title, stable id, and tree position. The body is optional free-form Markdown. Meaning comes from the title, ancestry, body, and useful graph relationships; there are no node types or required progress, next-action, completion, or status fields.

Tree nesting expresses decomposition and supplies reading context. Add an edge for a useful relationship that containment does not already express. Short project, file, or person context may appear naturally in the nearest relevant ancestor body. Independently reusable context belongs in Memory when it passes Memory admission. This does not introduce fixed context fields or another registry.

Canonical storage is `PURSUITS.md` and its reachable `PURSUIT_<id>.md` detail files. Logical hierarchy continues recursively through `F#`; those boundaries arrange physical storage and carry no additional semantic meaning. The editor hides ids, heading depth, and document boundaries during ordinary map editing. The shared schema's `####` terminal-reference rule still applies inside each file.

## Ownership

There are two normal entrances for semantic map changes: the human-facing editor and the explicitly invoked `maintain-pursuit-map` workflow. Creating, deleting, renaming, merging, reorganizing, and focusing directions are decisions owned by the user.

A precise requested map edit is authorized directly. Broad cleanup or ambiguous reorganization requires a concise proposed tree change and approval before editing.

Update, ordinary orchestration, review, pruning, insight generation, and acting agents may read Pursuit as context. They do not infer, submit, or apply map changes from task progress, blockage, waiting, next steps, handoff, or completion. Sync repair may reconcile already-authorized map state within its bounded repair transaction; it does not choose new directions for the user.

## Focus

`## Focus` is an ordered list of Pursuit heading ids, each written in backticks. It marks current attention, not an execution queue, importance ranking, or permission to act.

The editor presents Focus as a small marker on the map. A direction can remain in the map without being focused. Removing a direction also removes its Focus reference.

## Removal

When the user decides a direction is completed, abandoned, or superseded, preserve only consequences that independently qualify for Memory, repair Focus and graph references, and remove the direction from the live map. Memory maintenance remains separately authorized; map deletion does not authorize unrelated Memory edits.

Git history preserves earlier map states. Do not accumulate completed, archived, or parked statuses. A user who wants to retain a direction for later may place it under a natural branch such as “Later” or explain the intended meaning in its body.

Deleting a map node removes its logical subtree. Repair typed edges targeting the deleted ids in both graph trees and remove unreachable Pursuit backing files; leave prose mentions and unrelated content unchanged.

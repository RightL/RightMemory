# RightMemory Design Notes

## Human-Owned Pursuit Map

### Ownership follows the kind of state

Memory preserves reusable context, and Update can judge whether new evidence improves that context. A Pursuit records a different decision: which directions the user wants to keep visible and how to group them. Task progress cannot reliably establish that decision. Keeping those judgments separate prevents incomplete work, transient blockers, and completed experiments from accumulating as a task ledger inside the map.

The human editor and explicitly requested `maintain-pursuit-map` workflow own semantic map changes. Other agents may use the map to understand the user's directions without acquiring authority to reorganize them. Update's bounded tools and accepted write surface enforce this distinction in code. Sync still reconciles already-authorized map state, because safe transport needs the complete graph rather than a second ownership model.

### The tree carries meaning without task fields

A title, ancestry, and optional Markdown note are enough to describe a direction. A shared ancestor can establish a project's starting document or repository without adding fixed project, person, or file fields. Existing graph edges represent real relationships when tree placement is insufficient. Reusable context remains ordinary Memory, and execution detail remains in project artifacts.

Completion is a user decision to remove a direction after considering any independently durable consequence. Git supplies the earlier state. A completed-status collection would turn the map into a work log, while a natural branch such as “Later” already expresses a direction the user wants to retain.

### The visual map is a view of Markdown

`PURSUITS.md` and reachable `PURSUIT_<id>.md` files remain authoritative. The logical tree comes from the canonical graph index, so retrieval, validation, sync, and editing agree about identity and ancestry. There is no second Pursuit database or task registry.

The canvas hides physical heading depth. When a deeper logical branch needs a file boundary, `F#` continues that hierarchy in a detail file. Existing boundaries stay stable to avoid unrelated rewrites. Pan, zoom, folding, and selection belong to browser-local view state and do not alter semantic Markdown.

### Confirmed actions are saved; editing batches become Git transactions

The canvas shows changes immediately. Each confirmed action is checked against the session's current editing state, validated in an isolated candidate, and durably recorded before the server acknowledges it. This recovery record belongs to operational editor state in `.runtime`; Markdown and Git remain the canonical Pursuit store. The active root stays clean while the user continues editing.

A short editing batch is the Git transaction boundary. Actual editing activity keeps the batch open, while an internal duration limit bounds its size. A pause or a boundary that needs canonical state flushes the batch: the store constructs the exact candidate from the captured base, validates the complete Memory/Pursuit graph, and publishes one commit while the active root still matches that base. A batch with no net file change creates no commit. Copy context, root changes, leaving the map, and normal service shutdown finish saved edits before proceeding.

Undo and redo follow user actions across these checkpoints. The session history keeps stable action identities and exact file states, so undoing a deletion can restore the subtree, backing files, Focus, and repaired typed edges together. Undoing an already published action becomes a later compensating checkpoint; shared Git history is never reset or rewritten. Deletion's mechanical reference repair remains distinct from authority to rewrite Memory meaning.

Recovery continues saved edits when the captured base is unchanged. If an external writer changes that base, the editor retains the recovery record and reports a conflict without replaying the actions or overwriting the newer root. A second browser session is refused write access while another session owns pending edits. This keeps acknowledged work recoverable without creating independently writable versions of the map.

### Context connects the map to Codex App

The map organizes the user's directions and their background. Codex App owns projects, tasks, conversations, and execution. Copying context is enough to carry a direction into an ordinary Codex App conversation without requiring a second conversation client, a host or project registry, or persistent links between tasks and Pursuit.

**Copy context** finishes pending edits, then uses the canonical graph index through one opening-context builder. It selects the current direction, direct incoming and outgoing neighbors, and their logical heading ancestors in the established order, with the direction's direct connections. The output presents their titles, prose, and readable relationships as background. Internal identifiers, source locations, and runtime metadata are unnecessary for this handoff. Generating that text is read-only and stores no conversation association; finishing pending edits may first create their Git checkpoint.

The pasted text describes the graph when copied. The user's accompanying request establishes what work to perform; context alone does not authorize a task. Later task progress, failure, or completion does not synchronize back into Pursuit or establish that a direction should be removed.

### Manager uses the existing ownership boundaries

Manager is the package-owned `rightmemory-manager` skill in Codex App. It handles explicit RightMemory management through `maintain-rightmemory` and `maintain-pursuit-map`, keeping their validated editing workflows and ownership boundaries. An explicit request authorizes the requested change, so Manager asks when a target or meaning cannot be resolved safely rather than adding a routine approval step. It refreshes and verifies canonical state after requested changes.

Manager also coordinates work through Codex App's native project and task tools when asked. Creating a separate task requires an explicit request or confirmation of a concrete proposal. The receiving task retains the user's objective and relevant context, with implementation choices left for its inspection of the project. This gives Manager both management and coordination responsibilities without a dedicated service, database, or runtime.

### Existing data remains readable

Old Pursuit field blocks are retained as body text so existing roots can open before a separate, explicitly approved cleanup. Their old action labels have no current control semantics. This is a narrow reading accommodation, not a second schema or an automatic migration. Installing or opening the editor does not rewrite a user's root.

The [Pursuit rules](rightmemory/reference/PURSUIT_RULES.md) own semantic meaning; the [schema](rightmemory/reference/rightmemory-schema.md) owns representation; the [Pursuit Map guide](docs/PURSUIT_MAP.md) covers use and implementation entry points.

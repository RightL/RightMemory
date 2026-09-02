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

### A confirmed interaction is the transaction boundary

Typing and pointer movement are local editing activity. Confirming a rename, structural move, note, deletion, or Focus change creates the candidate state. The store checks the expected revision, validates the complete candidate graph, and lands it only while the active root still matches the captured state. Failure leaves the active root unchanged.

This allows immediate canvas editing while preserving an inspectable Git change. Undo and redo create compensating commits and reject stale history; they do not reset the shared branch. Deletion can repair incoming typed edges in Memory, but that mechanical repair is not authority to rewrite Memory meaning.

### Context connects the map to Codex App

The map organizes the user's directions and their background. Codex App owns projects, tasks, conversations, and execution. Copying context is enough to carry a direction into an ordinary Codex App conversation without requiring a second conversation client, a host or project registry, or persistent links between tasks and Pursuit.

**Copy context** uses the canonical graph index through one opening-context builder. It selects the current direction, direct incoming and outgoing neighbors, and their logical heading ancestors in the established order, with the direction's direct connections. The output presents their titles, prose, and readable relationships as background. Internal identifiers, source locations, and runtime metadata are unnecessary for this handoff. Generating that text is read-only and creates no Git commit or stored conversation association.

The pasted text describes the graph when copied. The user's accompanying request establishes what work to perform; context alone does not authorize a task. Later task progress, failure, or completion does not synchronize back into Pursuit or establish that a direction should be removed.

### Manager uses the existing ownership boundaries

Manager is the package-owned `rightmemory-manager` skill in Codex App. It handles explicit RightMemory management through `maintain-rightmemory` and `maintain-pursuit-map`, keeping their validated editing workflows and ownership boundaries. An explicit request authorizes the requested change, so Manager asks when a target or meaning cannot be resolved safely rather than adding a routine approval step. It refreshes and verifies canonical state after requested changes.

Manager also coordinates work through Codex App's native project and task tools when asked. Creating a separate task requires an explicit request or confirmation of a concrete proposal. The receiving task retains the user's objective and relevant context, with implementation choices left for its inspection of the project. This gives Manager both management and coordination responsibilities without a dedicated service, database, or runtime.

### Existing data remains readable

Old Pursuit field blocks are retained as body text so existing roots can open before a separate, explicitly approved cleanup. Their old action labels have no current control semantics. This is a narrow reading accommodation, not a second schema or an automatic migration. Installing or opening the editor does not rewrite a user's root.

The [Pursuit rules](rightmemory/reference/PURSUIT_RULES.md) own semantic meaning; the [schema](rightmemory/reference/rightmemory-schema.md) owns representation; the [Pursuit Map guide](docs/PURSUIT_MAP.md) covers use and implementation entry points.

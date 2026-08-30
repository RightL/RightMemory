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

### Conversation attachments are operational state

A Codex conversation can be displayed beside one Pursuit without becoming part of the Pursuit graph. RightMemory stores the attachment, host, and working-directory project below the active root's `.runtime` directory. Codex App Server remains authoritative for the thread and turn protocol. Deleting a Pursuit does not delete its conversation, and conversation events do not infer completion, Focus, or map edits.

RightMemory opens its own App Server process locally or through a named SSH host. The conversation workspace speaks that protocol directly rather than routing turns through the Codex SDK, and it does not share live ownership with a task currently open in the Codex desktop app. Registered working directories form RightMemory's project layer because the stable protocol needed for conversation control is available across Codex versions while native desktop Project methods are experimental and version-specific.

Composer inputs follow the same boundary. Pasted PNG and JPEG images and large text are staged in root-local runtime state, not embedded in Pursuit Markdown. Before a remote turn starts, RightMemory copies staged inputs to the conversation's SSH host so App Server and its tools receive paths that exist in their own environment. These files support a conversation turn; they are not graph resources or durable semantic evidence by themselves.

App Server thread items are also the source for visible subagent activity. The browser presents those items with the turn's other work details instead of guessing agent activity from prose. Side chats use separate App Server threads but remain scoped to the current browser/app session. They are not saved as durable Pursuit attachments and do not enter the map's conversation summaries.

A Pursuit node may show the aggregate state of its attached conversations: active work, a waiting request, an unread final answer, or a completed turn. This is a navigation and attention aid. In particular, a completed conversation turn does not mean the Pursuit is semantically complete; only an explicit map edit changes what directions the user keeps visible.

### Existing data remains readable

Old Pursuit field blocks are retained as body text so existing roots can open before a separate, explicitly approved cleanup. Their old action labels have no current control semantics. This is a narrow reading accommodation, not a second schema or an automatic migration. Installing or opening the editor does not rewrite a user's root.

The [Pursuit rules](rightmemory/reference/PURSUIT_RULES.md) own semantic meaning; the [schema](rightmemory/reference/rightmemory-schema.md) owns representation; the [Pursuit Map guide](docs/PURSUIT_MAP.md) covers use and implementation entry points.

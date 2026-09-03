# Pursuit Map

The Pursuit map is a visual editor for directions you want to keep visible. A node normally has a title and, when useful, a Markdown note. Its place in the tree explains how it relates to broader directions. It has no task status, required next action, or execution link.

## Open The Map

```bash
rightmemory pursuit
```

This starts or reuses the existing Web Studio and opens its Pursuit Map view. Use `rightmemory pursuit --no-open` to print the address without opening a browser, or `rightmemory --profile <name> pursuit` for a named profile. The command does not create a separate service.

Web Studio accepts loopback hosts only, so another device cannot reach it directly. Opening the page automatically creates a signed local browser session and enables request protection without asking for a credential. Each browser session has its own active-root selection; the session is not remote-access authentication.

Check the active root before editing. The map uses the current browser session's selected root. Starting the service or opening the map does not rewrite Pursuit files.

## Use A Direction In Codex App

Right-click a direction or open its **More** menu and choose **Copy context**. Paste the Markdown into an ordinary Codex App task or conversation alongside the work you want done. The copied text is background from the map; it does not ask the agent to perform a task by itself.

Context comes from the current canonical graph. It contains the selected direction's title and prose, its direct incoming and outgoing neighbors, those blocks' logical heading ancestors, and readable direct connections. Selection and ordering use the shared opening-context builder. It does not expand second-hop edges, resource backings, Focus, sibling nodes, or child subtrees. The text has no generated block identifiers, roles, source locations, revision, Memory-root path, or execution metadata. Copying does not edit Pursuit, create a Git commit, or record a conversation association.

To coordinate work for a direction over time, set up one long-lived Manager conversation in Codex App:

```text
Pursuit Map
→ Copy context
→ create a Codex App conversation
→ invoke RightMemory Manager
→ paste the copied context
→ name and pin the conversation
```

Use **Manager**, the installed `rightmemory-manager` skill in Codex App, as this stable coordination point. Manager keeps an integrated understanding of the direction, prior work, and open questions, helps decide what should happen next, creates or coordinates separate temporary worker tasks, and synthesizes their outcomes. Substantial investigation, implementation, experimentation, and debugging belong in worker tasks; Manager creates a separate task only when explicitly requested or confirmed.

Codex App owns conversations, tasks, and pinning. RightMemory stores no Manager-conversation association or task status. Worker task progress, failure, or completion does not change the map; a Pursuit or Memory change remains an explicit user decision using the appropriate maintenance workflow.

## Edit On The Canvas

Each top-level direction is the root of its own map on a shared canvas. Independent maps appear in stored order, with space between them and no shared parent node. A map with several first-level branches alternates them left and right, starting on the left; a single branch extends right. Descendants continue on their branch's side.

Long titles wrap within their nodes. Titles support bold (`**Important**`), underline (`<u>Important</u>`), and strikethrough (`~~Earlier approach~~`), including nested marks and manually authored partial formatting. Renaming opens the original title syntax. Only these balanced forms are rendered; unmatched delimiters and all other HTML, including tags with attributes, remain literal text. Strikethrough is presentation, not a task status or an instruction to an agent.

| Action | Interaction |
| --- | --- |
| Select a node | Click its title. |
| Copy a direction's context | Right-click its node or open **More**, then choose **Copy context**. |
| Select several enclosed nodes | Hold `Shift` and drag a rectangle from empty canvas space. |
| Add enclosed nodes to the selection | Hold `Ctrl/Cmd+Shift` and drag from empty canvas space. |
| Add or remove one node | Hold `Ctrl/Cmd` and click its title. |
| Rename | Double-click the title or press `F2`; edit in place. |
| Create a sibling | Press `Enter` and type the title. |
| Insert a sibling before | Press `Shift+Enter` and type the title. |
| Reorder siblings | Press `Alt+ArrowUp` or `Alt+ArrowDown`; this also works for top-level directions. |
| Create a child | Press `Tab` and type the title. |
| Bold, underline, or strike selected topics | Use **B**, **U**, or **S** beside the active node, or `Ctrl/Cmd+B`, `Ctrl/Cmd+U`, or `Ctrl/Cmd+Shift+X`. One action and one Undo apply to the complete selection. |
| Promote a branch | Press `Shift+Tab`. |
| Move or reorder a branch | Drag it to the indicated insertion position, including across independent maps. Dragging moves that branch only, even when several nodes were selected. |
| Make a branch top-level | Drop it on empty canvas space. |
| Delete a branch | Press `Delete` or `Backspace`; only the active branch is deleted, and undo is available after deletion. |
| Fold a branch | Use its small branch control or press `Space`. |
| Navigate the tree | Left/right follow the branch's side; up/down traverse visible nodes across maps. `Home` selects the first root; `End` selects the last visible node. |
| Search titles and notes | Press `Ctrl/Cmd+F`; `Enter` and `Shift+Enter` move through results. |
| Fit the map | Use Fit or `Ctrl/Cmd+0`. |
| Undo or redo | Use `Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z`, or `Ctrl/Cmd+Y` for redo. |
| Edit a note | Open its note control or press `N`. |
| Mark current attention | Toggle its Focus marker or press `F`. |

New nodes receive stable ids automatically. Renaming or moving an anchored node preserves its id. Formatting an editable plain heading assigns it a stable id in the same transaction. Physical files, heading depth, and graph syntax stay out of the normal editing controls.

Every selected node has a visible selection state; the active node has the nearby toolbar for whole-topic formatting, Note, Focus, and **More**. Click blank canvas space or outside the map to clear the selection and hide this toolbar; dragging blank space still pans without clearing the selection. A normal node click returns to a single selection. Formatting buttons show whether a mark wraps every selected title, none of them, or a mixture. Applying a mark to a mixed selection adds it to all selected titles while preserving partial formatting inside each title. Finish raw title editing before using formatting shortcuts: they are suppressed while the title editor is open so the browser cannot insert rich-text HTML into it.

Right-click a node or choose its **More** button for **Copy context**, structural actions, formatting, notes, Focus, folding, promotion, and deletion. Right-click empty canvas space for top-level creation or Fit. Menus support arrow keys and close with Escape or an outside click. The fixed toolbar retains broad map operations; its **More** also includes the read-only relation summary. An empty map offers an **Add a direction** button.

Drag empty canvas space or use the wheel/trackpad to pan; use the zoom buttons or `Ctrl/Cmd` with the wheel to zoom. Dragging a branch near a canvas edge pans the view; hovering over the middle of a collapsed destination for about two-thirds of a second expands it. Hover expansion changes only the browser view and creates no Git commit. Escape cancels a drag. Touch pans even when it starts on a label; two fingers pinch to zoom. Touch gestures do not move directions; structural dragging uses a mouse or pen. Fit includes every independent map.

The note editor holds raw Markdown, not a set of generated task fields. Save with its button or `Ctrl/Cmd+S`. Closing the panel also saves; a failed save leaves it open with the text intact. Keep stable context near the direction it explains, or in a shared ancestor when it applies to a whole branch. Detailed progress, commands, test output, and experiment history belong in project artifacts.

Focus is an ordered attention marker. It does not grant permission to execute work or turn the map into a queue. A direction can remain visible without being focused.

## Saving, Conflicts, And Undo

The canvas shows an edit immediately and sends confirmed changes in order. Typing a title or dragging a pointer does not create a Git commit for every intermediate step. A confirmed semantic interaction produces a validated Git change; the returned snapshot is authoritative.

Every write carries the revision it was based on. If another editor, sync, or an agent changes the root, a stale operation is rejected and the current snapshot is returned. The interface reports the conflict rather than silently overwriting the newer state. Failed note and title edits remain available for review and retry in the current page. These drafts are temporary, not stored in a separate database; preserve any needed text before closing or reloading the page.

Undo and redo create new commits. They never reset the shared branch or erase another writer's history. The service accepts only history associated with the current editing session and rejects operations whose expected state no longer matches. The browser's undo stack lasts for the current page and is cleared after an external revision change or conflict; Git still preserves the commits.

The active root must be clean and valid for a map transaction. Unrelated dirty files are not absorbed into map commits. Candidate edits run in a temporary worktree, validate against the complete Memory/Pursuit graph, and land only while the active root still matches the captured state. A rejected operation leaves active files unchanged.

The editor also refuses roots whose Git settings could conceal or rewrite map data: `assume-unchanged` or `skip-worktree` index flags, ignored untracked canonical files, and checkout or filter settings that would alter existing or intended file bytes. The refusal explains the condition; the editor does not change Git configuration or normalize those files automatically.

Deleting a node removes its entire logical subtree, including unreachable Pursuit detail files. The transaction also removes deleted ids from Focus and typed edges that target them in either Memory or Pursuit. It leaves prose mentions untouched and reports repaired references. This mechanical reference repair does not authorize rewriting Memory content.

## Canonical Storage

The map remains ordinary RightMemory Markdown:

```md
# Pursuits

## Focus

- `example-application`

## Example Application {#example-application}

Start from the repository's current design document.

### Simpler Onboarding {#example-onboarding}
```

`PURSUITS.md` and reachable `PURSUIT_<id>.md` files are the source of truth. The editor reads the canonical graph index instead of maintaining a separate database. Deep logical branches continue through `F#` detail files so each physical file can obey the shared heading rules. The renderer creates required boundaries automatically and preserves existing boundaries where possible.

Pan, zoom, folding, and single or multiple selection are browser-local view state. They are not stored in Markdown and do not affect retrieval.

Whole-topic formatting stays in the title string, with no style fields or separate metadata. Combined marks use a fixed order: bold outside underline outside strikethrough, such as `**<u>~~Important direction~~</u>**`. One formatting action sends the selected titles through one compound rename transaction, so the group is one Undo step. It preserves body, edges, Focus, backing files, and sibling order; anchored node ids also remain unchanged. New stable ids use visible title text, and a title with no visible text is rejected. Existing unusual titles remain readable.

Existing old field blocks remain readable as body text until the user explicitly requests cleanup. Their former `do`, `ask`, and `wait` labels do not control agent actions. Existing plain structural headings and graph-node bullets are preserved; content that cannot be safely edited through the normal map controls is surfaced as a read-only item or diagnostic. Opening the map is not a migration.

When a direction is completed, abandoned, or superseded, remove it through an explicit map edit after considering any independently durable consequence for Memory. Earlier map states remain in Git. A natural branch such as “Later” can express something you still want visible; there is no archived or parked status system.

## Agent Ownership

The human editor and an explicit `maintain-pursuit-map` request are the two normal semantic write entrances. A request to change the map authorizes that change. The maintenance workflow asks only when a missing target or materially different interpretation cannot be resolved safely from canonical state; it does not add a routine proposal-and-approval step.

Update, ordinary orchestration, transcript review, and Memory maintenance may read Pursuit. They do not infer, submit, or apply map changes from progress, unfinished work, new ideas, or completion. Update can retain independently durable evidence from a mixed candidate while reporting its Pursuit portion as skipped. Sync repair can reconcile already-authorized map state within its existing bounded repair workflow.

Both install modes include five skills: `rightmemory-auto-orchestrator`, `maintain-pursuit-map`, `maintain-rightmemory`, `review-agent-guidance-inbox`, and `rightmemory-manager`. The three maintenance skills and Manager are available alongside ordinary orchestration. The maintenance skills do not invoke one another or exchange queued map work; Manager uses the workflow that owns the requested surface when the user explicitly asks to change RightMemory.

## Implementation Entry Points

| Area | Source |
| --- | --- |
| Meaning and ownership | [Pursuit rules](../rightmemory/reference/PURSUIT_RULES.md) |
| Graph grammar and index | [graph.py](../rightmemory/graph.py), [shared schema](../rightmemory/reference/rightmemory-schema.md) |
| Logical tree and Markdown rendering | [pursuit_tree.py](../rightmemory/pursuit_tree.py) |
| Transactions, revisions, and history | [pursuit_store.py](../rightmemory/pursuit_store.py) |
| Existing Web service and map routes | [web/app.py](../rightmemory/web/app.py), [web/service.py](../rightmemory/web/service.py) |
| Browser source and static build | [web/frontend](../rightmemory/web/frontend/), [web/static](../rightmemory/web/static/) |
| Context selection and Markdown output | [opening_context.py](../rightmemory/opening_context.py) |
| Manager in Codex App | [rightmemory-manager](../skills/rightmemory-manager/SKILL.md) |
| Direct agent editing | [maintain-pursuit-map](../skills/maintain-pursuit-map/SKILL.md) |

The internal API is `GET /api/pursuit-map`, `GET /api/pursuit-map/context?item_id=...&expected_revision=...`, `POST /api/pursuit-map/operations`, and the corresponding `/undo` and `/redo` endpoints. The context endpoint returns generated Markdown without writing state. Mutations use the current browser session, active-root selection, and CSRF protection. A revision conflict returns HTTP `409` with a fresh snapshot. These routes serve the editor; there is no public create/edit/move command family or task-execution API.

For frontend changes, install locked dependencies and run the focused checks from `rightmemory/web/frontend`:

```bash
npm ci
npm run typecheck
npm test
npm run build
```

The production build is shipped with the Python package, so end users do not need Node or runtime CDN access. For executable changes, also run the repository checks:

```bash
python -m tests
python -m compileall -q rightmemory tests
```

Use disposable roots for browser and destructive transaction checks. Agent-facing rules, prompts, skill wording, and copied context text are reviewed directly rather than asserted by tests; context tests cover structured selection and the surrounding read-only behavior. `.xmind` interchange, typed-edge editing controls, and task reconciliation remain outside the map.

For visible browser checks, run `npm run dev` from the frontend directory and open the printed loopback address. **Run interaction checks** exercises the bundled UI with disposable in-memory data, including formatting payloads and history, menus, keyboard operations, hover expansion, auto-pan cancellation, HTML escaping, read-only controls, and a 500-direction fixture. The synthetic pointer checks replace only pointer-capture ownership; check a real mouse/pen drag as well. No real Memory root is used.

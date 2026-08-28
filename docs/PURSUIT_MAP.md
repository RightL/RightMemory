# Pursuit Map

The Pursuit map is a visual editor for directions you want to keep visible. A node normally has a title and, when useful, a Markdown note. Its place in the tree explains how it relates to broader directions. It has no task status, required next action, or execution link.

## Open The Map

```bash
rightmemory pursuit
```

This starts or reuses the existing Web Studio and opens its Pursuit Map view. Use `rightmemory pursuit --no-open` to print the address without opening a browser, or `rightmemory --profile <name> pursuit` for a named profile. The command does not create a separate service or a second authentication system.

Sign in with the Web Studio operator token and check the active root before editing. A newly generated token is printed by the launch command; an existing service keeps its existing token and sessions. The map uses that authenticated session's selected root. Starting the service or opening the map does not rewrite Pursuit files.

## Edit On The Canvas

Each top-level direction is the root of its own map on a shared canvas. Independent maps appear in stored order, with space between them and no shared parent node. A map with several first-level branches alternates them left and right, starting on the left; a single branch extends right. Descendants continue on their branch's side.

Long titles wrap within their nodes. Markdown strikethrough such as `~~Earlier approach~~` is shown as crossed-out text; renaming opens the original Markdown. This is title formatting, not a task status, and HTML in titles remains literal text.

| Action | Interaction |
| --- | --- |
| Select a node | Click its title. |
| Rename | Double-click the title or press `F2`; edit in place. |
| Create a sibling | Press `Enter` and type the title. |
| Create a child | Press `Tab` and type the title. |
| Promote a branch | Press `Shift+Tab`. |
| Move or reorder a branch | Drag it to the indicated insertion position, including across independent maps. |
| Make a branch top-level | Drop it on empty canvas space. |
| Delete a branch | Press `Delete` or `Backspace`; undo is available after deletion. |
| Fold a branch | Use its small branch control or press `Space`. |
| Navigate the tree | Left/right follow the branch's side; up/down traverse visible nodes across maps. `Home` selects the first root; `End` selects the last visible node. |
| Search titles and notes | Press `Ctrl/Cmd+F`; `Enter` and `Shift+Enter` move through results. |
| Fit the map | Use Fit or `Ctrl/Cmd+0`. |
| Undo or redo | Use `Ctrl/Cmd+Z`, `Ctrl/Cmd+Shift+Z`, or `Ctrl/Cmd+Y` for redo. |
| Edit a note | Open its note control, press `N`, or press `Shift+Enter`. |
| Mark current attention | Toggle its Focus marker or press `F`. |

New nodes receive stable ids automatically. Renaming or moving a node preserves its id. Physical files, heading depth, and graph syntax stay out of the normal editing controls.

Toolbar controls provide the same editing actions. **More** includes top-level creation and the read-only relation summary. An empty map offers an **Add a direction** button. Drag empty canvas space or use the wheel/trackpad to pan; use the zoom buttons or `Ctrl/Cmd` with the wheel to zoom. Touch pans even when it starts on a label; two fingers pinch to zoom. Touch gestures do not move directions; structural dragging uses a mouse or pen. Fit includes every independent map.

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

Pan, zoom, folding, and selection are browser-local view state. They are not stored in Markdown and do not affect retrieval.

Existing old field blocks remain readable as body text until the user explicitly requests cleanup. Their former `do`, `ask`, and `wait` labels do not control agent actions. Existing plain structural headings and graph-node bullets are preserved; content that cannot be safely edited through the normal map controls is surfaced as a read-only item or diagnostic. Opening the map is not a migration.

When a direction is completed, abandoned, or superseded, remove it through an explicit map edit after considering any independently durable consequence for Memory. Earlier map states remain in Git. A natural branch such as “Later” can express something you still want visible; there is no archived or parked status system.

## Agent Ownership

The human editor and an explicit `maintain-pursuit-map` request are the two normal semantic write entrances. A precise request such as “Rename this map branch to Research” authorizes that edit. Broad requests such as “Simplify my map” first produce a concise proposed tree change for approval.

Update, ordinary orchestration, transcript review, and Memory maintenance may read Pursuit. They do not infer, submit, or apply map changes from progress, unfinished work, new ideas, or completion. Update can retain independently durable evidence from a mixed candidate while reporting its Pursuit portion as skipped. Sync repair can reconcile already-authorized map state within its existing bounded repair workflow.

Both install modes include `maintain-pursuit-map` alongside `maintain-rightmemory` and `review-agent-guidance-inbox`. These are independent skills, not alternatives to ordinary orchestration. They do not invoke one another or exchange queued map work.

## Implementation Entry Points

| Area | Source |
| --- | --- |
| Meaning and ownership | [Pursuit rules](../rightmemory/reference/PURSUIT_RULES.md) |
| Graph grammar and index | [graph.py](../rightmemory/graph.py), [shared schema](../rightmemory/reference/rightmemory-schema.md) |
| Logical tree and Markdown rendering | [pursuit_tree.py](../rightmemory/pursuit_tree.py) |
| Transactions, revisions, and history | [pursuit_store.py](../rightmemory/pursuit_store.py) |
| Existing Web service and map routes | [web/app.py](../rightmemory/web/app.py), [web/service.py](../rightmemory/web/service.py) |
| Browser source and static build | [web/frontend](../rightmemory/web/frontend/), [web/static](../rightmemory/web/static/) |
| Direct agent editing | [maintain-pursuit-map](../skills/maintain-pursuit-map/SKILL.md) |

The internal API is `GET /api/pursuit-map`, `POST /api/pursuit-map/operations`, and the corresponding `/undo` and `/redo` endpoints. Mutations use the existing authenticated session, active-root selection, and CSRF protection. A revision conflict returns HTTP `409` with a fresh snapshot. These routes serve the editor; there is no public create/edit/move command family or task-execution API.

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

Use disposable roots for browser and destructive transaction checks. Agent-facing rules, prompts, and skill wording are reviewed directly rather than asserted by tests. `.xmind` interchange, typed-edge editing controls, task registries, task execution, and task reconciliation are outside this map's scope.

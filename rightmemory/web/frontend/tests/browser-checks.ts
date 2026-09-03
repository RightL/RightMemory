import { mountMap, type MountMapOptions, type PursuitMapController } from '../src/pursuit-map.ts';
import { applyOperation, indexTree, type Operation, type Snapshot } from '../src/tree.ts';
import { ApiError, type Transport } from '../src/queue.ts';
import { forestFixture } from './fixtures.ts';

function check(value: unknown, message: string): asserts value { if (!value) throw new Error(message); }
const pause = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
async function until(predicate: () => unknown, message: string): Promise<void> {
  const start = performance.now();
  while (!predicate()) { if (performance.now() - start > 4000) throw new Error(message); await pause(); }
}

/** Runs the real bundled controller/renderer in a visible disposable fixture.
 * Synthetic pointers cannot acquire native capture, so only capture ownership is
 * replaced here. Hit testing, layout, timers, events, and mutations remain real.
 */
export async function runBrowserChecks(host: HTMLElement, report: (line: string) => void): Promise<PursuitMapController | undefined> {
  let current: Snapshot;
  let serial = 0;
  let loads = 0;
  let flushes = 0;
  let failNext = false;
  let heldSave: Promise<void> | undefined;
  const operations: Operation[] = [];
  const contextRequests: Array<{ itemId: string; revision: string }> = [];
  const clipboardWrites: string[] = [];
  const copiedMarker = `fixture-context-${crypto.randomUUID()}`;
  let contextError: Error | undefined;
  let clipboardError: Error | undefined;
  const mapOptions: MountMapOptions = {
    context: async (itemId, revision) => {
      contextRequests.push({ itemId, revision });
      await pause();
      if (contextError) throw contextError;
      check(revision === current.revision, 'Context requests use the saved map revision');
      check(indexTree(current).has(itemId), 'Context requests use a saved item id');
      check(!current.pending, 'Context requests follow the pending checkpoint flush');
      return copiedMarker;
    },
    writeClipboard: async (text) => {
      if (clipboardError) throw clipboardError;
      clipboardWrites.push(text);
    },
  };
  type IdRemap = { from: string; to: string };
  const actions = new Map<string, { before: Snapshot; after: Snapshot; remaps: IdRemap[] }>();
  const remapSnapshot = (snapshot: Snapshot, remaps: readonly IdRemap[]): Snapshot => {
    const ids = new Map(remaps.map(({ from, to }) => [from, to]));
    const mapped = (id: string) => ids.get(id) ?? id;
    return {
      ...snapshot,
      items: snapshot.items.map((item) => ({
        ...item,
        id: mapped(item.id),
        parent_id: item.parent_id === null ? null : mapped(item.parent_id),
        child_ids: item.child_ids.map(mapped),
        edges: item.edges.map(([kind, target]) => [kind, mapped(target)]),
      })),
      root_ids: snapshot.root_ids.map(mapped),
      focus_ids: snapshot.focus_ids.map(mapped),
    };
  };
  let controller: PursuitMapController | undefined;
  const captured = new Map<number, Element>();
  const names = ['setPointerCapture', 'releasePointerCapture', 'hasPointerCapture'] as const;
  const descriptors = names.map((name) => Object.getOwnPropertyDescriptor(HTMLElement.prototype, name));
  Object.defineProperties(HTMLElement.prototype, {
    setPointerCapture: { configurable: true, value(this: Element, id: number) { captured.set(id, this); } },
    releasePointerCapture: { configurable: true, value(this: Element, id: number) { if (captured.get(id) === this) captured.delete(id); } },
    hasPointerCapture: { configurable: true, value(this: Element, id: number) { return captured.get(id) === this; } },
  });
  const transport: Transport = {
    load: async () => { loads++; return structuredClone(current); },
    mutate: async (revision, operation) => {
      operations.push(operation);
      const waiting = heldSave; heldSave = undefined;
      if (waiting) await waiting;
      await pause();
      if (failNext) { failNext = false; throw new ApiError('Fixture conflict', 409, structuredClone(current)); }
      check(revision === current.revision, 'Mutation revision must follow the last response');
      const id = operation.type === 'create' ? `created-${serial}`
        : operation.type === 'rename_many' ? operation.renames[0]?.id ?? null : operation.id;
      const before = structuredClone(current);
      const remaps = operation.type === 'rename_many'
        ? operation.renames.filter((rename) => rename.id.startsWith('plain:'))
          .map((rename, index) => ({ from: rename.id, to: `promoted-${serial + 1}-${index}` }))
        : [];
      const operation_id = `operation-${++serial}`;
      current = {
        ...remapSnapshot(applyOperation(current, operation, id), remaps), revision: `r${serial}`, pending: true,
        history: { undo: [...before.history.undo, operation_id], redo: [] },
      };
      actions.set(operation_id, { before, after: structuredClone(current), remaps });
      const selected = remaps.find((mapping) => mapping.from === id)?.to ?? id;
      return { snapshot: structuredClone(current), commit: null, operation_id, repaired_references: [], undoable: true, selected_id: selected, id_remaps: remaps };
    },
    history: async (kind, revision, operation_id) => {
      check(revision === current.revision, 'History uses the last saved revision');
      const action = actions.get(operation_id);
      check(action && current.history[kind].at(-1) === operation_id, 'History references the last available action');
      const history = structuredClone(current.history);
      history[kind].pop();
      history[kind === 'undo' ? 'redo' : 'undo'].push(operation_id);
      current = {
        ...structuredClone(kind === 'undo' ? action.before : action.after),
        revision: `r${++serial}`, git_head: current.git_head, pending: true, history,
      };
      const remaps = kind === 'undo' ? action.remaps.map(({ from, to }) => ({ from: to, to: from })) : action.remaps;
      return { snapshot: structuredClone(current), commit: null, operation_id, repaired_references: [], undoable: true, selected_id: null, id_remaps: remaps };
    },
    flush: async (revision) => {
      flushes++;
      check(revision === current.revision, 'Checkpoint flush uses the last saved revision');
      const commit = current.pending ? `c${++serial}` : null;
      if (commit) current = { ...current, revision: `r${serial}`, git_head: commit, pending: false };
      return { snapshot: structuredClone(current), commit, operation_id: '', repaired_references: [], undoable: false, selected_id: null, id_remaps: [] };
    },
    activity: async () => {},
  };
  const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => {
    const element = host.querySelector<T>(selector); check(element, `Missing ${selector}`); return element;
  };
  const topic = (id: string): HTMLElement => {
    const element = document.getElementById(`pm-node-${id}`);
    check(element && host.contains(element), `Missing topic ${id}`);
    return element;
  };
  const selectedIds = () => [...host.querySelectorAll<HTMLElement>('me-tpc[aria-selected="true"]')]
    .map((node) => node.id.slice('pm-node-'.length));
  const checkSelected = (expected: string[], message: string) => {
    check(JSON.stringify(selectedIds().sort()) === JSON.stringify([...expected].sort()), message);
  };
  const button = (command: string, scope = '.pm-toolbar') => $<HTMLButtonElement>(`${scope} [data-command="${command}"]`);
  const key = (name: string, options: KeyboardEventInit = {}, target = $('.pm-canvas')) => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...options }));
  };
  const pointer = (target: HTMLElement, type: string, x: number, y: number, buttons = 1, options: PointerEventInit = {}) => {
    target.dispatchEvent(new PointerEvent(type, { pointerId: 1, pointerType: 'mouse', button: 0, buttons, clientX: x, clientY: y, bubbles: true, cancelable: true, ...options }));
  };
  const select = (id: string, options: PointerEventInit = {}) => {
    const node = topic(id); const rect = node.getBoundingClientRect();
    pointer(node, 'pointerdown', rect.x + rect.width / 2, rect.y + rect.height / 2, 1, options);
    pointer(node, 'pointerup', rect.x + rect.width / 2, rect.y + rect.height / 2, 0, options);
  };
  const click = (target: HTMLElement) => {
    const rect = target.getBoundingClientRect();
    pointer(target, 'pointerdown', rect.right - 8, rect.bottom - 8);
    pointer(target, 'pointerup', rect.right - 8, rect.bottom - 8, 0);
    target.click();
  };
  const settled = () => until(() => !controller!.hasUnsavedChanges, 'The save queue did not settle');
  const reset = async (count = 22, prepare?: (snapshot: Snapshot) => Snapshot) => {
    await controller?.destroy();
    const fixture = { ...forestFixture(count), root_key: `interaction-check-${crypto.randomUUID()}` };
    current = prepare ? prepare(fixture) : fixture;
    actions.clear();
    operations.length = 0;
    contextRequests.length = 0;
    clipboardWrites.length = 0;
    contextError = undefined;
    clipboardError = undefined;
    controller = await mountMap(host, transport, mapOptions);
    await pause();
  };
  try {
    await reset();
    select('design');
    const selections: Array<string | null> = [];
    const unsubscribeSelection = controller!.subscribeSelection((id) => selections.push(id));
    check(selections.at(-1) === 'design', 'The selection facade reports the selected Pursuit');
    unsubscribeSelection();
    click($('.pm-canvas'));
    check($('.pm-topic-toolbar').hidden && !host.querySelector('[aria-selected="true"]'), 'A blank click dismisses the toolbar and clears selection');
    check(!$('.pm-canvas').hasAttribute('aria-activedescendant'), 'A cleared selection has no active tree item');
    await controller!.refresh();
    check($('.pm-topic-toolbar').hidden && !host.querySelector('[aria-selected="true"]'), 'Refreshing must not restore a dismissed selection');
    await controller!.destroy(); controller = await mountMap(host, transport, mapOptions); await pause();
    check($('.pm-topic-toolbar').hidden, 'Reopening the same map preserves the cleared selection');
    key('ArrowDown');
    check(topic('directions').getAttribute('aria-selected') === 'true', 'Keyboard navigation can select again after dismissal');
    select('design');
    const panCanvas = $('.pm-canvas'); const panRect = panCanvas.getBoundingClientRect();
    const panBefore = $('.pm-forest').style.transform;
    pointer(panCanvas, 'pointerdown', panRect.right - 12, panRect.bottom - 12);
    pointer(panCanvas, 'pointermove', panRect.right - 52, panRect.bottom - 52);
    pointer(panCanvas, 'pointerup', panRect.right - 52, panRect.bottom - 52, 0);
    panCanvas.click();
    check($('.pm-forest').style.transform !== panBefore, 'Dragging blank space still pans');
    check(topic('design').getAttribute('aria-selected') === 'true' && !$('.pm-topic-toolbar').hidden, 'A click emitted after panning must keep the selection');
    click(document.body);
    check($('.pm-topic-toolbar').hidden && !host.querySelector('[aria-selected="true"]'), 'Clicks outside the map dismiss the toolbar');

    await reset();
    const marquee = async (ids: string[], additive = false, reverse = false) => {
      const rects = ids.map((id) => topic(id).getBoundingClientRect());
      const bounds = {
        left: Math.min(...rects.map((rect) => rect.left)) - 8,
        top: Math.min(...rects.map((rect) => rect.top)) - 8,
        right: Math.max(...rects.map((rect) => rect.right)) + 8,
        bottom: Math.max(...rects.map((rect) => rect.bottom)) + 8,
      };
      const start = reverse ? { x: bounds.right, y: bounds.bottom } : { x: bounds.left, y: bounds.top };
      const end = reverse ? { x: bounds.left, y: bounds.top } : { x: bounds.right, y: bounds.bottom };
      const modifiers = { shiftKey: true, ctrlKey: additive };
      const canvas = $('.pm-canvas');
      pointer(canvas, 'pointerdown', start.x, start.y, 1, modifiers);
      pointer(canvas, 'pointermove', end.x, end.y, 1, modifiers);
      await pause();
      const rectangle = host.querySelector<HTMLElement>('.pm-selection-rectangle');
      check(rectangle, 'Shift-drag shows a marquee rectangle while selecting');
      const rectangleBox = rectangle.getBoundingClientRect();
      const rectangleStyle = getComputedStyle(rectangle);
      check(rectangleBox.width > 8 && rectangleBox.height > 8 && rectangleStyle.visibility !== 'hidden'
        && rectangleStyle.display !== 'none' && parseFloat(rectangleStyle.borderTopWidth) > 0,
      'The marquee has visible, nonzero geometry');
      pointer(canvas, 'pointerup', end.x, end.y, 0, modifiers);
      check(!host.querySelector('.pm-selection-rectangle'), 'The marquee rectangle is removed after selection');
    };
    select('design');
    const primaryUpdates: Array<string | null> = [];
    const unsubscribePrimary = controller!.subscribeSelection((id) => primaryUpdates.push(id));
    select('research', { ctrlKey: true });
    checkSelected(['design', 'research'], 'Ctrl-click adds a secondary direction to the selection');
    check(controller!.getSelectedId() === 'design' && primaryUpdates.length === 1, 'Adding a secondary direction preserves the primary selection');
    select('research', { ctrlKey: true });
    checkSelected(['design'], 'Ctrl-click removes a secondary direction without disturbing the primary selection');
    check(primaryUpdates.length === 1, 'Toggling a secondary direction does not notify the singular selection facade');
    unsubscribePrimary();
    select('writing');
    await marquee(['design', 'interaction']);
    checkSelected(['design', 'interaction'], 'Shift-drag selects exactly the fully enclosed directions');
    check(topic('visual').getAttribute('aria-selected') === 'false', 'A partially intersecting direction is excluded from the marquee');
    check(topic('writing').getAttribute('aria-selected') === 'false', 'Replacement marquee drops the previous selection');
    const firstMarqueeIds = [...host.querySelectorAll<HTMLElement>('me-tpc[aria-selected="true"]')].map((node) => node.id.slice('pm-node-'.length));
    await marquee(['research'], true, true);
    check(firstMarqueeIds.every((id) => topic(id).getAttribute('aria-selected') === 'true') && topic('research').getAttribute('aria-selected') === 'true', 'Ctrl+Shift reverse-drag adds without clearing the existing selection');
    check([...host.querySelectorAll<HTMLElement>('me-tpc[aria-selected="true"]')].every((node) => node.classList.contains('pm-selected')), 'Every selected direction has the app-owned visual state');
    select('design', { ctrlKey: true });
    check(topic('design').getAttribute('aria-selected') === 'false' && topic('research').getAttribute('aria-selected') === 'true', 'Ctrl-click toggles one member without clearing the group');
    select('writing');
    check(host.querySelectorAll('me-tpc[aria-selected="true"]').length === 1 && topic('writing').getAttribute('aria-selected') === 'true', 'A normal click returns to ordinary single selection');
    select('research'); key(' '); await pause();
    await marquee(['research']);
    checkSelected(['research'], 'A marquee does not select descendants hidden by a collapsed branch');
    check(!host.querySelector('#pm-node-level-1'), 'Collapsed descendants stay absent from marquee hit testing');
    select('design');
    const researchRect = topic('research').getBoundingClientRect();
    pointer($('.pm-canvas'), 'pointerdown', researchRect.left - 8, researchRect.top - 8, 1, { shiftKey: true });
    pointer($('.pm-canvas'), 'pointermove', researchRect.right + 8, researchRect.bottom + 8, 1, { shiftKey: true });
    await pause(); key('Escape');
    check(!host.querySelector('.pm-selection-rectangle') && captured.size === 0 && topic('design').getAttribute('aria-selected') === 'true', 'Escape cancels marquee preview and restores the committed selection');
    check(operations.length === 0, 'Marquee and modifier selection remain local view state');
    report('PASS marquee replace/add, reverse drag, modifier toggle, normal click, visual state, and cancellation');

    await reset();
    select('design'); select('research', { ctrlKey: true });
    const moveSource = topic('design').getBoundingClientRect();
    const moveTarget = topic('writing').getBoundingClientRect();
    pointer(topic('design'), 'pointerdown', moveSource.x + moveSource.width / 2, moveSource.y + moveSource.height / 2);
    checkSelected(['design'], 'Starting a structural drag collapses a group to the dragged direction');
    pointer($('.pm-canvas'), 'pointermove', moveTarget.x + moveTarget.width / 2, moveTarget.y + moveTarget.height / 2);
    pointer($('.pm-canvas'), 'pointerup', moveTarget.x + moveTarget.width / 2, moveTarget.y + moveTarget.height / 2, 0);
    await settled();
    const moved = operations.at(-1);
    check(operations.slice().length === 1 && moved?.type === 'move' && moved.id === 'design'
      && indexTree(current!).get('design')?.parent_id === 'writing'
      && indexTree(current!).get('research')?.parent_id === 'directions',
    'Dragging a selected member moves only that direction');

    await reset();
    select('design'); select('research', { ctrlKey: true });
    key('Delete'); await settled();
    const deleted = operations.at(-1);
    check(operations.slice().length === 1 && deleted?.type === 'delete' && deleted.id === 'design'
      && !indexTree(current!).has('design') && indexTree(current!).has('research'),
    'Deleting a multi-selection removes only its primary direction');
    report('PASS multi-selection does not introduce bulk move or delete behavior');

    await reset();
    select('design');
    click(button('note', '.pm-toolbar'));
    check(!$('.pm-note').hidden && topic('design').getAttribute('aria-selected') === 'true', 'Fixed toolbar actions retain their selected target');
    click(button('close-note', '.pm-note')); await pause();
    check(operations.length === 0, 'Dismissal, navigation, and panning never write Pursuit data');
    click(button('bold', '.pm-topic-toolbar'));
    click($('.pm-canvas')); await settled();
    check(indexTree(current!).get('design')!.title === '**Design 设计**' && $('.pm-topic-toolbar').hidden, 'A pending save keeps its target without restoring dismissed tools');
    select('design'); key('F2');
    await until(() => host.querySelector('#input-box'), 'The selected title should open for editing');
    const editedTitle = $('#input-box');
    editedTitle.textContent = 'Edited before dismissal'; editedTitle.dispatchEvent(new Event('input', { bubbles: true }));
    const blank = $('.pm-canvas'); const blankRect = blank.getBoundingClientRect();
    pointer(blank, 'pointerdown', blankRect.right - 8, blankRect.bottom - 8);
    editedTitle.blur(); await pause(0);
    pointer(blank, 'pointerup', blankRect.right - 8, blankRect.bottom - 8, 0); blank.click();
    await settled();
    check(indexTree(current!).get('design')!.title === 'Edited before dismissal' && $('.pm-topic-toolbar').hidden, 'Blank clicks finish title editing without losing text or reopening tools');
    report('PASS selection facade, outside-click dismissal, refresh/reopen persistence, keyboard recovery, toolbar actions, pan, and pending edits');

    await reset(); select('design');
    const checkpointBeforeEdits = current!.git_head;
    click(button('bold', '.pm-topic-toolbar')); await settled();
    click(button('underline', '.pm-topic-toolbar')); await settled();
    const savedActions = [...current!.history.undo];
    check($('.pm-save-status').textContent === 'Saved' && !controller!.hasUnsavedChanges
      && current!.pending && current!.git_head === checkpointBeforeEdits && savedActions.length === 2,
    'Acknowledged actions show Saved before their shared Git checkpoint');
    const unload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(unload);
    check(!unload.defaultPrevented, 'A pending Git checkpoint alone never blocks leaving the page');
    await controller!.flush();
    check(!current!.pending && current!.git_head !== checkpointBeforeEdits
      && JSON.stringify(current!.history.undo) === JSON.stringify(savedActions),
    'Flushing the batch preserves both saved action IDs');
    await controller!.destroy(); controller = await mountMap(host, transport, mapOptions); await pause();
    check(!button('undo').disabled, 'Reopening the map restores available action history from the server');
    button('undo').click(); await settled();
    check(topic('design').querySelector('strong') && !topic('design').querySelector('u')
      && current!.history.redo.at(-1) === savedActions.at(-1),
    'Undo after a checkpoint reverses only the latest action and retains its ID');
    await controller!.flush();
    button('redo').click(); await settled();
    check(topic('design').querySelector('strong > u') && current!.history.undo.at(-1) === savedActions.at(-1),
      'Redo after another checkpoint reapplies that same action');
    report('PASS immediate Saved state, unload without a checkpoint warning, and action undo/redo across checkpoints and reopen');

    await reset(); select('design');
    click(button('bold', '.pm-topic-toolbar')); await settled();
    const typingCheckpoint = current!.git_head;
    const flushesBeforeTyping = flushes;
    click(button('note', '.pm-topic-toolbar'));
    const typingNote = $<HTMLTextAreaElement>('.pm-note textarea');
    for (let index = 0; index < 6; index++) {
      typingNote.value = `Draft being typed ${index + 1}`;
      typingNote.dispatchEvent(new Event('input', { bubbles: true }));
      await pause(600);
      check(current!.pending && current!.git_head === typingCheckpoint && flushes === flushesBeforeTyping,
        'Continuous note input keeps the saved action in its pending batch beyond the idle interval');
    }
    const typedDraft = typingNote.value;
    await until(() => !current!.pending && current!.git_head !== typingCheckpoint,
      'The saved action should checkpoint once note input stops');
    check(typingNote.value === typedDraft && controller!.hasUnsavedChanges
      && indexTree(current!).get('design')!.body === '' && operations.slice().length === 1,
    'The idle checkpoint preserves the dirty note without saving or discarding its draft');
    const dirtyUnload = new Event('beforeunload', { cancelable: true });
    window.dispatchEvent(dirtyUnload);
    check(dirtyUnload.defaultPrevented, 'A dirty note still protects against leaving after the saved action checkpoints');
    click(button('discard-note', '.pm-note'));
    const operationsBeforeInactive = operations.length;
    controller!.setActive(false);
    button('bold', '.pm-topic-toolbar').click(); await pause();
    check(operations.length === operationsBeforeInactive && indexTree(current!).get('design')!.title === '**Design 设计**',
      'Clicks cannot enqueue changes after the map is made inactive');
    controller!.setActive(true);
    click(button('bold', '.pm-topic-toolbar')); await settled();
    check(operations.length === operationsBeforeInactive + 1 && indexTree(current!).get('design')!.title === 'Design 设计',
      'Reactivating the map restores editing');
    report('PASS continuous typing delays idle checkpoint, stopping input preserves dirty draft and unload protection, and inactive maps reject edits');

    await reset();
    select('design'); select('research', { ctrlKey: true });
    click(button('bold', '.pm-topic-toolbar')); await settled();
    check(topic('design').querySelector('strong') && topic('research').querySelector('strong'), 'One formatting action applies to every selected direction');
    const grouped = operations.at(-1);
    check(operations.slice().length === 1 && grouped?.type === 'rename_many' && grouped.renames.length === 2, 'Multi-selection formatting sends one narrow compound operation');
    button('undo').click(); await settled();
    check(!topic('design').querySelector('strong') && !topic('research').querySelector('strong'), 'One Undo restores every title in the grouped format action');
    button('redo').click(); await settled();
    check(topic('design').querySelector('strong') && topic('research').querySelector('strong'), 'One Redo reapplies every grouped title');
    report('PASS multi-selection formatting uses one operation and one undo/redo step');

    await reset();
    select('design'); click(button('underline', '.pm-topic-toolbar')); await settled();
    select('research', { ctrlKey: true });
    check(button('underline', '.pm-topic-toolbar').getAttribute('aria-pressed') === 'mixed', 'A partially marked selection exposes the mixed formatting state');
    const beforeMixedFormat = operations.length;
    click(button('underline', '.pm-topic-toolbar')); await settled();
    const groupedUnderline = operations.at(-1);
    check(topic('design').querySelector('u') && topic('research').querySelector('u')
      && operations.length === beforeMixedFormat + 1 && groupedUnderline?.type === 'rename_many'
      && groupedUnderline.renames.length === 1 && groupedUnderline.renames[0].id === 'research',
    'Applying a mixed underline state marks every selected direction in one operation');
    click(button('strike', '.pm-topic-toolbar')); await settled();
    const groupedStrike = operations.at(-1);
    check(topic('design').querySelector('s') && topic('research').querySelector('s')
      && groupedStrike?.type === 'rename_many' && groupedStrike.renames.length === 2,
    'Strikethrough also applies to the complete selection in one operation');
    report('PASS mixed underline and grouped strikethrough formatting');

    const plainId = 'plain:PURSUITS.md:19';
    await reset(22, (snapshot) => remapSnapshot(snapshot, [{ from: 'design', to: plainId }]));
    select(plainId); key(' '); select('research', { ctrlKey: true });
    click(button('bold', '.pm-topic-toolbar')); await settled();
    const promoted = current!.items.find((item) => item.title === '**Design 设计**')?.id;
    check(promoted && promoted !== plainId && controller!.getSelectedId() === promoted
      && topic(promoted).getAttribute('aria-expanded') === 'false' && topic('research').getAttribute('aria-selected') === 'true',
    'Grouped formatting remaps a promoted primary while preserving selection and collapsed view state');
    button('undo').click(); await settled();
    check(controller!.getSelectedId() === plainId && topic(plainId).getAttribute('aria-expanded') === 'false'
      && topic('research').getAttribute('aria-selected') === 'true',
    'Undo applies the inverse ID remap to multi-selection view state');
    button('redo').click(); await settled();
    check(controller!.getSelectedId() === promoted && topic(promoted).getAttribute('aria-expanded') === 'false'
      && topic('research').getAttribute('aria-selected') === 'true',
    'Redo reapplies the forward ID remap to multi-selection view state');
    report('PASS plural ID remaps survive grouped formatting and undo/redo');

    await reset();
    select('design');
    check(!$('.pm-topic-toolbar').hidden, 'Selecting a real node shows its toolbar');
    for (const command of ['bold', 'underline', 'strike']) { click(button(command, '.pm-topic-toolbar')); await settled(); }
    check(indexTree(current!).get('design')!.title === '**<u>~~Design 设计~~</u>**', 'B/U/S must use canonical rename titles');
    check(topic('design').querySelector('strong > u > s'), 'All three marks must be rendered');
    check(operations.every((operation) => operation.type === 'rename_many' && operation.renames.length === 1 && operation.renames[0].id === 'design'), 'Formatting uses the atomic title operation and keeps the selected id');
    check(document.activeElement === $('.pm-canvas'), 'Formatting keeps canvas keyboard focus');
    for (const [name, options] of [['b', { ctrlKey: true }], ['u', { metaKey: true }], ['X', { ctrlKey: true, shiftKey: true }]] as const) {
      key(name, options); await settled(); key(name, options); await settled();
    }
    check(indexTree(current!).get('design')!.title === '**<u>~~Design 设计~~</u>**', 'Formatting shortcuts toggle the same whole-topic marks');
    await controller!.refresh();
    for (const command of ['bold', 'underline', 'strike']) check(button(command, '.pm-topic-toolbar').getAttribute('aria-pressed') === 'true', 'Marks survive authoritative refresh');
    button('undo').click(); await settled();
    check(!topic('design').querySelector('s'), 'Undo removes only the last mark');
    button('redo').click(); await settled();
    check(topic('design').querySelector('s'), 'Redo restores the mark');
    select('design'); button('note', '.pm-topic-toolbar').click();
    check($('.pm-note h2').textContent === 'Design 设计', 'Note headings use visible title text');
    button('close-note', '.pm-note').click(); await pause();
    report('PASS formatting, rename payloads, active state, focus, undo/redo, and plain note heading');

    const nodeRect = topic('design').getBoundingClientRect();
    topic('design').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: nodeRect.right, clientY: nodeRect.bottom }));
    check(!$('.pm-context-menu').hidden, 'Right-click opens the node menu');
    check($('.pm-context-menu').querySelectorAll('button').length === 13, 'Node menu contains the supported actions');
    const first = document.activeElement as HTMLElement;
    key('ArrowDown', {}, first);
    check(document.activeElement !== first, 'Menu arrow keys move focus');
    key('Escape', {}, document.activeElement as HTMLElement);
    check($('.pm-context-menu').hidden, 'Escape closes the menu');
    const canvasRect = $('.pm-canvas').getBoundingClientRect();
    $('.pm-canvas').dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: canvasRect.right - 2, clientY: canvasRect.bottom - 2 }));
    check($('.pm-context-menu').querySelectorAll('button').length === 2, 'Canvas menu has only create and fit');
    const menuRect = $('.pm-context-menu').getBoundingClientRect();
    check(menuRect.right <= canvasRect.right && menuRect.bottom <= canvasRect.bottom, 'Menu stays inside the stage');
    key('Escape', {}, document.activeElement as HTMLElement);
    report('PASS node/canvas menus, keyboard navigation, dismissal, and viewport clamping');

    await reset();
    const openNodeMenu = (id: string) => {
      const rect = topic(id).getBoundingClientRect();
      topic(id).dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: rect.right, clientY: rect.bottom }));
    };
    const copyContext = () => click(button('copy-context', '.pm-context-menu'));
    const toastMessage = () => $('.pm-toast').hidden ? '' : $('.pm-toast > span').textContent ?? '';
    const beforeCopy = JSON.stringify(current!);
    const loadsBeforeCopy = loads;
    openNodeMenu('design');
    check(!button('copy-context', '.pm-context-menu').disabled, 'Right-click exposes Copy context for its selected direction');
    copyContext();
    await until(() => clipboardWrites.length === 1 && /copied/i.test(toastMessage()), 'Copy context should finish with visible success feedback');
    check(contextRequests.slice().length === 1 && contextRequests[0].itemId === 'design'
      && contextRequests[0].revision === current!.revision && clipboardWrites[0] === copiedMarker,
    'Copy context forwards the selected saved id and revision and writes the returned payload unchanged');
    check(JSON.stringify(current!) === beforeCopy && operations.length === 0 && loads === loadsBeforeCopy,
      'Copying context does not mutate or reload the map');
    select('research'); click(button('context-menu', '.pm-topic-toolbar'));
    check(!$('.pm-context-menu').hidden && !button('copy-context', '.pm-context-menu').disabled,
      'The selected topic More menu also exposes Copy context');
    copyContext();
    await until(() => clipboardWrites.length === 2, 'The More menu should copy the selected direction');
    check(contextRequests[1].itemId === 'research' && contextRequests[1].revision === current!.revision,
      'The More menu uses the current selection and revision');
    report('PASS context copy through right-click and More menus, exact payload delivery, success feedback, and no map mutation');

    await reset(); select('design');
    click(button('bold', '.pm-topic-toolbar')); await settled();
    const contextAction = current!.history.undo.at(-1);
    const contextCheckpoint = current!.git_head;
    const flushesBeforeContext = flushes;
    check(current!.pending && !controller!.hasUnsavedChanges, 'Context copying can start after an action is saved but before its checkpoint');
    openNodeMenu('design'); copyContext();
    await until(() => clipboardWrites.length === 1, 'Copy context should flush the saved action batch and finish copying');
    check(flushes > flushesBeforeContext && !current!.pending && current!.git_head !== contextCheckpoint
      && contextRequests[0].revision === current!.revision && current!.history.undo.at(-1) === contextAction
      && operations.slice().length === 1 && topic('design').querySelector('strong'),
    'Context flush advances the checkpoint before its request while preserving the saved map and action history');
    report('PASS context copying flushes saved pending actions before reading context');

    await reset(); select('design');
    let releaseSave!: () => void;
    heldSave = new Promise<void>((resolve) => { releaseSave = resolve; });
    click(button('bold', '.pm-topic-toolbar'));
    check(controller!.hasUnsavedChanges, 'The fixture save remains pending for the context-copy check');
    openNodeMenu('design'); copyContext();
    check(contextRequests.length === 0 && clipboardWrites.length === 0 && toastMessage().length > 0,
      'Copy context explains a pending save without requesting or copying context');
    releaseSave(); await settled();
    click(button('note', '.pm-topic-toolbar'));
    const unsavedNote = $<HTMLTextAreaElement>('.pm-note textarea');
    unsavedNote.value = 'Unsaved fixture note'; unsavedNote.dispatchEvent(new Event('input', { bubbles: true }));
    const operationsBeforeDirtyCopy = operations.length;
    openNodeMenu('design'); copyContext();
    check(contextRequests.length === 0 && clipboardWrites.length === 0 && toastMessage().length > 0
      && unsavedNote.value === 'Unsaved fixture note' && operations.length === operationsBeforeDirtyCopy,
    'Copy context explains an unsaved note without saving, discarding, requesting, or copying it');
    click(button('discard-note', '.pm-note'));
    report('PASS pending saves and unsaved notes block context requests with visible feedback');

    await reset();
    contextError = new Error('Fixture context unavailable');
    openNodeMenu('design'); copyContext();
    await until(() => toastMessage().includes('Fixture context unavailable'), 'Context request errors should be visible');
    check(clipboardWrites.length === 0 && operations.length === 0, 'A failed context request never writes the clipboard or map');
    contextError = undefined;
    clipboardError = new Error('Fixture clipboard unavailable');
    openNodeMenu('design'); copyContext();
    await until(() => /clipboard/i.test(toastMessage()), 'Clipboard errors should be visible');
    check(clipboardWrites.length === 0 && operations.length === 0, 'A failed clipboard write does not mutate the map');
    clipboardError = undefined;
    contextError = new ApiError('Fixture context changed', 409, structuredClone(current!));
    const loadsBeforeStaleCopy = loads;
    openNodeMenu('design'); copyContext();
    await until(() => loads > loadsBeforeStaleCopy && /review|reload|changed|retry/i.test(toastMessage()),
      'Stale context should refresh the map and ask for review or retry');
    check(clipboardWrites.length === 0 && operations.length === 0,
      'A stale context response never writes the clipboard or mutates the map');
    report('PASS context and clipboard error feedback and stale-response recovery without clipboard writes');

    await reset();
    select('design'); key('Enter', { shiftKey: true });
    await until(() => host.querySelector('#input-box'), 'Shift+Enter should begin a new title');
    const editor = $<HTMLElement>('#input-box');
    editor.textContent = 'Before design'; editor.dispatchEvent(new Event('input', { bubbles: true })); editor.blur();
    await settled();
    const created = operations.at(-1)!;
    check(created.type === 'create' && created.parent_id === 'directions' && created.after_id === 'research', 'Sibling-before must use the preceding sibling');
    select('design'); key('ArrowUp', { altKey: true }); await settled();
    check(indexTree(current!).get('directions')!.child_ids[1] === 'design', 'Alt+Up moves the selected sibling earlier');
    key('ArrowDown', { altKey: true }); await settled();
    check(indexTree(current!).get('directions')!.child_ids[2] === 'design', 'Alt+Down moves the selected sibling later');
    select('directions'); const countBefore = operations.length; key('ArrowUp', { altKey: true }); await pause();
    check(operations.length === countBefore, 'Reordering the first root earlier is a no-op');
    key('ArrowDown', { altKey: true }); await settled();
    check(current!.root_ids[1] === 'directions', 'Root siblings can also be reordered');
    select('design'); key('F2'); await until(() => host.querySelector('#input-box'), 'F2 should edit the raw title');
    const rawEditor = $('#input-box'); const raw = rawEditor.textContent;
    key('b', { ctrlKey: true }, rawEditor); key('u', { ctrlKey: true }, rawEditor); key('X', { ctrlKey: true, shiftKey: true }, rawEditor);
    check(rawEditor.textContent === raw && !rawEditor.querySelector('b,strong,u,s'), 'Raw editing suppresses native rich-text shortcuts');
    key('Escape', {}, rawEditor); await pause();
    report('PASS sibling-before, sibling reorder, root boundaries, and raw-editor shortcut safety');

    await reset();
    select('research'); key(' '); button('fit').click(); await pause();
    const source = topic('design');
    const sourceRect = source.getBoundingClientRect();
    const targetRect = topic('research').getBoundingClientRect();
    pointer(source, 'pointerdown', sourceRect.x + sourceRect.width / 2, sourceRect.y + sourceRect.height / 2);
    pointer($('.pm-canvas'), 'pointermove', targetRect.x + targetRect.width / 2, targetRect.y + targetRect.height / 2);
    check($('.pm-topic-toolbar').hidden && host.querySelector('.pm-drag-ghost'), 'Dragging hides the toolbar and shows a ghost');
    await until(() => topic('research').getAttribute('aria-expanded') === 'true', 'Hovering a collapsed in-drop destination should expand it');
    check(host.querySelector('.pm-drag-ghost'), 'Hover expansion must not cancel the drag');
    key('Escape');
    check(!host.querySelector('.pm-drag-ghost, .pm-drop-in, .pm-drop-before, .pm-drop-after'), 'Escape removes every drag indicator');
    check(operations.length === 0 && captured.size === 0, 'Hover expansion and cancellation create no semantic write or stale capture');
    report('PASS delayed hover expansion, capture transfer, and Escape cleanup without writes');

    const beginEdgeDrag = () => {
      select('design');
      const node = topic('design'); const rect = node.getBoundingClientRect();
      pointer(node, 'pointerdown', rect.x + rect.width / 2, rect.y + rect.height / 2);
      const canvas = $('.pm-canvas').getBoundingClientRect();
      pointer($('.pm-canvas'), 'pointermove', canvas.right - 2, canvas.top + canvas.height / 2);
    };
    beginEdgeDrag();
    const beforePan = $('.pm-forest').style.transform;
    await until(() => $('.pm-forest').style.transform !== beforePan, 'Edge dragging should pan the shared viewport');
    const canvas = $('.pm-canvas').getBoundingClientRect();
    pointer($('.pm-canvas'), 'pointermove', canvas.left + canvas.width / 2, canvas.top + canvas.height / 2);
    const stopped = $('.pm-forest').style.transform; await pause(90);
    check($('.pm-forest').style.transform === stopped, 'Leaving the edge band immediately stops auto-pan');
    key('Escape');
    for (const cancellation of ['pointercancel', 'lostpointercapture', 'blur']) {
      beginEdgeDrag();
      if (cancellation === 'blur') window.dispatchEvent(new Event('blur'));
      else if (cancellation === 'lostpointercapture') pointer(captured.get(1) as HTMLElement, cancellation, 0, 0, 0);
      else pointer($('.pm-canvas'), cancellation, 0, 0, 0);
      const transform = $('.pm-forest').style.transform; await pause(60);
      check(!host.querySelector('.pm-drag-ghost') && captured.size === 0 && $('.pm-forest').style.transform === transform, `${cancellation} must stop the drag and its animation`);
    }
    check(operations.length === 0, 'Canceled drags never enqueue moves');
    report('PASS edge auto-pan, stop at band exit, pointer cancellation, capture loss, and window blur');

    await reset();
    const unsafe = ['<img src=x onerror=alert(1)>', '<script>alert(1)</script>', '<u onclick=alert(1)>text</u>', '**<svg onload=alert(1)>**'];
    for (const title of unsafe) {
      current = applyOperation(current!, { type: 'rename', id: 'design', title });
      current.revision = `external-${++serial}`;
      await controller!.refresh();
      check(!topic('design').querySelector('img,script,svg,[onclick],[onerror],[onload]'), 'Title HTML must never create executable DOM');
    }
    current = applyOperation(current!, { type: 'rename', id: 'design', title: '**<u>Readable title</u>**' });
    current.writable = false; current.revision = `external-${++serial}`;
    await controller!.refresh(); select('design');
    check(!$('.pm-topic-toolbar').hidden && topic('design').querySelector('strong > u'), 'Read-only roots retain readable formatting and toolbar');
    for (const command of ['bold', 'underline', 'strike', 'focus']) check(button(command, '.pm-topic-toolbar').disabled, 'Read-only formatting and Focus writes must be disabled');
    openNodeMenu('design');
    check(!button('copy-context', '.pm-context-menu').disabled, 'Read-only roots still allow context copying');
    copyContext();
    await until(() => clipboardWrites.length === 1, 'Read-only context copying should reach the clipboard');
    check(operations.length === 0, 'Context copying from a read-only root leaves its data unchanged');
    report('PASS HTML injection regression checks and read-only controls');

    await reset(); select('design'); select('research', { ctrlKey: true }); failNext = true;
    button('bold', '.pm-topic-toolbar').click(); await settled();
    check(indexTree(current!).get('design')!.title === 'Design 设计' && indexTree(current!).get('research')!.title === 'Research 研究'
      && !topic('design').querySelector('strong') && !topic('research').querySelector('strong'), 'A failed grouped format operation restores every authoritative title');
    check(topic('design').getAttribute('aria-selected') === 'true' && topic('research').getAttribute('aria-selected') === 'true', 'Failed formatting preserves the multi-selection');
    check(operations.slice().length === 1 && operations[0].type === 'rename_many', 'A failed grouped format is still one atomic request');
    report('PASS grouped formatting conflict recovery');

    current = applyOperation(current!, { type: 'rename', id: 'design', title: 'A **bold** label' });
    indexTree(current).get('research')!.edges = [['rel', 'design']];
    current.revision = `external-${++serial}`; await controller!.refresh();
    button('search').click();
    const search = $<HTMLInputElement>('.pm-search input');
    search.value = 'A bold label'; search.dispatchEvent(new Event('input', { bubbles: true }));
    check($('.pm-search-count').textContent === '1 / 1' && topic('design').getAttribute('aria-selected') === 'true', 'Search matches visible text across formatting boundaries');
    button('close-search', '.pm-search').click();
    select('research'); button('relations').click();
    check($('.pm-relations').textContent?.includes('A bold label') && !$('.pm-relations').textContent?.includes('**'), 'Relations use visible title text');
    button('close-relations', '.pm-relations').click();
    select('design'); button('delete').click(); await settled();
    check($('.pm-toast > span').textContent === '“A bold label” removed.', 'Deletion toast uses visible title text');
    const deletedAction = current!.history.undo.at(-1);
    await controller!.flush();
    click($<HTMLButtonElement>('.pm-toast > button:not(.pm-toast-close)')); await settled();
    check(indexTree(current!).has('design') && current!.history.redo.at(-1) === deletedAction,
      'The deletion toast Undo targets its saved action after the checkpoint advances');
    report('PASS plain-text search, relation labels, deletion messages, and toast Undo across checkpoint');

    const started = performance.now(); await reset(500);
    const mountMs = Math.round(performance.now() - started);
    const loadsBefore = loads; select('generated-499');
    $('.pm-canvas').dispatchEvent(new WheelEvent('wheel', { deltaY: 24, bubbles: true, cancelable: true }));
    await pause(190);
    check(loads === loadsBefore && operations.length === 0, 'Selection and pan are local with 500 directions');
    report(`PASS 500-direction fixture (${mountMs} ms mount), local selection and pan`);

    report('All browser checks passed.');
  } catch (error) {
    report(`FAIL: ${(error as Error).message}`);
  } finally {
    controller?.setActive(false);
    names.forEach((name, index) => {
      if (descriptors[index]) Object.defineProperty(HTMLElement.prototype, name, descriptors[index]!);
      else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[name];
    });
    controller?.setActive(true);
  }
  return controller;
}

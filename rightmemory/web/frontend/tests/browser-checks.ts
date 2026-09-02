import { mountMap, type PursuitMapController } from '../src/pursuit-map.ts';
import { applyOperation, indexTree, type Operation, type Snapshot } from '../src/tree.ts';
import { ApiError, type Transport } from '../src/queue.ts';
import { forestFixture } from './fixtures.ts';
import { ConversationRenderer, type ConversationRendererActions } from '../src/conversation-renderer.ts';
import { initialConversationState, normalizeConversationDetail, normalizeEvent, normalizeWorkspace, reduceConversationState, type ConversationState } from '../src/conversation-state.ts';
import { ConversationWorkspace } from '../src/conversation-workspace.ts';

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
  const selectionBoundary = document.createElement('section');
  selectionBoundary.className = 'browser-selection-boundary';
  host.before(selectionBoundary);
  selectionBoundary.append(host);
  const conversationPane = document.createElement('button');
  conversationPane.type = 'button';
  conversationPane.textContent = 'Conversation pane fixture';
  conversationPane.style.cssText = 'position:fixed;right:-1000px;top:0';
  selectionBoundary.append(conversationPane);
  let current: Snapshot;
  let serial = 0;
  let loads = 0;
  let failNext = false;
  const operations: Operation[] = [];
  type IdRemap = { from: string; to: string };
  const history = new Map<string, { snapshot: Snapshot; remaps: IdRemap[] }>();
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
      await pause();
      if (failNext) { failNext = false; throw new ApiError('Fixture conflict', 409, structuredClone(current)); }
      check(revision === current.revision, 'Mutation revision must follow the last response');
      const id = operation.type === 'create' ? `created-${serial}`
        : operation.type === 'rename_many' ? operation.renames[0]?.id ?? null : operation.id;
      const before = current;
      const remaps = operation.type === 'rename_many'
        ? operation.renames.filter((rename) => rename.id.startsWith('plain:'))
          .map((rename, index) => ({ from: rename.id, to: `promoted-${serial + 1}-${index}` }))
        : [];
      current = { ...remapSnapshot(applyOperation(current, operation, id), remaps), revision: `r${++serial}`, git_head: `c${serial}` };
      history.set(current.git_head, { snapshot: before, remaps: remaps.map(({ from, to }) => ({ from: to, to: from })) });
      const selected = remaps.find((mapping) => mapping.from === id)?.to ?? id;
      return { snapshot: structuredClone(current), commit: current.git_head, operation_id: current.git_head, repaired_references: [], undoable: true, selected_id: selected, id_remaps: remaps };
    },
    history: async (_kind, _revision, commit) => {
      const restored = history.get(commit);
      check(restored, 'Undo must reference a saved interaction');
      const before = current;
      current = { ...restored.snapshot, revision: `r${++serial}`, git_head: `c${serial}` };
      history.set(current.git_head, { snapshot: before, remaps: restored.remaps.map(({ from, to }) => ({ from: to, to: from })) });
      return { snapshot: structuredClone(current), commit: current.git_head, operation_id: current.git_head, repaired_references: [], undoable: true, selected_id: null, id_remaps: restored.remaps };
    },
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
    controller?.destroy();
    const fixture = { ...forestFixture(count), root_key: `interaction-check-${crypto.randomUUID()}` };
    current = prepare ? prepare(fixture) : fixture;
    operations.length = 0;
    controller = await mountMap(host, transport, { selectionBoundary });
    await pause();
  };
  try {
    await reset();
    select('design');
    const selections: Array<string | null> = [];
    const unsubscribeSelection = controller!.subscribeSelection((id) => selections.push(id));
    click(conversationPane);
    check(controller!.getSelectedId() === 'design' && topic('design').getAttribute('aria-selected') === 'true', 'Clicks in the conversation boundary retain map selection');
    check(selections.at(-1) === 'design', 'The selection facade reports the selected Pursuit');
    unsubscribeSelection();
    click($('.pm-canvas'));
    check($('.pm-topic-toolbar').hidden && !host.querySelector('[aria-selected="true"]'), 'A blank click dismisses the toolbar and clears selection');
    check(!$('.pm-canvas').hasAttribute('aria-activedescendant'), 'A cleared selection has no active tree item');
    await controller!.refresh();
    check($('.pm-topic-toolbar').hidden && !host.querySelector('[aria-selected="true"]'), 'Refreshing must not restore a dismissed selection');
    controller!.destroy(); controller = await mountMap(host, transport, { selectionBoundary }); await pause();
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
    check(controller!.getSelectedId() === 'design' && primaryUpdates.length === 1, 'Adding a secondary direction preserves the primary Manager reference');
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
    report('PASS workspace selection boundary/facade, outside-click dismissal, refresh/reopen persistence, keyboard recovery, toolbar actions, pan, and pending edits');

    controller!.setConversationIndicators([
      { pursuitId: 'research', status: 'running' },
      { pursuitId: 'writing', status: 'failed' },
      { pursuitId: 'design', status: 'idle', unreadFinal: true },
      { pursuitId: 'practice', status: 'completed' },
    ]);
    const contrastRatio = (foreground: string, background: string): number => {
      const luminance = (value: string): number => {
        const channels = (value.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number).map((channel) => {
          const normalized = channel / 255;
          return normalized <= .04045 ? normalized / 12.92 : ((normalized + .055) / 1.055) ** 2.4;
        });
        return .2126 * channels[0] + .7152 * channels[1] + .0722 * channels[2];
      };
      const first = luminance(foreground); const second = luminance(background);
      return (Math.max(first, second) + .05) / (Math.min(first, second) + .05);
    };
    for (const [id, state, cue, label] of [
      ['research', 'working', '↻', 'working'],
      ['writing', 'needs-attention', '!', 'needing attention'],
      ['design', 'unread-final', 'NEW', 'unread final response'],
      ['practice', 'completed', '✓', 'completed'],
    ] as const) {
      const marker = topic(id).querySelector<HTMLElement>(`.pm-conversation-indicator-${state}`);
      check(marker, `Missing ${state} conversation marker`);
      const style = getComputedStyle(marker);
      const renderedCue = getComputedStyle(marker, '::before').content.replace(/["']/g, '');
      check(renderedCue === cue && marker.getBoundingClientRect().height >= 14, `${state} uses a persistent non-color status cue`);
      check(contrastRatio(style.color, style.backgroundColor) >= 4.5, `${state} status cue keeps readable foreground contrast`);
      check(topic(id).getAttribute('aria-label')?.includes(label), `${state} status remains available to assistive technology`);
    }
    report('PASS readable non-color conversation state cues and accessible labels');

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
    check($('.pm-context-menu').querySelectorAll('button').length === 12, 'Node menu contains the supported actions');
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
    report('PASS plain-text search, relation labels, and deletion messages');

    const started = performance.now(); await reset(500);
    const mountMs = Math.round(performance.now() - started);
    const loadsBefore = loads; select('generated-499');
    $('.pm-canvas').dispatchEvent(new WheelEvent('wheel', { deltaY: 24, bubbles: true, cancelable: true }));
    await pause(190);
    check(loads === loadsBefore && operations.length === 0, 'Selection and pan are local with 500 directions');
    report(`PASS 500-direction fixture (${mountMs} ms mount), local selection and pan`);

    const conversationHost = document.createElement('aside');
    conversationHost.style.cssText = 'position:fixed;right:-1200px;top:0;width:400px;height:700px';
    selectionBoundary.append(conversationHost);
    const sentMessages: Array<{ text: string; attachmentIds: string[] }> = [];
    const uploadedFiles: File[] = [];
    const uploadAttempts: Array<{ file: File; attachmentId: string; attachmentKind?: 'file' }> = [];
    let uploadsInFlight = 0;
    let maximumUploadsInFlight = 0;
    let failNextUpload = false;
    let uploadGate: Promise<void> | null = null;
    let echoNextUploadId = false;
    let failNextDelete = false;
    let notFoundNextDelete = false;
    let failNextSend = false;
    let interrupts = 0;
    const deletedAttachmentIds: string[] = [];
    const responses: Array<{ decision?: string; response?: unknown }> = [];
    const reconnects: string[] = [];
    const openedConversations: string[] = [];
    const createdSideChats: string[] = [];
    const closedSideChats: string[] = [];
    const modelCatalogLoads: string[] = [];
    const settingsUpdates: Array<{ model: string; reasoningEffort: string }> = [];
    const earlierLoads: string[] = [];
    let managerOpenRequests = 0;
    let managerReferenceRemovals = 0;
    const actions: ConversationRendererActions = {
      toggleCollapsed() {}, openManager() { managerOpenRequests++; }, openConversation(conversationId) { openedConversations.push(conversationId); }, loadEarlier(conversationId) { earlierLoads.push(conversationId); }, closeConversation() {}, createConversation() {}, createManager() {}, removeManagerReference() { managerReferenceRemovals++; }, interrupt() { interrupts++; }, archive() {}, reload() {},
      createSideChat(parentConversationId) { createdSideChats.push(parentConversationId); },
      closeSideChat(sideChatId) { closedSideChats.push(sideChatId); },
      acknowledgeRead() {},
      loadModelCatalog(hostId) { modelCatalogLoads.push(hostId); },
      updateConversationSettings(model, reasoningEffort) { settingsUpdates.push({ model, reasoningEffort }); },
      reconnect(conversationId) { reconnects.push(conversationId); },
      createHost() {}, probeHost() {}, createProject() {}, retry() {},
      async uploadAttachment(conversationId, file, attachmentId, attachmentKind) {
        uploadAttempts.push({ file, attachmentId, attachmentKind });
        uploadsInFlight++;
        maximumUploadsInFlight = Math.max(maximumUploadsInFlight, uploadsInFlight);
        try {
          const gate = uploadGate;
          uploadGate = null;
          const echoUploadId = echoNextUploadId;
          echoNextUploadId = false;
          if (gate) await gate;
          await pause(15);
          if (failNextUpload) { failNextUpload = false; throw new Error('fixture upload failed'); }
          uploadedFiles.push(file);
          return {
            attachmentId: echoUploadId ? attachmentId : `uploaded-${uploadedFiles.length}`, conversationId,
            kind: attachmentKind === 'file' ? 'file' : file.type.startsWith('image/') ? 'image' : 'pasted_text', displayName: file.name,
            mediaType: file.type, byteSize: file.size, state: 'staged', url: '', raw: {},
          };
        } finally { uploadsInFlight--; }
      },
      async deleteAttachment(_conversationId, attachmentId) {
        deletedAttachmentIds.push(attachmentId);
        if (failNextDelete) { failNextDelete = false; throw new Error('fixture delete failed'); }
        if (notFoundNextDelete) {
          notFoundNextDelete = false;
          throw Object.assign(new Error('The attachment was not found.'), { status: 404, code: 'attachment_not_found' });
        }
      },
      sendMessage(text, attachmentIds) {
        if (failNextSend) { failNextSend = false; return false; }
        sentMessages.push({ text, attachmentIds });
        return true;
      },
      respond(_request, response) { responses.push(response); },
    };
    const draftRoot = `conversation-browser-${crypto.randomUUID()}`;
    const conversationRenderer = new ConversationRenderer(conversationHost, draftRoot, actions);
    conversationRenderer.setModelCatalog({
      hostId: 'local', defaultModel: 'gpt-5.6', defaultReasoningEffort: 'medium',
      models: [
        { id: 'gpt-5.6', displayName: 'GPT-5.6', defaultReasoningEffort: 'medium', isDefault: true, supportedReasoningEfforts: [
          { reasoningEffort: 'low', description: 'Faster' }, { reasoningEffort: 'medium', description: 'Balanced' }, { reasoningEffort: 'high', description: 'Deeper reasoning' },
        ] },
        { id: 'gpt-5.6-mini', displayName: 'GPT-5.6 mini', defaultReasoningEffort: 'low', isDefault: false, supportedReasoningEfforts: [
          { reasoningEffort: 'low', description: 'Faster' }, { reasoningEffort: 'medium', description: 'Balanced' },
        ] },
      ],
    });
    conversationRenderer.setModelCatalog({
      hostId: 'gpu', defaultModel: 'gpt-5.6-codex', defaultReasoningEffort: 'high',
      models: [{ id: 'gpt-5.6-codex', displayName: 'GPT-5.6 Codex', defaultReasoningEffort: 'high', isDefault: true, supportedReasoningEfforts: [
        { reasoningEffort: 'medium', description: 'Balanced' }, { reasoningEffort: 'high', description: 'Deeper reasoning' },
      ] }],
    });
    let conversationState = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace({
      hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }, { host_id: 'gpu', kind: 'ssh', display_name: 'GPU' }],
      projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }, { project_id: 'gpu-repo', host_id: 'gpu', label: 'Remote fixture', cwd: '/srv/fixture' }],
      conversations: [
        { conversation_id: 'conversation-1', pursuit_id: 'design', host_id: 'local', project_id: 'local-root', model: 'gpt-5.6', reasoning_effort: 'high', thread_title: 'Safe conversation', status: 'waiting_input' },
        { conversation_id: 'conversation-2', pursuit_id: 'design', host_id: 'local', project_id: 'local-root', model: 'gpt-5.6', reasoning_effort: 'medium', thread_title: 'Second conversation', status: 'idle', last_final_event_id: 9, last_read_event_id: 4 },
      ],
      pending_requests: [], pursuit_defaults: { design: { pursuit_id: 'design', host_id: 'gpu', project_id: 'gpu-repo' } }, cursor: 0,
    }) });
    conversationState = reduceConversationState(conversationState, { type: 'pursuit-selected', pursuitId: 'design' });
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loading', conversationId: 'conversation-1' });
    const conversationDetail = normalizeConversationDetail({
      conversation: { conversation_id: 'conversation-1', pursuit_id: 'design', host_id: 'local', project_id: 'local-root', model: 'gpt-5.6', reasoning_effort: 'high', thread_title: 'Safe conversation', status: 'waiting_input' },
      events: [
        { event_id: 1, conversation_id: 'conversation-1', turn_id: null, kind: 'user.message', payload: { text: 'Check this', attachment_ids: ['history-image', 'history-text'] } },
        { event_id: 2, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'turn.started', payload: { turn: { id: 'turn-1' } } },
        { event_id: 3, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'user-1', type: 'userMessage', content: [{ type: 'text', text: 'Check this\nRead the pasted text at this managed absolute path as part of the user message: C:\\internal\\attachment.txt' }] } } },
        { event_id: 4, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { id: 'commentary-1', type: 'agentMessage', phase: 'commentary' } } },
        { event_id: 5, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'agent.message.delta', payload: { item_id: 'commentary-1', delta: 'DRAFT TOKEN **carefully**' } },
        { event_id: 6, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'turn.started', payload: { turn: { id: 'turn-1' } } },
        { event_id: 7, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'protocol.notification', payload: { method: 'turn/started' } },
        { event_id: 8, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'agent.message.delta', payload: { item_id: 'commentary-1', delta: ' with provisional text' } },
        { event_id: 9, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'commentary-1', type: 'agentMessage', phase: 'commentary', content: [{ type: 'text', text: 'Working **carefully** with inline $x^2$ and \\(z^2\\), including $\\sqrt{x}$.\n\n$$y = mx + b$$\n\n\\[\\int_0^1 x\\,dx\\]\n\n<img src=x onerror=alert(1)><svg onload=alert(2)><path d="M0 0"/></svg>' }] } } },
        { event_id: 10, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { id: 'command-1', type: 'commandExecution', command: 'printf lifecycle', status: 'inProgress' } } },
        { event_id: 11, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'command.output', payload: { item_id: 'command-1', delta: 'first\n' } },
        { event_id: 12, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'command.output', payload: { item_id: 'command-1', delta: 'second\n' } },
        { event_id: 13, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'command-1', type: 'commandExecution', command: 'printf lifecycle', aggregatedOutput: 'first\nsecond\nterminal\n', status: 'completed', exitCode: 0 } } },
        { event_id: 14, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { id: 'file-1', type: 'fileChange', path: 'src/example.ts', status: 'inProgress' } } },
        { event_id: 15, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item/fileChange/outputDelta', payload: { item_id: 'file-1', delta: '-old\n' } },
        { event_id: 16, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'file-1', type: 'fileChange', path: 'src/example.ts', diff: '-old\n+new\n', status: 'completed' } } },
        { event_id: 17, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'turn.plan.updated', payload: { steps: [{ step: 'Inspect', status: 'pending' }] } },
        { event_id: 18, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'turn.plan.updated', payload: { steps: [{ step: 'Inspect', status: 'completed' }, { step: 'Verify', status: 'in progress' }] } },
        { event_id: 19, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { type: 'subAgentActivity', id: 'subagent-call-1', kind: 'started', agentThreadId: 'agent-thread-1', agentPath: '/root/inspect_backend' } } },
        { event_id: 20, conversation_id: 'conversation-1', turn_id: 'turn-2', kind: 'item.started', payload: { item: { type: 'subAgentActivity', id: 'subagent-call-2', kind: 'resumed', agentThreadId: 'agent-thread-1', agentPath: '/root/inspect_backend' } } },
        { event_id: 21, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { threadId: 'provider-thread-1', turnId: 'turn-1', startedAtMs: 1720000000000, item: { type: 'collabAgentToolCall', id: 'collab-call-1', tool: 'spawnAgent', status: 'inProgress', senderThreadId: 'provider-thread-1', receiverThreadIds: ['agent-thread-1'], prompt: 'Inspect the attachment backend without changing files.', model: 'gpt-5.6', reasoningEffort: 'high', agentsStates: { 'agent-thread-1': { status: 'running', message: 'Tracing routes' } } } } },
        { event_id: '21.1', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'reasoning.summary_part', payload: { itemId: 'reasoning-1', summaryIndex: 0 } },
        { event_id: '21.2', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'reasoning.summary_delta', payload: { itemId: 'reasoning-1', summaryIndex: 0, delta: 'Inspecting **attachment state**.' } },
        { event_id: '21.3', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'reasoning.summary_delta', payload: { itemId: 'reasoning-1', summaryIndex: 1, delta: 'Checking deletion behavior.' } },
        { event_id: '21.4', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'reasoning-1', type: 'reasoning', summary: ['Confirmed the **staged record**.', 'Verified safe cleanup.'], content: ['PRIVATE CHAIN OF THOUGHT'] } } },
        { event_id: '21.5', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { id: 'mcp-tool-1', type: 'mcpToolCall', server: 'files', tool: 'search', status: 'inProgress', arguments: { query: 'attachment state' } } } },
        { event_id: '21.55', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'mcp.progress', payload: { itemId: 'mcp-tool-1', message: 'Searching the attachment index' } },
        { event_id: '21.6', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'mcp-tool-1', type: 'mcpToolCall', server: 'files', tool: 'search', status: 'completed', arguments: { query: 'attachment state' }, result: { raw: 'GIANT RAW RESULT MUST STAY HIDDEN' }, durationMs: 1250 } } },
        { event_id: '21.7', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'dynamic-tool-1', type: 'dynamicToolCall', namespace: 'browser', tool: 'inspect', status: 'completed', arguments: { path: '/visible/page' }, durationMs: 80 } } },
        { event_id: '21.8', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'web-search-1', type: 'webSearch', query: 'official docs' } } },
        { event_id: '21.9', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'image-view-1', type: 'imageView', path: 'C:\\fixture\\screen.png' } } },
        { event_id: '21.91', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'image-generation-1', type: 'imageGeneration', status: 'completed', revisedPrompt: 'A compact UI preview.' } } },
        { event_id: '21.92', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'review-1', type: 'enteredReviewMode', review: 'Review current changes' } } },
        { event_id: '21.93', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'compact-1', type: 'contextCompaction' } } },
        { event_id: '21.94', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'protocol.notification', payload: { method: 'rawResponseItem/completed', params: { item: { id: 'legacy-raw-message', type: 'message', role: 'assistant', content: [{ type: 'output_text', text: 'DUPLICATE LEGACY RAW OUTPUT' }] } } } },
        { event_id: '21.95', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'protocol.notification', payload: { method: 'rawResponse/completed', params: { response: { output_text: 'DUPLICATE LEGACY RESPONSE' } } } },
        { event_id: '21.96', conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'protocol.notification', payload: { method: 'item/reasoning/textDelta', params: { itemId: 'reasoning-1', delta: 'LEGACY RAW REASONING' } } },
        { event_id: 22, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'future.item', payload: { html: '<iframe srcdoc=bad>' } },
      ],
      attachments: [
        { attachment_id: 'history-image', conversation_id: 'conversation-1', kind: 'image', display_name: 'diagram.png', media_type: 'image/png', byte_size: 2048, state: 'sent' },
        { attachment_id: 'history-text', conversation_id: 'conversation-1', kind: 'pasted_text', display_name: 'Pasted text', media_type: 'text/plain', byte_size: 9000, state: 'sent' },
      ],
      pending_requests: [
        { request_key: 'input-1', conversation_id: 'conversation-1', method: 'item/tool/requestUserInput', state: 'pending', payload: { questions: [
          { id: 'scope', header: 'Scope', question: 'Which scope?', options: [{ label: 'Current branch', description: 'Review the checked-out branch.' }] },
          { id: 'note', header: 'Note', question: 'Anything else?' },
        ] } },
        { request_key: 'permission-1', conversation_id: 'conversation-1', method: 'item/permissions/requestApproval', state: 'pending', payload: { permissions: { network: true } } },
        { request_key: 'mcp-1', conversation_id: 'conversation-1', method: 'mcpServer/elicitation/request', state: 'pending', payload: { prompt: 'Supply MCP content' } },
        { request_key: 'tool-1', conversation_id: 'conversation-1', method: 'item/tool/call', state: 'pending', payload: { prompt: 'Return tool output' } },
        { request_key: 'future-1', conversation_id: 'conversation-1', method: 'future/object/request', state: 'pending', payload: { prompt: 'Return future data' } },
      ],
      has_earlier_events: true,
      cursor: 22,
    });
    check(conversationDetail, 'Conversation detail fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loaded', detail: conversationDetail });
    conversationState = reduceConversationState(conversationState, { type: 'connection', connection: 'open' });
    conversationRenderer.render(conversationState);
    const loadEarlier = conversationHost.querySelector<HTMLButtonElement>('.cw-load-earlier')!;
    check(!loadEarlier.hidden && !loadEarlier.disabled, 'A bounded transcript exposes an earlier-history control');
    loadEarlier.click();
    check(earlierLoads.at(-1) === 'conversation-1', 'The earlier-history control targets the visible conversation');
    loadEarlier.focus();
    conversationState = reduceConversationState(conversationState, {
      type: 'conversation-history-loaded',
      page: { conversationId: 'conversation-1', events: [], hasEarlierEvents: false },
    });
    conversationRenderer.render(conversationState);
    await settled();
    check(loadEarlier.hidden && document.activeElement === conversationHost.querySelector('.cw-activity'), 'When the oldest page is reached, focus moves from the hidden history control to the transcript');
    const unreadConversation = conversationHost.querySelector<HTMLElement>('[data-conversation-id="conversation-2"]')!;
    check(unreadConversation.querySelector('.cw-conversation-new')?.textContent === 'NEW'
      && unreadConversation.getAttribute('aria-label')?.includes('new final response'), 'The conversation list identifies exactly which attached conversation has an unread final response');
    check(conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="host"]')?.value === 'gpu' && conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="project"]')?.value === 'gpu-repo', 'The selected Pursuit restores its recent host and project');
    check(conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="model"]')?.value === 'gpt-5.6-codex' && conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="effort"]')?.value === 'high', 'New conversations use the selected host model defaults');
    check(conversationHost.querySelectorAll('.cw-user').length === 1 && !conversationHost.textContent?.includes('managed absolute path'), 'The local user message owns display text and hides provider-only attachment path instructions');
    check(conversationHost.querySelector<HTMLImageElement>('.cw-sent-image img')?.alt === 'diagram.png' && conversationHost.querySelector('.cw-sent-file')?.textContent?.includes('Pasted text'), 'Sent image and pasted-text attachments render from normalized metadata');
    const runningCommentary = conversationHost.querySelector<HTMLDetailsElement>('.cw-commentary-group')!;
    check(runningCommentary.open && runningCommentary.querySelector('.cw-commentary')?.textContent?.includes('Working carefully') && !conversationHost.textContent?.includes('DRAFT TOKEN'), 'Commentary stays expanded while work is running and completion replaces streamed text by item id');
    check(runningCommentary.querySelector('.cw-commentary > small')?.textContent === 'UPDATE' && runningCommentary.querySelector('strong'), 'Commentary uses its phase label and renders Markdown structure');
    check(runningCommentary.querySelectorAll('.katex').length >= 4 && runningCommentary.querySelectorAll('.katex-display').length >= 2, 'Conversation rich text renders both supported inline and display math delimiter styles');
    check(runningCommentary.querySelector('.katex svg path'), 'KaTeX formulas that require SVG glyphs remain intact after sanitization');
    check(!conversationHost.querySelector('.cw-message-text img,script,iframe,object,embed,foreignObject,image,animate,animateMotion,animateTransform,set,[onerror],[onload]')
      && [...conversationHost.querySelectorAll('.cw-message-text svg')].every((svg) => svg.closest('.katex')), 'Raw HTML cannot create active content, while sanitized KaTeX SVG stays scoped to formulas');
    const commandCard = conversationHost.querySelector<HTMLElement>('.cw-command')!;
    check(conversationHost.querySelectorAll('.cw-command').length === 1 && commandCard.querySelector('pre')?.textContent === 'first\nsecond\nterminal\n' && commandCard.textContent?.includes('exit 0'), 'Command started, output, and completed events coalesce into one card with terminal output once');
    check(conversationHost.querySelectorAll('.cw-file').length === 1 && conversationHost.querySelector('.cw-file pre')?.textContent === '-old\n+new\n', 'File-change deltas and completion coalesce into one card with the completed diff');
    check(conversationHost.querySelectorAll('.cw-plan').length === 1 && [...conversationHost.querySelectorAll('.cw-plan li')].map((entry) => entry.textContent).join('|') === 'Inspect — completed|Verify — in progress', 'Plan lifecycle updates replace one stable plan card instead of duplicating it');
    check(conversationHost.querySelectorAll('.cw-subagent-activity').length === 2 && conversationHost.querySelectorAll('.cw-commentary-group').length === 2, 'The same child agent resumed in a later parent turn stays visible in that later turn instead of coalescing across turns');
    check(conversationHost.querySelector('.cw-subagent-activity')?.textContent?.includes('inspect backend') && conversationHost.querySelector('.cw-collab-activity')?.textContent?.includes('Started an agent') && conversationHost.querySelector('.cw-agent-states')?.textContent?.includes('agent thread 1') && conversationHost.querySelector('.cw-agent-states')?.textContent?.includes('Tracing routes'), 'Top-level App Server collaboration items preserve agentsStates map identities in compact work-detail activity');
    const reasoningSummary = conversationHost.querySelector<HTMLElement>('.cw-reasoning-summary')!;
    check(reasoningSummary.textContent?.includes('Confirmed the staged record.')
      && reasoningSummary.textContent?.includes('Verified safe cleanup.')
      && reasoningSummary.querySelector('strong')
      && !conversationHost.textContent?.includes('PRIVATE CHAIN OF THOUGHT'), 'Provider reasoning summaries replace streamed parts, render rich text, and never expose raw reasoning content');
    check(conversationHost.querySelectorAll('.cw-provider-activity').length === 7
      && conversationHost.textContent?.includes('MCP · files · search')
      && conversationHost.textContent?.includes('Tool · inspect')
      && conversationHost.textContent?.includes('Searching the web')
      && conversationHost.textContent?.includes('Viewing an image')
      && conversationHost.textContent?.includes('Generating an image')
      && conversationHost.textContent?.includes('Entered review mode')
      && conversationHost.textContent?.includes('Compacting conversation context')
      && !conversationHost.textContent?.includes('GIANT RAW RESULT MUST STAY HIDDEN'), 'Known provider work items, including MCP progress, coalesce into compact safe activity rows without dumping raw results');
    check(['.cw-commentary', '.cw-reasoning-summary', '.cw-command', '.cw-file', '.cw-plan', '.cw-subagent-activity', '.cw-collab-activity', '.cw-provider-activity']
      .every((selector) => runningCommentary.contains(conversationHost.querySelector(selector))), 'Commentary, command, file, plan, and agent activity share one per-turn Work details group');
    check(!conversationHost.querySelector('.cw-unknown') && !conversationHost.textContent?.includes('future.item') && !conversationHost.textContent?.includes('turn/started')
      && !conversationHost.textContent?.includes('DUPLICATE LEGACY RAW OUTPUT')
      && !conversationHost.textContent?.includes('DUPLICATE LEGACY RESPONSE')
      && !conversationHost.textContent?.includes('LEGACY RAW REASONING'), 'Raw lifecycle, legacy provider output, private reasoning, and unknown event cards stay hidden');
    for (const rawEvent of [
      { event_id: 23, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.started', payload: { item: { id: 'answer-1', type: 'agentMessage', phase: 'final_answer' } } },
      { event_id: 24, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'agent.message.delta', payload: { item_id: 'answer-1', delta: '# PROVISIONAL STREAM\n\n- old result' } },
    ]) {
      const normalized = normalizeEvent(rawEvent);
      check(normalized, 'Phase-aware agent event fixture must normalize');
      conversationState = reduceConversationState(conversationState, { type: 'event', event: normalized });
    }
    conversationRenderer.render(conversationState);
    const streamingCommentary = conversationHost.querySelector<HTMLDetailsElement>('.cw-commentary-group')!;
    check(streamingCommentary.open && conversationHost.querySelector('.cw-final-answer')?.textContent?.includes('PROVISIONAL STREAM'), 'Commentary stays expanded while the final answer is still streaming');
    const commandBeforeFinal = conversationHost.querySelector<HTMLElement>('.cw-command > summary')!;
    commandBeforeFinal.focus();
    const completedAnswer = normalizeEvent({ event_id: 25, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { id: 'answer-1', type: 'agentMessage', phase: 'final_answer', content: [{ type: 'text', text: '## Final result\n\n- kept result\n\n`const ready = true`\n\n[reference](https://example.com/) · [unsafe](javascript:alert(1))\n\n![remote tracker](https://example.com/tracker.png)\n\n<script>alert(1)</script>' }] } } });
    check(completedAnswer, 'Completed final-answer fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: completedAnswer });
    conversationRenderer.render(conversationState);
    const completedCommentary = conversationHost.querySelector<HTMLDetailsElement>('.cw-commentary-group')!;
    check(!completedCommentary.open && completedCommentary.querySelector('summary')?.textContent === 'Work details', 'Commentary automatically collapses after the final answer completes');
    check(document.activeElement === completedCommentary.querySelector(':scope > summary'), 'Final-answer collapse moves nested work focus to the visible Work details summary');
    completedCommentary.open = true;
    check(completedCommentary.open && completedCommentary.querySelector('.cw-commentary'), 'Collapsed work details remain manually expandable');
    const expandedCommand = conversationHost.querySelector<HTMLDetailsElement>('.cw-command')!;
    expandedCommand.open = true;
    expandedCommand.querySelector<HTMLElement>('summary')!.focus();
    const laterOperationalEvent = normalizeEvent({ event_id: 26, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'protocol.notification', payload: { method: 'thread/name/updated' } });
    check(laterOperationalEvent, 'Later operational event fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: laterOperationalEvent });
    conversationRenderer.render(conversationState);
    const preservedCommentary = conversationHost.querySelector<HTMLDetailsElement>('.cw-commentary-group')!;
    const preservedCommand = conversationHost.querySelector<HTMLDetailsElement>('.cw-command')!;
    check(preservedCommentary.open && preservedCommand.open, 'Manual disclosure choices survive later activity rerenders');
    check(document.activeElement === preservedCommand.querySelector('summary'), 'Keyboard focus follows its stable activity item across a rerender');
    for (const rawEvent of [
      { event_id: 27, conversation_id: 'conversation-1', turn_id: 'turn-2', kind: 'turn.completed', payload: { turn: { status: 'interrupted' } } },
      { event_id: 28, conversation_id: 'conversation-1', turn_id: 'turn-3', kind: 'item.completed', payload: { item: { id: 'failed-commentary', type: 'agentMessage', phase: 'commentary', content: [{ type: 'text', text: 'Trying a risky step.' }] } } },
      { event_id: 29, conversation_id: 'conversation-1', turn_id: 'turn-3', kind: 'turn.completed', payload: { turn: { status: 'failed' } } },
    ]) {
      const normalized = normalizeEvent(rawEvent);
      check(normalized, 'Terminal work-detail fixture must normalize');
      conversationState = reduceConversationState(conversationState, { type: 'event', event: normalized });
    }
    conversationRenderer.render(conversationState);
    const interruptedWork = conversationHost.querySelector<HTMLDetailsElement>('[data-activity-key="commentary:turn-2"]')!;
    const failedWork = conversationHost.querySelector<HTMLDetailsElement>('[data-activity-key="commentary:turn-3"]')!;
    check(!interruptedWork.open && interruptedWork.querySelector('summary')?.textContent === 'Work details · Stopped'
      && !failedWork.open && failedWork.querySelector('summary')?.textContent === 'Work details · Failed', 'Interrupted and failed turns collapse their work details instead of remaining stuck on Working');
    check(conversationHost.querySelectorAll('.cw-agent').length === 3 && conversationHost.querySelectorAll('.cw-commentary').length === 2 && conversationHost.querySelectorAll('.cw-final-answer').length === 1, 'Distinct commentary and final-answer item ids each produce exactly one phase bubble');
    const finalAnswer = conversationHost.querySelector<HTMLElement>('.cw-final-answer')!;
    check(finalAnswer.querySelector('small')?.textContent === 'ANSWER' && finalAnswer.querySelector('h2') && finalAnswer.querySelector('li')?.textContent === 'kept result' && finalAnswer.querySelector('code')?.textContent === 'const ready = true' && !finalAnswer.textContent?.includes('PROVISIONAL STREAM'), 'The final answer renders completed Markdown without retaining streamed provisional content');
    const safeLink = finalAnswer.querySelector<HTMLAnchorElement>('a[href="https://example.com/"]');
    check(safeLink?.target === '_blank' && safeLink.rel.includes('noopener') && !conversationHost.querySelector('.cw-message-text img,script,iframe,object,embed,foreignObject,image,animate,animateMotion,animateTransform,set,[onerror],[onload]') && ![...conversationHost.querySelectorAll<HTMLAnchorElement>('a')].some((link) => link.getAttribute('href')?.trim().toLowerCase().startsWith('javascript:')), 'Links are hardened while raw HTML and dangerous URLs remain harmless');
    check(!conversationHost.textContent?.includes('(empty message)'), 'Empty item-started events do not create placeholder message text');
    const turnModel = conversationHost.querySelector<HTMLSelectElement>('.cw-composer [name="model"]')!;
    const turnEffort = conversationHost.querySelector<HTMLSelectElement>('.cw-composer [name="effort"]')!;
    check(!turnModel.disabled && !turnEffort.disabled && turnModel.value === 'gpt-5.6' && turnEffort.value === 'high', 'Model and reasoning selectors remain available while a turn awaits input');
    turnModel.value = 'gpt-5.6-mini';
    turnModel.dispatchEvent(new Event('change', { bubbles: true }));
    const switchedEffort = conversationHost.querySelector<HTMLSelectElement>('.cw-composer [name="effort"]')!.value;
    check(switchedEffort === 'low' && JSON.stringify(settingsUpdates.at(-1)) === JSON.stringify({ model: 'gpt-5.6-mini', reasoningEffort: 'low' }), 'Changing the model mid-conversation selects a supported reasoning default and saves both settings');
    conversationState = reduceConversationState(conversationState, { type: 'conversation-settings-selected', conversationId: 'conversation-1', model: 'gpt-5.6-mini', reasoningEffort: 'low' });
    conversationRenderer.render(conversationState);
    turnEffort.value = 'medium';
    turnEffort.dispatchEvent(new Event('change', { bubbles: true }));
    check(JSON.stringify(settingsUpdates.at(-1)) === JSON.stringify({ model: 'gpt-5.6-mini', reasoningEffort: 'medium' }), 'Reasoning effort can change between messages without creating a new conversation');
    check(modelCatalogLoads.length === 0, 'Preloaded model catalogs are reused without duplicate requests');
    const questionCard = conversationHost.querySelector<HTMLElement>('.cw-request:first-child')!;
    questionCard.querySelector<HTMLInputElement>('input[type="radio"]')!.checked = true;
    questionCard.querySelector<HTMLTextAreaElement>('textarea')!.value = 'Keep it concise';
    questionCard.querySelector<HTMLFormElement>('form')!.requestSubmit();
    check(JSON.stringify(responses[0]) === JSON.stringify({ response: { scope: { answers: ['Current branch'] }, note: { answers: ['Keep it concise'] } } }), 'Question responses preserve answers by question id');
    conversationState = reduceConversationState(conversationState, { type: 'response-in-flight', key: 'permission-1', active: true });
    conversationRenderer.render(conversationState);
    check([...conversationHost.querySelectorAll<HTMLButtonElement>('[data-request-key="permission-1"] button')].every((entry) => entry.disabled), 'An in-flight permission response cannot be submitted twice');
    conversationState = reduceConversationState(conversationState, { type: 'response-in-flight', key: 'permission-1', active: false });
    conversationRenderer.render(conversationState);
    const permissionCard = conversationHost.querySelector<HTMLElement>('[data-request-key="permission-1"]')!;
    [...permissionCard.querySelectorAll('button')].find((entry) => entry.textContent === 'Deny')!.click();
    check(JSON.stringify(responses[1]) === JSON.stringify({ decision: 'decline' }), 'Permission requests can be denied with the protocol decision shape');
    const mcpCard = conversationHost.querySelector<HTMLElement>('[data-request-key="mcp-1"]')!;
    const mcpInput = mcpCard.querySelector<HTMLTextAreaElement>('textarea')!;
    mcpInput.value = '{"kept":true}';
    conversationState = reduceConversationState(conversationState, { type: 'response-in-flight', key: 'mcp-1', active: true });
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector<HTMLTextAreaElement>('[data-request-key="mcp-1"] textarea')?.value === '{"kept":true}', 'In-flight response controls retain entered JSON');
    conversationState = reduceConversationState(conversationState, { type: 'response-in-flight', key: 'mcp-1', active: false });
    conversationRenderer.render(conversationState);
    mcpInput.value = '{bad'; mcpCard.querySelector<HTMLFormElement>('form')!.requestSubmit();
    check(responses.length === 2 && mcpCard.textContent?.includes('Enter valid JSON.'), 'MCP elicitation rejects malformed JSON locally');
    mcpInput.value = '{"choice":"safe"}'; mcpCard.querySelector<HTMLFormElement>('form')!.requestSubmit();
    check(JSON.stringify(responses[2]) === JSON.stringify({ decision: 'accept', response: { choice: 'safe' } }), 'MCP elicitation submits validated object content');
    for (const [key, value] of [['tool-1', 'tool'], ['future-1', 'future']] as const) {
      const card = conversationHost.querySelector<HTMLElement>(`[data-request-key="${key}"]`)!;
      card.querySelector<HTMLTextAreaElement>('textarea')!.value = `{"kind":"${value}"}`;
      card.querySelector<HTMLFormElement>('form')!.requestSubmit();
    }
    check(JSON.stringify(responses.slice(3)) === JSON.stringify([{ response: { kind: 'tool' } }, { response: { kind: 'future' } }]), 'Tool-call and future object requests remain answerable');
    const failedResponse = normalizeEvent({ event_id: 30, conversation_id: 'conversation-1', kind: 'server_response_failed', payload: { request_key: 'future-1' } });
    check(failedResponse, 'Failed-response fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: failedResponse });
    conversationRenderer.render(conversationState);
    check(!conversationHost.querySelector('[data-request-key="future-1"]'), 'An uncertain server response removes its terminal request card');
    check(conversationHost.querySelector('.cw-reload')?.getAttribute('aria-label') === 'Reload conversation', 'Reload has an accessible name');
    check(conversationHost.querySelector<HTMLButtonElement>('.cw-composer button[type="submit"]')?.disabled && !conversationHost.querySelector<HTMLTextAreaElement>('.cw-composer textarea')?.disabled, 'Provider-busy state disables Send without locking the draft');
    conversationState = reduceConversationState(conversationState, { type: 'interrupt-in-flight', conversationId: 'conversation-1', active: true });
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector<HTMLButtonElement>('.cw-stop')?.disabled && conversationHost.querySelector('.cw-stop')?.textContent === 'Stopping…', 'An in-flight interrupt cannot be submitted twice');
    conversationState = reduceConversationState(conversationState, { type: 'interrupt-in-flight', conversationId: 'conversation-1', active: false });
    conversationState = reduceConversationState(conversationState, { type: 'host-create-in-flight', active: true });
    conversationRenderer.render(conversationState);
    check([...conversationHost.querySelectorAll<HTMLInputElement | HTMLButtonElement>('.cw-add-host input, .cw-add-host button')].every((entry) => entry.disabled), 'An in-flight host creation disables its form');
    conversationState = reduceConversationState(conversationState, { type: 'host-create-in-flight', active: false });
    const idle = normalizeEvent({ event_id: 31, conversation_id: 'conversation-1', kind: 'thread.status', payload: { status: { type: 'idle' } } });
    check(idle, 'Idle status fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: idle });
    conversationRenderer.render(conversationState);
    conversationState = reduceConversationState(conversationState, { type: 'send-in-flight', conversationId: 'conversation-1', active: true });
    conversationRenderer.render(conversationState);
    const preparingButton = conversationHost.querySelector<HTMLButtonElement>('.cw-send-stop')!;
    const preparingWork = conversationHost.querySelector<HTMLDetailsElement>('[data-activity-key="commentary:__pending__"]')!;
    check(preparingButton.textContent === 'Preparing…' && preparingButton.disabled && preparingButton.dataset.mode === 'send'
      && preparingWork.open && preparingWork.querySelector('summary')?.textContent?.startsWith('Preparing…'), 'A send shows immediate live work without exposing Stop before the provider has accepted a turn');
    const startingConversation = conversationState.conversations.find((entry) => entry.conversationId === 'conversation-1')!;
    conversationState = reduceConversationState(conversationState, { type: 'conversation-updated', conversation: { ...startingConversation, status: 'starting' } });
    const startedTurn = normalizeEvent({ event_id: '31.1', conversation_id: 'conversation-1', turn_id: 'turn-4', kind: 'turn.started', payload: { turn: { id: 'turn-4' } } });
    check(startedTurn, 'Started turn fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: startedTurn });
    conversationRenderer.render(conversationState);
    const stopButton = conversationHost.querySelector<HTMLButtonElement>('.cw-send-stop')!;
    check(stopButton.textContent === 'Stop' && !stopButton.disabled && stopButton.dataset.mode === 'stop'
      && conversationHost.querySelector('[data-activity-key="commentary:turn-4"] summary')?.textContent?.startsWith('Working…'), 'The unified Send button becomes Stop only after the turn is interruptible');
    stopButton.click(); stopButton.click();
    const stoppingButton = conversationHost.querySelector<HTMLButtonElement>('.cw-send-stop')!;
    check(interrupts === 1 && stoppingButton.disabled && stoppingButton.textContent === 'Stopping…'
      && conversationHost.querySelector('[data-activity-key="commentary:turn-4"] summary')?.textContent?.startsWith('Stopping…'), 'Stop is latched immediately, invokes interrupt once, and keeps visible stopping state');
    const stoppedTurn = normalizeEvent({ event_id: '31.2', conversation_id: 'conversation-1', turn_id: 'turn-4', kind: 'turn.interrupted', payload: { turn: { id: 'turn-4', status: 'interrupted' } } });
    check(stoppedTurn, 'Interrupted turn fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: stoppedTurn });
    conversationState = reduceConversationState(conversationState, { type: 'send-in-flight', conversationId: 'conversation-1', active: false });
    const stoppedConversation = conversationState.conversations.find((entry) => entry.conversationId === 'conversation-1')!;
    conversationState = reduceConversationState(conversationState, { type: 'conversation-updated', conversation: { ...stoppedConversation, status: 'idle' } });
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector('[data-activity-key="commentary:turn-4"] summary')?.textContent === 'Work details · Stopped'
      && conversationHost.querySelector<HTMLButtonElement>('.cw-send-stop')?.textContent === 'Send', 'A terminal interrupt ends the live indicator and restores the unified Send action');
    const composer = conversationHost.querySelector<HTMLTextAreaElement>('.cw-composer textarea')!;
    composer.value = 'Send from composer'; composer.dispatchEvent(new Event('input', { bubbles: true }));
    composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    composer.value = 'Keep newline'; composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true }));
    check(sentMessages.length === 1 && sentMessages[0].text === 'Send from composer' && sentMessages[0].attachmentIds.length === 0, 'Composer Enter sends while Shift+Enter stays local');
    check(localStorage.getItem(`rightmemory:conversation-draft:${encodeURIComponent(draftRoot)}:conversation-1`) === 'Send from composer', 'Conversation drafts use a root-scoped storage key');
    composer.value = 'Next draft'; composer.dispatchEvent(new Event('input', { bubbles: true }));
    conversationRenderer.clearComposerIfUnchanged('Send from composer');
    check(composer.value === 'Next draft', 'A late send response does not erase text typed for the next message');

    const paste = (data: DataTransfer): Event => {
      const event = new Event('paste', { bubbles: true, cancelable: true });
      Object.defineProperty(event, 'clipboardData', { value: data });
      composer.dispatchEvent(event);
      return event;
    };
    const smallPaste = new DataTransfer();
    smallPaste.setData('text/plain', 'short paste');
    check(!paste(smallPaste).defaultPrevented, 'Ordinary text paste stays in the normal textarea path');
    failNextUpload = true;
    const failedPaste = new DataTransfer();
    failedPaste.setData('text/plain', 'F'.repeat(8_000));
    check(paste(failedPaste).defaultPrevented, 'A large paste enters managed upload before failure is known');
    await until(() => conversationHost.querySelector('.cw-pasted-text')?.textContent?.includes('Upload failed'), 'Failed pasted text should remain visibly staged');
    check(conversationHost.querySelector('.cw-pasted-text')?.textContent?.includes('fixture upload failed')
      && conversationHost.querySelector('.cw-composer-notice')?.textContent?.includes('fixture upload failed'), 'The visible chip and composer notice expose the actual upload rejection reason');
    composer.value = 'Must not send without the failed paste'; composer.dispatchEvent(new Event('input', { bubbles: true }));
    check(conversationHost.querySelector<HTMLButtonElement>('.cw-composer button[type="submit"]')?.disabled, 'A failed attachment blocks Send until the user retries or removes it');
    const retryUpload = conversationHost.querySelector<HTMLButtonElement>('.cw-pasted-text .cw-attachment-retry')!;
    check(retryUpload.textContent === 'Retry' && !retryUpload.disabled, 'A failed initial upload exposes a usable Retry action while retaining the local file');
    retryUpload.click();
    await until(() => conversationHost.querySelector('.cw-pasted-text')?.textContent?.includes('Ready'), 'Retrying a failed pasted-text upload should stage the retained file');
    check(uploadedFiles[0]?.name.startsWith('pasted-text-') && uploadedFiles[0].size === 8_000 && composer.value === 'Must not send without the failed paste'
      && uploadAttempts.length >= 2 && uploadAttempts.at(-2)?.attachmentId === uploadAttempts.at(-1)?.attachmentId
      && /^[0-9a-f]{32}$/.test(uploadAttempts.at(-1)?.attachmentId ?? ''), 'Attachment retry reuses one stable lowercase-hex upload identity and the retained file without changing the draft');
    failNextSend = true;
    composer.value = ''; composer.dispatchEvent(new Event('input', { bubbles: true }));
    composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    await until(() => conversationHost.querySelector('.cw-pasted-text')?.textContent?.includes('Ready')
      && !conversationHost.querySelector<HTMLButtonElement>('.cw-pasted-text .cw-attachment-remove')?.disabled, 'A rejected send should release its staged attachment');
    check(sentMessages.length === 1, 'A rejected send keeps the attachment available without recording a successful message');
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loading', conversationId: 'conversation-2' });
    conversationRenderer.render(conversationState);
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loading', conversationId: 'conversation-1' });
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loaded', detail: conversationDetail });
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector('.cw-pasted-text')?.textContent?.includes('Pasted text'), 'Staged attachment chips survive conversation switching within the page session');
    composer.value = ''; composer.dispatchEvent(new Event('input', { bubbles: true }));
    composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    check(sentMessages[1]?.text === '' && JSON.stringify(sentMessages[1]?.attachmentIds) === JSON.stringify(['uploaded-1']), 'An attachment can be sent without message text');

    const imagePaste = new DataTransfer();
    imagePaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'clipboard.png', { type: 'image/png' }));
    check(paste(imagePaste).defaultPrevented, 'Pasted images are intercepted for managed upload');
    check(!conversationHost.querySelector('.cw-attachment-chip.cw-image img')
      && conversationHost.querySelector('.cw-attachment-chip.cw-image')?.textContent?.includes('Uploading'), 'An image is not decoded or rendered while its upload is still untrusted');
    await until(() => conversationHost.querySelector('.cw-image')?.textContent?.includes('Ready'), 'Pasted image should finish staging');
    conversationRenderer.clearComposerIfUnchanged('', ['uploaded-1']);
    check(!conversationHost.querySelector('.cw-pasted-text') && conversationHost.querySelector<HTMLImageElement>('.cw-attachment-chip.cw-image img'), 'A successful send clears only its attachment and preserves a newly pasted image');
    failNextDelete = true;
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-chip.cw-image .cw-attachment-remove')!.click();
    await until(() => conversationHost.querySelector('.cw-composer-notice')?.textContent?.includes('fixture delete failed'), 'Failed attachment deletion should report its backend error');
    check(conversationHost.querySelector('.cw-attachment-chip.cw-image')?.textContent?.includes('Ready'), 'A failed backend deletion restores the staged chip so it remains sendable or removable');
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-chip.cw-image .cw-attachment-remove')!.click();
    await until(() => deletedAttachmentIds.filter((id) => id === 'uploaded-2').length === 2 && !conversationHost.querySelector('.cw-attachment-chip'), 'Retrying staged image removal should delete its server attachment and clear the chip');
    check(deletedAttachmentIds.filter((id) => id === 'uploaded-2').length === 2, 'Image removal retries exactly after the failed backend deletion');
    notFoundNextDelete = true;
    conversationState = {
      ...conversationState,
      attachmentsByConversation: {
        ...conversationState.attachmentsByConversation,
        'conversation-1': [
          ...(conversationState.attachmentsByConversation['conversation-1'] ?? []),
          {
            attachmentId: 'already-deleted', conversationId: 'conversation-1', kind: 'image', displayName: 'already-deleted.png',
            mediaType: 'image/png', byteSize: 4, state: 'staged', url: '', raw: {},
          },
        ],
      },
    };
    conversationRenderer.render(conversationState);
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-remove')!.click();
    await until(() => !conversationHost.querySelector('.cw-attachment-chip'), 'A not-found delete response should clear the stale local chip');
    check(!conversationHost.querySelector('.cw-composer-notice')?.textContent?.includes('not found'), 'Already-absent attachment deletion is treated as successful cleanup');
    conversationState = {
      ...conversationState,
      attachmentsByConversation: {
        ...conversationState.attachmentsByConversation,
        'conversation-1': [
          ...(conversationState.attachmentsByConversation['conversation-1'] ?? [])
            .filter((attachment) => attachment.attachmentId !== 'already-deleted'),
          {
            attachmentId: 'stale-server-attachment', conversationId: 'conversation-1', kind: 'file', displayName: 'stale.zip',
            mediaType: 'application/zip', byteSize: 4, state: 'staged', url: '', raw: {},
          },
        ],
      },
    };
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector('[data-attachment-id="stale-server-attachment"]'), 'An authoritative staged server attachment is restored');
    conversationState = {
      ...conversationState,
      attachmentsByConversation: {
        ...conversationState.attachmentsByConversation,
        'conversation-1': (conversationState.attachmentsByConversation['conversation-1'] ?? [])
          .filter((attachment) => attachment.attachmentId !== 'stale-server-attachment'),
      },
    };
    conversationRenderer.render(conversationState);
    check(!conversationHost.querySelector('[data-attachment-id="stale-server-attachment"]'), 'A later authoritative snapshot prunes an orphaned ready chip');
    const mixedPaste = new DataTransfer();
    mixedPaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'mixed.png', { type: 'image/png' }));
    mixedPaste.setData('text/plain', 'M'.repeat(8_000));
    check(paste(mixedPaste).defaultPrevented, 'A mixed image and large-text paste is handled as one managed paste action');
    await until(() => conversationHost.querySelectorAll('.cw-attachment-chip').length === 2
      && [...conversationHost.querySelectorAll('.cw-attachment-chip')].every((chip) => chip.textContent?.includes('Ready')), 'Both parts of a mixed paste should finish staging');
    check(conversationHost.querySelector('.cw-attachment-chip.cw-image') && conversationHost.querySelector('.cw-attachment-chip.cw-pasted-text'), 'Mixed paste retains both the image and large text');
    for (const remove of [...conversationHost.querySelectorAll<HTMLButtonElement>('.cw-attachment-remove')]) remove.click();
    await until(() => deletedAttachmentIds.includes('uploaded-3') && deletedAttachmentIds.includes('uploaded-4'), 'Mixed-paste cleanup should delete both staged attachments');
    composer.value = 'Before ';
    composer.setSelectionRange(composer.value.length, composer.value.length);
    const mixedSmallPaste = new DataTransfer();
    mixedSmallPaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'mixed-small.png', { type: 'image/png' }));
    mixedSmallPaste.setData('text/plain', 'after');
    check(paste(mixedSmallPaste).defaultPrevented, 'A mixed image and ordinary-text paste is fully handled');
    await until(() => conversationHost.querySelector('.cw-attachment-chip.cw-image')?.textContent?.includes('Ready'), 'Mixed small-text image should finish staging');
    check(composer.value === 'Before after', 'Ordinary text accompanying a pasted image is inserted at the textarea selection');
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-remove')!.click();
    await until(() => deletedAttachmentIds.includes('uploaded-5'), 'Mixed small-text image cleanup should delete its staged attachment');
    const excessiveImagePaste = new DataTransfer();
    for (let index = 0; index < 5; index++) {
      excessiveImagePaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], `count-${index}.png`, { type: 'image/png' }));
    }
    const attemptsBeforeExcessiveImages = uploadAttempts.length;
    check(paste(excessiveImagePaste).defaultPrevented, 'A multi-image clipboard paste is managed as one action');
    check(conversationHost.querySelectorAll('.cw-attachment-chip').length === 4
      && conversationHost.querySelector('.cw-composer-notice')?.textContent?.includes('including 4 images'), 'The fifth image is rejected by client-side count preflight before upload');
    await until(() => [...conversationHost.querySelectorAll('.cw-attachment-chip')].every((chip) => chip.textContent?.includes('Ready')), 'Accepted images should complete their bounded upload queue');
    const firstReadyImage = conversationHost.querySelector<HTMLImageElement>('.cw-attachment-chip.cw-image img');
    check(firstReadyImage?.loading === 'lazy' && firstReadyImage.decoding === 'async', 'Ready staged images use deferred loading and asynchronous decoding');
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector<HTMLImageElement>('.cw-attachment-chip.cw-image img') === firstReadyImage, 'Rerendering keeps the keyed staged image node instead of decoding it again');
    check(uploadAttempts.length === attemptsBeforeExcessiveImages + 4 && maximumUploadsInFlight === 1, 'Clipboard uploads are serialized and never run concurrently');
    for (const remove of [...conversationHost.querySelectorAll<HTMLButtonElement>('.cw-attachment-remove')]) remove.click();
    await until(() => !conversationHost.querySelector('.cw-attachment-chip'), 'Count-preflight fixture attachments should clean up');

    const genericImagePaste = new DataTransfer();
    genericImagePaste.items.add(new File([new Uint8Array([82, 73, 70, 70])], 'clipboard.webp', { type: 'image/webp' }));
    const attemptsBeforeGenericImage = uploadAttempts.length;
    check(paste(genericImagePaste).defaultPrevented, 'Non-native clipboard images are intercepted as managed files');
    check(!conversationHost.querySelector('.cw-attachment-chip.cw-file img')
      && conversationHost.querySelector('.cw-attachment-chip.cw-file')?.textContent?.includes('Uploading'), 'WebP stages as a generic FILE without eager image decoding or preview');
    await until(() => conversationHost.querySelector('.cw-attachment-chip.cw-file')?.textContent?.includes('Ready'), 'A generic clipboard image should finish staging');
    check(uploadAttempts.length === attemptsBeforeGenericImage + 1
      && uploadAttempts.at(-1)?.attachmentKind === 'file'
      && conversationHost.querySelector('.cw-attachment-chip.cw-file')?.textContent?.includes('FILE')
      && !!conversationHost.querySelector('.cw-attachment-chip.cw-file .cw-attachment-download'), 'WebP uses the explicit generic-file upload contract and exposes a download action');
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-chip.cw-file .cw-attachment-remove')!.click();
    await until(() => !conversationHost.querySelector('.cw-attachment-chip'), 'Generic clipboard file cleanup should remove its staged chip');

    const attachButton = conversationHost.querySelector<HTMLButtonElement>('.cw-attach')!;
    const fileInput = conversationHost.querySelector<HTMLInputElement>('.cw-file-input')!;
    let pickerClicks = 0;
    Object.defineProperty(fileInput, 'click', { configurable: true, value: () => { pickerClicks++; } });
    attachButton.click();
    check(pickerClicks === 1 && fileInput.multiple && fileInput.hidden, 'The paperclip opens a hidden multi-file picker');
    const selectedFiles = new DataTransfer();
    selectedFiles.items.add(new File(['selected text'], 'selected.txt', { type: 'text/plain' }));
    selectedFiles.items.add(new File([new Uint8Array([80, 75, 3, 4])], 'archive.zip', { type: 'application/zip' }));
    const attemptsBeforeSelection = uploadAttempts.length;
    Object.defineProperty(fileInput, 'files', { configurable: true, value: selectedFiles.files });
    fileInput.dispatchEvent(new Event('change', { bubbles: true }));
    check(conversationHost.querySelectorAll('.cw-attachment-chip.cw-file').length === 2
      && !conversationHost.querySelector('.cw-attachment-chip.cw-file img'), 'Selected text and ZIP files stage as generic FILE chips without previews');
    await until(() => [...conversationHost.querySelectorAll('.cw-attachment-chip.cw-file')].every((chip) => chip.textContent?.includes('Ready')), 'Selected generic files should complete staging');
    check(uploadAttempts.length === attemptsBeforeSelection + 2
      && uploadAttempts.slice(-2).every((attempt) => attempt.attachmentKind === 'file')
      && conversationHost.querySelectorAll('.cw-attachment-chip.cw-file .cw-attachment-download').length === 2
      && maximumUploadsInFlight === 1, 'File-picker uploads preserve generic-file identity and remain serialized');
    for (const remove of [...conversationHost.querySelectorAll<HTMLButtonElement>('.cw-attachment-remove')]) remove.click();
    await until(() => !conversationHost.querySelector('.cw-attachment-chip'), 'File-picker fixture attachments should clean up');

    failNextUpload = true;
    const removableFailure = new DataTransfer();
    removableFailure.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'invalid.png', { type: 'image/png' }));
    paste(removableFailure);
    await until(() => conversationHost.querySelector('.cw-attachment-chip')?.textContent?.includes('Upload failed'), 'A rejected image remains removable without a preview');
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-remove')!.click();
    check(!conversationHost.querySelector('.cw-attachment-chip')
      && !conversationHost.querySelector('.cw-composer-notice')?.textContent, 'Removing a rejected image clears its stale failure notice');

    let releaseReconcileUpload = () => {};
    uploadGate = new Promise<void>((resolve) => { releaseReconcileUpload = resolve; });
    echoNextUploadId = true;
    const reconcilingImage = new DataTransfer();
    reconcilingImage.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'reconcile.png', { type: 'image/png' }));
    const attemptsBeforeReconcile = uploadAttempts.length;
    paste(reconcilingImage);
    await until(() => uploadAttempts.length === attemptsBeforeReconcile + 1, 'The reconciliation fixture upload should start');
    const reconcilingUploadId = uploadAttempts.at(-1)!.attachmentId;
    conversationState = {
      ...conversationState,
      attachmentsByConversation: {
        ...conversationState.attachmentsByConversation,
        'conversation-1': [
          ...(conversationState.attachmentsByConversation['conversation-1'] ?? []),
          {
            attachmentId: reconcilingUploadId, conversationId: 'conversation-1', kind: 'image',
            displayName: 'reconcile.png', mediaType: 'image/png', byteSize: 4, state: 'staged', url: '', raw: {},
          },
        ],
      },
    };
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelectorAll('.cw-attachment-chip.cw-image').length === 1,
      'A staged server record reconciles with its in-flight upload identity instead of creating a duplicate chip');
    const reconciledImage = conversationHost.querySelector<HTMLImageElement>('.cw-attachment-chip.cw-image img');
    releaseReconcileUpload();
    await until(() => conversationHost.querySelector('.cw-attachment-chip.cw-image')?.textContent?.includes('Ready')
      && uploadsInFlight === 0, 'The reconciled upload should finish');
    check(conversationHost.querySelector<HTMLImageElement>('.cw-attachment-chip.cw-image img') === reconciledImage,
      'The reconciled upload keeps its keyed image node when the local preview becomes available');
    conversationHost.querySelector<HTMLButtonElement>('.cw-attachment-remove')!.click();
    await until(() => !conversationHost.querySelector('.cw-attachment-chip'), 'The reconciliation fixture should clean up');

    conversationState = {
      ...conversationState,
      attachmentsByConversation: {
        ...conversationState.attachmentsByConversation,
        'conversation-1': (conversationState.attachmentsByConversation['conversation-1'] ?? [])
          .filter((attachment) => attachment.state !== 'staged'),
      },
    };
    conversationRenderer.render(conversationState);
    let releaseArchiveUpload = () => {};
    uploadGate = new Promise<void>((resolve) => { releaseArchiveUpload = resolve; });
    const archiveCleanupPaste = new DataTransfer();
    archiveCleanupPaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'archive-running.png', { type: 'image/png' }));
    archiveCleanupPaste.items.add(new File([new Uint8Array([137, 80, 78, 71])], 'archive-queued.png', { type: 'image/png' }));
    const attemptsBeforeArchiveCleanup = uploadAttempts.length;
    paste(archiveCleanupPaste);
    await until(() => uploadAttempts.length === attemptsBeforeArchiveCleanup + 1,
      'The archive cleanup fixture should have one running upload and one queued upload');
    composer.value = 'Draft remains separate from staged files';
    composer.dispatchEvent(new Event('input', { bubbles: true }));
    conversationRenderer.clearStagedAttachments('conversation-1');
    check(!conversationHost.querySelector('.cw-attachment-chip') && composer.value === 'Draft remains separate from staged files',
      'Successful archive cleanup forgets staged files without erasing unrelated draft text');
    releaseArchiveUpload();
    await until(() => uploadsInFlight === 0, 'The forgotten running upload should finish its cleanup');
    check(uploadAttempts.length === attemptsBeforeArchiveCleanup + 1 && !conversationHost.querySelector('.cw-attachment-chip'),
      'Archive cleanup cancels queued uploads and does not restore a running upload after completion');

    const malformedClipboard = {
      get files(): FileList { throw new Error('files unavailable'); },
      get items(): DataTransferItemList { throw new Error('items unavailable'); },
      getData(): string { throw new Error('text unavailable'); },
    } as unknown as DataTransfer;
    paste(malformedClipboard);
    check(conversationHost.querySelector('.cw-composer-notice')?.textContent?.includes('Could not read clipboard'), 'Malformed clipboard data is contained and reported without taking down the conversation UI');
    const disconnected = normalizeEvent({ event_id: 32, conversation_id: null, kind: 'connection.disconnected', payload: { host_id: 'local', conversation_ids: ['conversation-1'] } });
    check(disconnected, 'Disconnect fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: disconnected });
    conversationRenderer.render(conversationState);
    const reconnect = conversationHost.querySelector<HTMLButtonElement>('.cw-reconnect')!;
    check(!reconnect.hidden && conversationHost.querySelector<HTMLButtonElement>('.cw-composer button[type="submit"]')?.disabled, 'Unknown provider state offers recovery while keeping Send disabled');
    reconnect.click();
    check(reconnects[0] === 'conversation-1', 'Reconnect targets the current conversation');
    const sideChatDetail = normalizeConversationDetail({
      conversation: {
        conversation_id: 'side-chat-1', pursuit_id: 'design', kind: 'side_chat', parent_conversation_id: 'conversation-1',
        host_id: 'local', project_id: 'local-root', model: 'gpt-5.6', reasoning_effort: 'medium', thread_title: 'Untitled conversation', status: 'idle',
      },
      events: [], pending_requests: [], cursor: 19,
    });
    check(sideChatDetail, 'Side-chat fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'side-chat-session', conversationIds: ['side-chat-1'] });
    conversationState = reduceConversationState(conversationState, { type: 'side-chat-restored', detail: sideChatDetail });
    conversationRenderer.render(conversationState);
    const tabsBeforeOpen = [...conversationHost.querySelectorAll<HTMLButtonElement>('.cw-conversation-tab')];
    check(tabsBeforeOpen.length === 2 && tabsBeforeOpen[1].textContent === 'Side chat' && tabsBeforeOpen[0].getAttribute('aria-selected') === 'true', 'The detail header exposes the main conversation and session side chat as accessible tabs');
    const parentArchive = conversationHost.querySelector<HTMLButtonElement>('.cw-archive')!;
    check(parentArchive.disabled && parentArchive.title === 'Close side chats before archiving', 'Parent archive stays unavailable while a session side chat exists');
    const unresolvedGuardState = reduceConversationState(conversationState, { type: 'side-chat-session', conversationIds: ['unresolved-side-chat'] });
    conversationRenderer.render(unresolvedGuardState);
    check(conversationHost.querySelector<HTMLButtonElement>('.cw-archive')!.disabled, 'An unresolved restored side-chat id conservatively blocks parent archive');
    conversationRenderer.render(conversationState);
    let createSideChatButton = conversationHost.querySelector<HTMLButtonElement>('.cw-new-side-chat')!;
    createSideChatButton.focus();
    const busySideChatState = reduceConversationState(conversationState, { type: 'side-chat-create-in-flight', active: true });
    conversationRenderer.render(busySideChatState);
    check(document.activeElement === conversationHost.querySelector('.cw-conversation-tab[aria-selected="true"]')
      && conversationHost.querySelector<HTMLButtonElement>('.cw-archive')!.disabled, 'A pending side-chat creation moves focus to a usable tab and blocks archive');
    const failedSideChatState = reduceConversationState(busySideChatState, { type: 'side-chat-create-in-flight', active: false });
    conversationRenderer.render(failedSideChatState);
    check(document.activeElement === conversationHost.querySelector('.cw-new-side-chat'), 'A failed side-chat creation restores keyboard focus to the enabled plus control');
    conversationRenderer.render(conversationState);
    createSideChatButton = conversationHost.querySelector<HTMLButtonElement>('.cw-new-side-chat')!;
    createSideChatButton.focus();
    createSideChatButton.click();
    check(createdSideChats.at(-1) === 'conversation-1', 'The plus control creates a side chat from the main parent');
    const currentSideTab = [...conversationHost.querySelectorAll<HTMLButtonElement>('.cw-conversation-tab')]
      .find((tab) => tab.textContent === 'Side chat');
    check(currentSideTab, 'The restored side-chat tab remains available after focus recovery checks');
    currentSideTab.click();
    check(openedConversations.at(-1) === 'side-chat-1', 'A side-chat tab opens through the normal conversation loader');
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loading', conversationId: 'side-chat-1' });
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loaded', detail: sideChatDetail });
    conversationRenderer.render(conversationState);
    const activeSideTab = conversationHost.querySelector<HTMLButtonElement>('.cw-conversation-tab[aria-selected="true"]')!;
    check(activeSideTab.textContent === 'Side chat' && !conversationHost.querySelector<HTMLElement>('.cw-side-chat-note')!.hidden && conversationHost.querySelector<HTMLButtonElement>('.cw-archive')!.hidden, 'A new side chat uses the temporary explanation and omits persistent lifecycle controls');
    check(document.activeElement === activeSideTab, 'Creating and selecting a side chat moves keyboard focus from the busy plus control to its selected tab');
    activeSideTab.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowLeft', bubbles: true, cancelable: true }));
    check(openedConversations.at(-1) === 'conversation-1', 'Arrow keys move between accessible conversation tabs');
    const nativeConfirm = window.confirm;
    let confirmDecision = false;
    let confirmCalls = 0;
    window.confirm = () => { confirmCalls++; return confirmDecision; };
    conversationHost.querySelector<HTMLButtonElement>('[data-close-side-chat="side-chat-1"]')!.click();
    check(Number(confirmCalls) === 1 && closedSideChats.at(-1) !== 'side-chat-1', 'Every side-chat close confirms because another open tab may hold unseen work');
    confirmDecision = true;
    conversationHost.querySelector<HTMLButtonElement>('[data-close-side-chat="side-chat-1"]')!.click();
    check(Number(confirmCalls) === 2 && closedSideChats.at(-1) === 'side-chat-1', 'Confirmed clean side-chat close proceeds after the cross-tab warning');
    const cleanCloseCount = closedSideChats.length;
    confirmDecision = false;
    confirmCalls = 0;
    const sideDraftKey = `rightmemory:side-chat-draft:${encodeURIComponent(draftRoot)}:${encodeURIComponent('side-chat-1')}`;
    composer.value = 'Draft that survives reload';
    composer.dispatchEvent(new Event('input', { bubbles: true }));
    check(sessionStorage.getItem(sideDraftKey) === 'Draft that survives reload', 'Side-chat drafts use root-and-conversation-scoped session storage');
    const reloadHost = document.createElement('aside');
    selectionBoundary.append(reloadHost);
    const reloadRenderer = new ConversationRenderer(reloadHost, draftRoot, actions);
    reloadRenderer.render(conversationState);
    check(reloadHost.querySelector<HTMLTextAreaElement>('.cw-composer textarea')?.value === 'Draft that survives reload', 'A side-chat draft survives a same-tab page reconstruction');
    reloadRenderer.destroy(); reloadHost.remove();
    conversationRenderer.clearComposerIfUnchanged('Draft that survives reload', [], 'side-chat-1');
    check(sessionStorage.getItem(sideDraftKey) === null, 'A successfully submitted side-chat draft is removed from session storage');
    composer.value = 'Discard only after confirmation';
    composer.dispatchEvent(new Event('input', { bubbles: true }));
    conversationHost.querySelector<HTMLButtonElement>('[data-close-side-chat="side-chat-1"]')!.click();
    check(Number(confirmCalls) === 1 && closedSideChats.length === cleanCloseCount
      && sessionStorage.getItem(sideDraftKey) === 'Discard only after confirmation', 'Canceling destructive side-chat close preserves its draft and temporary thread');
    confirmDecision = true;
    conversationHost.querySelector<HTMLButtonElement>('[data-close-side-chat="side-chat-1"]')!.click();
    check(Number(confirmCalls) === 2 && closedSideChats.length === cleanCloseCount + 1, 'Confirmed side-chat close proceeds after warning that temporary content will be purged');
    conversationRenderer.forgetConversation('side-chat-1');
    check(sessionStorage.getItem(sideDraftKey) === null, 'Successful side-chat close removes its session draft');
    window.confirm = nativeConfirm;

    const managerHost = document.createElement('aside');
    selectionBoundary.append(managerHost);
    const managerRenderer = new ConversationRenderer(managerHost, draftRoot, actions);
    managerRenderer.setModelCatalog({
      hostId: 'local', defaultModel: 'gpt-5.6', defaultReasoningEffort: 'medium',
      models: [{ id: 'gpt-5.6', displayName: 'GPT-5.6', defaultReasoningEffort: 'medium', isDefault: true, supportedReasoningEfforts: [{ reasoningEffort: 'medium', description: '' }] }],
    });
    const managerSummary = normalizeWorkspace({ conversations: [{
      conversation_id: 'manager-1', pursuit_id: null, kind: 'manager', host_id: 'local', project_id: 'local-root',
      execution_cwd: 'C:\\fixture', model: 'gpt-5.6', reasoning_effort: 'medium', thread_title: 'Manager fixture', status: 'idle',
    }] }).conversations[0];
    let managerState: ConversationState = {
      ...conversationState,
      managerOpen: true,
      managerReferencePursuitId: 'design',
      currentConversationId: null,
      conversations: [...conversationState.conversations, managerSummary],
    };
    managerRenderer.render(managerState);
    check(!managerHost.querySelector<HTMLElement>('.cw-manager-view')!.hidden
      && managerHost.querySelector<HTMLElement>('.cw-list-view')!.hidden
      && managerHost.querySelectorAll('.cw-manager-list .cw-conversation').length === 1,
    'The fixed Manager mode has its own persistent list without exposing the Pursuit host/project picker');
    managerHost.querySelector<HTMLButtonElement>('.cw-manager-entry')!.click();
    check(managerOpenRequests === 1, 'The Manager entry uses an explicit pane mode instead of clearing the map selection');
    const longOpeningContext = 'opaque-context-segment-'.repeat(3_500);
    const projectedManagerEcho = `${longOpeningContext.slice(0, 65_536 - 14)}...[truncated]`;
    const managerDetail = normalizeConversationDetail({
      conversation: managerSummary.raw,
      events: [
        { event_id: 80, conversation_id: 'manager-1', kind: 'user.message', payload: { text: 'Visible Manager request', opening_context: longOpeningContext, references: [{ kind: 'pursuit', id: 'design', title: 'Design' }] } },
        { event_id: 81, conversation_id: 'manager-1', turn_id: 'manager-turn', kind: 'item.completed', payload: { item: { id: 'manager-user-echo', type: 'userMessage', role: 'user', content: [{ type: 'text', text: projectedManagerEcho }] } } },
      ],
      pending_requests: [], cursor: 81,
    });
    check(managerDetail, 'Manager fixture must normalize');
    managerState = reduceConversationState(managerState, { type: 'conversation-loading', conversationId: 'manager-1' });
    managerState = reduceConversationState(managerState, { type: 'conversation-loaded', detail: managerDetail });
    managerRenderer.render(managerState);
    const openingContext = managerHost.querySelector<HTMLDetailsElement>('.cw-opening-context');
    check(openingContext && !openingContext.open
      && managerHost.querySelectorAll('.cw-user').length === 1
      && managerHost.querySelectorAll('.cw-sent-reference').length === 1,
    'Stored oversized opening context is collapsed in the local user bubble, its explicitly truncated provider echo is suppressed, and sent references remain visible');
    check(!managerHost.querySelector('.cw-new-side-chat')
      && managerHost.querySelector('.cw-staged-references .cw-reference-chip'),
    'Manager conversations keep the captured per-message Pursuit reference and omit side-chat controls');
    managerHost.querySelector<HTMLButtonElement>('.cw-reference-remove')!.click();
    check(managerReferenceRemovals === 1, 'The captured Manager reference is removable before send');
    managerRenderer.destroy(); managerHost.remove();
    const conversationPreview = conversationHost.cloneNode(true) as HTMLElement;
    conversationRenderer.destroy(); conversationHost.remove();
    localStorage.removeItem(`rightmemory:conversation-draft:${encodeURIComponent(draftRoot)}:conversation-1`);
    report('PASS conversation phases, rich output, subagents, attachments, model settings, guarded requests, recovery, composer keys, and scoped drafts');

    const boundaryHost = document.createElement('section');
    const boundaryPane = document.createElement('aside');
    document.body.append(boundaryHost, boundaryPane);
    const streamListeners = new Map<string, EventListener[]>();
    let streamClosed = false;
    let eventUrl = '';
    const boundaryStream = {
      close() { streamClosed = true; },
      addEventListener(type: string, listener: EventListener) { streamListeners.set(type, [...(streamListeners.get(type) ?? []), listener]); },
      onopen: null,
      onerror: null,
      onmessage: null,
    };
    let reloads = 0;
    let releaseRequest: RequestInit | null = null;
    const boundaryWorkspace = new ConversationWorkspace(
      boundaryHost,
      boundaryPane,
      async (path, options) => {
        if (path === '/api/conversation-session/release') {
          releaseRequest = options ?? null;
          return { data: { released: true } };
        }
        return { data: { root_key: 'root-one', hosts: [], projects: [], conversations: [], pending_requests: [], cursor: 8 } };
      },
      'root-one',
      () => { reloads++; },
      (url) => { eventUrl = url; return boundaryStream; },
    );
    await boundaryWorkspace.start();
    const boundaryEventUrl = new URL(eventUrl, location.href);
    check(
      boundaryEventUrl.pathname === '/api/conversation-events'
        && boundaryEventUrl.searchParams.get('after_event_id') === '8'
        && !!boundaryEventUrl.searchParams.get('view_id')
        && !!boundaryEventUrl.searchParams.get('page_id'),
      'A new event stream resumes from the REST snapshot cursor with per-tab and per-page identity',
    );
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }));
    await until(() => releaseRequest !== null, 'Page exit should explicitly release its temporary-chat view');
    const releaseBody = JSON.parse(String(releaseRequest!.body)) as { view_id?: string; page_id?: string };
    check(
      releaseRequest!.keepalive === true
        && releaseBody.view_id === boundaryEventUrl.searchParams.get('view_id')
        && releaseBody.page_id === boundaryEventUrl.searchParams.get('page_id'),
      'Page exit sends a keepalive release tied to the exact streamed view instance',
    );
    boundaryWorkspace.setActive(false);
    check(!streamClosed, 'Switching to another Web Studio panel keeps the app-session stream alive for temporary side chats');
    boundaryWorkspace.setActive(true);
    const changedRoot = new MessageEvent('snapshot', { data: JSON.stringify({ root_key: 'root-two', hosts: [], projects: [], conversations: [], pending_requests: [], cursor: 9 }) });
    for (const listener of streamListeners.get('snapshot') ?? []) listener(changedRoot);
    check(reloads === 1 && streamClosed, 'A cross-tab root change closes the old workspace before requesting a full reload');

    const destroyHost = document.createElement('section');
    const destroyPane = document.createElement('aside');
    document.body.append(destroyHost, destroyPane);
    let destroyReleaseCount = 0;
    let destroyStreamClosed = false;
    const destroyWorkspace = new ConversationWorkspace(
      destroyHost,
      destroyPane,
      async (path) => {
        if (path === '/api/conversation-session/release') {
          destroyReleaseCount++;
          return { data: { released: true } };
        }
        return { data: { root_key: 'root-destroy', hosts: [], projects: [], conversations: [], pending_requests: [], cursor: 0 } };
      },
      'root-destroy',
      () => undefined,
      () => ({
        close() { destroyStreamClosed = true; },
        addEventListener() {},
        onopen: null,
        onerror: null,
        onmessage: null,
      }),
    );
    await destroyWorkspace.start();
    destroyWorkspace.destroy();
    await until(() => destroyReleaseCount === 1, 'Destroy should explicitly release its temporary-chat page instance');
    destroyWorkspace.destroy();
    window.dispatchEvent(new PageTransitionEvent('pagehide', { persisted: false }));
    await pause();
    check(destroyReleaseCount === 1 && destroyStreamClosed, 'Destroy releases before closing its stream and repeated disposal stays idempotent');
    destroyHost.remove(); destroyPane.remove();
    sessionStorage.removeItem(`rightmemory:conversation-view:${encodeURIComponent('root-one')}`);
    sessionStorage.removeItem(`rightmemory:conversation-view:${encodeURIComponent('root-destroy')}`);
    boundaryHost.remove(); boundaryPane.remove();
    report('PASS conversation root boundary and REST-to-SSE cursor replay');

    const managerRecoveryHost = document.createElement('section');
    const managerRecoveryPane = document.createElement('aside');
    document.body.append(managerRecoveryHost, managerRecoveryPane);
    let managerRecoveryStatus = 'idle';
    let managerInitialContextState = 'eligible';
    let managerRecoveryCursor = 1;
    let managerWorkspaceLoads = 0;
    let managerDetailLoads = 0;
    let managerCanonicalRefreshes = 0;
    let managerMessageAttempts = 0;
    let managerMessageShouldFail = true;
    let managerPendingUserEventId: number | null = null;
    const managerRecoveryListeners = new Map<string, EventListener[]>();
    const managerRecoveryStream = {
      close() {},
      addEventListener(type: string, listener: EventListener) {
        managerRecoveryListeners.set(type, [...(managerRecoveryListeners.get(type) ?? []), listener]);
      },
      onopen: null,
      onerror: null,
      onmessage: null,
    };
    const managerRecoveryConversation = () => ({
      conversation_id: 'manager-recovery', pursuit_id: null, kind: 'manager', host_id: 'local', project_id: 'local-root',
      execution_cwd: 'C:\\fixture', model: 'gpt-5.6', reasoning_effort: 'medium', thread_title: 'Manager recovery',
      status: managerRecoveryStatus, initial_context_state: managerInitialContextState,
      updated_at: `2026-01-01T00:00:0${managerRecoveryCursor}Z`,
    });
    const managerRecoveryWorkspace = new ConversationWorkspace(
      managerRecoveryHost,
      managerRecoveryPane,
      async (path, options) => {
        if (path === '/api/conversation-workspace') {
          managerWorkspaceLoads++;
          return { data: {
            root_key: 'manager-recovery-root',
            hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }],
            projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }],
            conversations: [managerRecoveryConversation()], pending_requests: [], cursor: managerRecoveryCursor,
          } };
        }
        if (path === '/api/conversation-models?host_id=local') return { data: {
          host_id: 'local', default_model: 'gpt-5.6', default_reasoning_effort: 'medium',
          models: [{ id: 'gpt-5.6', display_name: 'GPT-5.6', default_reasoning_effort: 'medium', supported_reasoning_efforts: [{ reasoning_effort: 'medium' }] }],
        } };
        if (path.startsWith('/api/pursuit-conversations?')) return { data: { conversations: [], default: null } };
        if (path === '/api/conversations/manager-recovery') {
          managerDetailLoads++;
          return { data: { conversation: managerRecoveryConversation(), events: [], attachments: [], pending_requests: [], cursor: managerRecoveryCursor } };
        }
        if (path === '/api/conversations/manager-recovery/messages' && options?.method === 'POST') {
          managerMessageAttempts++;
          managerRecoveryCursor++;
          if (managerMessageShouldFail) {
            managerRecoveryStatus = 'unknown';
            managerInitialContextState = 'unknown';
            managerPendingUserEventId = managerRecoveryCursor;
            throw new Error('turn/start response lost');
          }
          managerRecoveryStatus = 'completed';
          managerInitialContextState = 'accepted';
          return { data: { conversation: managerRecoveryConversation() } };
        }
        if (path === '/api/conversations/manager-recovery/reconcile' && options?.method === 'POST') {
          managerRecoveryStatus = 'idle';
          managerInitialContextState = 'accepted';
          managerRecoveryCursor++;
          return { data: {
            conversation: managerRecoveryConversation(), resolved: true,
            accepted_user_event_id: managerPendingUserEventId,
          } };
        }
        if (path === '/api/conversation-session/release') return { data: { released: true } };
        throw new Error(`Unexpected Manager recovery request: ${path}`);
      },
      'manager-recovery-root',
      () => undefined,
      () => managerRecoveryStream,
      () => undefined,
      () => { managerCanonicalRefreshes++; },
    );
    await managerRecoveryWorkspace.start();
    (managerRecoveryStream.onopen as ((event: Event) => void) | null)?.(new Event('open'));
    managerRecoveryWorkspace.selectPursuit('design');
    managerRecoveryWorkspace.openManager();
    managerRecoveryWorkspace.openConversation('manager-recovery');
    const managerDraft = managerRecoveryPane.querySelector<HTMLTextAreaElement>('.cw-composer textarea')!;
    managerDraft.value = 'Uncertain Manager request';
    managerDraft.dispatchEvent(new Event('input', { bubbles: true }));
    await until(() => managerDetailLoads === 1
      && !managerRecoveryPane.querySelector<HTMLButtonElement>('.cw-send-stop')!.disabled,
    'The Manager recovery fixture should finish loading and become sendable');
    check(!await managerRecoveryWorkspace.sendMessage('Uncertain Manager request', []) && managerMessageAttempts === 1,
      'A lost Manager turn/start response leaves the exact submitted message pending for reconciliation');
    const unknownAfterSend = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', kind: 'conversation.state',
      payload: { conversation: managerRecoveryConversation() },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(unknownAfterSend);
    managerDraft.value = 'Later Manager draft';
    managerDraft.dispatchEvent(new Event('input', { bubbles: true }));
    managerRecoveryWorkspace.selectPursuit('research');
    managerRecoveryWorkspace.openManager();
    managerRecoveryWorkspace.openConversation('manager-recovery');
    await until(() => managerDetailLoads >= 2
      && !managerRecoveryPane.querySelector<HTMLButtonElement>('.cw-reconnect')!.hidden
      && !managerRecoveryPane.querySelector<HTMLButtonElement>('.cw-reconnect')!.disabled,
    'The Manager recovery fixture should restore and become ready to reconcile after a later reference selection');
    await managerRecoveryWorkspace.reconnect('manager-recovery');
    await until(() => managerCanonicalRefreshes === 1 && managerWorkspaceLoads >= 2,
      'A resolved unknown-to-idle reconciliation should refresh canonical Manager state and the workspace');
    check(managerDraft.value === 'Later Manager draft'
      && managerRecoveryPane.querySelector<HTMLElement>('.cw-reference-chip')?.textContent?.includes('research'),
    'Accepted reconciliation clears only the submitted Manager draft/reference version and preserves later edits and selection');

    managerRecoveryStatus = 'running';
    managerRecoveryCursor++;
    const runningFallback = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-2', kind: 'conversation.state',
      payload: { conversation: managerRecoveryConversation() },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(runningFallback);
    managerRecoveryStatus = 'completed';
    managerRecoveryCursor++;
    const terminalFallback = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-2', kind: 'conversation.state',
      payload: { conversation: managerRecoveryConversation() },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(terminalFallback);
    managerRecoveryCursor++;
    const lateTerminal = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-2', kind: 'turn.completed',
      payload: { turn: { id: 'manager-turn-2', status: 'completed' } },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(lateTerminal);
    await until(() => managerCanonicalRefreshes === 2,
      'A fallback terminal conversation state should recover a missed Manager turn notification');
    await pause();
    check(managerCanonicalRefreshes === 2 && managerDraft.value === 'Later Manager draft',
      'The later duplicate turn event does not cause another refresh or erase the Manager draft');

    managerRecoveryStatus = 'running';
    managerRecoveryCursor++;
    const snapshotTurnRunning = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-3', kind: 'conversation.state',
      payload: { conversation: managerRecoveryConversation() },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(snapshotTurnRunning);
    managerRecoveryStatus = 'completed';
    managerRecoveryCursor++;
    const reconnectTerminalSnapshot = new MessageEvent('snapshot', { data: JSON.stringify({
      root_key: 'manager-recovery-root',
      hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }],
      projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }],
      conversations: [managerRecoveryConversation()], pending_requests: [], cursor: managerRecoveryCursor,
    }) });
    for (const listener of managerRecoveryListeners.get('snapshot') ?? []) listener(reconnectTerminalSnapshot);
    const replayedTerminalState = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-3', kind: 'conversation.state',
      payload: { conversation: managerRecoveryConversation() },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(replayedTerminalState);
    managerRecoveryCursor++;
    const replayedTerminalTurn = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: managerRecoveryCursor, conversation_id: 'manager-recovery', turn_id: 'manager-turn-3', kind: 'turn.completed',
      payload: { turn: { id: 'manager-turn-3', status: 'completed' } },
    }) });
    for (const listener of managerRecoveryListeners.get('conversation') ?? []) listener(replayedTerminalTurn);
    await until(() => managerCanonicalRefreshes === 3,
      'A reconnect snapshot that arrives before replayed terminal events should recover the Manager refresh');
    await pause();
    check(Number(managerCanonicalRefreshes) === 3,
      'Replayed state and turn events remain coalesced with the authoritative terminal snapshot');

    managerMessageShouldFail = false;
    managerDraft.value = 'Terminal HTTP Manager request';
    managerDraft.dispatchEvent(new Event('input', { bubbles: true }));
    check(await managerRecoveryWorkspace.sendMessage('Terminal HTTP Manager request', []) && Number(managerMessageAttempts) === 2,
      'A Manager send can return an already-terminal conversation summary');
    await until(() => managerCanonicalRefreshes === 4,
      'A terminal Manager send HTTP result should refresh canonical state without waiting for SSE');
    check(managerDraft.value === '' && !managerRecoveryPane.querySelector('.cw-reference-chip'),
      'The successful terminal HTTP send clears the matching draft and captured reference');
    managerRecoveryWorkspace.destroy();
    localStorage.removeItem(`rightmemory:conversation-draft:${encodeURIComponent('manager-recovery-root')}:manager-recovery`);
    managerRecoveryHost.remove(); managerRecoveryPane.remove();
    report('PASS Manager terminal refresh recovery across reconcile, snapshots, HTTP results, and draft/reference preservation');

    const settingsHost = document.createElement('section');
    const settingsPane = document.createElement('aside');
    document.body.append(settingsHost, settingsPane);
    const settingsConversation = {
      conversation_id: 'settings-conversation', kind: 'pursuit', pursuit_id: 'design', host_id: 'local', project_id: 'local-root',
      thread_title: 'Settings recovery', model: 'gpt-5.6', reasoning_effort: 'medium', lifecycle: 'active', status: 'idle',
      last_final_event_id: null, last_read_event_id: null,
    };
    let settingsAttempts = 0;
    let settingsDetailLoads = 0;
    let settingsSends = 0;
    const inertStream = { close() {}, addEventListener() {}, onopen: null, onerror: null, onmessage: null };
    const settingsWorkspace = new ConversationWorkspace(
      settingsHost,
      settingsPane,
      async (path, options) => {
        if (path === '/api/conversation-workspace') return { data: {
          root_key: 'settings-root',
          hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }],
          projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }],
          conversations: [settingsConversation], pending_requests: [], cursor: 1,
        } };
        if (path === '/api/pursuit-conversations?pursuit_id=design') return { data: { conversations: [settingsConversation], default: null } };
        if (path === '/api/conversation-models?host_id=local') return { data: {
          host_id: 'local', default_model: 'gpt-5.6', default_reasoning_effort: 'medium',
          models: [{ id: 'gpt-5.6', display_name: 'GPT-5.6', default_reasoning_effort: 'medium', supported_reasoning_efforts: [{ reasoning_effort: 'medium' }] }],
        } };
        if (path === '/api/conversations/settings-conversation') {
          settingsDetailLoads++;
          return { data: { conversation: settingsConversation, events: [], attachments: [], pending_requests: [], cursor: 1 } };
        }
        if (path.endsWith('/settings') && options?.method === 'POST') {
          settingsAttempts++;
          throw new Error('fixture settings failure');
        }
        if (path.endsWith('/messages') && options?.method === 'POST') {
          settingsSends++;
          return { data: { conversation: settingsConversation } };
        }
        throw new Error(`Unexpected settings recovery request: ${path}`);
      },
      'settings-root',
      () => undefined,
      () => inertStream,
    );
    await settingsWorkspace.start();
    (inertStream.onopen as ((event: Event) => void) | null)?.(new Event('open'));
    settingsWorkspace.selectPursuit('design');
    settingsWorkspace.openConversation('settings-conversation');
    await until(() => settingsDetailLoads === 1, 'Settings recovery conversation should load');
    settingsWorkspace.updateConversationSettings('gpt-5.6-next', 'high');
    const queuedSend = settingsWorkspace.sendMessage('Do not send under rolled-back settings', []);
    await until(() => settingsAttempts === 1 && settingsDetailLoads === 2, 'A failed setting update should reload persisted settings');
    check(!await queuedSend && settingsSends === 0, 'A send already waiting on a failed selector intent is canceled after rollback');
    check(await settingsWorkspace.sendMessage('Send after selector rollback', []) && Number(settingsSends) === 1, 'A fresh send works after persisted selectors are restored');
    settingsWorkspace.destroy();
    settingsHost.remove(); settingsPane.remove();
    report('PASS failed model-setting rollback leaves the conversation sendable');

    const raceHost = document.createElement('section');
    const racePane = document.createElement('aside');
    document.body.append(raceHost, racePane);
    const raceRoot = 'side-chat-race-root';
    const raceStorageKey = `rightmemory:side-chats:${encodeURIComponent(raceRoot)}`;
    const raceParent = { ...settingsConversation, conversation_id: 'race-parent', thread_title: 'Race parent' };
    const oldSideChat = {
      ...settingsConversation, conversation_id: 'side-old', kind: 'side_chat', parent_conversation_id: 'race-parent',
      pursuit_id: 'design', thread_title: 'Old side chat',
    };
    const newSideChat = { ...oldSideChat, conversation_id: 'side-new', thread_title: 'New side chat' };
    sessionStorage.setItem(raceStorageKey, JSON.stringify(['side-old']));
    let resolveOldSideChat!: (value: { data: unknown }) => void;
    const oldSideChatDetail = new Promise<{ data: unknown }>((resolve) => { resolveOldSideChat = resolve; });
    let oldDetailRequested = false;
    const raceStreamListeners = new Map<string, EventListener[]>();
    const raceStream = {
      close() {},
      addEventListener(type: string, listener: EventListener) {
        raceStreamListeners.set(type, [...(raceStreamListeners.get(type) ?? []), listener]);
      },
      onopen: null,
      onerror: null,
      onmessage: null,
    };
    const raceWorkspace = new ConversationWorkspace(
      raceHost,
      racePane,
      async (path, options) => {
        if (path === '/api/conversation-workspace') return { data: {
          root_key: raceRoot,
          hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }],
          projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }],
          conversations: [raceParent, oldSideChat], pending_requests: [], cursor: 2,
        } };
        if (path === '/api/conversations/side-old') { oldDetailRequested = true; return oldSideChatDetail; }
        if (path === '/api/side-chats/side-old' && options?.method === 'DELETE') return { data: {} };
        if (path === '/api/conversations/race-parent/side-chats' && options?.method === 'POST') return { data: { conversation: newSideChat } };
        throw new Error(`Unexpected side-chat race request: ${path}`);
      },
      raceRoot,
      () => undefined,
      () => raceStream,
    );
    const raceStart = raceWorkspace.start();
    await until(() => oldDetailRequested, 'Stored side-chat restoration should begin');
    await Promise.all([
      raceWorkspace.closeSideChat('side-old'),
      raceWorkspace.createSideChat('race-parent'),
    ]);
    resolveOldSideChat({ data: { conversation: oldSideChat, events: [], attachments: [], pending_requests: [], cursor: 2 } });
    await raceStart;
    check(sessionStorage.getItem(raceStorageKey) === JSON.stringify(['side-new']), 'Concurrent close/create mutations win over an older side-chat restore snapshot');
    raceWorkspace.setActive(false);
    const remoteClose = new MessageEvent('conversation', { data: JSON.stringify({
      event_id: 3, conversation_id: null, kind: 'side_chat.closed', payload: { conversation_id: 'side-new' },
    }) });
    for (const listener of raceStreamListeners.get('conversation') ?? []) listener(remoteClose);
    check(sessionStorage.getItem(raceStorageKey) === null, 'An inactive duplicate page consumes a scoped close tombstone and removes its stale side-chat membership');
    raceWorkspace.destroy();
    sessionStorage.removeItem(raceStorageKey);
    raceHost.remove(); racePane.remove();
    report('PASS side-chat restore races and cross-page close tombstones reconcile temporary tabs');

    if (matchMedia('(max-width: 760px)').matches) {
      const workspaceProbe = document.createElement('section');
      workspaceProbe.className = 'pursuit-workspace';
      workspaceProbe.style.cssText = 'position:fixed;left:-1200px;top:0;width:390px';
      const mapProbe = document.createElement('div');
      mapProbe.className = 'pw-map-shell';
      const paneProbe = conversationPreview.cloneNode(true) as HTMLElement;
      paneProbe.removeAttribute('style');
      workspaceProbe.append(mapProbe, paneProbe);
      selectionBoundary.append(workspaceProbe);
      const workspaceBounds = workspaceProbe.getBoundingClientRect();
      const mapBounds = mapProbe.getBoundingClientRect();
      const paneBounds = paneProbe.getBoundingClientRect();
      const footer = paneProbe.querySelector<HTMLElement>('.cw-composer-footer')!;
      const footerHint = footer.querySelector<HTMLElement>('small')!;
      check(Math.abs(mapBounds.height + paneBounds.height - workspaceProbe.clientHeight) < 1
        && paneBounds.bottom <= workspaceBounds.bottom + 1, 'Mobile workspace tracks stay within the bounded workspace height');
      check(getComputedStyle(footer).display === 'grid'
        && getComputedStyle(footerHint).order === '0'
        && getComputedStyle(footerHint).flexBasis === 'auto', 'Mobile composer keeps its grid placement without flex-only overrides');
      workspaceProbe.remove();
      report('PASS bounded mobile workspace tracks and grid composer placement');
    }

    document.body.append(conversationPreview);
    conversationPreview.style.cssText = 'position:fixed;right:12px;top:12px;width:400px;height:min(700px,calc(100vh - 24px));z-index:1000;box-shadow:0 14px 42px #18312633';
    conversationPane.style.cssText = 'position:fixed;right:424px;top:12px;z-index:1001';
    conversationPane.textContent = 'Hide conversation fixture';
    conversationPane.addEventListener('click', () => {
      conversationPreview.hidden = !conversationPreview.hidden;
      conversationPane.textContent = conversationPreview.hidden ? 'Show conversation fixture' : 'Hide conversation fixture';
    });
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

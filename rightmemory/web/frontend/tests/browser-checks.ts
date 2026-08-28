import { mountMap, type PursuitMapController } from '../src/pursuit-map.ts';
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
  let failNext = false;
  const operations: Operation[] = [];
  const history = new Map<string, Snapshot>();
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
      const id = operation.type === 'create' ? `created-${serial}` : operation.id;
      const before = current;
      current = { ...applyOperation(current, operation, id), revision: `r${++serial}`, git_head: `c${serial}` };
      history.set(current.git_head, before);
      return { snapshot: structuredClone(current), commit: current.git_head, operation_id: current.git_head, repaired_references: [], undoable: true, selected_id: id };
    },
    history: async (_kind, _revision, commit) => {
      const restored = history.get(commit);
      check(restored, 'Undo must reference a saved interaction');
      const before = current;
      current = { ...restored, revision: `r${++serial}`, git_head: `c${serial}` };
      history.set(current.git_head, before);
      return { snapshot: structuredClone(current), commit: current.git_head, operation_id: current.git_head, repaired_references: [], undoable: true, selected_id: current.root_ids[0] };
    },
  };
  const $ = <T extends HTMLElement = HTMLElement>(selector: string): T => {
    const element = host.querySelector<T>(selector); check(element, `Missing ${selector}`); return element;
  };
  const topic = (id: string) => $(`#pm-node-${id}`);
  const button = (command: string, scope = '.pm-toolbar') => $<HTMLButtonElement>(`${scope} [data-command="${command}"]`);
  const key = (name: string, options: KeyboardEventInit = {}, target = $('.pm-canvas')) => {
    target.dispatchEvent(new KeyboardEvent('keydown', { key: name, bubbles: true, cancelable: true, ...options }));
  };
  const pointer = (target: HTMLElement, type: string, x: number, y: number, buttons = 1) => {
    target.dispatchEvent(new PointerEvent(type, { pointerId: 1, pointerType: 'mouse', button: 0, buttons, clientX: x, clientY: y, bubbles: true, cancelable: true }));
  };
  const select = (id: string) => {
    const node = topic(id); const rect = node.getBoundingClientRect();
    pointer(node, 'pointerdown', rect.x + rect.width / 2, rect.y + rect.height / 2);
    pointer(node, 'pointerup', rect.x + rect.width / 2, rect.y + rect.height / 2, 0);
  };
  const settled = () => until(() => !controller!.hasUnsavedChanges, 'The save queue did not settle');
  const reset = async (count = 22) => {
    controller?.destroy();
    current = { ...forestFixture(count), root_key: `interaction-check-${crypto.randomUUID()}` };
    operations.length = 0;
    controller = await mountMap(host, transport);
    await pause();
  };
  try {
    await reset();
    select('design');
    check(!$('.pm-topic-toolbar').hidden, 'Selecting a real node shows its toolbar');
    for (const command of ['bold', 'underline', 'strike']) { button(command, '.pm-topic-toolbar').click(); await settled(); }
    check(indexTree(current!).get('design')!.title === '**<u>~~Design 设计~~</u>**', 'B/U/S must use canonical rename titles');
    check(topic('design').querySelector('strong > u > s'), 'All three marks must be rendered');
    check(operations.every((operation) => operation.type === 'rename' && operation.id === 'design'), 'Formatting reuses rename and keeps the selected id');
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

    await reset(); select('design'); failNext = true;
    button('bold', '.pm-topic-toolbar').click(); await settled();
    check(indexTree(current!).get('design')!.title === 'Design 设计' && !topic('design').querySelector('strong'), 'A failed format operation restores authoritative text');
    check(topic('design').getAttribute('aria-selected') === 'true', 'Failed formatting preserves selection');
    report('PASS formatting conflict recovery');

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

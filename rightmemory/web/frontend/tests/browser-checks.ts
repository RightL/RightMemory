import { mountMap, type PursuitMapController } from '../src/pursuit-map.ts';
import { applyOperation, indexTree, type Operation, type Snapshot } from '../src/tree.ts';
import { ApiError, type Transport } from '../src/queue.ts';
import { forestFixture } from './fixtures.ts';
import { ConversationRenderer, type ConversationRendererActions } from '../src/conversation-renderer.ts';
import { initialConversationState, normalizeConversationDetail, normalizeEvent, normalizeWorkspace, reduceConversationState } from '../src/conversation-state.ts';
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
  const click = (target: HTMLElement) => {
    const rect = target.getBoundingClientRect();
    pointer(target, 'pointerdown', rect.right - 8, rect.bottom - 8);
    pointer(target, 'pointerup', rect.right - 8, rect.bottom - 8, 0);
    target.click();
  };
  const settled = () => until(() => !controller!.hasUnsavedChanges, 'The save queue did not settle');
  const reset = async (count = 22) => {
    controller?.destroy();
    current = { ...forestFixture(count), root_key: `interaction-check-${crypto.randomUUID()}` };
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

    await reset();
    select('design');
    check(!$('.pm-topic-toolbar').hidden, 'Selecting a real node shows its toolbar');
    for (const command of ['bold', 'underline', 'strike']) { click(button(command, '.pm-topic-toolbar')); await settled(); }
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

    const conversationHost = document.createElement('aside');
    conversationHost.style.cssText = 'position:fixed;right:-1200px;top:0;width:400px;height:700px';
    selectionBoundary.append(conversationHost);
    const sentMessages: string[] = [];
    const responses: Array<{ decision?: string; response?: unknown }> = [];
    const reconnects: string[] = [];
    const actions: ConversationRendererActions = {
      toggleCollapsed() {}, openConversation() {}, closeConversation() {}, createConversation() {}, interrupt() {}, archive() {}, reload() {},
      reconnect(conversationId) { reconnects.push(conversationId); },
      createHost() {}, probeHost() {}, createProject() {}, retry() {},
      sendMessage(text) { sentMessages.push(text); },
      respond(_request, response) { responses.push(response); },
    };
    const draftRoot = `conversation-browser-${crypto.randomUUID()}`;
    const conversationRenderer = new ConversationRenderer(conversationHost, draftRoot, actions);
    let conversationState = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace({
      hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer' }, { host_id: 'gpu', kind: 'ssh', display_name: 'GPU' }],
      projects: [{ project_id: 'local-root', host_id: 'local', label: 'Fixture', cwd: 'C:\\fixture' }, { project_id: 'gpu-repo', host_id: 'gpu', label: 'Remote fixture', cwd: '/srv/fixture' }],
      conversations: [{ conversation_id: 'conversation-1', pursuit_id: 'design', host_id: 'local', project_id: 'local-root', thread_title: 'Safe conversation', status: 'waiting_input' }],
      pending_requests: [], pursuit_defaults: { design: { pursuit_id: 'design', host_id: 'gpu', project_id: 'gpu-repo' } }, cursor: 0,
    }) });
    conversationState = reduceConversationState(conversationState, { type: 'pursuit-selected', pursuitId: 'design' });
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loading', conversationId: 'conversation-1' });
    const conversationDetail = normalizeConversationDetail({
      conversation: { conversation_id: 'conversation-1', pursuit_id: 'design', host_id: 'local', project_id: 'local-root', thread_title: 'Safe conversation', status: 'waiting_input' },
      events: [
        { event_id: 1, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { type: 'userMessage', content: [{ type: 'text', text: 'Check this' }] } } },
        { event_id: 2, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'agent.delta', payload: { delta: '<img src=x onerror=alert(1)>' } },
        { event_id: 3, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'agent.delta', payload: { delta: ' remains text' } },
        { event_id: 4, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'item.completed', payload: { item: { type: 'commandExecution', command: 'echo <script>', aggregatedOutput: '<svg onload=alert(1)>', exitCode: 0 } } },
        { event_id: 5, conversation_id: 'conversation-1', turn_id: 'turn-1', kind: 'future.item', payload: { html: '<iframe srcdoc=bad>' } },
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
      cursor: 5,
    });
    check(conversationDetail, 'Conversation detail fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'conversation-loaded', detail: conversationDetail });
    conversationState = reduceConversationState(conversationState, { type: 'connection', connection: 'open' });
    conversationRenderer.render(conversationState);
    check(conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="host"]')?.value === 'gpu' && conversationHost.querySelector<HTMLSelectElement>('.cw-new-form [name="project"]')?.value === 'gpu-repo', 'The selected Pursuit restores its recent host and project');
    check(!conversationHost.querySelector('img,script,svg,iframe,[onerror],[onload]'), 'Conversation output must remain escaped text');
    check(conversationHost.querySelectorAll('.cw-agent').length === 1 && conversationHost.querySelector('.cw-agent')?.textContent?.includes('remains text'), 'Agent deltas merge into one visible message');
    check(conversationHost.querySelector('.cw-command')?.textContent?.includes('exit 0'), 'Completed commands show output and exit status');
    check(conversationHost.querySelector('.cw-unknown')?.textContent?.includes('future.item'), 'Unknown work items remain visible');
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
    const failedResponse = normalizeEvent({ event_id: 6, conversation_id: 'conversation-1', kind: 'server_response_failed', payload: { request_key: 'future-1' } });
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
    const idle = normalizeEvent({ event_id: 7, conversation_id: 'conversation-1', kind: 'thread.status', payload: { status: { type: 'idle' } } });
    check(idle, 'Idle status fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: idle });
    conversationRenderer.render(conversationState);
    const composer = conversationHost.querySelector<HTMLTextAreaElement>('.cw-composer textarea')!;
    composer.value = 'Send from composer'; composer.dispatchEvent(new Event('input', { bubbles: true }));
    composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }));
    composer.value = 'Keep newline'; composer.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true, cancelable: true }));
    check(sentMessages.length === 1 && sentMessages[0] === 'Send from composer', 'Composer Enter sends while Shift+Enter stays local');
    check(localStorage.getItem(`rightmemory:conversation-draft:${encodeURIComponent(draftRoot)}:conversation-1`) === 'Send from composer', 'Conversation drafts use a root-scoped storage key');
    composer.value = 'Next draft'; composer.dispatchEvent(new Event('input', { bubbles: true }));
    conversationRenderer.clearComposerIfUnchanged('Send from composer');
    check(composer.value === 'Next draft', 'A late send response does not erase text typed for the next message');
    const disconnected = normalizeEvent({ event_id: 8, conversation_id: null, kind: 'connection.disconnected', payload: { host_id: 'local', conversation_ids: ['conversation-1'] } });
    check(disconnected, 'Disconnect fixture must normalize');
    conversationState = reduceConversationState(conversationState, { type: 'event', event: disconnected });
    conversationRenderer.render(conversationState);
    const reconnect = conversationHost.querySelector<HTMLButtonElement>('.cw-reconnect')!;
    check(!reconnect.hidden && conversationHost.querySelector<HTMLButtonElement>('.cw-composer button[type="submit"]')?.disabled, 'Unknown provider state offers recovery while keeping Send disabled');
    reconnect.click();
    check(reconnects[0] === 'conversation-1', 'Reconnect targets the current conversation');
    conversationRenderer.destroy(); conversationHost.remove();
    localStorage.removeItem(`rightmemory:conversation-draft:${encodeURIComponent(draftRoot)}:conversation-1`);
    report('PASS conversation streaming/work cards, escaped output, guarded requests, recovery, composer keys, and root-scoped drafts');

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
    const boundaryWorkspace = new ConversationWorkspace(
      boundaryHost,
      boundaryPane,
      async () => ({ data: { root_key: 'root-one', hosts: [], projects: [], conversations: [], pending_requests: [], cursor: 8 } }),
      'root-one',
      () => { reloads++; },
      (url) => { eventUrl = url; return boundaryStream; },
    );
    await boundaryWorkspace.start();
    check(eventUrl === '/api/conversation-events?after_event_id=8', 'A new event stream resumes from the REST snapshot cursor');
    const changedRoot = new MessageEvent('snapshot', { data: JSON.stringify({ root_key: 'root-two', hosts: [], projects: [], conversations: [], pending_requests: [], cursor: 9 }) });
    for (const listener of streamListeners.get('snapshot') ?? []) listener(changedRoot);
    check(reloads === 1 && streamClosed, 'A cross-tab root change closes the old workspace before requesting a full reload');
    boundaryHost.remove(); boundaryPane.remove();
    report('PASS conversation root boundary and REST-to-SSE cursor replay');
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

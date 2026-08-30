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
    const sentMessages: Array<{ text: string; attachmentIds: string[] }> = [];
    const uploadedFiles: File[] = [];
    const uploadAttempts: Array<{ file: File; attachmentId: string }> = [];
    let failNextUpload = false;
    let failNextDelete = false;
    let failNextSend = false;
    const deletedAttachmentIds: string[] = [];
    const responses: Array<{ decision?: string; response?: unknown }> = [];
    const reconnects: string[] = [];
    const openedConversations: string[] = [];
    const createdSideChats: string[] = [];
    const closedSideChats: string[] = [];
    const modelCatalogLoads: string[] = [];
    const settingsUpdates: Array<{ model: string; reasoningEffort: string }> = [];
    const earlierLoads: string[] = [];
    const actions: ConversationRendererActions = {
      toggleCollapsed() {}, openConversation(conversationId) { openedConversations.push(conversationId); }, loadEarlier(conversationId) { earlierLoads.push(conversationId); }, closeConversation() {}, createConversation() {}, interrupt() {}, archive() {}, reload() {},
      createSideChat(parentConversationId) { createdSideChats.push(parentConversationId); },
      closeSideChat(sideChatId) { closedSideChats.push(sideChatId); },
      acknowledgeRead() {},
      loadModelCatalog(hostId) { modelCatalogLoads.push(hostId); },
      updateConversationSettings(model, reasoningEffort) { settingsUpdates.push({ model, reasoningEffort }); },
      reconnect(conversationId) { reconnects.push(conversationId); },
      createHost() {}, probeHost() {}, createProject() {}, retry() {},
      async uploadAttachment(conversationId, file, attachmentId) {
        uploadAttempts.push({ file, attachmentId });
        if (failNextUpload) { failNextUpload = false; throw new Error('fixture upload failed'); }
        uploadedFiles.push(file);
        return {
          attachmentId: `uploaded-${uploadedFiles.length}`, conversationId,
          kind: file.type.startsWith('image/') ? 'image' : 'pasted_text', displayName: file.name,
          mediaType: file.type, byteSize: file.size, state: 'staged', url: '', raw: {},
        };
      },
      async deleteAttachment(_conversationId, attachmentId) {
        deletedAttachmentIds.push(attachmentId);
        if (failNextDelete) { failNextDelete = false; throw new Error('fixture delete failed'); }
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
    check(['.cw-commentary', '.cw-command', '.cw-file', '.cw-plan', '.cw-subagent-activity', '.cw-collab-activity']
      .every((selector) => runningCommentary.contains(conversationHost.querySelector(selector))), 'Commentary, command, file, plan, and agent activity share one per-turn Work details group');
    check(!conversationHost.querySelector('.cw-unknown') && !conversationHost.textContent?.includes('future.item') && !conversationHost.textContent?.includes('turn/started'), 'Raw lifecycle, protocol, and unknown event cards stay hidden');
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
    check(!interruptedWork.open && interruptedWork.querySelector('summary')?.textContent === 'Work details · Interrupted'
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

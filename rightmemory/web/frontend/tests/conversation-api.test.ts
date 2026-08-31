import assert from 'node:assert/strict';
import test from 'node:test';
import { ConversationApi, type EventStream, type FetchJson } from '../src/conversation-api.ts';

class FakeStream implements EventStream {
  onopen: ((event: Event) => void) | null = null;
  onerror: ((event: Event) => void) | null = null;
  onmessage: ((event: MessageEvent<string>) => void) | null = null;
  closed = false;
  listeners = new Map<string, EventListener[]>();
  addEventListener(type: string, listener: EventListener): void {
    this.listeners.set(type, [...(this.listeners.get(type) ?? []), listener]);
  }
  close(): void { this.closed = true; }
  emit(type: string, value: unknown): void {
    const event = new MessageEvent(type, { data: JSON.stringify(value) });
    for (const listener of this.listeners.get(type) ?? []) listener(event);
    if (type === 'message') this.onmessage?.(event);
  }
}

test('conversation API uses the controller HTTP boundary and exact snake-case request bodies', async () => {
  const requests: Array<{ path: string; options?: RequestInit }> = [];
  const fetchJson: FetchJson = async (path, options) => {
    requests.push({ path, options });
    if (path === '/api/conversation-workspace') return { data: { hosts: [], projects: [], conversations: [], pending_requests: [], cursor: null } };
    if (path.startsWith('/api/pursuit-conversations?')) return { data: { conversations: [], default: { pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one' } } };
    if (path.startsWith('/api/conversation-models?')) return { data: {
      host_id: 'h/one',
      models: [{ id: 'gpt-5.6', display_name: 'GPT-5.6', default_reasoning_effort: 'medium', supported_reasoning_efforts: [{ reasoning_effort: 'low', description: 'Faster' }, { reasoning_effort: 'medium', description: 'Balanced' }], is_default: true }],
      default_model: 'gpt-5.6',
      default_reasoning_effort: 'medium',
    } };
    if (path === '/api/pursuit-conversations') return { data: { conversation: { conversation_id: 'c1', pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', model: 'gpt-5.6', reasoning_effort: 'medium' } } };
    if (path === '/api/manager-conversations') return { data: { conversation: { conversation_id: 'manager/one', pursuit_id: null, kind: 'manager', host_id: 'local', project_id: 'local-root', execution_cwd: 'C:\\memory', model: 'gpt-5.6', reasoning_effort: 'high' } } };
    if (path.endsWith('/side-chats') && options?.method === 'POST') return { data: { conversation: { conversation_id: 'side/one', pursuit_id: 'p/one', kind: 'side_chat', parent_conversation_id: 'c/one', host_id: 'h1', project_id: 'project one' } } };
    if (path.includes('/history?')) return { data: {
      conversation_id: 'c/one',
      events: [{ event_id: 12, conversation_id: 'c/one', kind: 'user.message', payload: { text: 'Earlier' } }],
      has_earlier_events: true,
    } };
    if (path.endsWith('/read')) return { data: { conversation: { conversation_id: 'c/one', pursuit_id: 'p/one', last_final_event_id: 51, last_read_event_id: 51 } } };
    if (path.endsWith('/settings')) return { data: { conversation: { conversation_id: 'c/one', pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', model: 'gpt-5.6-mini', reasoning_effort: 'low' } } };
    if (path.endsWith('/attachments') && options?.method === 'POST') {
      const headers = options.headers as Record<string, string>;
      const genericFile = headers['x-attachment-kind'] === 'file';
      return { data: { attachment: {
        attachment_id: genericFile ? 'a/two' : 'a/one',
        conversation_id: 'c/one',
        kind: genericFile ? 'file' : 'image',
        display_name: decodeURIComponent(headers['x-filename']),
        media_type: headers['content-type'],
        byte_size: (options.body as File).size,
        state: 'staged',
      } } };
    }
    if (path === '/api/conversation-hosts') return { data: { host: { host_id: 'h2', kind: 'ssh', display_name: 'GPU', ssh_alias: 'gpu' } } };
    if (path === '/api/conversation-projects') return { data: { project: { project_id: 'p2', host_id: 'h2', label: 'Repo', cwd: '/repo' } } };
    if (path.endsWith('/reconcile')) return { data: {
      conversation: { conversation_id: 'c/one', pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', status: 'idle' },
      resolved: true,
      accepted_user_event_id: 52,
      thread: { turns: [{ id: 'turn-2' }] },
    } };
    return { data: {} };
  };
  const api = new ConversationApi(fetchJson);
  await api.workspace();
  const pursuit = await api.pursuitConversations('p/one');
  const catalog = await api.modelCatalog('h/one');
  const created = await api.createConversation('p/one', 'h1', 'project one', 'gpt-5.6', 'medium');
  const manager = await api.createManager('gpt-5.6', 'high');
  const updated = await api.updateSettings('c/one', 'gpt-5.6-mini', 'low');
  const sideChat = await api.createSideChat('c/one');
  const earlier = await api.earlierConversation('c/one', '40');
  const read = await api.acknowledgeRead('c/one', 51);
  await api.deleteSideChat('side/one');
  const uploadId = '0123456789abcdef0123456789abcdef';
  const uploaded = await api.uploadAttachment('c/one', new File([new Uint8Array([1, 2, 3])], 'diagram.png', { type: 'image/png' }), uploadId);
  const fileUploadId = 'fedcba9876543210fedcba9876543210';
  const uploadedFile = await api.uploadAttachment('c/one', new File(['notes'], 'notes.txt', { type: 'text/plain' }), fileUploadId, 'file');
  await api.sendMessage('c/one', 'hello', ['a/one']);
  await api.sendMessage('manager/one', 'Inspect this.', [], [{ kind: 'pursuit', id: 'p/one' }]);
  await api.deleteAttachment('c/one', 'a/one');
  await api.interrupt('c/one');
  const reconciled = await api.reconcile('c/one');
  await api.respond('c/one', 'request/1', { decision: 'accept' });
  await api.respond('c/one', 'input/2', { response: { scope: { answers: ['Current branch'] }, mode: { answers: ['Review'] } } });
  await api.createHost('GPU', 'gpu');
  await api.createProject('h2', 'Repo', '/repo');
  await api.releaseView('view/one', 'page one');
  const request = (path: string) => {
    const entry = requests.find((candidate) => candidate.path === path);
    assert(entry, `Missing request ${path}`);
    return entry;
  };
  assert.equal(requests[1].path, '/api/pursuit-conversations?pursuit_id=p%2Fone');
  assert.equal(pursuit.default?.projectId, 'project one');
  assert.equal(requests[2].path, '/api/conversation-models?host_id=h%2Fone');
  assert.equal(catalog.models[0].displayName, 'GPT-5.6');
  assert.equal(catalog.models[0].supportedReasoningEfforts[0].reasoningEffort, 'low');
  assert.equal(catalog.defaultReasoningEffort, 'medium');
  assert.equal(created.model, 'gpt-5.6');
  assert.equal(manager.kind, 'manager');
  assert.equal(manager.pursuitId, null);
  assert.equal(manager.executionCwd, 'C:\\memory');
  assert.deepEqual(JSON.parse(String(request('/api/pursuit-conversations').options?.body)), {
    pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', model: 'gpt-5.6', reasoning_effort: 'medium',
  });
  assert.deepEqual(JSON.parse(String(request('/api/manager-conversations').options?.body)), {
    model: 'gpt-5.6', reasoning_effort: 'high',
  });
  assert.equal(updated.reasoningEffort, 'low');
  assert.equal(sideChat.parentConversationId, 'c/one');
  assert.equal(sideChat.kind, 'side_chat');
  assert.equal(earlier.events[0].eventId, '12');
  assert.equal(earlier.hasEarlierEvents, true);
  assert.equal(requests.find((entry) => entry.path.includes('/history?'))?.path, '/api/conversations/c%2Fone/history?before_event_id=40');
  assert.equal(read.lastReadEventId, 51);
  assert.equal(request('/api/conversations/c%2Fone/side-chats').options?.method, 'POST');
  assert.equal(request('/api/conversations/c%2Fone/read').options?.method, 'POST');
  assert.deepEqual(JSON.parse(String(request('/api/conversations/c%2Fone/read').options?.body)), { event_id: 51 });
  assert.equal(request('/api/side-chats/side%2Fone').options?.method, 'DELETE');
  assert.deepEqual(JSON.parse(String(request('/api/conversations/c%2Fone/settings').options?.body)), { model: 'gpt-5.6-mini', reasoning_effort: 'low' });
  const uploadRequests = requests.filter((entry) => entry.path === '/api/conversations/c%2Fone/attachments');
  assert.equal(uploadRequests.length, 2);
  const uploadRequest = uploadRequests[0];
  assert(uploadRequest.options?.body instanceof File);
  assert.equal((uploadRequest.options?.headers as Record<string, string>)['content-type'], 'image/png');
  assert.equal((uploadRequest.options?.headers as Record<string, string>)['x-filename'], 'diagram.png');
  assert.equal((uploadRequest.options?.headers as Record<string, string>)['x-attachment-id'], uploadId);
  assert.equal((uploadRequest.options?.headers as Record<string, string>)['x-attachment-kind'], undefined);
  assert.equal(uploaded.displayName, 'diagram.png');
  const fileUploadRequest = uploadRequests[1];
  assert.equal((fileUploadRequest.options?.headers as Record<string, string>)['content-type'], 'text/plain');
  assert.equal((fileUploadRequest.options?.headers as Record<string, string>)['x-filename'], 'notes.txt');
  assert.equal((fileUploadRequest.options?.headers as Record<string, string>)['x-attachment-id'], fileUploadId);
  assert.equal((fileUploadRequest.options?.headers as Record<string, string>)['x-attachment-kind'], 'file');
  assert.equal(uploadedFile.kind, 'file');
  assert.deepEqual(JSON.parse(String(request('/api/conversations/c%2Fone/messages').options?.body)), { text: 'hello', attachment_ids: ['a/one'] });
  assert.deepEqual(JSON.parse(String(request('/api/conversations/manager%2Fone/messages').options?.body)), {
    text: 'Inspect this.', attachment_ids: [], references: [{ kind: 'pursuit', id: 'p/one' }],
  });
  assert.equal(request('/api/conversations/c%2Fone/attachments/a%2Fone').options?.method, 'DELETE');
  assert.equal(request('/api/conversations/c%2Fone/reconcile').path, '/api/conversations/c%2Fone/reconcile');
  assert.equal(reconciled.conversation.status, 'idle');
  assert.equal(reconciled.resolved, true);
  assert.equal(reconciled.acceptedUserEventId, '52');
  assert.equal(request('/api/conversations/c%2Fone/server-requests/request%2F1/respond').path, '/api/conversations/c%2Fone/server-requests/request%2F1/respond');
  const inputResponse = requests.find((entry) => entry.path.endsWith('/input%2F2/respond'))!;
  assert.deepEqual(JSON.parse(String(inputResponse.options?.body)), { response: { scope: { answers: ['Current branch'] }, mode: { answers: ['Review'] } } });
  assert.deepEqual(JSON.parse(String(request('/api/conversation-hosts').options?.body)), { kind: 'ssh', display_name: 'GPU', ssh_alias: 'gpu' });
  const releaseRequest = request('/api/conversation-session/release');
  assert.equal(releaseRequest.options?.keepalive, true);
  assert.deepEqual(JSON.parse(String(releaseRequest.options?.body)), { view_id: 'view/one', page_id: 'page one' });
});

test('event stream handles snapshots, conversation events, unknown kinds, malformed data, and close', () => {
  const fake = new FakeStream();
  const snapshots: unknown[] = [];
  const events: unknown[] = [];
  let opens = 0;
  let errors = 0;
  let streamUrl = '';
  const api = new ConversationApi(async () => ({ data: {} }), (url) => { streamUrl = url; return fake; });
  const stream = api.events('42', 'view/one', 'page one', {
    snapshot: (snapshot) => snapshots.push(snapshot),
    event: (event) => events.push(event),
    open: () => { opens++; },
    error: () => { errors++; },
  });
  fake.onopen?.(new Event('open'));
  fake.emit('snapshot', { root_key: 'root-one', hosts: [{ host_id: 'local', display_name: 'Local' }], projects: [], conversations: [], pending_requests: [] });
  fake.emit('conversation', { event_id: 'e1', conversation_id: 'c1', kind: 'future.kind', payload: { safe: true } });
  fake.onmessage?.(new MessageEvent('message', { data: '{bad' }));
  fake.onerror?.(new Event('error'));
  assert.equal(opens, 1);
  assert.equal(errors, 1);
  assert.equal(streamUrl, '/api/conversation-events?after_event_id=42&view_id=view%2Fone&page_id=page%20one');
  assert.equal(snapshots.length, 1);
  assert.equal((snapshots[0] as { rootKey: string }).rootKey, 'root-one');
  assert.equal(events.length, 1);
  assert.equal((events[0] as { kind: string }).kind, 'future.kind');
  stream.close();
  assert.equal(fake.closed, true);
});

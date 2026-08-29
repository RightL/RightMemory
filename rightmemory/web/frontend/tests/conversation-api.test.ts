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
    if (path.endsWith('/settings')) return { data: { conversation: { conversation_id: 'c/one', pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', model: 'gpt-5.6-mini', reasoning_effort: 'low' } } };
    if (path === '/api/conversation-hosts') return { data: { host: { host_id: 'h2', kind: 'ssh', display_name: 'GPU', ssh_alias: 'gpu' } } };
    if (path === '/api/conversation-projects') return { data: { project: { project_id: 'p2', host_id: 'h2', label: 'Repo', cwd: '/repo' } } };
    if (path.endsWith('/reconcile')) return { data: { conversation: { conversation_id: 'c/one', pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', status: 'idle' } } };
    return { data: {} };
  };
  const api = new ConversationApi(fetchJson);
  await api.workspace();
  const pursuit = await api.pursuitConversations('p/one');
  const catalog = await api.modelCatalog('h/one');
  const created = await api.createConversation('p/one', 'h1', 'project one', 'gpt-5.6', 'medium');
  const updated = await api.updateSettings('c/one', 'gpt-5.6-mini', 'low');
  await api.sendMessage('c/one', 'hello');
  await api.interrupt('c/one');
  const reconciled = await api.reconcile('c/one');
  await api.respond('c/one', 'request/1', { decision: 'accept' });
  await api.respond('c/one', 'input/2', { response: { scope: { answers: ['Current branch'] }, mode: { answers: ['Review'] } } });
  await api.createHost('GPU', 'gpu');
  await api.createProject('h2', 'Repo', '/repo');
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
  assert.deepEqual(JSON.parse(String(request('/api/pursuit-conversations').options?.body)), {
    pursuit_id: 'p/one', host_id: 'h1', project_id: 'project one', model: 'gpt-5.6', reasoning_effort: 'medium',
  });
  assert.equal(updated.reasoningEffort, 'low');
  assert.deepEqual(JSON.parse(String(request('/api/conversations/c%2Fone/settings').options?.body)), { model: 'gpt-5.6-mini', reasoning_effort: 'low' });
  assert.deepEqual(JSON.parse(String(request('/api/conversations/c%2Fone/messages').options?.body)), { text: 'hello' });
  assert.equal(requests[7].path, '/api/conversations/c%2Fone/reconcile');
  assert.equal(reconciled.status, 'idle');
  assert.equal(requests[8].path, '/api/conversations/c%2Fone/server-requests/request%2F1/respond');
  assert.deepEqual(JSON.parse(String(requests[9].options?.body)), { response: { scope: { answers: ['Current branch'] }, mode: { answers: ['Review'] } } });
  assert.deepEqual(JSON.parse(String(requests[10].options?.body)), { kind: 'ssh', display_name: 'GPU', ssh_alias: 'gpu' });
});

test('event stream handles snapshots, conversation events, unknown kinds, malformed data, and close', () => {
  const fake = new FakeStream();
  const snapshots: unknown[] = [];
  const events: unknown[] = [];
  let opens = 0;
  let errors = 0;
  let streamUrl = '';
  const api = new ConversationApi(async () => ({ data: {} }), (url) => { streamUrl = url; return fake; });
  const stream = api.events('42', {
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
  assert.equal(streamUrl, '/api/conversation-events?after_event_id=42');
  assert.equal(snapshots.length, 1);
  assert.equal((snapshots[0] as { rootKey: string }).rootKey, 'root-one');
  assert.equal(events.length, 1);
  assert.equal((events[0] as { kind: string }).kind, 'future.kind');
  stream.close();
  assert.equal(fake.closed, true);
});

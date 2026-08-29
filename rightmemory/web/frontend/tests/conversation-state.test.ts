import assert from 'node:assert/strict';
import test from 'node:test';
import {
  conversationCanSend,
  conversationsForPursuit,
  initialConversationState,
  normalizeConversationDetail,
  normalizeEvent,
  normalizeWorkspace,
  reduceConversationState,
} from '../src/conversation-state.ts';

const workspacePayload = {
  hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer', status: 'online' }],
  projects: [{ project_id: 'active-root', host_id: 'local', label: 'RightMemory', cwd: 'C:\\repo' }],
  conversations: [
    { conversation_id: 'c-old', pursuit_id: 'design', host_id: 'local', project_id: 'active-root', title: 'Older', status: 'idle', updated_at: '2026-01-01' },
    { conversation_id: 'c-new', pursuit_id: 'design', host_id: 'local', project_id: 'active-root', title: 'Newer', status: 'running', updated_at: '2026-01-02' },
    { conversation_id: 'c-other', pursuit_id: 'research', host_id: 'local', project_id: 'active-root', title: 'Other' },
  ],
  pending_requests: [{ key: 'approve-1', conversation_id: 'c-new', kind: 'approval', payload: { reason: 'Run tests' } }],
  pursuit_defaults: { design: { pursuit_id: 'design', host_id: 'local', project_id: 'active-root', last_used_at: '2026-01-02' } },
  cursor: 42,
};

test('workspace records are normalized from the snake-case API without losing raw fields', () => {
  const snapshot = normalizeWorkspace(workspacePayload);
  assert.equal(snapshot.rootKey, '');
  assert.equal(snapshot.hosts[0].displayName, 'This computer');
  assert.equal(snapshot.projects[0].cwd, 'C:\\repo');
  assert.equal(snapshot.conversations[1].conversationId, 'c-new');
  assert.equal(snapshot.pendingRequests[0].key, 'approve-1');
  assert.equal(snapshot.pursuitDefaults.design.projectId, 'active-root');
  assert.equal(snapshot.cursor, '42');
});

test('selection and pursuit loads remain scoped when responses arrive out of order', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'pursuit-selected', pursuitId: 'design' });
  state = reduceConversationState(state, { type: 'pursuit-loaded', pursuitId: 'research', conversations: [], default: null });
  assert.equal(state.loadingPursuit, true);
  assert.deepEqual(conversationsForPursuit(state).map((item) => item.conversationId), ['c-new', 'c-old']);
  state = reduceConversationState(state, { type: 'pursuit-loaded', pursuitId: 'design', conversations: normalizeWorkspace(workspacePayload).conversations.slice(0, 2), default: normalizeWorkspace(workspacePayload).pursuitDefaults.design });
  assert.equal(state.loadingPursuit, false);
});

test('conversation history, streaming events, status, unknown kinds, and pending resolution reduce independently', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'pursuit-selected', pursuitId: 'design' });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-new' });
  const detail = normalizeConversationDetail({
    conversation: workspacePayload.conversations[1],
    events: [{ event_id: 'e1', conversation_id: 'c-new', turn_id: 't1', kind: 'agent_message_delta', payload: { delta: 'Hello' } }],
    pending_requests: workspacePayload.pending_requests,
  });
  assert(detail);
  state = reduceConversationState(state, { type: 'conversation-loaded', detail });
  const delta = normalizeEvent({ event_id: 'e2', conversation_id: 'c-new', turn_id: 't1', kind: 'agent_message_delta', payload: { delta: ' world' } });
  const unknown = normalizeEvent({ event_id: 'e3', conversation_id: 'c-new', turn_id: 't1', kind: 'future/item', payload: { novel: true } });
  const completed = normalizeEvent({ event_id: 'e4', conversation_id: 'c-new', turn_id: 't1', kind: 'turn.completed', payload: {} });
  assert(delta && unknown && completed);
  state = reduceConversationState(state, { type: 'event', event: delta });
  state = reduceConversationState(state, { type: 'event', event: unknown });
  state = reduceConversationState(state, { type: 'event', event: completed });
  state = reduceConversationState(state, { type: 'event', event: completed });
  assert.deepEqual(state.eventsByConversation['c-new'].map((event) => event.eventId), ['e1', 'e2', 'e3', 'e4']);
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'completed');
  state = reduceConversationState(state, { type: 'pending-resolved', conversationId: 'c-new', key: 'approve-1' });
  assert.equal(state.pendingRequests.length, 0);
});

test('a late conversation detail cannot replace the conversation the user opened next', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-new' });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-old' });
  const late = normalizeConversationDetail({ conversation: workspacePayload.conversations[1], events: [], pending_requests: [] });
  assert(late);
  assert.equal(reduceConversationState(state, { type: 'conversation-loaded', detail: late }), state);
});

test('live server-request events add and resolve pending cards without losing timeline evidence', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const pending = normalizeEvent({
    event_id: 50,
    conversation_id: 'c-new',
    turn_id: 't2',
    kind: 'server_request.pending',
    payload: { request: { request_key: 'input-2', method: 'item/tool/requestUserInput', payload: { questions: [{ id: 'scope', question: 'Which scope?' }] } } },
  });
  assert(pending);
  state = reduceConversationState(state, { type: 'event', event: pending });
  assert.equal(state.pendingRequests.find((request) => request.key === 'input-2')?.kind, 'item/tool/requestUserInput');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'waiting_input');
  const resolved = normalizeEvent({ event_id: 51, conversation_id: 'c-new', kind: 'server_request_resolved', payload: { request_key: 'input-2' } });
  assert(resolved);
  state = reduceConversationState(state, { type: 'event', event: resolved });
  assert(!state.pendingRequests.some((request) => request.key === 'input-2'));
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'idle');
  assert.deepEqual(state.eventsByConversation['c-new'].slice(-2).map((event) => event.eventId), ['50', '51']);
});

test('archived lifecycle is kept separate from operational turn status', () => {
  const snapshot = normalizeWorkspace({
    hosts: [], projects: [], pending_requests: [],
    conversations: [{ conversation_id: 'archived', pursuit_id: 'design', lifecycle: 'archived', status: 'completed' }],
  });
  assert.equal(snapshot.conversations[0].archived, true);
  assert.equal(snapshot.conversations[0].status, 'completed');
});

test('a workspace response older than streamed events cannot restore stale status, title, or pending requests', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const renamed = normalizeEvent({
    event_id: 43,
    conversation_id: 'c-new',
    kind: 'thread.name',
    created_at: '2026-01-03T00:00:00Z',
    payload: { threadName: 'Live title' },
  });
  const resolved = normalizeEvent({
    event_id: 44,
    conversation_id: 'c-new',
    kind: 'server_request_resolved',
    created_at: '2026-01-03T00:00:01Z',
    payload: { request_key: 'approve-1' },
  });
  const completed = normalizeEvent({
    event_id: 45,
    conversation_id: 'c-new',
    kind: 'turn.completed',
    created_at: '2026-01-03T00:00:02Z',
    payload: {},
  });
  assert(renamed && resolved && completed);
  state = reduceConversationState(state, { type: 'event', event: renamed });
  state = reduceConversationState(state, { type: 'event', event: resolved });
  state = reduceConversationState(state, { type: 'event', event: completed });
  state = reduceConversationState(state, { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const conversation = state.conversations.find((item) => item.conversationId === 'c-new');
  assert.equal(state.cursor, '45');
  assert.equal(conversation?.title, 'Live title');
  assert.equal(conversation?.status, 'completed');
  assert(!state.pendingRequests.some((request) => request.key === 'approve-1'));
});

test('a late detail merges its history without replacing newer streamed state', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-new' });
  const live = normalizeEvent({
    event_id: 43,
    conversation_id: 'c-new',
    kind: 'thread.name',
    created_at: '2026-01-03T00:00:00Z',
    payload: { threadName: 'Renamed while loading' },
  });
  assert(live);
  state = reduceConversationState(state, { type: 'event', event: live });
  const detail = normalizeConversationDetail({
    conversation: workspacePayload.conversations[1],
    events: [{ event_id: 41, conversation_id: 'c-new', kind: 'item.completed', payload: { item: { type: 'userMessage', text: 'Earlier' } } }],
    pending_requests: workspacePayload.pending_requests,
    cursor: 42,
  });
  assert(detail);
  state = reduceConversationState(state, { type: 'conversation-loaded', detail });
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.title, 'Renamed while loading');
  assert.deepEqual(state.eventsByConversation['c-new'].map((event) => event.eventId), ['41', '43']);
});

test('a host-wide disconnect projects unknown state and recovery evidence onto every affected conversation', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const disconnected = normalizeEvent({
    event_id: 46,
    conversation_id: null,
    kind: 'connection.disconnected',
    created_at: '2026-01-03T00:01:00Z',
    payload: { host_id: 'local', conversation_ids: ['c-new', 'c-old'] },
  });
  assert(disconnected);
  state = reduceConversationState(state, { type: 'event', event: disconnected });
  assert.equal(state.hosts[0].status, 'offline');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'unknown');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-old')?.status, 'unknown');
  assert.equal(state.eventsByConversation['c-new'].at(-1)?.kind, 'connection.disconnected');
  assert.equal(state.eventsByConversation['c-old'].at(-1)?.kind, 'connection.disconnected');
});

test('sendability stays guarded while transport, provider, or a send is busy', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const conversation = state.conversations.find((item) => item.conversationId === 'c-old')!;
  assert.equal(conversationCanSend(state, conversation), false);
  state = reduceConversationState(state, { type: 'connection', connection: 'open' });
  assert.equal(conversationCanSend(state, conversation), true);
  state = reduceConversationState(state, { type: 'send-in-flight', conversationId: 'c-old', active: true });
  assert.equal(conversationCanSend(state, conversation), false);
  const running = { ...conversation, status: 'running' };
  assert.equal(conversationCanSend({ ...state, sendingConversationId: null }, running), false);
});

test('an equal-cursor workspace response conservatively keeps local metadata and live summaries', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const host = normalizeWorkspace({ hosts: [{ host_id: 'remote', kind: 'ssh', display_name: 'Remote' }] }).hosts[0];
  const project = normalizeWorkspace({ projects: [{ project_id: 'remote-repo', host_id: 'remote', label: 'Repo', cwd: '/repo' }] }).projects[0];
  state = reduceConversationState(state, { type: 'host-added', host });
  state = reduceConversationState(state, { type: 'project-added', project });
  const current = state.conversations.find((item) => item.conversationId === 'c-new')!;
  state = reduceConversationState(state, { type: 'conversation-updated', conversation: { ...current, title: 'Local live title', updatedAt: '2026-01-03' } });
  state = reduceConversationState(state, { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  assert(state.hosts.some((item) => item.hostId === 'remote'));
  assert(state.projects.some((item) => item.projectId === 'remote-repo'));
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.title, 'Local live title');
});

test('detail events beyond its captured cursor project once before an SSE duplicate arrives', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-new' });
  const raced = { event_id: 43, conversation_id: 'c-new', turn_id: 't1', kind: 'turn.completed', created_at: '2026-01-03', payload: {} };
  const detail = normalizeConversationDetail({ conversation: workspacePayload.conversations[1], events: [raced], pending_requests: workspacePayload.pending_requests, cursor: 42 });
  assert(detail);
  state = reduceConversationState(state, { type: 'conversation-loaded', detail });
  assert.equal(state.cursor, '43');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'completed');
  const replay = normalizeEvent(raced);
  assert(replay);
  const replayed = reduceConversationState(state, { type: 'event', event: replay });
  assert.equal(replayed.conversations.find((item) => item.conversationId === 'c-new')?.status, 'completed');
  assert.equal(replayed.eventsByConversation['c-new'].length, 1);
});

test('uncertain protocol failures stay recoverable and a replayed disconnect cannot undo recovery', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const uncertain = normalizeEvent({ event_id: 43, conversation_id: 'c-old', kind: 'protocol.error', created_at: '2026-01-03', payload: { operation: 'turn/start', message: 'lost' } });
  assert(uncertain);
  state = reduceConversationState(state, { type: 'event', event: uncertain });
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-old')?.status, 'unknown');
  const disconnected = normalizeEvent({ event_id: 44, conversation_id: null, kind: 'connection.disconnected', created_at: '2026-01-03', payload: { host_id: 'local', conversation_ids: ['c-old'] } });
  assert(disconnected);
  state = reduceConversationState(state, { type: 'event', event: disconnected });
  const unknown = state.conversations.find((item) => item.conversationId === 'c-old')!;
  state = reduceConversationState(state, { type: 'conversation-updated', conversation: { ...unknown, status: 'idle', updatedAt: '2026-01-04' } });
  state = reduceConversationState(state, { type: 'event', event: disconnected });
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-old')?.status, 'idle');
});

test('an uncertain server response is terminal in the browser and cannot leave a retryable pending card', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const failed = normalizeEvent({
    event_id: 43,
    conversation_id: 'c-new',
    kind: 'server_response_failed',
    payload: { request_key: 'approve-1', message: 'write outcome unknown' },
  });
  assert(failed);
  state = reduceConversationState(state, { type: 'event', event: failed });
  assert(!state.pendingRequests.some((request) => request.key === 'approve-1'));
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'unknown');
});

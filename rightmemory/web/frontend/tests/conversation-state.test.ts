import assert from 'node:assert/strict';
import test from 'node:test';
import {
  conversationCanSend,
  conversationsForPursuit,
  initialConversationState,
  managerConversations,
  normalizeConversationDetail,
  normalizeEvent,
  normalizeModelCatalog,
  normalizeWorkspace,
  reduceConversationState,
} from '../src/conversation-state.ts';

const workspacePayload = {
  hosts: [{ host_id: 'local', kind: 'local', display_name: 'This computer', status: 'online' }],
  projects: [{ project_id: 'active-root', host_id: 'local', label: 'RightMemory', cwd: 'C:\\repo' }],
  conversations: [
    { conversation_id: 'c-old', pursuit_id: 'design', host_id: 'local', project_id: 'active-root', title: 'Older', status: 'idle', updated_at: '2026-01-01' },
    { conversation_id: 'c-new', pursuit_id: 'design', host_id: 'local', project_id: 'active-root', model: 'gpt-5.6', reasoning_effort: 'high', title: 'Newer', status: 'running', updated_at: '2026-01-02' },
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
  assert.equal(snapshot.conversations[1].model, 'gpt-5.6');
  assert.equal(snapshot.conversations[1].reasoningEffort, 'high');
  assert.equal(snapshot.pendingRequests[0].key, 'approve-1');
  assert.equal(snapshot.pursuitDefaults.design.projectId, 'active-root');
  assert.equal(snapshot.cursor, '42');
});

test('conversation detail normalizes attachment metadata and unread summary cursors defensively', () => {
  const detail = normalizeConversationDetail({
    conversation: {
      conversation_id: 'c-with-files', pursuit_id: 'design', kind: 'side_chat', parent_conversation_id: 'c-new',
      last_final_event_id: '57', last_read_event_id: 52,
    },
    attachments: [
      { attachment_id: 'image/1', kind: 'image', display_name: '图.png', media_type: 'image/png', byte_size: '2048', state: 'sent', url: '/preview/image-1' },
      { attachmentId: 'text-1', conversationId: 'c-with-files', type: 'pasted_text', filename: 'pasted-text.txt', contentType: 'text/plain', byteSize: 9000 },
      { attachment_id: 'file-1', kind: 'file', display_name: 'archive.zip', media_type: 'application/zip', byte_size: 4096, state: 'staged' },
      { kind: 'image' },
    ],
    has_earlier_events: true,
  });
  assert(detail);
  assert.equal(detail.conversation.kind, 'side_chat');
  assert.equal(detail.conversation.parentConversationId, 'c-new');
  assert.equal(detail.conversation.lastFinalEventId, 57);
  assert.equal(detail.conversation.lastReadEventId, 52);
  assert.equal(detail.attachments.length, 3);
  assert.equal(detail.attachments[0].conversationId, 'c-with-files');
  assert.equal(detail.attachments[0].byteSize, 2048);
  assert.equal(detail.attachments[1].kind, 'pasted_text');
  assert.equal(detail.attachments[2].kind, 'file');
  assert.equal(detail.attachments[2].displayName, 'archive.zip');
  assert.equal(detail.hasEarlierEvents, true);
});

test('earlier history pages prepend chronologically, dedupe, and survive a later detail refresh', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'conversation-loading', conversationId: 'c-new' });
  const detail = normalizeConversationDetail({
    conversation: workspacePayload.conversations[1],
    events: [
      { event_id: 501, conversation_id: 'c-new', kind: 'user.message', payload: { text: 'Recent' } },
      { event_id: 502, conversation_id: 'c-new', kind: 'item.completed', payload: {} },
    ],
    has_earlier_events: true,
    cursor: 502,
  });
  assert(detail);
  state = reduceConversationState(state, { type: 'conversation-loaded', detail });
  assert.equal(state.hasEarlierEventsByConversation['c-new'], true);
  state = reduceConversationState(state, { type: 'conversation-history-in-flight', conversationId: 'c-new', active: true });
  state = reduceConversationState(state, {
    type: 'conversation-history-loaded',
    page: {
      conversationId: 'c-new',
      events: [
        normalizeEvent({ event_id: 1, conversation_id: 'c-new', kind: 'thread.started', payload: {} })!,
        normalizeEvent({ event_id: 501, conversation_id: 'c-new', kind: 'user.message', payload: { text: 'Recent' } })!,
      ],
      hasEarlierEvents: false,
    },
  });
  assert.deepEqual(state.eventsByConversation['c-new'].map((event) => event.eventId), ['1', '501', '502']);
  assert.equal(state.hasEarlierEventsByConversation['c-new'], false);
  assert.deepEqual(state.loadingEarlierConversationIds, []);

  state = reduceConversationState(state, { type: 'conversation-loaded', detail });
  assert.equal(state.hasEarlierEventsByConversation['c-new'], false);
});

test('model catalogs and optimistic mid-conversation settings normalize without leaking API casing', () => {
  const catalog = normalizeModelCatalog({ data: {
    host_id: 'local',
    models: [
      {
        id: 'gpt-5.6',
        display_name: 'GPT-5.6',
        default_reasoning_effort: 'medium',
        supported_reasoning_efforts: [
          { reasoning_effort: 'low', description: 'Faster answers' },
          { reasoning_effort: 'medium', description: 'Balanced answers' },
          { reasoning_effort: 'high', description: 'Deeper reasoning' },
        ],
        is_default: true,
      },
    ],
    default_model: 'gpt-5.6',
    default_reasoning_effort: 'medium',
  } });
  assert.equal(catalog.hostId, 'local');
  assert.equal(catalog.defaultModel, 'gpt-5.6');
  assert.deepEqual(catalog.models[0].supportedReasoningEfforts.map((option) => option.reasoningEffort), ['low', 'medium', 'high']);

  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, {
    type: 'conversation-settings-selected', conversationId: 'c-new', model: 'gpt-5.6-mini', reasoningEffort: 'low',
  });
  const selected = state.conversations.find((conversation) => conversation.conversationId === 'c-new');
  assert.equal(selected?.model, 'gpt-5.6-mini');
  assert.equal(selected?.reasoningEffort, 'low');
  assert.equal(state.conversations.find((conversation) => conversation.conversationId === 'c-old')?.model, '');
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

test('Manager mode preserves the selected Pursuit and keeps root-local conversations out of Pursuit lists', () => {
  const snapshot = normalizeWorkspace({
    ...workspacePayload,
    conversations: [...workspacePayload.conversations, {
      conversation_id: 'manager-1', pursuit_id: null, kind: 'manager', host_id: 'local', project_id: 'active-root',
      execution_cwd: 'C:\\captured-root', title: 'Manager', status: 'idle', updated_at: '2026-01-03',
    }],
  });
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot });
  state = reduceConversationState(state, { type: 'pursuit-selected', pursuitId: 'design' });
  state = reduceConversationState(state, { type: 'manager-opened', pursuitId: 'design' });
  assert.equal(state.selectedPursuitId, 'design');
  assert.equal(state.managerOpen, true);
  assert.equal(state.managerReferencePursuitId, 'design');
  const firstReferenceVersion = state.managerReferenceVersion;
  assert.deepEqual(managerConversations(state).map((item) => item.conversationId), ['manager-1']);
  assert.equal(managerConversations(state)[0].executionCwd, 'C:\\captured-root');
  assert.deepEqual(conversationsForPursuit(state).map((item) => item.conversationId), ['c-new', 'c-old']);
  state = reduceConversationState(state, { type: 'manager-reference-removed' });
  assert.equal(state.managerReferencePursuitId, null);
  state = reduceConversationState(state, { type: 'manager-opened', pursuitId: 'research' });
  state = reduceConversationState(state, { type: 'manager-reference-sent', version: firstReferenceVersion });
  assert.equal(state.managerReferencePursuitId, 'research');
  state = reduceConversationState(state, { type: 'pursuit-selected', pursuitId: 'design' });
  assert.equal(state.managerOpen, false);
  assert.equal(state.selectedPursuitId, 'design');
});

test('session side chats stay out of Pursuit lists and defaults while their detail state can be restored and removed', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'pursuit-selected', pursuitId: 'design' });
  const sideChat = normalizeWorkspace({ conversations: [{
    conversation_id: 'side-1', pursuit_id: 'design', kind: 'side_chat', parent_conversation_id: 'c-new',
    host_id: 'local', project_id: 'active-root', title: 'Untitled conversation', status: 'idle', updated_at: '2026-01-03',
  }] }).conversations[0];
  state = reduceConversationState(state, { type: 'side-chat-session', conversationIds: ['side-1', 'side-1', ''] });
  state = reduceConversationState(state, { type: 'conversation-created', conversation: sideChat });
  assert.deepEqual(state.sessionSideChatIds, ['side-1']);
  assert.equal(state.pursuitDefaults.design.hostId, 'local');
  assert.deepEqual(conversationsForPursuit(state).map((item) => item.conversationId), ['c-new', 'c-old']);

  state = reduceConversationState(state, { type: 'conversation-closed' });
  const detail = normalizeConversationDetail({
    conversation: sideChat.raw,
    events: [{ event_id: 60, conversation_id: 'side-1', kind: 'user.message', payload: { text: 'temporary' } }],
    pending_requests: [], cursor: 60,
  });
  assert(detail);
  state = reduceConversationState(state, { type: 'side-chat-restored', detail });
  assert.equal(state.currentConversationId, null);
  assert.equal(state.eventsByConversation['side-1'][0].eventId, '60');
  state = reduceConversationState(state, { type: 'side-chat-removed', conversationId: 'side-1' });
  assert(!state.conversations.some((item) => item.conversationId === 'side-1'));
  assert(!state.eventsByConversation['side-1']);
  assert.deepEqual(state.sessionSideChatIds, []);
});

test('a global side-chat close event removes stale state in another page', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const sideChat = normalizeWorkspace({ conversations: [{
    conversation_id: 'side-remote', pursuit_id: 'design', kind: 'side_chat', parent_conversation_id: 'c-new',
    host_id: 'local', project_id: 'active-root', title: 'Side chat', status: 'idle', updated_at: '2026-01-03',
  }] }).conversations[0];
  state = reduceConversationState(state, { type: 'side-chat-session', conversationIds: ['side-remote'] });
  state = reduceConversationState(state, { type: 'conversation-created', conversation: sideChat });
  state = reduceConversationState(state, { type: 'send-in-flight', conversationId: 'side-remote', active: true });
  const closed = normalizeEvent({
    event_id: 61, conversation_id: null, kind: 'side_chat.closed',
    payload: { conversation_id: 'side-remote' },
  });
  assert(closed);
  state = reduceConversationState(state, { type: 'event', event: closed });
  assert(!state.conversations.some((item) => item.conversationId === 'side-remote'));
  assert.deepEqual(state.sessionSideChatIds, []);
  assert.deepEqual(state.sendingConversationIds, []);
  assert.equal(state.cursor, '61');
});

test('side-chat restore keeps detail requests and replays only newer live events', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'side-chat-session', conversationIds: ['side-race'] });
  const live = normalizeEvent({
    event_id: 44, conversation_id: 'side-race', turn_id: 'turn-race', kind: 'server_request_resolved',
    payload: { request_key: 'older-request' },
  });
  assert(live);
  state = reduceConversationState(state, { type: 'event', event: live });
  const detail = normalizeConversationDetail({
    conversation: { conversation_id: 'side-race', pursuit_id: 'design', kind: 'side_chat', parent_conversation_id: 'c-new', status: 'waiting_input' },
    events: [],
    pending_requests: [
      { request_key: 'older-request', conversation_id: 'side-race', method: 'item/tool/requestUserInput', payload: {} },
      { request_key: 'current-request', conversation_id: 'side-race', method: 'item/tool/requestUserInput', payload: {} },
    ],
    cursor: 43,
  });
  assert(detail);
  state = reduceConversationState(state, { type: 'side-chat-restored', detail });
  assert(!state.pendingRequests.some((request) => request.key === 'older-request'));
  assert(state.pendingRequests.some((request) => request.key === 'current-request'));
  assert.equal(state.cursor, '44');
});

test('fresh workspace snapshots retain requests owned by restored side chats', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'side-chat-session', conversationIds: ['side-pending'] });
  state = {
    ...state,
    pendingRequests: [...state.pendingRequests, normalizeWorkspace({ pending_requests: [{
      request_key: 'side-input', conversation_id: 'side-pending', method: 'item/tool/requestUserInput', payload: {},
    }] }).pendingRequests[0]],
  };
  const newer = normalizeWorkspace({ ...workspacePayload, cursor: 50, pending_requests: [] });
  state = reduceConversationState(state, { type: 'workspace-loaded', snapshot: newer });
  assert(state.pendingRequests.some((request) => request.conversationId === 'side-pending' && request.key === 'side-input'));
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

test('a live final answer advances the unread cursor before any workspace refresh', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const final = normalizeEvent({
    event_id: 53,
    conversation_id: 'c-old',
    turn_id: 't-final',
    kind: 'item.completed',
    payload: { item: { id: 'answer-final', type: 'agentMessage', phase: 'final_answer', content: [{ type: 'text', text: 'Done' }] } },
  });
  assert(final);
  state = reduceConversationState(state, { type: 'event', event: final });
  const conversation = state.conversations.find((item) => item.conversationId === 'c-old');
  assert.equal(conversation?.lastFinalEventId, 53);
  assert.equal(conversation?.lastReadEventId, null);
});

test('conversation state events advance status and read cursors monotonically', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const first = normalizeEvent({
    event_id: 43,
    conversation_id: 'c-new',
    kind: 'conversation.state',
    payload: { conversation: {
      ...workspacePayload.conversations[1], status: 'starting', model: 'gpt-5.6-next',
      reasoning_effort: 'medium', last_final_event_id: 41, last_read_event_id: 40,
    } },
  });
  const staleRead = normalizeEvent({
    event_id: 44,
    conversation_id: 'c-new',
    kind: 'conversation.state',
    payload: { conversation: {
      ...workspacePayload.conversations[1], status: 'running', model: 'gpt-5.6-next',
      reasoning_effort: 'medium', last_final_event_id: 41, last_read_event_id: 39,
    } },
  });
  const added = normalizeEvent({
    event_id: 45,
    conversation_id: 'c-remote',
    kind: 'conversation.state',
    payload: { conversation: {
      conversation_id: 'c-remote', pursuit_id: 'design', host_id: 'gpu', project_id: 'gpu-root', updated_at: '2026-01-04',
      title: 'Started elsewhere', status: 'starting', model: 'gpt-5.6', reasoning_effort: 'high',
    } },
  });
  assert(first && staleRead && added);
  state = reduceConversationState(state, { type: 'event', event: first });
  state = reduceConversationState(state, { type: 'event', event: staleRead });
  assert.deepEqual(state.pursuitDefaults.design, {
    pursuitId: 'design', hostId: 'local', projectId: 'active-root', lastUsedAt: '2026-01-02',
  });
  state = reduceConversationState(state, { type: 'event', event: added });
  const conversation = state.conversations.find((item) => item.conversationId === 'c-new');
  assert.equal(conversation?.status, 'running');
  assert.equal(conversation?.lastReadEventId, 40);
  assert.equal(conversation?.model, 'gpt-5.6-next');
  assert.equal(conversation?.reasoningEffort, 'medium');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-remote')?.status, 'starting');
  assert.deepEqual(state.pursuitDefaults.design, {
    pursuitId: 'design', hostId: 'gpu', projectId: 'gpu-root', lastUsedAt: '2026-01-04',
  });

  const moved = normalizeEvent({
    event_id: 46,
    conversation_id: 'c-new',
    kind: 'conversation.state',
    payload: { conversation: {
      ...workspacePayload.conversations[1], pursuit_id: 'research', host_id: 'local', project_id: 'active-root',
      updated_at: '2026-01-05', status: 'idle',
    } },
  });
  assert(moved);
  state = reduceConversationState(state, { type: 'event', event: moved });
  assert.deepEqual(state.pursuitDefaults.research, {
    pursuitId: 'research', hostId: 'local', projectId: 'active-root', lastUsedAt: '2026-01-05',
  });
});

test('a bounded final event advances unread state from its safe event marker', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  const final = normalizeEvent({
    event_id: 54,
    conversation_id: 'c-old',
    turn_id: 't-large-final',
    kind: 'item.completed',
    marks_final: true,
    payload: { truncated: true, summary: '{"item":{"type":"agentMessage"…' },
  });
  assert(final);
  state = reduceConversationState(state, { type: 'event', event: final });
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-old')?.lastFinalEventId, 54);
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
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'conversation-archived', conversationId: 'c-new' });
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.archived, true);
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'idle');
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
  state = reduceConversationState(state, { type: 'send-in-flight', conversationId: 'c-new', active: true });
  assert.deepEqual(state.sendingConversationIds, ['c-old', 'c-new']);
  state = reduceConversationState(state, { type: 'send-in-flight', conversationId: 'c-old', active: false });
  assert.deepEqual(state.sendingConversationIds, ['c-new']);
  assert.equal(conversationCanSend(state, conversation), true);
  const running = { ...conversation, status: 'running' };
  assert.equal(conversationCanSend({ ...state, sendingConversationIds: [] }, running), false);
});

test('interrupt and reconnect requests stay scoped to each concurrent conversation', () => {
  let state = reduceConversationState(initialConversationState(), { type: 'workspace-loaded', snapshot: normalizeWorkspace(workspacePayload) });
  state = reduceConversationState(state, { type: 'connection', connection: 'open' });
  state = reduceConversationState(state, { type: 'interrupt-in-flight', conversationId: 'c-old', active: true });
  state = reduceConversationState(state, { type: 'interrupt-in-flight', conversationId: 'c-new', active: true });
  assert.deepEqual(state.interruptingConversationIds, ['c-old', 'c-new']);
  state = reduceConversationState(state, { type: 'interrupt-in-flight', conversationId: 'c-old', active: false });
  assert.deepEqual(state.interruptingConversationIds, ['c-new']);

  state = reduceConversationState(state, { type: 'reconcile-in-flight', conversationId: 'c-old', active: true });
  state = reduceConversationState(state, { type: 'reconcile-in-flight', conversationId: 'c-new', active: true });
  assert.deepEqual(state.reconcilingConversationIds, ['c-old', 'c-new']);
  assert.equal(conversationCanSend(state, state.conversations.find((item) => item.conversationId === 'c-old')!), false);
  state = reduceConversationState(state, { type: 'reconcile-in-flight', conversationId: 'c-old', active: false });
  assert.deepEqual(state.reconcilingConversationIds, ['c-new']);
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
  assert.equal(state.cursor, '42');
  assert.equal(state.conversations.find((item) => item.conversationId === 'c-new')?.status, 'completed');
  const replay = normalizeEvent(raced);
  assert(replay);
  const replayed = reduceConversationState(state, { type: 'event', event: replay });
  assert.equal(replayed.cursor, '43');
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

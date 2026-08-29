export type JsonRecord = Record<string, unknown>;

export interface ConversationHost {
  hostId: string;
  kind: string;
  displayName: string;
  sshAlias: string;
  status: string;
  raw: JsonRecord;
}

export interface ConversationProject {
  projectId: string;
  hostId: string;
  label: string;
  cwd: string;
  raw: JsonRecord;
}

export interface ConversationSummary {
  conversationId: string;
  pursuitId: string | null;
  hostId: string;
  projectId: string;
  model: string;
  reasoningEffort: string;
  title: string;
  status: string;
  createdAt: string;
  updatedAt: string;
  archived: boolean;
  raw: JsonRecord;
}

export interface ReasoningEffortOption {
  reasoningEffort: string;
  description: string;
}

export interface ConversationModel {
  id: string;
  displayName: string;
  defaultReasoningEffort: string;
  supportedReasoningEfforts: ReasoningEffortOption[];
  isDefault: boolean;
}

export interface ConversationModelCatalog {
  hostId: string;
  models: ConversationModel[];
  defaultModel: string;
  defaultReasoningEffort: string;
}

export interface ConversationEvent {
  eventId: string;
  conversationId: string;
  turnId: string | null;
  kind: string;
  payload: unknown;
  createdAt: string;
}

export interface PendingRequest {
  key: string;
  conversationId: string;
  kind: string;
  payload: unknown;
  createdAt: string;
  raw: JsonRecord;
}

export interface PursuitConversationDefault {
  pursuitId: string;
  hostId: string;
  projectId: string;
  lastUsedAt: string;
}

export interface WorkspaceSnapshot {
  rootKey: string;
  hosts: ConversationHost[];
  projects: ConversationProject[];
  conversations: ConversationSummary[];
  pendingRequests: PendingRequest[];
  pursuitDefaults: Record<string, PursuitConversationDefault>;
  cursor: string | null;
}

export interface PursuitConversationList {
  conversations: ConversationSummary[];
  default: PursuitConversationDefault | null;
}

export interface ConversationDetail {
  conversation: ConversationSummary;
  events: ConversationEvent[];
  pendingRequests: PendingRequest[];
  cursor: string | null;
}

export type ConnectionState = 'closed' | 'connecting' | 'open' | 'retrying';

export interface ConversationState extends WorkspaceSnapshot {
  selectedPursuitId: string | null;
  currentConversationId: string | null;
  eventsByConversation: Record<string, ConversationEvent[]>;
  loadingWorkspace: boolean;
  loadingPursuit: boolean;
  loadingConversation: boolean;
  creatingConversation: boolean;
  sendingConversationId: string | null;
  interruptingConversationId: string | null;
  reconcilingConversationId: string | null;
  creatingHost: boolean;
  creatingProject: boolean;
  respondingRequestKeys: string[];
  connection: ConnectionState;
  error: string | null;
}

export type ConversationAction =
  | { type: 'workspace-loading' }
  | { type: 'workspace-loaded'; snapshot: WorkspaceSnapshot }
  | { type: 'pursuit-selected'; pursuitId: string | null }
  | { type: 'pursuit-loading'; pursuitId: string }
  | { type: 'pursuit-loaded'; pursuitId: string; conversations: ConversationSummary[]; default: PursuitConversationDefault | null }
  | { type: 'conversation-loading'; conversationId: string }
  | { type: 'conversation-loaded'; detail: ConversationDetail }
  | { type: 'conversation-created'; conversation: ConversationSummary }
  | { type: 'conversation-updated'; conversation: ConversationSummary }
  | { type: 'conversation-settings-selected'; conversationId: string; model: string; reasoningEffort: string }
  | { type: 'conversation-closed' }
  | { type: 'conversation-archived'; conversationId: string }
  | { type: 'host-added'; host: ConversationHost }
  | { type: 'host-updated'; host: ConversationHost }
  | { type: 'project-added'; project: ConversationProject }
  | { type: 'event'; event: ConversationEvent }
  | { type: 'pending-resolved'; conversationId: string; key: string }
  | { type: 'create-in-flight'; active: boolean }
  | { type: 'send-in-flight'; conversationId: string; active: boolean }
  | { type: 'interrupt-in-flight'; conversationId: string; active: boolean }
  | { type: 'reconcile-in-flight'; conversationId: string; active: boolean }
  | { type: 'host-create-in-flight'; active: boolean }
  | { type: 'project-create-in-flight'; active: boolean }
  | { type: 'response-in-flight'; key: string; active: boolean }
  | { type: 'connection'; connection: ConnectionState }
  | { type: 'error'; message: string | null };

export function initialConversationState(): ConversationState {
  return {
    rootKey: '', hosts: [], projects: [], conversations: [], pendingRequests: [], pursuitDefaults: {}, cursor: null,
    selectedPursuitId: null, currentConversationId: null, eventsByConversation: {},
    loadingWorkspace: true, loadingPursuit: false, loadingConversation: false,
    creatingConversation: false, sendingConversationId: null, interruptingConversationId: null, reconcilingConversationId: null,
    creatingHost: false, creatingProject: false, respondingRequestKeys: [],
    connection: 'closed', error: null,
  };
}

function asRecord(value: unknown): JsonRecord {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as JsonRecord : {};
}

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : typeof value === 'number' ? String(value) : fallback;
}

function boolValue(value: unknown): boolean {
  return value === true || value === 1 || value === 'true';
}

function arrayValue(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function unwrapped(value: unknown): JsonRecord {
  const root = asRecord(value);
  return Object.keys(asRecord(root.data)).length ? asRecord(root.data) : root;
}

export function normalizeHost(value: unknown): ConversationHost | null {
  const raw = asRecord(value);
  const hostId = stringValue(raw.host_id ?? raw.id);
  if (!hostId) return null;
  const kind = stringValue(raw.kind, 'local');
  return {
    hostId,
    kind,
    displayName: stringValue(raw.display_name ?? raw.label ?? raw.name, hostId),
    sshAlias: stringValue(raw.ssh_alias ?? raw.alias),
    status: stringValue(raw.status ?? raw.state, raw.last_error ? 'offline' : raw.last_seen_at ? 'online' : ''),
    raw,
  };
}

export function normalizeProject(value: unknown): ConversationProject | null {
  const raw = asRecord(value);
  const projectId = stringValue(raw.project_id ?? raw.id);
  if (!projectId) return null;
  return {
    projectId,
    hostId: stringValue(raw.host_id),
    label: stringValue(raw.label ?? raw.name, projectId),
    cwd: stringValue(raw.cwd ?? raw.path),
    raw,
  };
}

export function normalizeConversation(value: unknown): ConversationSummary | null {
  const raw = asRecord(value);
  const conversationId = stringValue(raw.conversation_id ?? raw.id ?? raw.thread_id);
  if (!conversationId) return null;
  const pursuitId = stringValue(raw.pursuit_id) || null;
  const status = stringValue(raw.status ?? raw.state, 'idle');
  return {
    conversationId,
    pursuitId,
    hostId: stringValue(raw.host_id),
    projectId: stringValue(raw.project_id),
    model: stringValue(raw.model),
    reasoningEffort: stringValue(raw.reasoning_effort ?? raw.effort),
    title: stringValue(raw.thread_title ?? raw.title ?? raw.name ?? raw.label, 'Untitled conversation'),
    status,
    createdAt: stringValue(raw.created_at),
    updatedAt: stringValue(raw.updated_at ?? raw.last_activity_at ?? raw.created_at),
    archived: boolValue(raw.archived) || stringValue(raw.lifecycle).toLowerCase() === 'archived' || status.toLowerCase() === 'archived',
    raw,
  };
}

export function normalizeModelCatalog(value: unknown): ConversationModelCatalog {
  const data = unwrapped(value);
  const models = normalizedArray(data.models, (entry) => {
    const raw = asRecord(entry);
    const id = stringValue(raw.id ?? raw.model);
    if (!id) return null;
    const supportedReasoningEfforts = normalizedArray(
      raw.supported_reasoning_efforts ?? raw.supportedReasoningEfforts,
      (effortEntry) => {
        const effort = asRecord(effortEntry);
        const reasoningEffort = stringValue(effort.reasoning_effort ?? effort.reasoningEffort ?? effort.id ?? effort.value);
        return reasoningEffort ? {
          reasoningEffort,
          description: stringValue(effort.description),
        } : null;
      },
    );
    return {
      id,
      displayName: stringValue(raw.display_name ?? raw.displayName, id),
      defaultReasoningEffort: stringValue(raw.default_reasoning_effort ?? raw.defaultReasoningEffort),
      supportedReasoningEfforts,
      isDefault: boolValue(raw.is_default ?? raw.isDefault),
    };
  });
  return {
    hostId: stringValue(data.host_id ?? data.hostId),
    models,
    defaultModel: stringValue(data.default_model ?? data.defaultModel),
    defaultReasoningEffort: stringValue(data.default_reasoning_effort ?? data.defaultReasoningEffort),
  };
}

export function normalizeEvent(value: unknown, fallbackId = ''): ConversationEvent | null {
  const raw = asRecord(value);
  const conversationId = stringValue(raw.conversation_id ?? asRecord(raw.conversation).conversation_id);
  const kind = stringValue(raw.kind ?? raw.type, 'unknown');
  if (!conversationId && canonicalEventKind(kind) !== 'connection_disconnected') return null;
  const turnId = stringValue(raw.turn_id) || null;
  const createdAt = stringValue(raw.created_at ?? raw.timestamp);
  const eventId = stringValue(raw.event_id ?? raw.id) || fallbackId || `${conversationId}:${turnId ?? ''}:${kind}:${createdAt}`;
  return { eventId, conversationId, turnId, kind, payload: raw.payload ?? raw.data ?? {}, createdAt };
}

export function normalizePendingRequest(value: unknown): PendingRequest | null {
  const raw = asRecord(value);
  const nested = asRecord(raw.request);
  const key = stringValue(raw.key ?? raw.request_key ?? raw.server_request_id ?? raw.id ?? nested.id);
  const conversationId = stringValue(raw.conversation_id ?? nested.conversation_id);
  if (!key || !conversationId) return null;
  return {
    key,
    conversationId,
    kind: stringValue(raw.method ?? raw.kind ?? raw.request_type ?? raw.type ?? nested.method ?? nested.kind ?? nested.type, 'unknown'),
    payload: raw.payload ?? raw.data ?? raw.request ?? {},
    createdAt: stringValue(raw.created_at ?? nested.created_at),
    raw,
  };
}

export function normalizePursuitDefault(value: unknown, fallbackPursuitId = ''): PursuitConversationDefault | null {
  const raw = asRecord(value);
  const pursuitId = stringValue(raw.pursuit_id, fallbackPursuitId);
  const hostId = stringValue(raw.host_id);
  const projectId = stringValue(raw.project_id);
  if (!pursuitId || !hostId || !projectId) return null;
  return { pursuitId, hostId, projectId, lastUsedAt: stringValue(raw.last_used_at) };
}

function normalizePursuitDefaults(value: unknown): Record<string, PursuitConversationDefault> {
  const defaults: Record<string, PursuitConversationDefault> = {};
  if (Array.isArray(value)) {
    for (const entry of value) {
      const normalized = normalizePursuitDefault(entry);
      if (normalized) defaults[normalized.pursuitId] = normalized;
    }
    return defaults;
  }
  for (const [pursuitId, entry] of Object.entries(asRecord(value))) {
    const normalized = normalizePursuitDefault(entry, pursuitId);
    if (normalized) defaults[normalized.pursuitId] = normalized;
  }
  return defaults;
}

function normalizedArray<T>(value: unknown, convert: (entry: unknown, index: number) => T | null): T[] {
  return arrayValue(value).map(convert).filter((entry): entry is T => entry !== null);
}

export function normalizeWorkspace(value: unknown): WorkspaceSnapshot {
  const data = unwrapped(value);
  const pendingRequests = normalizedArray(data.pending_requests, normalizePendingRequest)
    .filter((request) => !request.raw.state || request.raw.state === 'pending');
  return {
    rootKey: stringValue(data.root_key),
    hosts: normalizedArray(data.hosts, normalizeHost),
    projects: normalizedArray(data.projects, normalizeProject),
    conversations: normalizedArray(data.conversations, normalizeConversation),
    pendingRequests,
    pursuitDefaults: normalizePursuitDefaults(data.pursuit_defaults),
    cursor: stringValue(data.cursor) || null,
  };
}

export function normalizeConversationList(value: unknown): ConversationSummary[] {
  const data = unwrapped(value);
  return normalizedArray(data.conversations ?? value, normalizeConversation);
}

export function normalizePursuitConversationList(value: unknown): PursuitConversationList {
  const data = unwrapped(value);
  return {
    conversations: normalizedArray(data.conversations ?? value, normalizeConversation),
    default: normalizePursuitDefault(data.default),
  };
}

export function normalizeConversationDetail(value: unknown): ConversationDetail | null {
  const data = unwrapped(value);
  const conversation = normalizeConversation(data.conversation ?? data);
  if (!conversation) return null;
  return {
    conversation,
    events: normalizedArray(data.events, (entry, index) => normalizeEvent(entry, `${conversation.conversationId}:loaded:${index}`)),
    pendingRequests: normalizedArray(data.pending_requests, normalizePendingRequest)
      .filter((request) => !request.raw.state || request.raw.state === 'pending'),
    cursor: stringValue(data.cursor) || null,
  };
}

function upsert<T>(items: T[], next: T, id: (item: T) => string): T[] {
  const index = items.findIndex((item) => id(item) === id(next));
  if (index < 0) return [...items, next];
  const copy = items.slice();
  copy[index] = next;
  return copy;
}

function canonicalEventKind(value: string): string {
  return value.toLowerCase().replace(/[.\s/-]+/g, '_');
}

function cursorNumber(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) ? parsed : null;
}

function cursorNeedsMerge(incoming: string | null, current: string | null): boolean {
  const incomingNumber = cursorNumber(incoming);
  const currentNumber = cursorNumber(current);
  return currentNumber !== null && (incomingNumber === null || incomingNumber <= currentNumber);
}

function newestCursor(left: string | null, right: string | null): string | null {
  const leftNumber = cursorNumber(left);
  const rightNumber = cursorNumber(right);
  if (leftNumber === null) return rightNumber === null ? left ?? right : right;
  if (rightNumber === null) return left;
  return leftNumber >= rightNumber ? left : right;
}

function summaryTimestamp(value: ConversationSummary): string {
  return value.updatedAt || value.createdAt;
}

function newerSummary(current: ConversationSummary, incoming: ConversationSummary): ConversationSummary {
  const currentTime = summaryTimestamp(current);
  const incomingTime = summaryTimestamp(incoming);
  if (currentTime && incomingTime && currentTime > incomingTime) return current;
  return incoming;
}

function mergeSummaries(current: ConversationSummary[], incoming: ConversationSummary[], preserveCurrent: boolean): ConversationSummary[] {
  const merged = new Map(current.map((item) => [item.conversationId, item]));
  for (const item of incoming) {
    const previous = merged.get(item.conversationId);
    merged.set(item.conversationId, previous && preserveCurrent ? newerSummary(previous, item) : item);
  }
  return [...merged.values()];
}

function mergeEvents(current: ConversationEvent[], incoming: ConversationEvent[]): ConversationEvent[] {
  const merged = new Map<string, ConversationEvent>();
  for (const event of [...current, ...incoming]) merged.set(event.eventId, event);
  return [...merged.values()].sort((left, right) => {
    const leftNumber = cursorNumber(left.eventId);
    const rightNumber = cursorNumber(right.eventId);
    if (leftNumber !== null && rightNumber !== null) return leftNumber - rightNumber;
    return (left.createdAt || left.eventId).localeCompare(right.createdAt || right.eventId);
  });
}

function eventStatus(event: ConversationEvent): string | null {
  const kind = canonicalEventKind(event.kind);
  const payload = asRecord(event.payload);
  const explicit = stringValue(payload.status ?? payload.state);
  if (explicit) return explicit;
  const threadStatus = asRecord(payload.status);
  if (kind.includes('thread_status') && stringValue(threadStatus.type)) {
    if (threadStatus.type === 'active') {
      if (threadStatus.waitingOnApproval === true) return 'waiting_approval';
      if (threadStatus.waitingOnUserInput === true) return 'waiting_input';
      return 'running';
    }
    if (threadStatus.type === 'idle') return 'idle';
    if (threadStatus.type === 'systemError') return 'failed';
    return 'unknown';
  }
  const turnStatus = stringValue(asRecord(payload.turn).status);
  if (turnStatus) {
    if (turnStatus === 'inProgress') return 'running';
    if (['completed', 'failed', 'interrupted'].includes(turnStatus)) return turnStatus;
  }
  if (kind.includes('turn_started') || kind.includes('turn_start')) return 'running';
  if (kind.includes('turn_completed') || kind.includes('turn_complete')) return 'completed';
  if (kind.includes('turn_failed')) return 'failed';
  if (kind === 'protocol_error') {
    if (payload.willRetry === false && event.turnId) return 'failed';
    if (payload.operation) return 'unknown';
  }
  if (kind.includes('interrupt')) return 'interrupted';
  return null;
}

export function reduceConversationState(state: ConversationState, action: ConversationAction): ConversationState {
  switch (action.type) {
    case 'workspace-loading':
      return { ...state, loadingWorkspace: true, error: null };
    case 'workspace-loaded':
      if (cursorNeedsMerge(action.snapshot.cursor, state.cursor)) {
        return {
          ...state,
          hosts: mergeById(action.snapshot.hosts, state.hosts, (item) => item.hostId),
          projects: mergeById(action.snapshot.projects, state.projects, (item) => item.projectId),
          conversations: mergeSummaries(action.snapshot.conversations, state.conversations, true),
          pursuitDefaults: { ...action.snapshot.pursuitDefaults, ...state.pursuitDefaults },
          cursor: newestCursor(state.cursor, action.snapshot.cursor),
          loadingWorkspace: false,
          error: null,
        };
      }
      return { ...state, ...action.snapshot, eventsByConversation: state.eventsByConversation, loadingWorkspace: false, error: null };
    case 'pursuit-selected':
      return {
        ...state,
        selectedPursuitId: action.pursuitId,
        currentConversationId: null,
        loadingPursuit: !!action.pursuitId,
        loadingConversation: false,
        error: null,
      };
    case 'pursuit-loading':
      return state.selectedPursuitId === action.pursuitId ? { ...state, loadingPursuit: true, error: null } : state;
    case 'pursuit-loaded': {
      if (state.selectedPursuitId !== action.pursuitId) return state;
      const other = state.conversations.filter((item) => item.pursuitId !== action.pursuitId);
      const current = state.conversations.filter((item) => item.pursuitId === action.pursuitId);
      const pursuitDefaults = { ...state.pursuitDefaults };
      if (action.default) pursuitDefaults[action.pursuitId] = action.default;
      else delete pursuitDefaults[action.pursuitId];
      return { ...state, conversations: [...other, ...mergeSummaries(current, action.conversations, true)], pursuitDefaults, loadingPursuit: false, error: null };
    }
    case 'conversation-loading':
      return { ...state, currentConversationId: action.conversationId, loadingConversation: true, error: null };
    case 'conversation-loaded': {
      const conversation = action.detail.conversation;
      if (state.currentConversationId !== conversation.conversationId) return state;
      const stale = cursorNeedsMerge(action.detail.cursor, state.cursor);
      const withoutPending = state.pendingRequests.filter((item) => item.conversationId !== conversation.conversationId);
      const currentSummary = state.conversations.find((item) => item.conversationId === conversation.conversationId);
      const summary = stale && currentSummary ? newerSummary(conversation, currentSummary) : conversation;
      const pendingRequests = stale
        ? state.pendingRequests
        : [...withoutPending, ...action.detail.pendingRequests];
      let loaded: ConversationState = {
        ...state,
        conversations: upsert(state.conversations, summary, (item) => item.conversationId),
        eventsByConversation: {
          ...state.eventsByConversation,
          [conversation.conversationId]: mergeEvents(state.eventsByConversation[conversation.conversationId] ?? [], action.detail.events),
        },
        pendingRequests,
        cursor: newestCursor(state.cursor, action.detail.cursor),
        loadingConversation: false,
        error: null,
      };
      const loadedCursor = cursorNumber(loaded.cursor);
      for (const event of action.detail.events) {
        const eventCursor = cursorNumber(event.eventId);
        if (eventCursor !== null && (loadedCursor === null || eventCursor > loadedCursor)) {
          loaded = reduceConversationState(loaded, { type: 'event', event });
        }
      }
      return loaded;
    }
    case 'conversation-created':
      return {
        ...state,
        conversations: upsert(state.conversations, action.conversation, (item) => item.conversationId),
        pursuitDefaults: action.conversation.pursuitId ? {
          ...state.pursuitDefaults,
          [action.conversation.pursuitId]: {
            pursuitId: action.conversation.pursuitId,
            hostId: action.conversation.hostId,
            projectId: action.conversation.projectId,
            lastUsedAt: action.conversation.updatedAt || action.conversation.createdAt,
          },
        } : state.pursuitDefaults,
        currentConversationId: action.conversation.conversationId,
        loadingConversation: true,
        error: null,
      };
    case 'conversation-updated': {
      const current = state.conversations.find((item) => item.conversationId === action.conversation.conversationId);
      const conversation = current ? newerSummary(current, action.conversation) : action.conversation;
      return { ...state, conversations: upsert(state.conversations, conversation, (item) => item.conversationId) };
    }
    case 'conversation-settings-selected':
      return {
        ...state,
        conversations: state.conversations.map((item) => item.conversationId === action.conversationId
          ? { ...item, model: action.model, reasoningEffort: action.reasoningEffort }
          : item),
      };
    case 'conversation-closed':
      return { ...state, currentConversationId: null, loadingConversation: false, error: null };
    case 'conversation-archived':
      return {
        ...state,
        conversations: state.conversations.map((item) => item.conversationId === action.conversationId ? { ...item, archived: true, status: 'archived' } : item),
        currentConversationId: state.currentConversationId === action.conversationId ? null : state.currentConversationId,
        pendingRequests: state.pendingRequests.filter((item) => item.conversationId !== action.conversationId),
      };
    case 'host-added':
    case 'host-updated':
      return { ...state, hosts: upsert(state.hosts, action.host, (item) => item.hostId), error: null };
    case 'project-added':
      return { ...state, projects: upsert(state.projects, action.project, (item) => item.projectId), error: null };
    case 'event': {
      const eventKind = canonicalEventKind(action.event.kind);
      const payload = asRecord(action.event.payload);
      if (eventKind === 'connection_disconnected') {
        const eventCursor = cursorNumber(action.event.eventId);
        const currentCursor = cursorNumber(state.cursor);
        if (eventCursor !== null && currentCursor !== null && eventCursor <= currentCursor) {
          return { ...state, cursor: newestCursor(state.cursor, action.event.eventId) };
        }
        const affected = new Set(arrayValue(payload.conversation_ids).map((value) => stringValue(value)).filter(Boolean));
        const eventsByConversation = { ...state.eventsByConversation };
        for (const conversationId of affected) {
          const projected = { ...action.event, conversationId };
          const previous = eventsByConversation[conversationId] ?? [];
          if (!previous.some((event) => event.eventId === projected.eventId)) eventsByConversation[conversationId] = [...previous, projected];
        }
        const hostId = stringValue(payload.host_id);
        return {
          ...state,
          eventsByConversation,
          conversations: state.conversations.map((item) => affected.has(item.conversationId) ? { ...item, status: 'unknown', updatedAt: action.event.createdAt || item.updatedAt } : item),
          hosts: state.hosts.map((host) => host.hostId === hostId ? { ...host, status: 'offline' } : host),
          cursor: newestCursor(state.cursor, action.event.eventId),
        };
      }
      const previous = state.eventsByConversation[action.event.conversationId] ?? [];
      const duplicate = previous.some((event) => event.eventId === action.event.eventId);
      const eventCursor = cursorNumber(action.event.eventId);
      const currentCursor = cursorNumber(state.cursor);
      const alreadyCovered = eventCursor !== null && currentCursor !== null && eventCursor <= currentCursor;
      if (duplicate && alreadyCovered) return { ...state, cursor: newestCursor(state.cursor, action.event.eventId) };
      const eventsByConversation = duplicate
        ? state.eventsByConversation
        : { ...state.eventsByConversation, [action.event.conversationId]: [...previous, action.event] };
      if (alreadyCovered) return { ...state, eventsByConversation, cursor: newestCursor(state.cursor, action.event.eventId) };
      let status = eventStatus(action.event);
      let pendingRequests = state.pendingRequests;
      if (eventKind === 'server_response_failed') {
        const key = stringValue(payload.request_key ?? payload.key);
        if (key) pendingRequests = pendingRequests.filter((request) => request.key !== key);
        status = 'unknown';
      }
      if (eventKind.includes('server_request')) {
        const rawRequest = asRecord(payload.request ?? payload);
        const key = stringValue(rawRequest.request_key ?? rawRequest.key ?? payload.request_key ?? payload.key);
        if (eventKind.includes('resolved') || eventKind.includes('stale')) {
          const pending = key ? pendingRequests.find((request) => request.key === key) : undefined;
          if (eventKind.includes('stale')) status = 'unknown';
          else if (pending) status = stringValue(asRecord(pending.payload).turnId ?? asRecord(pending.payload).turn_id) ? 'running' : 'idle';
          if (key) pendingRequests = pendingRequests.filter((request) => request.key !== key);
        } else if (eventKind === 'server_request' || eventKind.includes('pending') || eventKind.includes('requested')) {
          const request = normalizePendingRequest({ ...rawRequest, conversation_id: rawRequest.conversation_id ?? action.event.conversationId });
          if (request) {
            pendingRequests = upsert(pendingRequests, request, (item) => item.key);
            status = ['item/tool/requestUserInput', 'mcpServer/elicitation/request', 'item/tool/call'].includes(request.kind)
              ? 'waiting_input'
              : 'waiting_approval';
          }
        }
      }
      const conversations = state.conversations.map((item) => item.conversationId === action.event.conversationId ? {
        ...item,
        status: status ?? item.status,
        title: stringValue(payload.threadName ?? payload.thread_name ?? payload.title, item.title),
        archived: item.archived || eventKind === 'thread_archived',
        updatedAt: action.event.createdAt || item.updatedAt,
      } : item);
      return { ...state, eventsByConversation, conversations, pendingRequests, cursor: newestCursor(state.cursor, action.event.eventId) };
    }
    case 'pending-resolved':
      return { ...state, pendingRequests: state.pendingRequests.filter((item) => item.conversationId !== action.conversationId || item.key !== action.key) };
    case 'create-in-flight':
      return { ...state, creatingConversation: action.active };
    case 'send-in-flight':
      return {
        ...state,
        sendingConversationId: action.active ? action.conversationId : state.sendingConversationId === action.conversationId ? null : state.sendingConversationId,
      };
    case 'interrupt-in-flight':
      return {
        ...state,
        interruptingConversationId: action.active ? action.conversationId : state.interruptingConversationId === action.conversationId ? null : state.interruptingConversationId,
      };
    case 'reconcile-in-flight':
      return {
        ...state,
        reconcilingConversationId: action.active ? action.conversationId : state.reconcilingConversationId === action.conversationId ? null : state.reconcilingConversationId,
      };
    case 'response-in-flight':
      return {
        ...state,
        respondingRequestKeys: action.active
          ? state.respondingRequestKeys.includes(action.key) ? state.respondingRequestKeys : [...state.respondingRequestKeys, action.key]
          : state.respondingRequestKeys.filter((key) => key !== action.key),
      };
    case 'host-create-in-flight':
      return { ...state, creatingHost: action.active };
    case 'project-create-in-flight':
      return { ...state, creatingProject: action.active };
    case 'connection':
      return { ...state, connection: action.connection };
    case 'error':
      return { ...state, error: action.message, loadingWorkspace: false, loadingPursuit: false, loadingConversation: false };
  }
}

function mergeById<T>(base: T[], overlay: T[], id: (item: T) => string): T[] {
  const merged = new Map(base.map((item) => [id(item), item]));
  for (const item of overlay) merged.set(id(item), item);
  return [...merged.values()];
}

export function conversationsForPursuit(state: ConversationState): ConversationSummary[] {
  if (!state.selectedPursuitId) return [];
  return state.conversations
    .filter((conversation) => conversation.pursuitId === state.selectedPursuitId && !conversation.archived)
    .sort((left, right) => (right.updatedAt || right.createdAt).localeCompare(left.updatedAt || left.createdAt));
}

export function currentConversation(state: ConversationState): ConversationSummary | null {
  return state.conversations.find((item) => item.conversationId === state.currentConversationId) ?? null;
}

export function conversationCanSend(state: ConversationState, conversation: ConversationSummary): boolean {
  const status = conversation.status.toLowerCase().replace(/[\s-]+/g, '_');
  const providerBusy = ['starting', 'running', 'in_progress', 'waiting_approval', 'waiting_input'].includes(status);
  return state.connection === 'open'
    && !state.loadingConversation
    && !conversation.archived
    && state.sendingConversationId !== conversation.conversationId
    && state.reconcilingConversationId !== conversation.conversationId
    && !providerBusy
    && status !== 'unknown';
}

export function recordValue(value: unknown): JsonRecord { return asRecord(value); }
export function textValue(value: unknown, fallback = ''): string { return stringValue(value, fallback); }

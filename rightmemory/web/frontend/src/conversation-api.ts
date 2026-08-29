import {
  normalizeConversation,
  normalizeConversationDetail,
  normalizePursuitConversationList,
  normalizeEvent,
  normalizeHost,
  normalizeProject,
  normalizeWorkspace,
  type ConversationDetail,
  type ConversationEvent,
  type ConversationHost,
  type ConversationProject,
  type ConversationSummary,
  type PursuitConversationList,
  type WorkspaceSnapshot,
} from './conversation-state.ts';

export type FetchJson = (path: string, options?: RequestInit) => Promise<{ data: unknown }>;

export interface EventStream {
  close(): void;
  addEventListener(type: string, listener: EventListener): void;
  onopen: ((event: Event) => void) | null;
  onerror: ((event: Event) => void) | null;
  onmessage: ((event: MessageEvent<string>) => void) | null;
}

export type EventSourceFactory = (url: string) => EventStream;

function responseData(response: { data: unknown }): unknown { return response.data; }

function encoded(value: string): string { return encodeURIComponent(value); }

function body(value: unknown): RequestInit {
  return { method: 'POST', body: JSON.stringify(value) };
}

function required<T>(value: T | null, message: string): T {
  if (!value) throw new Error(message);
  return value;
}

export class ConversationApi {
  constructor(
    private fetchJson: FetchJson,
    private eventSourceFactory: EventSourceFactory = (url) => new EventSource(url),
  ) {}

  async workspace(): Promise<WorkspaceSnapshot> {
    return normalizeWorkspace(responseData(await this.fetchJson('/api/conversation-workspace')));
  }

  async pursuitConversations(pursuitId: string): Promise<PursuitConversationList> {
    const response = await this.fetchJson(`/api/pursuit-conversations?pursuit_id=${encoded(pursuitId)}`);
    return normalizePursuitConversationList(responseData(response));
  }

  async createConversation(pursuitId: string, hostId: string, projectId: string): Promise<ConversationSummary> {
    const response = await this.fetchJson('/api/pursuit-conversations', body({ pursuit_id: pursuitId, host_id: hostId, project_id: projectId }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation ?? data), 'The server did not return the new conversation.');
  }

  async conversation(conversationId: string): Promise<ConversationDetail> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}`);
    return required(normalizeConversationDetail(responseData(response)), 'The server returned an invalid conversation.');
  }

  async sendMessage(conversationId: string, text: string): Promise<ConversationSummary | null> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/messages`, body({ text }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return normalizeConversation(record.conversation);
  }

  async interrupt(conversationId: string): Promise<ConversationSummary | null> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/interrupt`, body({}));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return normalizeConversation(record.conversation);
  }

  async reconcile(conversationId: string): Promise<ConversationSummary> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/reconcile`, body({}));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation), 'The server did not return the reconciled conversation.');
  }

  async archive(conversationId: string): Promise<void> {
    await this.fetchJson(`/api/conversations/${encoded(conversationId)}/archive`, body({}));
  }

  async move(conversationId: string, pursuitId: string): Promise<void> {
    await this.fetchJson(`/api/conversations/${encoded(conversationId)}/move`, body({ pursuit_id: pursuitId }));
  }

  async respond(conversationId: string, key: string, response: { decision?: string; response?: unknown }): Promise<ConversationSummary | null> {
    const result = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/server-requests/${encoded(key)}/respond`, body(response));
    const data = responseData(result);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return normalizeConversation(record.conversation);
  }

  async createHost(displayName: string, sshAlias: string): Promise<ConversationHost> {
    const response = await this.fetchJson('/api/conversation-hosts', body({ kind: 'ssh', display_name: displayName, ssh_alias: sshAlias }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeHost(record.host ?? data), 'The server did not return the new host.');
  }

  async probeHost(hostId: string): Promise<ConversationHost | null> {
    const response = await this.fetchJson(`/api/conversation-hosts/${encoded(hostId)}/probe`, body({}));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return normalizeHost(record.host ?? data);
  }

  async createProject(hostId: string, label: string, cwd: string): Promise<ConversationProject> {
    const response = await this.fetchJson('/api/conversation-projects', body({ host_id: hostId, label, cwd }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeProject(record.project ?? data), 'The server did not return the new project.');
  }

  events(afterEventId: string | null, handlers: {
    snapshot(snapshot: WorkspaceSnapshot): void;
    event(event: ConversationEvent): void;
    open(): void;
    error(): void;
  }): EventStream {
    const source = this.eventSourceFactory(`/api/conversation-events?after_event_id=${encoded(afterEventId ?? '0')}`);
    const parse = (message: MessageEvent<string>, kind?: 'snapshot' | 'conversation') => {
      try {
        const value: unknown = JSON.parse(message.data);
        if (kind === 'snapshot') { handlers.snapshot(normalizeWorkspace(value)); return; }
        const record = value && typeof value === 'object' ? value as Record<string, unknown> : {};
        if (kind !== 'conversation' && (record.type === 'snapshot' || record.kind === 'snapshot')) {
          handlers.snapshot(normalizeWorkspace(record.snapshot ?? record.data ?? value));
          return;
        }
        const event = normalizeEvent(record.event ?? record.data ?? value);
        if (event) handlers.event(event);
      } catch {
        // A malformed stream record is isolated; the next valid record remains usable.
      }
    };
    source.addEventListener('snapshot', ((event: MessageEvent<string>) => parse(event, 'snapshot')) as EventListener);
    source.addEventListener('conversation', ((event: MessageEvent<string>) => parse(event, 'conversation')) as EventListener);
    source.onmessage = (event) => parse(event);
    source.onopen = () => handlers.open();
    source.onerror = () => handlers.error();
    return source;
  }
}

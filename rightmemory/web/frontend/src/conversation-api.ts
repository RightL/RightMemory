import {
  normalizeConversation,
  normalizeConversationDetail,
  normalizeConversationHistoryPage,
  normalizeModelCatalog,
  normalizePursuitConversationList,
  normalizeEvent,
  normalizeAttachment,
  normalizeHost,
  normalizeProject,
  normalizeWorkspace,
  type ConversationDetail,
  type ConversationAttachment,
  type ConversationEvent,
  type ConversationHost,
  type ConversationHistoryPage,
  type ConversationModelCatalog,
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

function attachmentBody(file: File, attachmentId: string, attachmentKind?: 'file'): RequestInit {
  const headers: Record<string, string> = {
    'content-type': file.type || 'application/octet-stream',
    'x-filename': encodeURIComponent(file.name || 'attachment'),
    'x-attachment-id': attachmentId,
  };
  if (attachmentKind === 'file') headers['x-attachment-kind'] = 'file';
  return {
    method: 'POST',
    body: file,
    headers,
  };
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

  async modelCatalog(hostId: string): Promise<ConversationModelCatalog> {
    const response = await this.fetchJson(`/api/conversation-models?host_id=${encoded(hostId)}`);
    return normalizeModelCatalog(responseData(response));
  }

  async createConversation(
    pursuitId: string,
    hostId: string,
    projectId: string,
    model: string,
    reasoningEffort: string,
  ): Promise<ConversationSummary> {
    const response = await this.fetchJson('/api/pursuit-conversations', body({
      pursuit_id: pursuitId,
      host_id: hostId,
      project_id: projectId,
      model,
      reasoning_effort: reasoningEffort,
    }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation ?? data), 'The server did not return the new conversation.');
  }

  async conversation(conversationId: string): Promise<ConversationDetail> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}`);
    return required(normalizeConversationDetail(responseData(response)), 'The server returned an invalid conversation.');
  }

  async earlierConversation(
    conversationId: string,
    beforeEventId: string,
  ): Promise<ConversationHistoryPage> {
    const response = await this.fetchJson(
      `/api/conversations/${encoded(conversationId)}/history?before_event_id=${encoded(beforeEventId)}`,
    );
    return normalizeConversationHistoryPage(responseData(response), conversationId);
  }

  async createSideChat(parentConversationId: string): Promise<ConversationSummary> {
    const response = await this.fetchJson(`/api/conversations/${encoded(parentConversationId)}/side-chats`, body({}));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation ?? data), 'The server did not return the new side chat.');
  }

  async deleteSideChat(sideChatId: string): Promise<void> {
    await this.fetchJson(`/api/side-chats/${encoded(sideChatId)}`, { method: 'DELETE' });
  }

  async acknowledgeRead(conversationId: string, eventId: number): Promise<ConversationSummary> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/read`, body({ event_id: eventId }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation ?? data), 'The server did not return the read conversation.');
  }

  async sendMessage(conversationId: string, text: string, attachmentIds: string[] = []): Promise<ConversationSummary | null> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/messages`, body({ text, attachment_ids: attachmentIds }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return normalizeConversation(record.conversation);
  }

  async uploadAttachment(
    conversationId: string,
    file: File,
    attachmentId: string,
    attachmentKind?: 'file',
  ): Promise<ConversationAttachment> {
    const response = await this.fetchJson(
      `/api/conversations/${encoded(conversationId)}/attachments`,
      attachmentBody(file, attachmentId, attachmentKind),
    );
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(
      normalizeAttachment(record.attachment ?? data, conversationId),
      'The server did not return the uploaded attachment.',
    );
  }

  async deleteAttachment(conversationId: string, attachmentId: string): Promise<void> {
    await this.fetchJson(
      `/api/conversations/${encoded(conversationId)}/attachments/${encoded(attachmentId)}`,
      { method: 'DELETE' },
    );
  }

  async updateSettings(conversationId: string, model: string, reasoningEffort: string): Promise<ConversationSummary> {
    const response = await this.fetchJson(`/api/conversations/${encoded(conversationId)}/settings`, body({ model, reasoning_effort: reasoningEffort }));
    const data = responseData(response);
    const record = data && typeof data === 'object' ? data as Record<string, unknown> : {};
    return required(normalizeConversation(record.conversation ?? data), 'The server did not return the updated conversation settings.');
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

  async releaseView(viewId: string, pageId: string): Promise<void> {
    await this.fetchJson('/api/conversation-session/release', {
      ...body({ view_id: viewId, page_id: pageId }),
      keepalive: true,
    });
  }

  events(afterEventId: string | null, viewId: string, pageId: string, handlers: {
    snapshot(snapshot: WorkspaceSnapshot): void;
    event(event: ConversationEvent): void;
    open(): void;
    error(): void;
  }): EventStream {
    const source = this.eventSourceFactory(
      `/api/conversation-events?after_event_id=${encoded(afterEventId ?? '0')}&view_id=${encoded(viewId)}&page_id=${encoded(pageId)}`,
    );
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

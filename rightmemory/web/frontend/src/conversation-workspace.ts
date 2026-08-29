import { ConversationApi, type EventSourceFactory, type EventStream, type FetchJson } from './conversation-api.ts';
import { ConversationRenderer, type ConversationRendererActions } from './conversation-renderer.ts';
import {
  conversationCanSend,
  currentConversation,
  initialConversationState,
  reduceConversationState,
  type ConversationAction,
  type ConversationState,
  type PendingRequest,
  type WorkspaceSnapshot,
} from './conversation-state.ts';
import { DRAFT_PREFIX } from './tree.ts';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stablePursuitId(value: string | null): string | null {
  return value && !value.startsWith(DRAFT_PREFIX) ? value : null;
}

export class ConversationWorkspace implements ConversationRendererActions {
  private state: ConversationState = initialConversationState();
  private renderer: ConversationRenderer;
  private api: ConversationApi;
  private stream: EventStream | null = null;
  private active = true;
  private destroyed = false;
  private pursuitLoad = 0;
  private conversationLoad = 0;
  private collapsed = false;

  constructor(
    private workspaceHost: HTMLElement,
    paneHost: HTMLElement,
    fetchJson: FetchJson,
    private rootKey: string,
    private reloadPage: () => void = () => location.reload(),
    eventSourceFactory?: EventSourceFactory,
  ) {
    this.api = new ConversationApi(fetchJson, eventSourceFactory);
    this.renderer = new ConversationRenderer(paneHost, rootKey, this);
    try { this.collapsed = localStorage.getItem(this.collapseKey()) === 'true'; } catch { /* Ignore unavailable storage. */ }
    this.renderer.setCollapsed(this.collapsed);
    this.workspaceHost.classList.toggle('pw-conversations-collapsed', this.collapsed);
    this.renderer.render(this.state);
  }

  private collapseKey(): string { return `rightmemory:conversation-pane:${encodeURIComponent(this.rootKey)}:collapsed`; }

  private dispatch(action: ConversationAction): void {
    this.state = reduceConversationState(this.state, action);
    this.renderer.render(this.state);
  }

  private acceptSnapshot(snapshot: WorkspaceSnapshot): boolean {
    if (!snapshot.rootKey || snapshot.rootKey === this.rootKey) return true;
    this.destroy();
    this.reloadPage();
    return false;
  }

  async start(): Promise<void> {
    await this.loadWorkspace();
    if (this.active) this.connect();
  }

  private async loadWorkspace(): Promise<void> {
    this.dispatch({ type: 'workspace-loading' });
    try {
      const snapshot = await this.api.workspace();
      if (this.destroyed) return;
      if (!this.acceptSnapshot(snapshot)) return;
      this.dispatch({ type: 'workspace-loaded', snapshot });
    } catch (error) {
      if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) });
    }
  }

  selectPursuit(id: string | null): void {
    const pursuitId = stablePursuitId(id);
    if (pursuitId === this.state.selectedPursuitId) return;
    this.dispatch({ type: 'pursuit-selected', pursuitId });
    if (pursuitId) void this.loadPursuit(pursuitId);
  }

  private async loadPursuit(pursuitId: string): Promise<void> {
    const generation = ++this.pursuitLoad;
    this.dispatch({ type: 'pursuit-loading', pursuitId });
    try {
      const result = await this.api.pursuitConversations(pursuitId);
      if (!this.destroyed && generation === this.pursuitLoad) this.dispatch({ type: 'pursuit-loaded', pursuitId, conversations: result.conversations, default: result.default });
    } catch (error) {
      if (!this.destroyed && generation === this.pursuitLoad && this.state.selectedPursuitId === pursuitId) this.dispatch({ type: 'error', message: errorMessage(error) });
    }
  }

  openConversation(conversationId: string): void { void this.loadConversation(conversationId); }

  private async loadConversation(conversationId: string): Promise<void> {
    const generation = ++this.conversationLoad;
    this.dispatch({ type: 'conversation-loading', conversationId });
    try {
      const detail = await this.api.conversation(conversationId);
      if (!this.destroyed && generation === this.conversationLoad) this.dispatch({ type: 'conversation-loaded', detail });
    } catch (error) {
      if (!this.destroyed && generation === this.conversationLoad && this.state.currentConversationId === conversationId) this.dispatch({ type: 'error', message: errorMessage(error) });
    }
  }

  closeConversation(): void {
    this.conversationLoad++;
    this.dispatch({ type: 'conversation-closed' });
  }

  async createConversation(hostId: string, projectId: string): Promise<void> {
    const pursuitId = this.state.selectedPursuitId;
    if (!pursuitId || this.state.creatingConversation) return;
    this.dispatch({ type: 'create-in-flight', active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const conversation = await this.api.createConversation(pursuitId, hostId, projectId);
      if (this.destroyed) return;
      this.dispatch({ type: 'conversation-created', conversation });
      await this.loadConversation(conversation.conversationId);
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'create-in-flight', active: false }); }
  }

  async sendMessage(text: string): Promise<void> {
    const conversationId = this.state.currentConversationId;
    const conversation = currentConversation(this.state);
    if (!conversationId || !conversation || !conversationCanSend(this.state, conversation)) return;
    this.dispatch({ type: 'send-in-flight', conversationId, active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const updated = await this.api.sendMessage(conversationId, text);
      if (!this.destroyed && updated) this.dispatch({ type: 'conversation-updated', conversation: updated });
      if (!this.destroyed && this.state.currentConversationId === conversationId) this.renderer.clearComposerIfUnchanged(text);
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'send-in-flight', conversationId, active: false }); }
  }

  async interrupt(): Promise<void> {
    const conversationId = this.state.currentConversationId;
    if (!conversationId || this.state.interruptingConversationId === conversationId) return;
    this.dispatch({ type: 'interrupt-in-flight', conversationId, active: true });
    try {
      const updated = await this.api.interrupt(conversationId);
      if (!this.destroyed && updated) this.dispatch({ type: 'conversation-updated', conversation: updated });
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'interrupt-in-flight', conversationId, active: false }); }
  }

  async archive(): Promise<void> {
    const conversationId = this.state.currentConversationId;
    if (!conversationId) return;
    try {
      await this.api.archive(conversationId);
      if (!this.destroyed) this.dispatch({ type: 'conversation-archived', conversationId });
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
  }

  reload(): void {
    const conversationId = this.state.currentConversationId;
    if (conversationId) void this.loadConversation(conversationId);
  }

  async reconnect(conversationId: string): Promise<void> {
    if (this.state.reconcilingConversationId === conversationId) return;
    const conversation = this.state.conversations.find((item) => item.conversationId === conversationId);
    if (!conversation || conversation.status.toLowerCase() !== 'unknown') return;
    this.dispatch({ type: 'reconcile-in-flight', conversationId, active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const updated = await this.api.reconcile(conversationId);
      if (this.destroyed) return;
      this.dispatch({ type: 'conversation-updated', conversation: updated });
      const host = this.state.hosts.find((item) => item.hostId === updated.hostId);
      if (host) this.dispatch({ type: 'host-updated', host: { ...host, status: 'online' } });
      if (this.state.currentConversationId === conversationId) await this.loadConversation(conversationId);
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'reconcile-in-flight', conversationId, active: false }); }
  }

  async createHost(displayName: string, sshAlias: string): Promise<void> {
    if (!displayName || !sshAlias || this.state.creatingHost) return;
    this.dispatch({ type: 'host-create-in-flight', active: true });
    try {
      let host = await this.api.createHost(displayName, sshAlias);
      if (this.destroyed) return;
      this.dispatch({ type: 'host-added', host });
      this.renderer.selectHost(host.hostId);
      this.renderer.resetHostForm();
      const probed = await this.api.probeHost(host.hostId);
      if (probed && !this.destroyed) {
        host = probed;
        this.dispatch({ type: 'host-updated', host });
        this.renderer.selectHost(host.hostId);
      }
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'host-create-in-flight', active: false }); }
  }

  async probeHost(hostId: string): Promise<void> {
    try {
      const host = await this.api.probeHost(hostId);
      if (host && !this.destroyed) this.dispatch({ type: 'host-updated', host });
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
  }

  async createProject(hostId: string, label: string, cwd: string): Promise<void> {
    if (!hostId || !label || !cwd || this.state.creatingProject) return;
    this.dispatch({ type: 'project-create-in-flight', active: true });
    try {
      const project = await this.api.createProject(hostId, label, cwd);
      if (this.destroyed) return;
      this.dispatch({ type: 'project-added', project });
      this.renderer.selectHost(hostId);
      this.renderer.selectProject(project.projectId);
      this.renderer.resetProjectForm();
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'project-create-in-flight', active: false }); }
  }

  async respond(request: PendingRequest, response: { decision?: string; response?: unknown }): Promise<void> {
    if (this.state.respondingRequestKeys.includes(request.key)) return;
    this.dispatch({ type: 'response-in-flight', key: request.key, active: true });
    try {
      const updated = await this.api.respond(request.conversationId, request.key, response);
      if (!this.destroyed && updated) this.dispatch({ type: 'conversation-updated', conversation: updated });
      if (!this.destroyed) this.dispatch({ type: 'pending-resolved', conversationId: request.conversationId, key: request.key });
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'response-in-flight', key: request.key, active: false }); }
  }

  retry(): void { void this.refresh(); }

  toggleCollapsed(): void {
    this.collapsed = !this.collapsed;
    this.renderer.setCollapsed(this.collapsed);
    this.workspaceHost.classList.toggle('pw-conversations-collapsed', this.collapsed);
    try { localStorage.setItem(this.collapseKey(), String(this.collapsed)); } catch { /* Ignore unavailable storage. */ }
  }

  private connect(): void {
    if (this.stream || !this.active || this.destroyed) return;
    this.dispatch({ type: 'connection', connection: 'connecting' });
    this.stream = this.api.events(this.state.cursor, {
      snapshot: (snapshot) => {
        if (!this.active || this.destroyed) return;
        if (!this.acceptSnapshot(snapshot)) return;
        this.dispatch({ type: 'workspace-loaded', snapshot });
      },
      event: (event) => {
        if (!this.active || this.destroyed) return;
        this.dispatch({ type: 'event', event });
      },
      open: () => { if (this.active && !this.destroyed) this.dispatch({ type: 'connection', connection: 'open' }); },
      error: () => { if (this.active && !this.destroyed) this.dispatch({ type: 'connection', connection: 'retrying' }); },
    });
  }

  async refresh(): Promise<void> {
    await this.loadWorkspace();
    const pursuitId = this.state.selectedPursuitId;
    const conversationId = this.state.currentConversationId;
    if (pursuitId) await this.loadPursuit(pursuitId);
    if (conversationId && this.state.currentConversationId === conversationId) await this.loadConversation(conversationId);
  }

  setActive(active: boolean): void {
    this.active = active;
    if (!active) {
      this.stream?.close();
      this.stream = null;
      this.dispatch({ type: 'connection', connection: 'closed' });
    } else {
      this.connect();
      void this.refresh();
    }
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.stream?.close();
    this.stream = null;
    this.renderer.destroy();
  }
}

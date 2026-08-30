import { ConversationApi, type EventSourceFactory, type EventStream, type FetchJson } from './conversation-api.ts';
import { ConversationRenderer, type ConversationRendererActions } from './conversation-renderer.ts';
import {
  conversationCanSend,
  currentConversation,
  initialConversationState,
  reduceConversationState,
  recordValue,
  textValue,
  type ConversationAttachment,
  type ConversationAction,
  type ConversationEvent,
  type ConversationModelCatalog,
  type ConversationState,
  type PendingRequest,
  type WorkspaceSnapshot,
} from './conversation-state.ts';
import type { OperationalConversationIndicatorInput } from './conversation-indicators.ts';
import { DRAFT_PREFIX } from './tree.ts';

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function stablePursuitId(value: string | null): string | null {
  return value && !value.startsWith(DRAFT_PREFIX) ? value : null;
}

function opaqueViewId(): string {
  try { return crypto.randomUUID(); }
  catch { return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`; }
}

function isCompletedFinalAnswer(event: ConversationEvent): boolean {
  if (event.marksFinal) return true;
  const kind = event.kind.toLowerCase().replace(/[.\s/-]+/g, '_');
  if (!kind.includes('completed')) return false;
  const payload = recordValue(event.payload);
  const params = recordValue(payload.params);
  const item = recordValue(payload.item ?? params.item ?? payload.message);
  const type = textValue(item.type ?? item.kind ?? payload.type).toLowerCase().replace(/[.\s/-]+/g, '_');
  const phase = textValue(item.phase ?? payload.phase).toLowerCase().replace(/[.\s/-]+/g, '_');
  return (type === 'agentmessage' || type === 'agent_message' || type === 'message')
    && (phase === 'final_answer' || phase === 'finalanswer');
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
  private modelCatalogs = new Map<string, ConversationModelCatalog>();
  private modelCatalogLoads = new Map<string, Promise<void>>();
  private settingsUpdates = new Map<string, { intent: symbol; promise: Promise<void> }>();
  private settingsIntents = new Map<string, symbol>();
  private failedSettingsIntents = new Map<string, Set<symbol>>();
  private closingSideChats = new Set<string>();
  private readAcknowledgements = new Set<string>();
  private sideChatRestore: Promise<void> | null = null;
  private sideChatMutation = 0;
  private archivingConversations = new Set<string>();
  private indicatorSignature = '';
  private indicatorSink: (items: readonly OperationalConversationIndicatorInput[]) => void;
  private readonly viewId: string;
  private readonly pageId = opaqueViewId();
  private viewReleased = false;
  private readonly visibilityHandler = (): void => {
    if (document.visibilityState === 'visible' && !this.collapsed && this.state.currentConversationId) {
      void this.acknowledgeReadIfNeeded(this.state.currentConversationId);
    }
  };
  private readonly pageHideHandler = (event: PageTransitionEvent): void => {
    if (event.persisted) return;
    this.releaseView();
  };

  constructor(
    private workspaceHost: HTMLElement,
    paneHost: HTMLElement,
    fetchJson: FetchJson,
    private rootKey: string,
    private reloadPage: () => void = () => location.reload(),
    eventSourceFactory?: EventSourceFactory,
    indicatorSink: (items: readonly OperationalConversationIndicatorInput[]) => void = () => undefined,
  ) {
    this.indicatorSink = indicatorSink;
    this.api = new ConversationApi(fetchJson, eventSourceFactory);
    this.viewId = this.storedViewId();
    this.renderer = new ConversationRenderer(paneHost, rootKey, this);
    try { this.collapsed = localStorage.getItem(this.collapseKey()) === 'true'; } catch { /* Ignore unavailable storage. */ }
    this.renderer.setCollapsed(this.collapsed);
    this.workspaceHost.classList.toggle('pw-conversations-collapsed', this.collapsed);
    this.renderer.render(this.state);
    document.addEventListener('visibilitychange', this.visibilityHandler);
    window.addEventListener('pagehide', this.pageHideHandler);
  }

  private collapseKey(): string { return `rightmemory:conversation-pane:${encodeURIComponent(this.rootKey)}:collapsed`; }

  private sideChatKey(): string { return `rightmemory:side-chats:${encodeURIComponent(this.rootKey)}`; }

  private viewKey(): string { return `rightmemory:conversation-view:${encodeURIComponent(this.rootKey)}`; }

  private storedViewId(): string {
    try {
      const stored = sessionStorage.getItem(this.viewKey());
      if (stored) return stored;
      const created = opaqueViewId();
      sessionStorage.setItem(this.viewKey(), created);
      return created;
    } catch { return opaqueViewId(); }
  }

  private releaseView(): void {
    if (this.viewReleased) return;
    this.viewReleased = true;
    void this.api.releaseView(this.viewId, this.pageId).catch(() => undefined);
  }

  private storedSideChatIds(): string[] {
    try {
      const parsed: unknown = JSON.parse(sessionStorage.getItem(this.sideChatKey()) ?? '[]');
      return Array.isArray(parsed)
        ? [...new Set(parsed.filter((value): value is string => typeof value === 'string' && !!value))]
        : [];
    } catch { return []; }
  }

  private rememberSideChatIds(conversationIds: string[]): void {
    const ids = [...new Set(conversationIds.filter(Boolean))];
    this.dispatch({ type: 'side-chat-session', conversationIds: ids });
    try {
      if (ids.length) sessionStorage.setItem(this.sideChatKey(), JSON.stringify(ids));
      else sessionStorage.removeItem(this.sideChatKey());
    } catch { /* Session storage can be unavailable in hardened browser modes. */ }
  }

  private dispatch(action: ConversationAction): void {
    this.state = reduceConversationState(this.state, action);
    this.renderer.render(this.state);
    const indicatorInputs = this.state.conversations.map((conversation) => ({
      pursuitId: conversation.pursuitId,
      status: conversation.status,
      unreadFinal: conversation.lastFinalEventId !== null
        && conversation.lastFinalEventId > (conversation.lastReadEventId ?? 0),
      sideChat: conversation.kind === 'side_chat',
      archived: conversation.archived,
    }));
    const signature = indicatorInputs
      .map((item) => `${item.pursuitId ?? ''}\u001f${item.status}\u001f${item.unreadFinal ? 1 : 0}\u001f${item.sideChat ? 1 : 0}\u001f${item.archived ? 1 : 0}`)
      .sort()
      .join('\u001e');
    if (signature !== this.indicatorSignature) {
      this.indicatorSignature = signature;
      this.indicatorSink(indicatorInputs);
    }
  }

  private acceptSnapshot(snapshot: WorkspaceSnapshot): boolean {
    if (!snapshot.rootKey || snapshot.rootKey === this.rootKey) return true;
    this.destroy();
    this.reloadPage();
    return false;
  }

  async start(): Promise<void> {
    await this.loadWorkspace();
    if (this.destroyed) return;
    if (this.active) this.connect();
    await this.restoreSessionSideChats();
  }

  private restoreSessionSideChats(): Promise<void> {
    if (this.sideChatRestore) return this.sideChatRestore;
    const restore = this.performSideChatRestore();
    const tracked = restore.finally(() => {
      if (this.sideChatRestore === tracked) this.sideChatRestore = null;
    });
    this.sideChatRestore = tracked;
    return tracked;
  }

  private async performSideChatRestore(): Promise<void> {
    const restoreMutation = this.sideChatMutation;
    const stored = this.storedSideChatIds();
    this.rememberSideChatIds(stored);
    if (!stored.length) return;
    const details = await Promise.all(stored.map(async (conversationId) => {
      try { return { conversationId, detail: await this.api.conversation(conversationId), retain: true }; }
      catch (error) {
        const status = typeof error === 'object' && error !== null && typeof (error as { status?: unknown }).status === 'number'
          ? (error as { status: number }).status
          : 0;
        return { conversationId, detail: null, retain: status !== 404 };
      }
    }));
    if (this.destroyed) return;
    const currentIds = new Set(
      this.sideChatMutation === restoreMutation ? stored : this.state.sessionSideChatIds,
    );
    const invalid = new Set<string>();
    let transientFailures = 0;
    for (const result of details) {
      if (!currentIds.has(result.conversationId)) continue;
      const detail = result.detail;
      if (!detail) {
        if (result.retain) transientFailures++;
        else invalid.add(result.conversationId);
        continue;
      }
      if (detail.conversation.kind !== 'side_chat' || !detail.conversation.parentConversationId) {
        invalid.add(result.conversationId);
        continue;
      }
      this.dispatch({ type: 'side-chat-restored', detail });
    }
    let fallbackParentId: string | null = null;
    for (const conversationId of invalid) {
      const stale = this.state.conversations.find((conversation) => conversation.conversationId === conversationId);
      if (this.state.currentConversationId === conversationId && stale?.parentConversationId) {
        fallbackParentId = stale.parentConversationId;
      }
      this.dispatch({ type: 'side-chat-removed', conversationId });
      this.renderer.forgetConversation(conversationId);
    }
    this.rememberSideChatIds([...currentIds].filter((conversationId) => !invalid.has(conversationId)));
    if (fallbackParentId) {
      await this.loadConversation(fallbackParentId);
      if (!this.destroyed && this.state.currentConversationId === fallbackParentId) {
        this.renderer.focusConversationTab(fallbackParentId);
      }
    }
    if (transientFailures) {
      this.dispatch({
        type: 'error',
        message: `${transientFailures === 1 ? 'A side chat' : `${transientFailures} side chats`} could not be restored. Retry to reconnect.`,
      });
    }
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

  async loadEarlier(conversationId: string): Promise<void> {
    if (this.state.loadingEarlierConversationIds.includes(conversationId)) return;
    if (!this.state.hasEarlierEventsByConversation[conversationId]) return;
    const oldestEventId = this.state.eventsByConversation[conversationId]?.[0]?.eventId;
    if (!oldestEventId || !/^[1-9]\d*$/.test(oldestEventId)) return;
    this.dispatch({ type: 'conversation-history-in-flight', conversationId, active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const page = await this.api.earlierConversation(conversationId, oldestEventId);
      if (this.destroyed) return;
      if (this.state.currentConversationId !== conversationId) return;
      if (page.conversationId !== conversationId) {
        throw new Error('The server returned history for a different conversation.');
      }
      this.dispatch({ type: 'conversation-history-loaded', page });
    } catch (error) {
      if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) });
    } finally {
      if (!this.destroyed) {
        this.dispatch({ type: 'conversation-history-in-flight', conversationId, active: false });
      }
    }
  }

  private async loadConversation(conversationId: string): Promise<void> {
    const generation = ++this.conversationLoad;
    this.dispatch({ type: 'conversation-loading', conversationId });
    try {
      const detail = await this.api.conversation(conversationId);
      if (!this.destroyed && generation === this.conversationLoad) {
        this.dispatch({ type: 'conversation-loaded', detail });
        void this.acknowledgeReadIfNeeded(conversationId);
      }
    } catch (error) {
      if (!this.destroyed && generation === this.conversationLoad && this.state.currentConversationId === conversationId) this.dispatch({ type: 'error', message: errorMessage(error) });
    }
  }

  closeConversation(): void {
    this.conversationLoad++;
    this.dispatch({ type: 'conversation-closed' });
  }

  async createConversation(hostId: string, projectId: string, model: string, reasoningEffort: string): Promise<void> {
    const pursuitId = this.state.selectedPursuitId;
    if (!pursuitId || this.state.creatingConversation) return;
    const navigation = this.conversationLoad;
    const currentConversationId = this.state.currentConversationId;
    this.dispatch({ type: 'create-in-flight', active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const conversation = await this.api.createConversation(pursuitId, hostId, projectId, model, reasoningEffort);
      if (this.destroyed) return;
      const select = this.conversationLoad === navigation
        && this.state.selectedPursuitId === pursuitId
        && this.state.currentConversationId === currentConversationId;
      this.dispatch({ type: 'conversation-created', conversation, select });
      if (select) await this.loadConversation(conversation.conversationId);
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'create-in-flight', active: false }); }
  }

  async createSideChat(parentConversationId: string): Promise<void> {
    const parent = this.state.conversations.find((conversation) => conversation.conversationId === parentConversationId);
    if (!parent || parent.kind === 'side_chat' || parent.archived || this.state.creatingSideChat || this.archivingConversations.has(parentConversationId)) return;
    this.dispatch({ type: 'side-chat-create-in-flight', active: true });
    this.dispatch({ type: 'error', message: null });
    const navigation = this.conversationLoad;
    try {
      const conversation = await this.api.createSideChat(parentConversationId);
      if (this.destroyed) return;
      this.sideChatMutation += 1;
      this.rememberSideChatIds([...this.state.sessionSideChatIds, conversation.conversationId]);
      const select = this.conversationLoad === navigation
        && this.state.currentConversationId === parentConversationId;
      this.dispatch({ type: 'conversation-created', conversation, select });
      if (select) {
        await this.loadConversation(conversation.conversationId);
        this.renderer.focusConversationTab(conversation.conversationId);
      }
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { if (!this.destroyed) this.dispatch({ type: 'side-chat-create-in-flight', active: false }); }
  }

  async closeSideChat(sideChatId: string): Promise<void> {
    if (this.closingSideChats.has(sideChatId)) return;
    const sideChat = this.state.conversations.find((conversation) => conversation.conversationId === sideChatId);
    if (!sideChat || sideChat.kind !== 'side_chat') return;
    const parentConversationId = sideChat.parentConversationId;
    const wasCurrent = this.state.currentConversationId === sideChatId;
    const navigation = this.conversationLoad;
    this.closingSideChats.add(sideChatId);
    this.sideChatMutation += 1;
    try {
      await this.api.deleteSideChat(sideChatId);
      if (this.destroyed) return;
      const returnToParent = wasCurrent
        && this.conversationLoad === navigation
        && this.state.currentConversationId === sideChatId;
      this.rememberSideChatIds(this.state.sessionSideChatIds.filter((id) => id !== sideChatId));
      this.dispatch({ type: 'side-chat-removed', conversationId: sideChatId });
      this.renderer.forgetConversation(sideChatId);
      if (returnToParent && parentConversationId) {
        await this.loadConversation(parentConversationId);
        this.renderer.focusConversationTab(parentConversationId);
      }
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { this.closingSideChats.delete(sideChatId); }
  }

  private async acknowledgeReadIfNeeded(conversationId: string): Promise<void> {
    if (this.destroyed || !this.active || this.readAcknowledgements.has(conversationId)) return;
    if (this.collapsed || document.hidden || this.state.currentConversationId !== conversationId) return;
    if (!this.renderer.isFollowingActivity(conversationId)) return;
    const conversation = this.state.conversations.find((item) => item.conversationId === conversationId);
    const observedFinalEventId = conversation?.lastFinalEventId;
    if (observedFinalEventId === null || observedFinalEventId === undefined) return;
    if (observedFinalEventId <= (conversation?.lastReadEventId ?? 0)) return;
    this.readAcknowledgements.add(conversationId);
    try {
      const updated = await this.api.acknowledgeRead(conversationId, observedFinalEventId);
      if (!this.destroyed) this.dispatch({ type: 'conversation-updated', conversation: updated });
    } catch { /* Visibility acknowledgement is retried on the next detail load. */ }
    finally {
      this.readAcknowledgements.delete(conversationId);
      const latest = this.state.conversations.find((item) => item.conversationId === conversationId)?.lastFinalEventId;
      if (latest !== null && latest !== undefined && latest > observedFinalEventId) {
        queueMicrotask(() => { void this.acknowledgeReadIfNeeded(conversationId); });
      }
    }
  }

  acknowledgeRead(conversationId: string): void {
    void this.acknowledgeReadIfNeeded(conversationId);
  }

  loadModelCatalog(hostId: string): void {
    if (!hostId) return;
    const cached = this.modelCatalogs.get(hostId);
    if (cached) { this.renderer.setModelCatalog(cached); return; }
    if (this.modelCatalogLoads.has(hostId)) return;
    const load = this.api.modelCatalog(hostId).then((catalog) => {
      if (this.destroyed) return;
      this.modelCatalogs.set(hostId, catalog);
      this.renderer.setModelCatalog(catalog);
    }).catch((error) => {
      if (this.destroyed) return;
      this.dispatch({ type: 'error', message: errorMessage(error) });
      this.renderer.releaseModelCatalog(hostId);
    }).finally(() => { this.modelCatalogLoads.delete(hostId); });
    this.modelCatalogLoads.set(hostId, load);
  }

  updateConversationSettings(model: string, reasoningEffort: string): void {
    const conversationId = this.state.currentConversationId;
    const conversation = currentConversation(this.state);
    if (!conversationId || !conversation || conversation.archived || !model || !reasoningEffort) return;
    const intent = Symbol(conversationId);
    this.settingsIntents.set(conversationId, intent);
    this.dispatch({ type: 'conversation-settings-selected', conversationId, model, reasoningEffort });
    const previous = this.settingsUpdates.get(conversationId)?.promise ?? Promise.resolve();
    const next = previous.catch(() => undefined).then(async () => {
      try {
        const updated = await this.api.updateSettings(conversationId, model, reasoningEffort);
        if (!this.destroyed && this.settingsIntents.get(conversationId) === intent) {
          this.dispatch({ type: 'conversation-updated', conversation: updated });
        }
      } catch (error) {
        if (this.destroyed) return;
        const failed = this.failedSettingsIntents.get(conversationId) ?? new Set<symbol>();
        failed.add(intent);
        this.failedSettingsIntents.set(conversationId, failed);
        if (this.settingsIntents.get(conversationId) === intent) {
          this.dispatch({ type: 'error', message: errorMessage(error) });
          if (this.state.currentConversationId === conversationId) await this.loadConversation(conversationId);
        }
      }
    }).finally(() => {
      if (this.settingsUpdates.get(conversationId)?.promise === next) this.settingsUpdates.delete(conversationId);
      if (this.settingsIntents.get(conversationId) === intent) this.settingsIntents.delete(conversationId);
    });
    this.settingsUpdates.set(conversationId, { intent, promise: next });
  }

  async uploadAttachment(
    conversationId: string,
    file: File,
    attachmentId: string,
    attachmentKind?: 'file',
  ): Promise<ConversationAttachment> {
    return this.api.uploadAttachment(conversationId, file, attachmentId, attachmentKind);
  }

  async deleteAttachment(conversationId: string, attachmentId: string): Promise<void> {
    await this.api.deleteAttachment(conversationId, attachmentId);
  }

  async sendMessage(text: string, attachmentIds: string[]): Promise<boolean> {
    const conversationId = this.state.currentConversationId;
    const conversation = currentConversation(this.state);
    if (!conversationId || !conversation || !conversationCanSend(this.state, conversation)) return false;
    this.dispatch({ type: 'send-in-flight', conversationId, active: true });
    this.dispatch({ type: 'error', message: null });
    try {
      const awaitedSettingsIntents: symbol[] = [];
      let settingsUpdate: { intent: symbol; promise: Promise<void> } | undefined;
      while ((settingsUpdate = this.settingsUpdates.get(conversationId))) {
        if (!awaitedSettingsIntents.includes(settingsUpdate.intent)) awaitedSettingsIntents.push(settingsUpdate.intent);
        await settingsUpdate.promise;
      }
      const failedIntents = this.failedSettingsIntents.get(conversationId);
      const awaitedIntentFailed = awaitedSettingsIntents.some((intent) => failedIntents?.has(intent));
      // Only one send can be active per conversation, so no other sender can
      // still need these completed intent results.
      this.failedSettingsIntents.delete(conversationId);
      if (awaitedIntentFailed) return false;
      const updated = await this.api.sendMessage(conversationId, text, attachmentIds);
      if (!this.destroyed && updated) this.dispatch({ type: 'conversation-updated', conversation: updated });
      if (!this.destroyed) this.renderer.clearComposerIfUnchanged(text, attachmentIds, conversationId);
      return true;
    } catch (error) {
      if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) });
      return false;
    }
    finally { if (!this.destroyed) this.dispatch({ type: 'send-in-flight', conversationId, active: false }); }
  }

  async interrupt(): Promise<void> {
    const conversationId = this.state.currentConversationId;
    if (!conversationId || this.state.interruptingConversationIds.includes(conversationId)) return;
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
    const conversation = this.state.conversations.find((item) => item.conversationId === conversationId);
    if (!conversation || conversation.kind === 'side_chat' || this.archivingConversations.has(conversationId)) return;
    const sessionSideChats = this.state.sessionSideChatIds.map((sideChatId) =>
      this.state.conversations.find((item) => item.conversationId === sideChatId));
    const hasChild = sessionSideChats.some((sideChat) => sideChat?.kind === 'side_chat'
      && sideChat.parentConversationId === conversationId);
    const hasUnresolved = sessionSideChats.some((sideChat) => !sideChat);
    if (this.state.creatingSideChat || hasChild || hasUnresolved) {
      this.dispatch({ type: 'error', message: this.state.creatingSideChat
        ? 'Wait for the side chat to finish opening before archiving.'
        : 'Close side chats before archiving this conversation.' });
      return;
    }
    this.archivingConversations.add(conversationId);
    try {
      await this.api.archive(conversationId);
      if (!this.destroyed) {
        this.renderer.clearStagedAttachments(conversationId);
        this.dispatch({ type: 'conversation-archived', conversationId });
      }
    } catch (error) { if (!this.destroyed) this.dispatch({ type: 'error', message: errorMessage(error) }); }
    finally { this.archivingConversations.delete(conversationId); }
  }

  reload(): void {
    const conversationId = this.state.currentConversationId;
    if (conversationId) void this.loadConversation(conversationId);
  }

  async reconnect(conversationId: string): Promise<void> {
    if (this.state.reconcilingConversationIds.includes(conversationId)) return;
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

  retry(): void {
    this.renderer.retryModelCatalogs();
    void this.refresh();
  }

  toggleCollapsed(): void {
    this.collapsed = !this.collapsed;
    this.renderer.setCollapsed(this.collapsed);
    this.workspaceHost.classList.toggle('pw-conversations-collapsed', this.collapsed);
    try { localStorage.setItem(this.collapseKey(), String(this.collapsed)); } catch { /* Ignore unavailable storage. */ }
    if (!this.collapsed && this.state.currentConversationId) {
      queueMicrotask(() => { void this.acknowledgeReadIfNeeded(this.state.currentConversationId!); });
    }
  }

  private connect(): void {
    if (this.stream || !this.active || this.destroyed) return;
    this.dispatch({ type: 'connection', connection: 'connecting' });
    this.stream = this.api.events(this.state.cursor, this.viewId, this.pageId, {
      snapshot: (snapshot) => {
        if (this.destroyed) return;
        if (!this.acceptSnapshot(snapshot)) return;
        this.dispatch({ type: 'workspace-loaded', snapshot });
      },
      event: (event) => {
        if (this.destroyed) return;
        const eventKind = event.kind.toLowerCase().replace(/[.\s/-]+/g, '_');
        const closedSideChatId = eventKind === 'side_chat_closed'
          ? textValue(recordValue(event.payload).conversation_id ?? recordValue(event.payload).conversationId)
          : '';
        const closedSideChat = closedSideChatId
          ? this.state.conversations.find((conversation) => conversation.conversationId === closedSideChatId)
          : undefined;
        const returnToParent = Boolean(closedSideChatId && closedSideChat?.parentConversationId
          && this.state.currentConversationId === closedSideChatId);
        this.dispatch({ type: 'event', event });
        if (closedSideChatId) {
          this.sideChatMutation += 1;
          this.rememberSideChatIds(this.state.sessionSideChatIds.filter((id) => id !== closedSideChatId));
          this.renderer.forgetConversation(closedSideChatId);
          if (returnToParent && closedSideChat?.parentConversationId) {
            const parentConversationId = closedSideChat.parentConversationId;
            void this.loadConversation(parentConversationId).then(() => {
              if (!this.destroyed && this.state.currentConversationId === parentConversationId) {
                this.renderer.focusConversationTab(parentConversationId);
              }
            });
          }
          return;
        }
        if (this.state.currentConversationId === event.conversationId && isCompletedFinalAnswer(event)) {
          queueMicrotask(() => { void this.acknowledgeReadIfNeeded(event.conversationId); });
        }
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
    if (!this.destroyed) await this.restoreSessionSideChats();
  }

  setActive(active: boolean): void {
    const changed = this.active !== active;
    this.active = active;
    // Panel navigation is not a browser-view boundary. Keep the stream current;
    // pagehide owns the explicit temporary-chat release handshake.
    if (active) {
      this.connect();
      if (changed) void this.refresh();
    }
  }

  destroy(): void {
    if (this.destroyed) return;
    this.destroyed = true;
    this.releaseView();
    this.stream?.close();
    this.stream = null;
    document.removeEventListener('visibilitychange', this.visibilityHandler);
    window.removeEventListener('pagehide', this.pageHideHandler);
    this.renderer.destroy();
  }
}

import {
  conversationCanSend,
  conversationsForPursuit,
  currentConversation,
  recordValue,
  textValue,
  type ConversationEvent,
  type ConversationState,
  type PendingRequest,
} from './conversation-state.ts';

export interface ConversationRendererActions {
  toggleCollapsed(): void;
  openConversation(conversationId: string): void;
  closeConversation(): void;
  createConversation(hostId: string, projectId: string): void;
  sendMessage(text: string): void;
  interrupt(): void;
  archive(): void;
  reload(): void;
  reconnect(conversationId: string): void;
  createHost(displayName: string, sshAlias: string): void;
  probeHost(hostId: string): void;
  createProject(hostId: string, label: string, cwd: string): void;
  respond(request: PendingRequest, response: { decision?: string; response?: unknown }): void;
  retry(): void;
}

const shell = `
  <header class="cw-rail-header">
    <div class="cw-rail-title"><strong>Conversations</strong><span class="cw-connection" aria-label="Event connection"></span></div>
    <button type="button" class="cw-collapse" aria-label="Collapse conversation pane" title="Collapse conversation pane">›</button>
  </header>
  <div class="cw-pane-body">
    <div class="cw-error" role="alert" hidden><span></span><button type="button">Retry</button></div>
    <section class="cw-no-pursuit"><p>Select a stable Pursuit to see its conversations.</p></section>
    <section class="cw-list-view" hidden>
      <header class="cw-list-header"><div><small>SELECTED PURSUIT</small><strong class="cw-pursuit-label"></strong></div></header>
      <form class="cw-new-form">
        <label><span>Host</span><select name="host" aria-label="Conversation host"></select></label>
        <label><span>Project</span><select name="project" aria-label="Conversation project"></select></label>
        <button type="submit" class="cw-primary">New conversation</button>
      </form>
      <div class="cw-list-status" role="status"></div>
      <div class="cw-conversation-list"></div>
      <div class="cw-setup">
        <details class="cw-add-host"><summary>Add SSH host</summary>
          <form><label>Display name<input name="display_name" required autocomplete="off"></label><label>SSH alias<input name="ssh_alias" required autocomplete="off" placeholder="From ~/.ssh/config"></label><button type="submit">Add and probe</button></form>
        </details>
        <details class="cw-add-project"><summary>Add project</summary>
          <form><label>Host<select name="host_id" required></select></label><label>Label<input name="label" required autocomplete="off"></label><label>Working directory<input name="cwd" required autocomplete="off" placeholder="Absolute path on host"></label><button type="submit">Add project</button></form>
        </details>
      </div>
    </section>
    <section class="cw-detail-view" hidden>
      <header class="cw-detail-header">
        <button type="button" class="cw-back" aria-label="Back to conversations">‹</button>
        <div><strong class="cw-detail-title"></strong><small class="cw-detail-location"></small></div>
        <div class="cw-detail-actions"><button type="button" class="cw-reload" aria-label="Reload conversation" title="Reload conversation">↻</button><button type="button" class="cw-reconnect" hidden>Reconnect and check status</button><button type="button" class="cw-archive">Archive</button></div>
      </header>
      <div class="cw-turn-status" role="status"></div>
      <div class="cw-activity" tabindex="0" aria-label="Conversation activity"></div>
      <button type="button" class="cw-unread" hidden>New activity ↓</button>
      <div class="cw-pending"></div>
      <form class="cw-composer">
        <textarea name="message" rows="3" aria-label="Message" placeholder="Message this conversation…"></textarea>
        <div><small>Enter to send · Shift+Enter for a new line</small><button type="button" class="cw-stop">Stop</button><button type="submit" class="cw-primary">Send</button></div>
      </form>
    </section>
  </div>
`;

function element<K extends keyof HTMLElementTagNameMap>(tag: K, className?: string): HTMLElementTagNameMap[K] {
  const node = document.createElement(tag);
  if (className) node.className = className;
  return node;
}

function appendText(parent: HTMLElement, tag: keyof HTMLElementTagNameMap, value: string, className?: string): HTMLElement {
  const node = element(tag, className);
  node.textContent = value;
  parent.append(node);
  return node;
}

function visibleText(value: unknown): string {
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return value.map((entry) => {
    const record = recordValue(entry);
    return visibleText(record.text ?? record.content ?? entry);
  }).filter(Boolean).join('');
  const record = recordValue(value);
  return textValue(record.text ?? record.message ?? record.content ?? record.delta ?? record.output ?? record.reason);
}

function canonicalKind(value: string): string {
  return value.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/[.\s/-]+/g, '_');
}

function payloadParts(event: ConversationEvent): { payload: Record<string, unknown>; item: Record<string, unknown>; kind: string } {
  const payload = recordValue(event.payload);
  const item = recordValue(payload.item ?? payload.work_item ?? payload.message);
  const discriminator = textValue(item.type ?? item.kind ?? payload.type ?? payload.kind);
  return { payload, item, kind: canonicalKind(`${event.kind} ${discriminator}`) };
}

function compactJson(value: unknown): string {
  try {
    const text = JSON.stringify(value, null, 2) ?? String(value);
    return text.length > 20_000 ? `${text.slice(0, 20_000)}\n… output truncated in browser` : text;
  } catch { return String(value); }
}

function eventText(payload: Record<string, unknown>, item: Record<string, unknown>): string {
  return visibleText(item.text ?? item.content ?? item.delta ?? payload.text ?? payload.content ?? payload.delta ?? payload.message);
}

function activityNodes(events: ConversationEvent[]): HTMLElement[] {
  const nodes: HTMLElement[] = [];
  let merge: { role: 'user' | 'agent'; turnId: string | null; text: HTMLElement; node: HTMLElement } | null = null;
  for (const event of events) {
    const { payload, item, kind } = payloadParts(event);
    const roleValue = textValue(item.role ?? payload.role).toLowerCase();
    const isUser = roleValue === 'user' || kind.includes('user_message');
    const isAgent = roleValue === 'assistant' || roleValue === 'agent' || kind.includes('agent_message') || kind.includes('assistant_message') || kind.includes('agent_delta');
    if (isUser || isAgent) {
      const role = isUser ? 'user' : 'agent';
      const text = eventText(payload, item);
      const delta = kind.includes('delta');
      if (merge && merge.role === role && merge.turnId === event.turnId && delta) {
        merge.text.textContent = `${merge.text.textContent ?? ''}${text}`;
        continue;
      }
      if (merge && merge.role === role && merge.turnId === event.turnId && !delta && text && text.startsWith(merge.text.textContent ?? '')) {
        merge.text.textContent = text;
        continue;
      }
      const node: HTMLElement = element('article', `cw-message cw-${role}`);
      appendText(node, 'small', role === 'user' ? 'YOU' : 'AGENT');
      const content = appendText(node, 'div', text || '(empty message)', 'cw-message-text');
      nodes.push(node);
      merge = { role, turnId: event.turnId, text: content, node };
      continue;
    }
    merge = null;
    if (kind.includes('command') || kind.includes('exec')) {
      const node = element('details', 'cw-work-card cw-command');
      const command = visibleText(item.command ?? item.cmd ?? payload.command ?? payload.cmd ?? item.argv ?? payload.argv) || 'Command';
      appendText(node, 'summary', command);
      const exitCode = item.exit_code ?? item.exitCode ?? payload.exit_code ?? payload.exitCode;
      const meta = [textValue(item.status ?? payload.status), exitCode !== undefined ? `exit ${textValue(exitCode)}` : ''].filter(Boolean).join(' · ');
      if (meta) appendText(node, 'small', meta);
      const output = visibleText(item.output ?? item.aggregated_output ?? item.aggregatedOutput ?? item.stdout ?? item.delta ?? payload.output ?? payload.aggregatedOutput ?? payload.stdout ?? payload.stderr ?? payload.delta);
      if (output) appendText(node, 'pre', output);
      nodes.push(node);
      continue;
    }
    if (kind.includes('file') || kind.includes('diff') || kind.includes('patch')) {
      const node = element('details', 'cw-work-card cw-file');
      const path = textValue(item.path ?? item.file_path ?? payload.path ?? payload.file_path, 'File change');
      appendText(node, 'summary', path);
      const diff = visibleText(item.diff ?? item.patch ?? item.changes ?? item.delta ?? payload.diff ?? payload.patch ?? payload.changes ?? payload.delta);
      appendText(node, 'pre', diff || compactJson(event.payload));
      nodes.push(node);
      continue;
    }
    if (kind.includes('plan')) {
      const node = element('section', 'cw-work-card cw-plan');
      appendText(node, 'strong', 'Plan');
      const steps = Array.isArray(item.steps) ? item.steps : Array.isArray(item.plan) ? item.plan : Array.isArray(payload.steps) ? payload.steps : Array.isArray(payload.plan) ? payload.plan : [];
      if (steps.length) {
        const list = element('ol');
        for (const step of steps) {
          const record = recordValue(step);
          const label = visibleText(record.step ?? record.text ?? record.label ?? step);
          appendText(list, 'li', [label, textValue(record.status)].filter(Boolean).join(' — '));
        }
        node.append(list);
      } else appendText(node, 'pre', compactJson(event.payload));
      nodes.push(node);
      continue;
    }
    if (kind.includes('turn_') || kind.includes('status') || kind.includes('interrupt')) {
      const status = textValue(payload.status ?? payload.state) || event.kind.replace(/[._-]+/g, ' ');
      const node = element('div', 'cw-status-event');
      appendText(node, 'span', status);
      nodes.push(node);
      continue;
    }
    const node = element('details', 'cw-work-card cw-unknown');
    appendText(node, 'summary', event.kind || 'Unknown work item');
    appendText(node, 'pre', compactJson(event.payload));
    nodes.push(node);
  }
  return nodes;
}

function requestPrompt(request: PendingRequest): string {
  const payload = recordValue(request.payload);
  return visibleText(payload.prompt ?? payload.message ?? payload.question ?? payload.reason ?? payload.description ?? payload.command ?? payload.path) || request.kind.replace(/[._/-]+/g, ' ');
}

function isUserInputRequest(request: PendingRequest): boolean {
  return request.kind === 'item/tool/requestUserInput';
}

function isDecisionApproval(request: PendingRequest): boolean {
  return [
    'item/commandExecution/requestApproval',
    'item/fileChange/requestApproval',
    'execCommandApproval',
    'applyPatchApproval',
    'item/permissions/requestApproval',
  ].includes(request.kind);
}

export class ConversationRenderer {
  private abort = new AbortController();
  private collapsed = false;
  private lastConversationId: string | null = null;
  private activitySignature = '';
  private state: ConversationState | null = null;
  private pickerPursuitId: string | null = null;
  private pickerTouched = false;

  constructor(private host: HTMLElement, private rootKey: string, private actions: ConversationRendererActions) {
    host.className = 'cw-pane';
    host.innerHTML = shell;
    this.$('.cw-collapse').addEventListener('click', () => actions.toggleCollapsed(), { signal: this.abort.signal });
    this.$('.cw-error button').addEventListener('click', () => actions.retry(), { signal: this.abort.signal });
    this.$('.cw-back').addEventListener('click', () => actions.closeConversation(), { signal: this.abort.signal });
    this.$('.cw-reload').addEventListener('click', () => actions.reload(), { signal: this.abort.signal });
    this.$('.cw-reconnect').addEventListener('click', () => {
      const conversation = this.state && currentConversation(this.state);
      if (conversation) actions.reconnect(conversation.conversationId);
    }, { signal: this.abort.signal });
    this.$('.cw-archive').addEventListener('click', () => actions.archive(), { signal: this.abort.signal });
    this.$('.cw-stop').addEventListener('click', () => actions.interrupt(), { signal: this.abort.signal });
    this.$('.cw-unread').addEventListener('click', () => this.scrollToBottom(), { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-new-form [name="host"]').addEventListener('change', () => { this.pickerTouched = true; this.renderProjectOptions(); }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-new-form [name="project"]').addEventListener('change', () => { this.pickerTouched = true; }, { signal: this.abort.signal });
    this.$<HTMLFormElement>('.cw-new-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const hostId = this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value;
      const projectId = this.$<HTMLSelectElement>('.cw-new-form [name="project"]').value;
      if (hostId && projectId) actions.createConversation(hostId, projectId);
    }, { signal: this.abort.signal });
    this.$<HTMLFormElement>('.cw-add-host form').addEventListener('submit', (event) => {
      event.preventDefault();
      const form = event.currentTarget as HTMLFormElement;
      const data = new FormData(form);
      actions.createHost(String(data.get('display_name') ?? '').trim(), String(data.get('ssh_alias') ?? '').trim());
    }, { signal: this.abort.signal });
    this.$<HTMLFormElement>('.cw-add-project form').addEventListener('submit', (event) => {
      event.preventDefault();
      const form = event.currentTarget as HTMLFormElement;
      const data = new FormData(form);
      actions.createProject(String(data.get('host_id') ?? ''), String(data.get('label') ?? '').trim(), String(data.get('cwd') ?? '').trim());
    }, { signal: this.abort.signal });
    const composer = this.$<HTMLFormElement>('.cw-composer');
    composer.addEventListener('submit', (event) => { event.preventDefault(); this.submitComposer(); }, { signal: this.abort.signal });
    this.$<HTMLTextAreaElement>('.cw-composer textarea').addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) { event.preventDefault(); this.submitComposer(); }
    }, { signal: this.abort.signal });
    this.$<HTMLTextAreaElement>('.cw-composer textarea').addEventListener('input', () => this.saveDraft(), { signal: this.abort.signal });
  }

  private $<T extends HTMLElement = HTMLElement>(selector: string): T { return this.host.querySelector<T>(selector)!; }

  setCollapsed(collapsed: boolean): void {
    this.collapsed = collapsed;
    this.host.classList.toggle('cw-collapsed', collapsed);
    const button = this.$('.cw-collapse');
    button.textContent = collapsed ? '‹' : '›';
    button.setAttribute('aria-label', collapsed ? 'Expand conversation pane' : 'Collapse conversation pane');
    button.setAttribute('title', collapsed ? 'Expand conversation pane' : 'Collapse conversation pane');
  }

  render(state: ConversationState): void {
    this.state = state;
    const current = currentConversation(state);
    const hasPursuit = !!state.selectedPursuitId;
    this.$('.cw-no-pursuit').hidden = hasPursuit || !!current;
    this.$('.cw-list-view').hidden = !hasPursuit || !!current;
    this.$('.cw-detail-view').hidden = !current;
    this.renderError(state.error);
    this.renderConnection(state.connection);
    this.renderPicker(state);
    this.renderList(state);
    this.renderDetail(state, current);
  }

  private renderError(message: string | null): void {
    const node = this.$('.cw-error');
    node.hidden = !message;
    this.$('.cw-error span').textContent = message ?? '';
  }

  private renderConnection(connection: ConversationState['connection']): void {
    const node = this.$('.cw-connection');
    node.className = `cw-connection cw-${connection}`;
    node.textContent = connection === 'open' ? 'Live' : connection === 'retrying' ? 'Reconnecting' : connection === 'connecting' ? 'Connecting' : 'Offline';
  }

  private renderPicker(state: ConversationState): void {
    const hostSelect = this.$<HTMLSelectElement>('.cw-new-form [name="host"]');
    if (this.pickerPursuitId !== state.selectedPursuitId) {
      this.pickerPursuitId = state.selectedPursuitId;
      this.pickerTouched = false;
    }
    const preference = state.selectedPursuitId ? state.pursuitDefaults[state.selectedPursuitId] : undefined;
    const priorHost = hostSelect.value;
    hostSelect.replaceChildren();
    for (const host of state.hosts) {
      const suffix = host.status && !['online', 'ready'].includes(host.status.toLowerCase()) ? ` · ${host.status}` : '';
      hostSelect.add(new Option(`${host.displayName}${suffix}`, host.hostId));
    }
    const local = state.hosts.find((host) => host.kind === 'local');
    const preferredHost = !this.pickerTouched && preference && state.hosts.some((host) => host.hostId === preference.hostId) ? preference.hostId : '';
    hostSelect.value = preferredHost || (state.hosts.some((host) => host.hostId === priorHost) ? priorHost : local?.hostId ?? state.hosts[0]?.hostId ?? '');
    const preferredProject = !this.pickerTouched && preference?.hostId === hostSelect.value ? preference.projectId : '';
    this.renderProjectOptions(preferredProject);
    const projectHost = this.$<HTMLSelectElement>('.cw-add-project [name="host_id"]');
    const priorProjectHost = projectHost.value;
    projectHost.replaceChildren(...state.hosts.map((host) => new Option(host.displayName, host.hostId)));
    projectHost.value = state.hosts.some((host) => host.hostId === priorProjectHost) ? priorProjectHost : hostSelect.value;
    const newButton = this.$<HTMLButtonElement>('.cw-new-form button[type="submit"]');
    newButton.disabled = state.creatingConversation || !state.selectedPursuitId || !hostSelect.value || !this.$<HTMLSelectElement>('.cw-new-form [name="project"]').value;
    newButton.textContent = state.creatingConversation ? 'Creating…' : 'New conversation';
    this.setFormBusy('.cw-add-host form', state.creatingHost, state.creatingHost ? 'Adding…' : 'Add and probe');
    this.setFormBusy('.cw-add-project form', state.creatingProject, state.creatingProject ? 'Adding…' : 'Add project');
  }

  private renderProjectOptions(preferredProjectId = ''): void {
    if (!this.state) return;
    const hostId = this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value;
    const projectSelect = this.$<HTMLSelectElement>('.cw-new-form [name="project"]');
    const previous = projectSelect.value;
    const projects = this.state.projects.filter((project) => project.hostId === hostId);
    projectSelect.replaceChildren(...projects.map((project) => new Option(project.label || project.cwd, project.projectId)));
    projectSelect.value = projects.some((project) => project.projectId === preferredProjectId) ? preferredProjectId : projects.some((project) => project.projectId === previous) ? previous : projects[0]?.projectId ?? '';
    this.$<HTMLButtonElement>('.cw-new-form button[type="submit"]').disabled = !!this.state?.creatingConversation || !hostId || !projectSelect.value;
  }

  private renderList(state: ConversationState): void {
    this.$('.cw-pursuit-label').textContent = state.selectedPursuitId ?? '';
    const status = this.$('.cw-list-status');
    status.textContent = state.loadingWorkspace || state.loadingPursuit ? 'Loading conversations…' : '';
    const list = this.$('.cw-conversation-list');
    list.replaceChildren();
    const conversations = conversationsForPursuit(state);
    if (!conversations.length && !state.loadingPursuit) {
      appendText(list, 'p', 'No conversations yet.', 'cw-empty-copy');
      return;
    }
    for (const conversation of conversations) {
      const button = element('button', 'cw-conversation');
      button.type = 'button';
      button.dataset.conversationId = conversation.conversationId;
      const title = appendText(button, 'span', conversation.title);
      title.className = 'cw-conversation-title';
      const host = state.hosts.find((item) => item.hostId === conversation.hostId)?.displayName || conversation.hostId;
      appendText(button, 'small', [host, conversation.status].filter(Boolean).join(' · '));
      button.addEventListener('click', () => this.actions.openConversation(conversation.conversationId), { signal: this.abort.signal });
      list.append(button);
    }
  }

  private renderDetail(state: ConversationState, conversation: ReturnType<typeof currentConversation>): void {
    if (!conversation) {
      if (this.lastConversationId) this.saveDraft(this.lastConversationId);
      this.lastConversationId = null;
      return;
    }
    const changed = this.lastConversationId !== conversation.conversationId;
    if (changed) {
      if (this.lastConversationId) this.saveDraft(this.lastConversationId);
      this.lastConversationId = conversation.conversationId;
      this.loadDraft(conversation.conversationId);
      this.activitySignature = '\u0000';
    }
    this.$('.cw-detail-title').textContent = conversation.title;
    const host = state.hosts.find((item) => item.hostId === conversation.hostId)?.displayName || conversation.hostId;
    const project = state.projects.find((item) => item.projectId === conversation.projectId);
    this.$('.cw-detail-location').textContent = [host, project?.cwd || project?.label || conversation.projectId].filter(Boolean).join(' · ');
    this.$('.cw-turn-status').textContent = state.loadingConversation
      ? 'Loading history…'
      : state.sendingConversationId === conversation.conversationId ? 'Sending…' : conversation.status || 'unknown';
    const running = ['starting', 'running', 'in_progress', 'waiting_approval', 'waiting approval', 'waiting_input', 'waiting input'].includes(conversation.status.toLowerCase());
    const interrupting = state.interruptingConversationId === conversation.conversationId;
    this.$<HTMLButtonElement>('.cw-stop').disabled = !running || interrupting;
    this.$<HTMLButtonElement>('.cw-stop').textContent = interrupting ? 'Stopping…' : 'Stop';
    this.$<HTMLButtonElement>('.cw-composer button[type="submit"]').disabled = !conversationCanSend(state, conversation);
    this.$<HTMLTextAreaElement>('.cw-composer textarea').disabled = conversation.archived;
    const reconnect = this.$<HTMLButtonElement>('.cw-reconnect');
    reconnect.hidden = conversation.status.toLowerCase() !== 'unknown';
    reconnect.disabled = state.loadingConversation || state.reconcilingConversationId === conversation.conversationId;
    reconnect.textContent = state.reconcilingConversationId === conversation.conversationId ? 'Checking…' : 'Reconnect and check status';
    this.renderActivity(state.eventsByConversation[conversation.conversationId] ?? [], changed);
    this.renderPending(
      state.pendingRequests.filter((request) => request.conversationId === conversation.conversationId),
      new Set(state.respondingRequestKeys),
    );
  }

  private renderActivity(events: ConversationEvent[], forceBottom: boolean): void {
    const signature = events.map((event) => event.eventId).join('\u001f');
    if (signature === this.activitySignature && !forceBottom) return;
    this.activitySignature = signature;
    const activity = this.$('.cw-activity');
    const nearBottom = forceBottom || activity.scrollHeight - activity.scrollTop - activity.clientHeight < 90;
    const hadContent = activity.childElementCount > 0;
    const nodes = activityNodes(events);
    if (!nodes.length) {
      activity.replaceChildren();
      appendText(activity, 'p', 'No activity yet. Send the first message when you are ready.', 'cw-empty-copy');
    } else activity.replaceChildren(...nodes);
    if (!nodes.length) return;
    requestAnimationFrame(() => {
      if (nearBottom) this.scrollToBottom();
      else if (hadContent) this.$('.cw-unread').hidden = false;
    });
  }

  private renderPending(requests: PendingRequest[], responding: Set<string>): void {
    const parent = this.$('.cw-pending');
    const existing = new Map(
      [...parent.querySelectorAll<HTMLElement>(':scope > .cw-request')]
        .map((card) => [card.dataset.requestKey ?? '', card] as const),
    );
    const cards: HTMLElement[] = [];
    for (const request of requests) {
      const inFlight = responding.has(request.key);
      let card = existing.get(request.key);
      if (!card || card.dataset.requestKind !== request.kind) {
        card = element('section', 'cw-request');
        card.dataset.requestKey = request.key;
        card.dataset.requestKind = request.kind;
        if (isUserInputRequest(request) && this.renderQuestionRequest(card, request, inFlight)) {
          // Structured question form already rendered.
        } else if (isDecisionApproval(request)) {
          const permissionRequest = request.kind === 'item/permissions/requestApproval';
          appendText(card, 'strong', permissionRequest ? 'Permission request' : 'Approval needed');
          appendText(card, 'p', requestPrompt(request));
          const details = element('details', 'cw-request-details');
          appendText(details, 'summary', 'Show request details');
          appendText(details, 'pre', compactJson(request.payload));
          card.append(details);
          const controls = element('div');
          const permissions = recordValue(recordValue(request.payload).permissions);
          const canGrantPermissions = !permissionRequest || Object.keys(permissions).length > 0;
          for (const [label, decision] of [['Allow', 'accept'], ['Allow for session', 'acceptForSession'], ['Deny', 'decline'], ['Cancel', 'cancel']] as const) {
            const button = element('button'); button.type = 'button'; button.textContent = label;
            if (!canGrantPermissions && decision.startsWith('accept')) button.dataset.unavailable = 'true';
            button.addEventListener('click', () => this.actions.respond(request, { decision }), { signal: this.abort.signal });
            controls.append(button);
          }
          card.append(controls);
        } else {
          this.renderObjectRequest(card, request, inFlight);
        }
      }
      this.setRequestBusy(card, inFlight);
      cards.push(card);
    }
    parent.replaceChildren(...cards);
  }

  private setRequestBusy(card: HTMLElement, busy: boolean): void {
    for (const control of card.querySelectorAll<HTMLInputElement | HTMLTextAreaElement | HTMLButtonElement | HTMLSelectElement>('input, textarea, button, select')) {
      control.disabled = busy || control.dataset.unavailable === 'true';
    }
  }

  private setFormBusy(selector: string, busy: boolean, buttonLabel: string): void {
    const form = this.$<HTMLFormElement>(selector);
    for (const control of form.querySelectorAll<HTMLInputElement | HTMLButtonElement | HTMLSelectElement>('input, button, select')) control.disabled = busy;
    form.querySelector<HTMLButtonElement>('button[type="submit"]')!.textContent = buttonLabel;
  }

  private renderQuestionRequest(card: HTMLElement, request: PendingRequest, inFlight: boolean): boolean {
    const payload = recordValue(request.payload);
    const questions = Array.isArray(payload.questions) ? payload.questions.map(recordValue) : [];
    if (!questions.length || questions.some((question) => !textValue(question.id))) return false;
    appendText(card, 'strong', 'Agent needs input');
    const form = element('form', 'cw-question-form');
    const readers: Array<{ id: string; answer(): string }> = [];
    questions.forEach((question, index) => {
      const id = textValue(question.id);
      const fieldset = element('fieldset');
      appendText(fieldset, 'legend', textValue(question.header, `Question ${index + 1}`));
      appendText(fieldset, 'p', textValue(question.question ?? question.prompt, id));
      const options = Array.isArray(question.options) ? question.options.map(recordValue) : [];
      if (options.length) {
        const optionInputs: HTMLInputElement[] = [];
        for (const [optionIndex, option] of options.entries()) {
          const labelText = textValue(option.label ?? option.value);
          if (!labelText) continue;
          const label = element('label', 'cw-question-option');
          const input = element('input');
          input.type = 'radio'; input.name = `question-${request.key}-${index}`; input.value = labelText;
          optionInputs.push(input);
          const copy = element('span');
          appendText(copy, 'strong', labelText);
          const description = textValue(option.description);
          if (description) appendText(copy, 'small', description);
          label.append(input, copy); fieldset.append(label);
        }
        const other = element('input');
        other.type = 'text'; other.placeholder = 'Other answer'; other.setAttribute('aria-label', `${textValue(question.header, id)} other answer`);
        fieldset.append(other);
        readers.push({ id, answer: () => other.value.trim() || (optionInputs.find((input) => input.checked)?.value ?? '') });
      } else {
        const input = element('textarea');
        input.rows = 2; input.setAttribute('aria-label', textValue(question.header, id));
        fieldset.append(input);
        readers.push({ id, answer: () => input.value.trim() });
      }
      form.append(fieldset);
    });
    const validation = appendText(form, 'div', '', 'cw-question-validation');
    validation.setAttribute('role', 'status');
    const submit = element('button'); submit.type = 'submit'; submit.textContent = 'Respond';
    submit.disabled = inFlight;
    form.append(submit);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      const answers: Record<string, { answers: string[] }> = {};
      for (const reader of readers) {
        const answer = reader.answer();
        if (!answer) { validation.textContent = 'Answer each question before responding.'; return; }
        answers[reader.id] = { answers: [answer] };
      }
      validation.textContent = '';
      this.actions.respond(request, { response: answers });
    }, { signal: this.abort.signal });
    card.append(form);
    return true;
  }

  private renderObjectRequest(card: HTMLElement, request: PendingRequest, inFlight: boolean): void {
    const elicitation = request.kind === 'mcpServer/elicitation/request';
    appendText(card, 'strong', elicitation ? 'MCP server needs input' : 'Server request needs an object response');
    appendText(card, 'p', requestPrompt(request));
    const details = element('details', 'cw-request-details');
    appendText(details, 'summary', 'Show request details');
    appendText(details, 'pre', compactJson(request.payload));
    card.append(details);
    const form = element('form', 'cw-question-form cw-object-response-form');
    const input = element('textarea');
    input.rows = 4;
    input.maxLength = 120_000;
    input.placeholder = '{\n  "key": "value"\n}';
    input.setAttribute('aria-label', elicitation ? 'MCP elicitation content as JSON object' : 'Server response as JSON object');
    input.disabled = inFlight;
    const validation = appendText(form, 'div', '', 'cw-question-validation');
    validation.setAttribute('role', 'status');
    const controls = element('div');
    const submit = element('button'); submit.type = 'submit'; submit.textContent = elicitation ? 'Accept response' : 'Send response'; submit.disabled = inFlight;
    controls.append(submit);
    if (elicitation) {
      for (const [label, decision] of [['Decline', 'decline'], ['Cancel', 'cancel']] as const) {
        const button = element('button'); button.type = 'button'; button.textContent = label; button.disabled = inFlight;
        button.addEventListener('click', () => this.actions.respond(request, { decision }), { signal: this.abort.signal });
        controls.append(button);
      }
    }
    form.append(input, validation, controls);
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      let response: unknown;
      try { response = JSON.parse(input.value); }
      catch { validation.textContent = 'Enter valid JSON.'; return; }
      if (!response || typeof response !== 'object' || Array.isArray(response)) {
        validation.textContent = 'The response must be a JSON object.';
        return;
      }
      validation.textContent = '';
      this.actions.respond(request, elicitation ? { decision: 'accept', response } : { response });
    }, { signal: this.abort.signal });
    card.append(form);
  }

  private submitComposer(): void {
    const input = this.$<HTMLTextAreaElement>('.cw-composer textarea');
    if (this.$<HTMLButtonElement>('.cw-composer button[type="submit"]').disabled) return;
    const text = input.value.trim();
    if (text) this.actions.sendMessage(text);
  }

  clearComposerIfUnchanged(submittedText: string): void {
    const input = this.$<HTMLTextAreaElement>('.cw-composer textarea');
    if (input.value.trim() !== submittedText) return;
    input.value = '';
    try { if (this.lastConversationId) localStorage.removeItem(this.draftKey(this.lastConversationId)); }
    catch { /* Storage can be unavailable in hardened browser modes. */ }
  }

  selectHost(hostId: string): void {
    this.pickerTouched = true;
    const select = this.$<HTMLSelectElement>('.cw-new-form [name="host"]');
    select.value = hostId;
    this.renderProjectOptions();
  }

  selectProject(projectId: string): void {
    this.pickerTouched = true;
    this.$<HTMLSelectElement>('.cw-new-form [name="project"]').value = projectId;
  }

  resetHostForm(): void {
    this.$<HTMLFormElement>('.cw-add-host form').reset();
    this.$<HTMLDetailsElement>('.cw-add-host').open = false;
  }

  resetProjectForm(): void {
    this.$<HTMLFormElement>('.cw-add-project form').reset();
    this.$<HTMLDetailsElement>('.cw-add-project').open = false;
  }

  private draftKey(conversationId: string): string {
    return `rightmemory:conversation-draft:${encodeURIComponent(this.rootKey)}:${encodeURIComponent(conversationId)}`;
  }

  private saveDraft(conversationId = this.lastConversationId): void {
    if (!conversationId) return;
    const value = this.$<HTMLTextAreaElement>('.cw-composer textarea').value;
    try {
      if (value) localStorage.setItem(this.draftKey(conversationId), value);
      else localStorage.removeItem(this.draftKey(conversationId));
    } catch { /* Storage can be unavailable in hardened browser modes. */ }
  }

  private loadDraft(conversationId: string): void {
    try { this.$<HTMLTextAreaElement>('.cw-composer textarea').value = localStorage.getItem(this.draftKey(conversationId)) ?? ''; }
    catch { this.$<HTMLTextAreaElement>('.cw-composer textarea').value = ''; }
  }

  private scrollToBottom(): void {
    const activity = this.$('.cw-activity');
    activity.scrollTop = activity.scrollHeight;
    this.$('.cw-unread').hidden = true;
  }

  destroy(): void {
    this.saveDraft();
    this.abort.abort();
  }
}

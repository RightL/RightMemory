import {
  conversationCanSend,
  conversationsForPursuit,
  currentConversation,
  managerConversations,
  normalizeAttachment,
  recordValue,
  textValue,
  type ConversationAttachment,
  type ConversationEvent,
  type ConversationModel,
  type ConversationModelCatalog,
  type ConversationReference,
  type ConversationState,
  type PendingRequest,
} from './conversation-state.ts';
import { RICH_TEXT_CACHE_LIMIT, renderRichText } from './rich-text.ts';

export interface ConversationRendererActions {
  toggleCollapsed(): void;
  openManager(): void;
  openConversation(conversationId: string): void;
  loadEarlier(conversationId: string): void;
  closeConversation(): void;
  createConversation(hostId: string, projectId: string, model: string, reasoningEffort: string): void;
  createManager(model: string, reasoningEffort: string): void;
  removeManagerReference(): void;
  createSideChat(parentConversationId: string): void;
  closeSideChat(sideChatId: string): void;
  acknowledgeRead(conversationId: string): void;
  loadModelCatalog(hostId: string): void;
  updateConversationSettings(model: string, reasoningEffort: string): void;
  uploadAttachment(conversationId: string, file: File, attachmentId: string, attachmentKind?: 'file'): Promise<ConversationAttachment>;
  deleteAttachment(conversationId: string, attachmentId: string): Promise<void>;
  sendMessage(text: string, attachmentIds: string[]): boolean | Promise<boolean>;
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
    <div class="cw-rail-actions"><button type="button" class="cw-manager-entry" aria-pressed="false">Manager</button><button type="button" class="cw-collapse" aria-label="Collapse conversation pane" title="Collapse conversation pane">›</button></div>
  </header>
  <div class="cw-pane-body">
    <div class="cw-error" role="alert" hidden><span></span><button type="button">Retry</button></div>
    <section class="cw-no-pursuit"><p>Select a stable Pursuit to see its conversations.</p></section>
    <section class="cw-list-view" hidden>
      <header class="cw-list-header"><div><small>SELECTED PURSUIT</small><strong class="cw-pursuit-label"></strong></div></header>
      <form class="cw-new-form">
        <label><span>Host</span><select name="host" aria-label="Conversation host"></select></label>
        <label><span>Project</span><select name="project" aria-label="Conversation project"></select></label>
        <div class="cw-new-models">
          <label><span>Model</span><select name="model" aria-label="Conversation model"></select></label>
          <label><span>Reasoning</span><select name="effort" aria-label="Conversation reasoning effort"></select></label>
        </div>
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
    <section class="cw-manager-view" hidden>
      <header class="cw-list-header"><div><small>LOCAL ROOT</small><strong>Manager</strong></div></header>
      <form class="cw-manager-form">
        <div class="cw-new-models">
          <label><span>Model</span><select name="model" aria-label="Manager model"></select></label>
          <label><span>Reasoning</span><select name="effort" aria-label="Manager reasoning effort"></select></label>
        </div>
        <button type="submit" class="cw-primary">New Manager conversation</button>
      </form>
      <div class="cw-manager-status" role="status"></div>
      <div class="cw-manager-list cw-conversation-list"></div>
    </section>
    <section class="cw-detail-view" hidden>
      <nav class="cw-conversation-tabs" role="tablist" aria-label="Conversation tabs"></nav>
      <header class="cw-detail-header">
        <button type="button" class="cw-back" aria-label="Back to conversations">‹</button>
        <div><strong class="cw-detail-title"></strong><small class="cw-detail-location"></small></div>
        <div class="cw-detail-actions"><button type="button" class="cw-reload" aria-label="Reload conversation" title="Reload conversation">↻</button><button type="button" class="cw-reconnect" hidden>Reconnect and check status</button><button type="button" class="cw-archive">Archive</button></div>
      </header>
      <p class="cw-side-chat-note" hidden>Side chats are temporary and disappear when you close this app.</p>
      <div class="cw-turn-status" role="status"></div>
      <button type="button" class="cw-load-earlier" hidden>Load earlier messages</button>
      <div class="cw-activity" tabindex="0" aria-label="Conversation activity"></div>
      <button type="button" class="cw-unread" hidden>New activity ↓</button>
      <div class="cw-pending"></div>
      <form class="cw-composer">
        <div class="cw-staged-references" aria-label="References for the next message" aria-live="polite" hidden></div>
        <div class="cw-staged-attachments" aria-label="Attachments for the next message" aria-live="polite"></div>
        <div class="cw-composer-input">
          <button type="button" class="cw-attach" aria-label="Attach files" title="Attach files">📎</button>
          <textarea name="message" rows="3" aria-label="Message" placeholder="Message this conversation…"></textarea>
          <input class="cw-file-input" type="file" multiple hidden aria-label="Choose files to attach">
        </div>
        <div class="cw-composer-notice" role="status" aria-live="polite" hidden></div>
        <div class="cw-composer-footer">
          <div class="cw-turn-options" aria-label="Next response settings">
            <select name="model" aria-label="Model for the next message" title="Model for the next message"></select>
            <select name="effort" aria-label="Reasoning effort for the next message" title="Reasoning effort for the next message"></select>
          </div>
          <small>Enter to send · Shift+Enter for a new line</small>
          <button type="submit" class="cw-primary cw-send-stop" data-mode="send">Send</button>
        </div>
      </form>
    </section>
  </div>
`;

/** Larger plain-text pastes become managed files so the composer stays responsive. */
export const LARGE_PASTE_CHARACTER_LIMIT = 8_000;
export const MAX_PASTED_IMAGE_BYTES = 20 * 1024 * 1024;
export const MAX_PASTED_TEXT_BYTES = 5 * 1024 * 1024;
export const MAX_GENERIC_FILE_BYTES = 20 * 1024 * 1024;
export const MAX_STAGED_IMAGE_COUNT = 4;
export const MAX_STAGED_TEXT_COUNT = 4;
export const MAX_STAGED_FILE_COUNT = 8;
export const MAX_STAGED_ATTACHMENT_COUNT = 8;

const PROJECTED_TEXT_TRUNCATION_SUFFIX = '...[truncated]';

/** Match the provider copy paired with a structured local user event.
 *
 * Projection preserves an oversized string as an exact prefix followed by the
 * explicit truncation suffix. Opening context is the first part of a Manager
 * provider message, so that prefix remains a durable pairing signal even when
 * projection removes the later user-message portion.
 */
export function isPairedProviderUserEcho(
  providerText: string,
  localSource: string,
  openingContext: string,
): boolean {
  if (!openingContext) return true;
  if (providerText.includes(openingContext) && (!localSource || providerText.includes(localSource))) return true;
  if (!providerText.endsWith(PROJECTED_TEXT_TRUNCATION_SUFFIX)) return false;

  const projectedPrefix = providerText.slice(0, -PROJECTED_TEXT_TRUNCATION_SUFFIX.length);
  if (!projectedPrefix) return false;
  return openingContext.startsWith(projectedPrefix)
    || projectedPrefix === `${openingContext}\n`
    || projectedPrefix.startsWith(`${openingContext}\n\n`);
}

const PASTED_IMAGE_MEDIA_TYPES = new Set(['image/png', 'image/jpeg']);

type StagedAttachment = {
  clientId: string;
  uploadId: string;
  conversationId: string;
  file: File | null;
  displayName: string;
  kind: 'image' | 'pasted_text' | 'file';
  previewUrl: string;
  status: 'queued' | 'uploading' | 'ready' | 'failed';
  attachment: ConversationAttachment | null;
  error: string;
  removed: boolean;
  submitting: boolean;
  removing: boolean;
  snapshotSeen: boolean;
};

type LiveWorkState = {
  active: boolean;
  stopping: boolean;
  label: string;
  startedAt: number | null;
};

function attachmentUploadId(): string {
  const bytes = new Uint8Array(16);
  try { crypto.getRandomValues(bytes); }
  catch {
    for (let index = 0; index < bytes.length; index++) bytes[index] = Math.floor(Math.random() * 256);
  }
  return [...bytes].map((value) => value.toString(16).padStart(2, '0')).join('');
}

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

function sideChatTitle(value: string): string {
  const title = value.trim();
  return !title || /^(untitled( conversation)?|new conversation)$/i.test(title) ? 'Side chat' : title;
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

function messageReferences(value: unknown): ConversationReference[] {
  if (!Array.isArray(value)) return [];
  const seen = new Set<string>();
  const references: ConversationReference[] = [];
  for (const entry of value) {
    const record = recordValue(entry);
    const kind = textValue(record.kind);
    const id = textValue(record.id ?? record.pursuit_id);
    if (kind !== 'pursuit' || !id || seen.has(id)) continue;
    seen.add(id);
    references.push({ kind: 'pursuit', id, title: textValue(record.title) || undefined });
  }
  return references;
}

function canonicalKind(value: string): string {
  return value.replace(/([a-z0-9])([A-Z])/g, '$1_$2').toLowerCase().replace(/[.\s/-]+/g, '_').replace(/^_+|_+$/g, '');
}

function payloadParts(event: ConversationEvent): { payload: Record<string, unknown>; item: Record<string, unknown>; itemId: string; kind: string } {
  const payload = recordValue(event.payload);
  const params = recordValue(payload.params);
  const item = recordValue(payload.item ?? params.item ?? payload.work_item ?? payload.message);
  const discriminator = textValue(item.type ?? item.kind ?? payload.type ?? payload.kind);
  const itemId = textValue(item.id ?? payload.itemId ?? payload.item_id ?? params.itemId ?? params.item_id);
  return { payload, item, itemId, kind: canonicalKind(`${event.kind} ${discriminator}`) };
}

function compactJson(value: unknown): string {
  try {
    const text = JSON.stringify(value, null, 2) ?? String(value);
    return text.length > 20_000 ? `${text.slice(0, 20_000)}\n… output truncated in browser` : text;
  } catch { return String(value); }
}

function shortVisibleText(value: unknown, limit = 180): string {
  const direct = visibleText(value).replace(/\s+/g, ' ').trim();
  if (direct) return direct.length > limit ? `${direct.slice(0, limit - 1)}…` : direct;
  const record = recordValue(value);
  const candidate = visibleText(
    record.query ?? record.prompt ?? record.path ?? record.url ?? record.command ?? record.name ?? record.description,
  ).replace(/\s+/g, ' ').trim();
  return candidate.length > limit ? `${candidate.slice(0, limit - 1)}…` : candidate;
}

function attachmentAlreadyAbsent(error: unknown): boolean {
  const value = error as { status?: unknown; code?: unknown; error?: unknown; message?: unknown } | null;
  const nested = recordValue(value?.error);
  const status = Number(value?.status ?? nested.status ?? 0);
  const code = canonicalKind(textValue(value?.code ?? nested.code));
  const message = textValue(value?.message ?? nested.message).toLowerCase();
  const specificallyMissing = /attachment (?:was |is )?not found/.test(message);
  return code === 'attachment_not_found' || specificallyMissing && (status === 0 || status === 404);
}

function eventText(payload: Record<string, unknown>, item: Record<string, unknown>): string {
  return visibleText(item.text ?? item.content ?? item.delta ?? payload.text ?? payload.content ?? payload.delta ?? payload.message);
}

function formatByteSize(value: number): string {
  if (!value) return '';
  if (value < 1_024) return `${value} B`;
  if (value < 1_048_576) return `${Math.round(value / 1_024)} KB`;
  return `${(value / 1_048_576).toFixed(value < 10_485_760 ? 1 : 0)} MB`;
}

function attachmentReadUrl(attachment: ConversationAttachment): string {
  const fallback = `/api/conversations/${encodeURIComponent(attachment.conversationId)}/attachments/${encodeURIComponent(attachment.attachmentId)}`;
  if (!attachment.url) return fallback;
  try {
    const candidate = new URL(attachment.url, location.href);
    if (candidate.origin === location.origin) return `${candidate.pathname}${candidate.search}${candidate.hash}`;
  } catch { /* Fall back to the authenticated same-origin route. */ }
  return fallback;
}

function attachmentCandidates(
  event: ConversationEvent,
  payload: Record<string, unknown>,
  item: Record<string, unknown>,
  known: Map<string, ConversationAttachment>,
): ConversationAttachment[] {
  const entries: unknown[] = [];
  for (const value of [item.attachments, payload.attachments]) {
    if (Array.isArray(value)) entries.push(...value);
  }
  for (const value of [item.attachment_ids, item.attachmentIds, payload.attachment_ids, payload.attachmentIds]) {
    if (Array.isArray(value)) entries.push(...value);
  }
  const result = new Map<string, ConversationAttachment>();
  for (const entry of entries) {
    const id = typeof entry === 'string' ? entry : textValue(recordValue(entry).attachment_id ?? recordValue(entry).attachmentId ?? recordValue(entry).id);
    const normalized = known.get(id) ?? normalizeAttachment(
      typeof entry === 'string' ? { attachment_id: entry, conversation_id: event.conversationId } : { ...recordValue(entry), conversation_id: textValue(recordValue(entry).conversation_id, event.conversationId) },
      event.conversationId,
    );
    if (normalized) result.set(normalized.attachmentId, normalized);
  }
  for (const attachment of known.values()) {
    const rawEventId = textValue(attachment.raw.event_id ?? attachment.raw.eventId);
    const rawTurnId = textValue(attachment.raw.turn_id ?? attachment.raw.turnId);
    if (rawEventId && rawEventId === event.eventId || rawTurnId && event.turnId && rawTurnId === event.turnId) {
      result.set(attachment.attachmentId, attachment);
    }
  }
  return [...result.values()];
}

function renderSentAttachments(
  parent: HTMLElement,
  attachments: ConversationAttachment[],
  reusableImages?: Map<string, HTMLElement[]>,
): void {
  const currentImages = new Map<string, HTMLElement[]>();
  for (const image of parent.querySelectorAll<HTMLElement>('.cw-sent-image[data-attachment-id]')) {
    const id = image.dataset.attachmentId;
    if (!id) continue;
    const entries = currentImages.get(id) ?? [];
    entries.push(image);
    currentImages.set(id, entries);
  }
  parent.replaceChildren();
  for (const attachment of attachments) {
    const url = attachmentReadUrl(attachment);
    if (attachment.kind === 'image') {
      const reusable = currentImages.get(attachment.attachmentId)?.shift()
        ?? reusableImages?.get(attachment.attachmentId)?.shift();
      if (reusable) {
        parent.append(reusable);
        continue;
      }
      const link = element('a', 'cw-sent-image');
      link.dataset.attachmentId = attachment.attachmentId;
      link.href = url;
      link.target = '_blank';
      link.rel = 'noopener';
      link.setAttribute('aria-label', `Open image ${attachment.displayName}`);
      const image = element('img');
      image.src = url;
      image.alt = attachment.displayName;
      image.loading = 'lazy';
      link.append(image);
      parent.append(link);
      continue;
    }
    const link = element('a', 'cw-sent-file');
    link.href = url;
    link.download = attachment.displayName || 'attachment';
    link.setAttribute('aria-label', `Download ${attachment.displayName || 'attachment'}`);
    appendText(link, 'span', attachment.kind === 'pasted_text' ? 'TXT' : 'FILE', 'cw-file-icon');
    const copy = element('span');
    appendText(copy, 'strong', attachment.displayName || 'Pasted text');
    const meta = [attachment.kind === 'pasted_text' ? 'Pasted text' : attachment.mediaType, formatByteSize(attachment.byteSize)].filter(Boolean).join(' · ');
    if (meta) appendText(copy, 'small', meta);
    link.append(copy);
    parent.append(link);
  }
  parent.hidden = attachments.length === 0;
}

function shortAgentPath(path: string): string {
  return path.split('/').filter(Boolean).at(-1)?.replace(/[_-]+/g, ' ') || 'Subagent';
}

function activityStatusLabel(value: string): string {
  const canonical = canonicalKind(value);
  const labels: Record<string, string> = {
    in_progress: 'Running', started: 'Started', interacted: 'Active', completed: 'Completed',
    failed: 'Failed', interrupted: 'Interrupted', pending: 'Pending', idle: 'Idle', working: 'Working',
  };
  return labels[canonical] ?? canonical.replace(/_/g, ' ').replace(/^./, (letter) => letter.toUpperCase());
}

type ProviderActivity = { label: string; detail: string };

function providerActivity(
  itemKind: string,
  eventKind: string,
  payload: Record<string, unknown>,
  item: Record<string, unknown>,
): ProviderActivity | null {
  const combined = `${itemKind} ${eventKind}`;
  const toolName = textValue(item.tool ?? item.name ?? item.toolName ?? item.tool_name ?? payload.tool ?? payload.name ?? payload.toolName ?? payload.tool_name);
  const server = textValue(item.server ?? item.serverName ?? item.server_name ?? payload.server ?? payload.serverName ?? payload.server_name);
  const failureDetail = shortVisibleText(item.error ?? payload.error ?? item.failure ?? payload.failure);
  const detail = failureDetail || shortVisibleText(
    item.query ?? payload.query
      ?? item.prompt ?? payload.prompt
      ?? item.revisedPrompt ?? item.revised_prompt ?? payload.revisedPrompt ?? payload.revised_prompt
      ?? item.path ?? payload.path
      ?? item.url ?? payload.url
      ?? item.review ?? payload.review
      ?? item.message ?? payload.message
      ?? item.arguments ?? payload.arguments
      ?? item.input ?? payload.input,
  );
  if (combined.includes('mcp_tool_call') || combined.includes('mcp_tool') || combined.includes('mcp_progress')) {
    const target = [server, toolName].filter(Boolean).join(' · ');
    return { label: target ? `MCP · ${target}` : 'Using an MCP tool', detail };
  }
  if (combined.includes('dynamic_tool_call') || combined.includes('dynamic_tool')) {
    return { label: toolName ? `Tool · ${toolName}` : 'Using a tool', detail };
  }
  if (combined.includes('web_search')) return { label: 'Searching the web', detail };
  if (combined.includes('image_view')) return { label: 'Viewing an image', detail };
  if (combined.includes('image_generation') || combined.includes('generate_image')) return { label: 'Generating an image', detail };
  if (combined.includes('entered_review_mode') || combined.includes('enter_review_mode')) return { label: 'Entered review mode', detail };
  if (combined.includes('exited_review_mode') || combined.includes('exit_review_mode')) return { label: 'Exited review mode', detail };
  if (combined.includes('context_compaction') || /(^|_)compaction(_|$)/.test(combined)) return { label: 'Compacting conversation context', detail };
  if (combined.includes('function_call') || combined.includes('custom_tool_call')) {
    return { label: toolName ? `Tool · ${toolName}` : 'Using a tool', detail };
  }
  return null;
}

function lifecycleStatus(kind: string, payload: Record<string, unknown>, item: Record<string, unknown>): string {
  const explicit = textValue(item.status ?? payload.status ?? item.state ?? payload.state);
  if (explicit) return activityStatusLabel(explicit);
  if (item.error || payload.error || item.failure || payload.failure || item.success === false || payload.success === false) return 'Failed';
  if (item.success === true || payload.success === true) return 'Completed';
  if (kind.includes('failed') || kind.includes('error')) return 'Failed';
  if (kind.includes('completed') || kind.includes('finished')) return 'Completed';
  if (kind.includes('started') || kind.includes('delta') || kind.includes('progress')) return 'Running';
  return 'Active';
}

function collabToolLabel(value: string): string {
  const labels: Record<string, string> = {
    spawnAgent: 'Started an agent', sendInput: 'Sent agent input', resumeAgent: 'Resumed an agent',
    wait: 'Waiting for agents', closeAgent: 'Closed an agent', sendMessage: 'Messaged an agent',
    followupTask: 'Assigned follow-up work', interruptAgent: 'Interrupted an agent', listAgents: 'Checked agent activity',
  };
  return labels[value] ?? value.replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/^./, (letter) => letter.toUpperCase());
}

function hiddenOperationalEvent(kind: string): boolean {
  return kind === 'conversation_state'
    || kind === 'protocol_notification'
    || kind === 'thread_started'
    || kind === 'thread_status'
    || kind === 'thread_name'
    || kind === 'thread_archived'
    || kind === 'thread_settings_updated'
    || kind === 'turn_started'
    || kind === 'turn_completed'
    || kind === 'item_started'
    || kind === 'item_completed'
    || kind.startsWith('item_started_')
    || kind.startsWith('server_request_');
}

function hiddenRawProviderEvent(event: ConversationEvent): boolean {
  const payload = recordValue(event.payload);
  const method = canonicalKind(textValue(payload.method ?? recordValue(payload.params).method));
  const eventKind = canonicalKind(event.kind);
  const protocolKind = method || eventKind;
  if (protocolKind.startsWith('raw_response')) return true;
  return protocolKind.includes('item_reasoning_text')
    || protocolKind.includes('reasoning_raw')
    || protocolKind.includes('reasoning_content');
}

function terminalWorkLabel(event: ConversationEvent): string | null {
  if (!event.turnId) return null;
  const kind = canonicalKind(event.kind);
  const payload = recordValue(event.payload);
  const turn = recordValue(payload.turn);
  const explicit = canonicalKind(textValue(turn.status ?? payload.status ?? payload.state));
  if (explicit === 'failed') return 'Work details · Failed';
  if (explicit === 'interrupted' || explicit === 'cancelled' || explicit === 'canceled') return 'Work details · Stopped';
  if (explicit === 'completed') return 'Work details';
  if (kind.includes('turn_failed') || kind === 'protocol_error' && payload.willRetry === false) return 'Work details · Failed';
  if (kind.includes('interrupt') || kind.includes('cancel')) return 'Work details · Stopped';
  if (kind.includes('turn_completed') || kind.includes('turn_complete')) return 'Work details';
  return null;
}

function reasoningEffortLabel(value: string): string {
  const labels: Record<string, string> = {
    none: 'None', minimal: 'Minimal', low: 'Low', medium: 'Medium', high: 'High',
    xhigh: 'Extra high', max: 'Max', ultra: 'Ultra',
  };
  return labels[value.toLowerCase()] ?? value.replace(/[_-]+/g, ' ').replace(/^./, (letter) => letter.toUpperCase());
}

function operationalStatus(value: string): string {
  return canonicalKind(value);
}

function turnCanInterrupt(status: string): boolean {
  return ['starting', 'running', 'in_progress', 'waiting_approval', 'waiting_input'].includes(operationalStatus(status));
}

function liveWorkLabel(status: string, stopping: boolean, sending: boolean): string {
  if (stopping) return 'Stopping…';
  const canonical = operationalStatus(status);
  if (canonical === 'waiting_approval') return 'Waiting for approval';
  if (canonical === 'waiting_input') return 'Waiting for input';
  return sending && !turnCanInterrupt(status) ? 'Preparing…' : 'Working…';
}

function catalogDefaultModel(catalog: ConversationModelCatalog): ConversationModel | null {
  return catalog.models.find((model) => model.id === catalog.defaultModel)
    ?? catalog.models.find((model) => model.isDefault)
    ?? catalog.models[0]
    ?? null;
}

function modelDefaultEffort(catalog: ConversationModelCatalog, model: ConversationModel): string {
  const supported = new Set(model.supportedReasoningEfforts.map((option) => option.reasoningEffort));
  const candidates = [
    model.id === catalog.defaultModel || model.isDefault ? catalog.defaultReasoningEffort : '',
    model.defaultReasoningEffort,
    catalog.defaultReasoningEffort,
    model.supportedReasoningEfforts[0]?.reasoningEffort ?? '',
  ];
  return candidates.find((effort) => effort && supported.has(effort)) ?? '';
}

type AgentMessagePhase = 'commentary' | 'final_answer' | 'unknown';

function agentMessagePhase(payload: Record<string, unknown>, item: Record<string, unknown>): AgentMessagePhase {
  const phase = canonicalKind(textValue(item.phase ?? payload.phase));
  if (phase === 'commentary') return 'commentary';
  if (phase === 'final_answer') return 'final_answer';
  return 'unknown';
}

function activityNodes(
  events: ConversationEvent[],
  attachments: ConversationAttachment[],
  reusableImages?: Map<string, HTMLElement[]>,
  liveWork?: LiveWorkState,
): HTMLElement[] {
  const nodes: HTMLElement[] = [];
  type MessageNode = {
    role: 'user' | 'agent';
    turnId: string | null;
    itemId: string;
    phase: AgentMessagePhase;
    source: string;
    completed: boolean;
    label: HTMLElement;
    content: HTMLElement;
    attachments: Map<string, ConversationAttachment>;
    attachmentList: HTMLElement;
    openingContext: string;
    openingContextDetails: HTMLDetailsElement;
    referenceList: HTMLElement;
    references: ConversationReference[];
    node: HTMLElement;
  };
  type CommentaryGroup = { node: HTMLDetailsElement; body: HTMLElement; summary: HTMLElement };
  type DetailsWorkNode = {
    node: HTMLDetailsElement;
    summary: HTMLElement;
    meta: HTMLElement;
    output: HTMLElement;
    outputText: string;
    title: string;
    status: string;
    exitCode: string;
  };
  type PlanWorkNode = {
    node: HTMLElement;
    body: HTMLElement;
    steps: unknown[] | null;
    fallback: string;
  };
  type ReasoningWorkNode = {
    node: HTMLElement;
    content: HTMLElement;
    parts: Map<string, string>;
    source: string;
    completed: boolean;
  };
  type CompactWorkNode = {
    node: HTMLElement;
    dot: HTMLElement;
    heading: HTMLElement;
    status: HTMLElement;
    detail: HTMLElement;
  };
  let merge: MessageNode | null = null;
  const messagesByItemId = new Map<string, MessageNode>();
  const messageNodes = new Set<MessageNode>();
  const commentaryGroups = new Map<string, CommentaryGroup>();
  const agentActivityNodes = new Map<string, HTMLElement>();
  const commandActivityNodes = new Map<string, DetailsWorkNode>();
  const fileActivityNodes = new Map<string, DetailsWorkNode>();
  const planActivityNodes = new Map<string, PlanWorkNode>();
  const reasoningActivityNodes = new Map<string, ReasoningWorkNode>();
  const providerActivityNodes = new Map<string, CompactWorkNode>();
  const turnsWithFinalAnswer = new Set<string>();
  const terminalTurns = new Map<string, string>();
  const providerEchoItemIds = new Set<string>();
  let pendingLocalUserMessage: MessageNode | null = null;
  const knownAttachments = new Map(attachments.map((attachment) => [attachment.attachmentId, attachment]));

  const turnKey = (turnId: string | null): string => turnId ?? '__without_turn__';

  const presentCommentarySummary = (
    group: CommentaryGroup,
    label: string,
    active = false,
    startedAt: number | null = null,
  ): void => {
    group.summary.replaceChildren();
    if (active) {
      const dot = element('span', 'cw-working-dot');
      dot.setAttribute('aria-hidden', 'true');
      appendText(group.summary, 'span', label, 'cw-working-label');
      group.summary.prepend(dot);
      if (startedAt !== null) {
        const elapsed = appendText(group.summary, 'time', '', 'cw-work-elapsed');
        elapsed.dataset.startedAt = String(startedAt);
      }
      return;
    }
    group.summary.textContent = label;
  };

  const commentaryGroup = (turnId: string | null): CommentaryGroup => {
    const key = turnKey(turnId);
    const existing = commentaryGroups.get(key);
    if (existing) return existing;
    const node = element('details', 'cw-commentary-group');
    node.dataset.activityKey = `commentary:${key}`;
    const terminalLabel = terminalTurns.get(key);
    const summary = element('summary');
    node.append(summary);
    const body = element('div', 'cw-commentary-items');
    node.append(body);
    node.open = !turnsWithFinalAnswer.has(key) && !terminalLabel;
    const group = { node, body, summary };
    presentCommentarySummary(group, turnsWithFinalAnswer.has(key) ? 'Work details' : terminalLabel ?? 'Working…');
    commentaryGroups.set(key, group);
    nodes.push(node);
    return group;
  };

  const markFinalAnswer = (turnId: string | null): void => {
    const key = turnKey(turnId);
    turnsWithFinalAnswer.add(key);
    const group = commentaryGroups.get(key);
    if (!group) return;
    presentCommentarySummary(group, 'Work details');
    group.node.open = false;
  };

  const markTerminalTurn = (turnId: string | null, label: string): void => {
    const key = turnKey(turnId);
    if (turnsWithFinalAnswer.has(key)) return;
    terminalTurns.set(key, label);
    const group = commentaryGroups.get(key) ?? (label === 'Work details' ? null : commentaryGroup(turnId));
    if (!group) return;
    presentCommentarySummary(group, label);
    group.node.open = false;
  };

  const placeMessage = (message: MessageNode): void => {
    const topLevelIndex = nodes.indexOf(message.node);
    if (message.role === 'agent' && message.phase === 'commentary') {
      if (topLevelIndex >= 0) nodes.splice(topLevelIndex, 1);
      commentaryGroup(message.turnId).body.append(message.node);
      return;
    }
    if (message.node.parentElement?.classList.contains('cw-commentary-items')) message.node.remove();
    if (topLevelIndex < 0) nodes.push(message.node);
  };

  const presentMessage = (message: MessageNode): void => {
    message.node.classList.toggle('cw-commentary', message.role === 'agent' && message.phase === 'commentary');
    message.node.classList.toggle('cw-final-answer', message.role === 'agent' && message.phase === 'final_answer');
    message.label.textContent = message.role === 'user'
      ? 'YOU'
      : message.phase === 'commentary'
        ? 'UPDATE'
        : message.phase === 'final_answer'
          ? 'ANSWER'
          : 'AGENT';
    message.node.hidden = !message.source && message.attachments.size === 0 && !message.openingContext && message.references.length === 0;
    renderSentAttachments(message.attachmentList, [...message.attachments.values()], reusableImages);
    message.openingContextDetails.hidden = !message.openingContext;
    const contextBody = message.openingContextDetails.querySelector('pre');
    if (contextBody) contextBody.textContent = message.openingContext;
    message.referenceList.replaceChildren(...message.references.map((reference) => {
      const chip = element('span', 'cw-reference-chip cw-sent-reference');
      chip.textContent = `Pursuit · ${reference.title || reference.id}`;
      chip.title = reference.title && reference.title !== reference.id ? reference.id : '';
      return chip;
    }));
    message.referenceList.hidden = message.references.length === 0;
  };

  const createMessage = (
    role: 'user' | 'agent',
    turnId: string | null,
    itemId: string,
    phase: AgentMessagePhase,
    source: string,
    completed: boolean,
    attachments: ConversationAttachment[],
    activityKey: string,
    openingContext = '',
    references: ConversationReference[] = [],
  ): MessageNode => {
    const node = element('article', `cw-message cw-${role}`);
    node.dataset.activityKey = activityKey;
    const label = appendText(node, 'small', '');
    const content = element('div', 'cw-message-text');
    const openingContextDetails = element('details', 'cw-opening-context');
    openingContextDetails.open = false;
    const openingContextSummary = element('summary');
    openingContextSummary.textContent = 'Opening context';
    const openingContextBody = element('pre');
    openingContextDetails.append(openingContextSummary, openingContextBody);
    const referenceList = element('div', 'cw-sent-references');
    const attachmentList = element('div', 'cw-sent-attachments');
    node.append(content, openingContextDetails, referenceList, attachmentList);
    const message = {
      role, turnId, itemId, phase, source, completed, label, content,
      attachments: new Map(attachments.map((attachment) => [attachment.attachmentId, attachment])),
      attachmentList, openingContext, openingContextDetails, referenceList, references,
      node,
    };
    messageNodes.add(message);
    presentMessage(message);
    placeMessage(message);
    if (role === 'agent' && phase === 'final_answer' && completed) markFinalAnswer(turnId);
    return message;
  };

  const updateMessage = (
    message: MessageNode,
    text: string,
    delta: boolean,
    phase: AgentMessagePhase,
    completed: boolean,
    attachments: ConversationAttachment[],
    openingContext = '',
    references: ConversationReference[] = [],
  ): void => {
    const previousPhase = message.phase;
    if (message.role === 'agent' && phase !== 'unknown') message.phase = phase;
    if (completed) message.completed = true;
    if (text) message.source = delta ? `${message.source}${text}` : text;
    for (const attachment of attachments) message.attachments.set(attachment.attachmentId, attachment);
    if (openingContext) message.openingContext = openingContext;
    if (references.length) message.references = references;
    presentMessage(message);
    if (message.phase !== previousPhase) placeMessage(message);
    if (message.role === 'agent' && message.phase === 'final_answer' && completed) markFinalAnswer(message.turnId);
  };

  const mergeLifecycleOutput = (current: string, next: string, delta: boolean, terminal: boolean): string => {
    if (!next) return current;
    if (terminal || !current) return next;
    if (next === current || current.endsWith(next)) return current;
    if (!delta && next.startsWith(current)) return next;
    const separator = current.endsWith('\n') || next.startsWith('\n') || delta ? '' : '\n';
    return `${current}${separator}${next}`;
  };

  const presentDetailsWork = (work: DetailsWorkNode, showExit: boolean): void => {
    work.summary.textContent = work.title;
    const meta = [work.status, showExit && work.exitCode ? `exit ${work.exitCode}` : ''].filter(Boolean).join(' · ');
    work.meta.textContent = meta;
    work.meta.hidden = !meta;
    work.output.textContent = work.outputText;
    work.output.hidden = !work.outputText;
  };

  for (const event of events) {
    if (hiddenRawProviderEvent(event)) continue;
    const terminalLabel = terminalWorkLabel(event);
    if (terminalLabel) markTerminalTurn(event.turnId, terminalLabel);
    const { payload, item, itemId, kind } = payloadParts(event);
    const roleValue = textValue(item.role ?? payload.role).toLowerCase();
    const isUser = roleValue === 'user' || kind.includes('user_message');
    const isAgent = roleValue === 'assistant' || roleValue === 'agent' || kind.includes('agent_message') || kind.includes('assistant_message') || kind.includes('agent_delta');
    if (isUser || isAgent) {
      const role = isUser ? 'user' : 'agent';
      if (role === 'agent') pendingLocalUserMessage = null;
      const localUserEvent = role === 'user' && canonicalKind(event.kind) === 'user_message';
      const text = eventText(payload, item);
      const openingContext = localUserEvent ? textValue(payload.opening_context) : '';
      const references = localUserEvent ? messageReferences(payload.references) : [];
      const delta = kind.includes('delta');
      const completed = !delta && kind.includes('completed');
      const phase = role === 'agent' ? agentMessagePhase(payload, item) : 'unknown';
      const messageAttachments = role === 'user' ? attachmentCandidates(event, payload, item, knownAttachments) : [];
      const identified = itemId ? messagesByItemId.get(itemId) : null;
      const pairedStructuredEcho = pendingLocalUserMessage
        ? isPairedProviderUserEcho(text, pendingLocalUserMessage.source, pendingLocalUserMessage.openingContext)
        : false;
      if (role === 'user' && !localUserEvent && pendingLocalUserMessage && pairedStructuredEcho) {
        pendingLocalUserMessage.turnId = event.turnId ?? pendingLocalUserMessage.turnId;
        if (completed) pendingLocalUserMessage.completed = true;
        if (itemId) {
          pendingLocalUserMessage.itemId = itemId;
          messagesByItemId.set(itemId, pendingLocalUserMessage);
          providerEchoItemIds.add(itemId);
        }
        for (const attachment of messageAttachments) pendingLocalUserMessage.attachments.set(attachment.attachmentId, attachment);
        presentMessage(pendingLocalUserMessage);
        merge = pendingLocalUserMessage;
        pendingLocalUserMessage = null;
        continue;
      }
      if (role === 'user' && !localUserEvent) pendingLocalUserMessage = null;
      if (identified && identified.role === role) {
        updateMessage(
          identified,
          role === 'user' && providerEchoItemIds.has(itemId) ? '' : text,
          delta,
          phase,
          completed,
          messageAttachments,
          openingContext,
          references,
        );
        merge = identified;
        if (localUserEvent) pendingLocalUserMessage = identified;
        continue;
      }
      if (merge && role === 'user' && merge.role === role && text && text === merge.source && (!merge.turnId || !event.turnId)) {
        merge.turnId = event.turnId ?? merge.turnId;
        if (completed) merge.completed = true;
        if (itemId) {
          merge.itemId = itemId;
          messagesByItemId.set(itemId, merge);
        }
        for (const attachment of messageAttachments) merge.attachments.set(attachment.attachmentId, attachment);
        presentMessage(merge);
        if (localUserEvent) pendingLocalUserMessage = merge;
        continue;
      }
      if (itemId) {
        merge = createMessage(role, event.turnId, itemId, phase, text, completed, messageAttachments, `message:${itemId || event.eventId}`, openingContext, references);
        messagesByItemId.set(itemId, merge);
        if (localUserEvent) pendingLocalUserMessage = merge;
        continue;
      }
      const phaseCompatible = role !== 'agent' || phase === 'unknown' || merge?.phase === 'unknown' || merge?.phase === phase;
      if (merge && merge.role === role && merge.turnId === event.turnId && phaseCompatible && delta) {
        updateMessage(merge, text, true, phase, false, messageAttachments, openingContext, references);
        if (localUserEvent) pendingLocalUserMessage = merge;
        continue;
      }
      if (merge && merge.role === role && merge.turnId === event.turnId && phaseCompatible && !delta && text && text.startsWith(merge.source)) {
        updateMessage(merge, text, false, phase, completed, messageAttachments, openingContext, references);
        if (localUserEvent) pendingLocalUserMessage = merge;
        continue;
      }
      if (!text && !messageAttachments.length && !openingContext && !references.length) continue;
      merge = createMessage(role, event.turnId, '', phase, text, completed, messageAttachments, `message:${event.eventId}`, openingContext, references);
      if (localUserEvent) pendingLocalUserMessage = merge;
      continue;
    }
    const itemType = textValue(item.type ?? item.kind ?? payload.type ?? payload.kind);
    const itemKind = canonicalKind(itemType);
    const reasoningEvent = itemKind === 'reasoning' || kind.includes('reasoning_summary');
    if (reasoningEvent) {
      merge = null;
      const summaryIndex = textValue(payload.summaryIndex ?? payload.summary_index, '0');
      const reasoningId = itemId || textValue(item.id ?? payload.id) || `${event.turnId ?? 'turn'}:${summaryIndex}`;
      const key = `reasoning:${turnKey(event.turnId)}:${reasoningId}`;
      const summaryValue = item.summary ?? payload.summary;
      const completedParts = Array.isArray(summaryValue)
        ? summaryValue.map((part) => visibleText(part)).filter(Boolean)
        : [];
      const completedSummary = completedParts.length ? completedParts.join('\n\n') : visibleText(summaryValue);
      const delta = kind.includes('summary_delta') ? visibleText(payload.delta) : '';
      const nextText = completedSummary || delta;
      let work = reasoningActivityNodes.get(key);
      if (!work && nextText) {
        const node = element('article', 'cw-reasoning-summary');
        node.dataset.activityKey = key;
        appendText(node, 'small', 'REASONING SUMMARY');
        const content = element('div', 'cw-message-text');
        node.append(content);
        work = { node, content, parts: new Map(), source: '', completed: false };
        reasoningActivityNodes.set(key, work);
        commentaryGroup(event.turnId).body.append(node);
      }
      if (work) {
        if (completedSummary) {
          work.parts = new Map(completedParts.length
            ? completedParts.map((part, index) => [String(index), part])
            : [['0', completedSummary]]);
          work.source = completedSummary;
          work.completed = true;
        } else if (delta) {
          const currentPart = work.parts.get(summaryIndex) ?? '';
          work.parts.set(summaryIndex, mergeLifecycleOutput(currentPart, delta, true, false));
          work.source = [...work.parts.entries()]
            .sort(([left], [right]) => Number(left) - Number(right))
            .map(([, part]) => part)
            .filter(Boolean)
            .join('\n\n');
        }
      }
      continue;
    }
    if (itemKind === 'sub_agent_activity') {
      merge = null;
      const agentThreadId = textValue(item.agentThreadId ?? item.agent_thread_id ?? payload.agentThreadId ?? payload.agent_thread_id);
      const agentPath = textValue(item.agentPath ?? item.agent_path ?? payload.agentPath ?? payload.agent_path);
      const key = `subagent:${turnKey(event.turnId)}:${agentThreadId || agentPath || itemId || event.eventId}`;
      let node = agentActivityNodes.get(key);
      if (!node) {
        node = element('article', 'cw-agent-activity cw-subagent-activity');
        node.dataset.activityKey = key;
        node.append(element('span', 'cw-agent-activity-dot'), element('div', 'cw-agent-activity-copy'));
        agentActivityNodes.set(key, node);
        commentaryGroup(event.turnId).body.append(node);
      }
      const copy = node.querySelector<HTMLElement>('.cw-agent-activity-copy')!;
      copy.replaceChildren();
      const status = textValue(item.kind ?? payload.kind, 'active');
      const heading = element('div', 'cw-agent-activity-heading');
      appendText(heading, 'strong', shortAgentPath(agentPath));
      appendText(heading, 'span', activityStatusLabel(status), `cw-agent-status cw-${canonicalKind(status)}`);
      copy.append(heading);
      if (agentPath) appendText(copy, 'small', agentPath);
      continue;
    }
    if (itemKind === 'collab_agent_tool_call') {
      merge = null;
      const key = `collab:${turnKey(event.turnId)}:${itemId || textValue(item.id ?? payload.id) || event.eventId}`;
      let node = agentActivityNodes.get(key);
      if (!node) {
        node = element('article', 'cw-agent-activity cw-collab-activity');
        node.dataset.activityKey = key;
        node.append(element('span', 'cw-agent-activity-dot'), element('div', 'cw-agent-activity-copy'));
        agentActivityNodes.set(key, node);
        commentaryGroup(event.turnId).body.append(node);
      }
      const copy = node.querySelector<HTMLElement>('.cw-agent-activity-copy')!;
      copy.replaceChildren();
      const tool = textValue(item.tool ?? payload.tool ?? item.name ?? payload.name, 'agent activity');
      const status = textValue(item.status ?? payload.status, 'inProgress');
      const heading = element('div', 'cw-agent-activity-heading');
      appendText(heading, 'strong', collabToolLabel(tool));
      appendText(heading, 'span', activityStatusLabel(status), `cw-agent-status cw-${canonicalKind(status)}`);
      copy.append(heading);
      const prompt = visibleText(item.prompt ?? payload.prompt);
      if (prompt) appendText(copy, 'p', prompt.length > 180 ? `${prompt.slice(0, 177)}…` : prompt);
      const receiverValue = item.receiverThreadIds ?? item.receiver_thread_ids ?? payload.receiverThreadIds ?? payload.receiver_thread_ids;
      const receiverIds: unknown[] = Array.isArray(receiverValue) ? receiverValue : [];
      const model = textValue(item.model ?? payload.model);
      const effort = textValue(item.reasoningEffort ?? item.reasoning_effort ?? payload.reasoningEffort ?? payload.reasoning_effort);
      const meta = [
        receiverIds.length ? `${receiverIds.length} agent${receiverIds.length === 1 ? '' : 's'}` : '',
        model,
        effort ? `${reasoningEffortLabel(effort)} reasoning` : '',
      ].filter(Boolean).join(' · ');
      if (meta) appendText(copy, 'small', meta);
      const statesValue = item.agentsStates ?? item.agents_states ?? payload.agentsStates ?? payload.agents_states;
      const states = Array.isArray(statesValue)
        ? statesValue.map((value, index) => [String(index + 1), value] as const)
        : Object.entries(recordValue(statesValue));
      if (states.length) {
        const stateList = element('ul', 'cw-agent-states');
        for (const [threadId, value] of states) {
          const state = recordValue(value);
          const label = textValue(state.nickname ?? state.agentPath ?? state.agent_path ?? state.path ?? state.threadId ?? state.thread_id, threadId || 'Agent');
          const stateStatus = activityStatusLabel(textValue(state.status ?? state.state, 'active'));
          const message = visibleText(state.message);
          appendText(stateList, 'li', `${shortAgentPath(label)} · ${stateStatus}${message ? ` — ${message}` : ''}`);
        }
        copy.append(stateList);
      }
      continue;
    }
    const lifecycleKey = `${turnKey(event.turnId)}:${itemId || event.eventId}`;
    const commandEvent = itemKind.includes('command_execution') || /(^|_)(command|exec)(_|$)/.test(kind);
    if (commandEvent) {
      merge = null;
      const key = `command:${lifecycleKey}`;
      let work = commandActivityNodes.get(key);
      if (!work) {
        const node = element('details', 'cw-work-card cw-command');
        node.dataset.activityKey = key;
        const summary = appendText(node, 'summary', 'Command');
        const meta = appendText(node, 'small', '');
        const output = appendText(node, 'pre', '');
        work = { node, summary, meta, output, outputText: '', title: 'Command', status: '', exitCode: '' };
        commandActivityNodes.set(key, work);
        commentaryGroup(event.turnId).body.append(node);
      }
      const command = visibleText(item.command ?? item.cmd ?? payload.command ?? payload.cmd ?? item.argv ?? payload.argv);
      if (command) work.title = command;
      const status = textValue(item.status ?? payload.status);
      if (status) work.status = activityStatusLabel(status);
      const exitCode = item.exit_code ?? item.exitCode ?? payload.exit_code ?? payload.exitCode;
      if (exitCode !== undefined && exitCode !== null) work.exitCode = textValue(exitCode);
      const aggregate = visibleText(item.aggregated_output ?? item.aggregatedOutput ?? payload.aggregated_output ?? payload.aggregatedOutput);
      const delta = visibleText(item.delta ?? payload.delta);
      const ordinary = visibleText(item.output ?? item.stdout ?? item.stderr ?? payload.output ?? payload.stdout ?? payload.stderr);
      const nextOutput = aggregate || delta || ordinary;
      const terminal = Boolean(aggregate) || kind.includes('completed') && Boolean(nextOutput);
      const incremental = Boolean(delta) || !aggregate && !kind.includes('completed') && kind.includes('output');
      work.outputText = mergeLifecycleOutput(work.outputText, nextOutput, incremental, terminal);
      presentDetailsWork(work, true);
      continue;
    }
    const fileEvent = itemKind.includes('file_change') || /(^|_)(file|diff|patch)(_|$)/.test(kind);
    if (fileEvent) {
      merge = null;
      const key = `file:${lifecycleKey}`;
      let work = fileActivityNodes.get(key);
      if (!work) {
        const node = element('details', 'cw-work-card cw-file');
        node.dataset.activityKey = key;
        const summary = appendText(node, 'summary', 'File change');
        const meta = appendText(node, 'small', '');
        const output = appendText(node, 'pre', '');
        work = { node, summary, meta, output, outputText: '', title: 'File change', status: '', exitCode: '' };
        fileActivityNodes.set(key, work);
        commentaryGroup(event.turnId).body.append(node);
      }
      const path = textValue(item.path ?? item.file_path ?? payload.path ?? payload.file_path);
      if (path) work.title = path;
      const status = textValue(item.status ?? payload.status);
      if (status) work.status = activityStatusLabel(status);
      const completedChange = visibleText(item.diff ?? item.patch ?? item.changes ?? payload.diff ?? payload.patch ?? payload.changes);
      const delta = visibleText(item.delta ?? payload.delta);
      const nextOutput = completedChange || delta;
      const terminal = Boolean(completedChange) && kind.includes('completed');
      work.outputText = mergeLifecycleOutput(work.outputText, nextOutput, Boolean(delta), terminal);
      presentDetailsWork(work, false);
      continue;
    }
    const planEvent = itemKind === 'plan' || itemKind.endsWith('_plan') || /(^|_)plan(_|$)/.test(kind);
    if (planEvent) {
      merge = null;
      const key = `plan:${turnKey(event.turnId)}:${itemId || 'turn-plan'}`;
      let work = planActivityNodes.get(key);
      if (!work) {
        const node = element('section', 'cw-work-card cw-plan');
        node.dataset.activityKey = key;
        appendText(node, 'strong', 'Plan');
        const body = element('div');
        node.append(body);
        work = { node, body, steps: null, fallback: '' };
        planActivityNodes.set(key, work);
        commentaryGroup(event.turnId).body.append(node);
      }
      const deltaRecord = recordValue(item.delta ?? payload.delta);
      const stepCandidates = [item.steps, item.plan, payload.steps, payload.plan, deltaRecord.steps, deltaRecord.plan];
      const steps = stepCandidates.find((candidate) => Array.isArray(candidate));
      if (Array.isArray(steps)) work.steps = steps;
      const fallback = visibleText(item.delta ?? payload.delta ?? item.text ?? payload.text);
      if (!steps && fallback) work.fallback = mergeLifecycleOutput(work.fallback, fallback, kind.includes('delta'), kind.includes('completed'));
      work.body.replaceChildren();
      if (work.steps) {
        const list = element('ol');
        for (const step of work.steps) {
          const record = recordValue(step);
          const label = visibleText(record.step ?? record.text ?? record.label ?? step);
          appendText(list, 'li', [label, textValue(record.status)].filter(Boolean).join(' — '));
        }
        work.body.append(list);
      } else if (work.fallback) appendText(work.body, 'pre', work.fallback);
      continue;
    }
    const provider = providerActivity(itemKind, kind, payload, item);
    if (provider) {
      merge = null;
      const key = `provider:${lifecycleKey}`;
      let work = providerActivityNodes.get(key);
      if (!work) {
        const node = element('article', 'cw-agent-activity cw-provider-activity');
        node.dataset.activityKey = key;
        const dot = element('span', 'cw-agent-activity-dot');
        const copy = element('div', 'cw-agent-activity-copy');
        const headingRow = element('div', 'cw-agent-activity-heading');
        const heading = appendText(headingRow, 'strong', provider.label);
        const status = appendText(headingRow, 'span', '', 'cw-agent-status');
        const detail = appendText(copy, 'p', '');
        copy.prepend(headingRow);
        node.append(dot, copy);
        work = { node, dot, heading, status, detail };
        providerActivityNodes.set(key, work);
        commentaryGroup(event.turnId).body.append(node);
      }
      if (!kind.includes('mcp_progress') || work.heading.textContent === 'Using an MCP tool') {
        work.heading.textContent = provider.label;
      }
      const status = lifecycleStatus(kind, payload, item);
      work.status.textContent = status;
      work.status.className = `cw-agent-status cw-${canonicalKind(status)}`;
      work.dot.classList.toggle('cw-active', ['running', 'active', 'in_progress', 'started'].includes(canonicalKind(status)));
      work.detail.textContent = provider.detail;
      work.detail.hidden = !provider.detail;
      const duration = Number(item.durationMs ?? item.duration_ms ?? payload.durationMs ?? payload.duration_ms ?? 0);
      let durationNode = work.node.querySelector<HTMLElement>('.cw-provider-duration');
      if (duration > 0) {
        if (!durationNode) durationNode = appendText(work.node.querySelector<HTMLElement>('.cw-agent-activity-copy')!, 'small', '', 'cw-provider-duration');
        durationNode.textContent = duration < 1_000 ? `${Math.round(duration)} ms` : `${(duration / 1_000).toFixed(1)} s`;
      } else if (durationNode) durationNode.remove();
      continue;
    }
    if (hiddenOperationalEvent(kind)) continue;
    merge = null;
    if (kind.includes('protocol_error') || kind.includes('connection_disconnected')) {
      const node = element('div', 'cw-system-message');
      node.dataset.activityKey = `system:${event.eventId}`;
      const message = kind.includes('connection_disconnected')
        ? 'Connection lost. Reconnect to continue.'
        : visibleText(payload.message ?? payload.error) || 'The conversation encountered a connection error.';
      appendText(node, 'span', message);
      nodes.push(node);
      continue;
    }
  }
  if (liveWork?.active) {
    const activeTurnId = [...events].reverse().find((event) => {
      if (!event.turnId) return false;
      const key = turnKey(event.turnId);
      return !terminalTurns.has(key) && !turnsWithFinalAnswer.has(key);
    })?.turnId ?? '__pending__';
    const group = commentaryGroup(activeTurnId);
    presentCommentarySummary(group, liveWork.label, true, liveWork.startedAt);
    group.node.classList.toggle('cw-stopping', liveWork.stopping);
    group.node.open = true;
  }
  const visibleMessages = [...messageNodes].filter((message) => message.source);
  const stableMessages = visibleMessages.filter((message) => message.completed);
  const cacheableMessages = new Set(stableMessages.slice(-RICH_TEXT_CACHE_LIMIT));
  for (const message of visibleMessages) {
    renderRichText(message.content, message.source, cacheableMessages.has(message));
  }
  for (const reasoning of reasoningActivityNodes.values()) {
    renderRichText(reasoning.content, reasoning.source, reasoning.completed);
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
  private activityFirstEventId: string | null = null;
  private state: ConversationState | null = null;
  private pickerPursuitId: string | null = null;
  private pickerTouched = false;
  private modelCatalogs = new Map<string, ConversationModelCatalog>();
  private requestedModelCatalogs = new Set<string>();
  private failedModelCatalogs = new Set<string>();
  private stagedAttachments = new Map<string, StagedAttachment[]>();
  private suppressedAttachmentIds = new Map<string, Set<string>>();
  private ephemeralDrafts = new Map<string, string>();
  private knownSideChatIds = new Set<string>();
  private attachmentSequence = 0;
  private attachmentUploadQueue: StagedAttachment[] = [];
  private attachmentUploadRunning = false;
  private workStartedAt = new Map<string, number>();
  private elapsedTimer: ReturnType<typeof setInterval> | null = null;
  private stoppingConversationIds = new Set<string>();
  private observedInterruptIds = new Set<string>();
  private stoppingBaselineErrors = new Map<string, string | null>();

  constructor(private host: HTMLElement, private rootKey: string, private actions: ConversationRendererActions) {
    host.className = 'cw-pane';
    host.innerHTML = shell;
    this.$('.cw-collapse').addEventListener('click', () => actions.toggleCollapsed(), { signal: this.abort.signal });
    this.$('.cw-manager-entry').addEventListener('click', () => actions.openManager(), { signal: this.abort.signal });
    this.$('.cw-error button').addEventListener('click', () => actions.retry(), { signal: this.abort.signal });
    this.$('.cw-back').addEventListener('click', () => actions.closeConversation(), { signal: this.abort.signal });
    this.$('.cw-reload').addEventListener('click', () => actions.reload(), { signal: this.abort.signal });
    this.$('.cw-load-earlier').addEventListener('click', () => {
      if (this.lastConversationId) actions.loadEarlier(this.lastConversationId);
    }, { signal: this.abort.signal });
    this.$('.cw-reconnect').addEventListener('click', () => {
      const conversation = this.state && currentConversation(this.state);
      if (conversation) actions.reconnect(conversation.conversationId);
    }, { signal: this.abort.signal });
    this.$('.cw-archive').addEventListener('click', () => actions.archive(), { signal: this.abort.signal });
    this.$<HTMLButtonElement>('.cw-send-stop').addEventListener('click', (event) => {
      const button = event.currentTarget as HTMLButtonElement;
      if (button.dataset.mode !== 'stop') return;
      event.preventDefault();
      const conversationId = this.lastConversationId;
      if (button.disabled || !conversationId || this.stoppingConversationIds.has(conversationId)) return;
      this.stoppingConversationIds.add(conversationId);
      this.stoppingBaselineErrors.set(conversationId, this.state?.error ?? null);
      if (this.state) this.render(this.state);
      actions.interrupt();
    }, { signal: this.abort.signal });
    this.$('.cw-unread').addEventListener('click', () => this.scrollToBottom(), { signal: this.abort.signal });
    this.$('.cw-activity').addEventListener('scroll', () => {
      if (!this.lastConversationId || !this.isFollowingActivity(this.lastConversationId)) return;
      this.$('.cw-unread').hidden = true;
      this.actions.acknowledgeRead(this.lastConversationId);
    }, { signal: this.abort.signal });
    const tabs = this.$('.cw-conversation-tabs');
    tabs.addEventListener('click', (event) => {
      const target = (event.target as Element | null)?.closest<HTMLButtonElement>('button');
      if (!target) return;
      const closeId = target.dataset.closeSideChat;
      const conversationId = target.dataset.conversationId;
      const parentId = target.dataset.newSideChat;
      if (closeId) this.requestCloseSideChat(closeId);
      else if (parentId) actions.createSideChat(parentId);
      else if (conversationId) actions.openConversation(conversationId);
    }, { signal: this.abort.signal });
    tabs.addEventListener('keydown', (event) => {
      if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return;
      const options = [...tabs.querySelectorAll<HTMLButtonElement>('[role="tab"]')];
      const current = (event.target as Element | null)?.closest<HTMLButtonElement>('[role="tab"]');
      const index = current ? options.indexOf(current) : -1;
      if (index < 0 || !options.length) return;
      event.preventDefault();
      const next = event.key === 'Home'
        ? 0
        : event.key === 'End'
          ? options.length - 1
          : (index + (event.key === 'ArrowRight' ? 1 : -1) + options.length) % options.length;
      options[next].focus();
      options[next].click();
    }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-new-form [name="host"]').addEventListener('change', () => {
      this.pickerTouched = true;
      this.retryModelCatalog(this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value);
      this.renderProjectOptions();
      this.renderNewConversationModelOptions();
    }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-new-form [name="project"]').addEventListener('change', () => { this.pickerTouched = true; }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-new-form [name="model"]').addEventListener('change', () => this.renderNewConversationEffortOptions(), { signal: this.abort.signal });
    this.$<HTMLFormElement>('.cw-new-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const hostId = this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value;
      const projectId = this.$<HTMLSelectElement>('.cw-new-form [name="project"]').value;
      const model = this.$<HTMLSelectElement>('.cw-new-form [name="model"]').value;
      const reasoningEffort = this.$<HTMLSelectElement>('.cw-new-form [name="effort"]').value;
      if (hostId && projectId && model && reasoningEffort) actions.createConversation(hostId, projectId, model, reasoningEffort);
    }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-manager-form [name="model"]').addEventListener('change', () => this.renderManagerEffortOptions(), { signal: this.abort.signal });
    this.$<HTMLFormElement>('.cw-manager-form').addEventListener('submit', (event) => {
      event.preventDefault();
      const model = this.$<HTMLSelectElement>('.cw-manager-form [name="model"]').value;
      const reasoningEffort = this.$<HTMLSelectElement>('.cw-manager-form [name="effort"]').value;
      if (model && reasoningEffort) actions.createManager(model, reasoningEffort);
    }, { signal: this.abort.signal });
    this.$('.cw-staged-references').addEventListener('click', (event) => {
      if ((event.target as Element | null)?.closest('[data-remove-manager-reference]')) actions.removeManagerReference();
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
    const composerInput = this.$<HTMLTextAreaElement>('.cw-composer textarea');
    composerInput.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing
        && this.$<HTMLButtonElement>('.cw-send-stop').dataset.mode === 'send') {
        event.preventDefault();
        this.submitComposer();
      }
    }, { signal: this.abort.signal });
    composerInput.addEventListener('input', () => { this.saveDraft(); this.updateComposerControls(); }, { signal: this.abort.signal });
    composerInput.addEventListener('paste', (event) => this.handleComposerPaste(event), { signal: this.abort.signal });
    const fileInput = this.$<HTMLInputElement>('.cw-file-input');
    this.$<HTMLButtonElement>('.cw-attach').addEventListener('click', () => fileInput.click(), { signal: this.abort.signal });
    fileInput.addEventListener('change', () => {
      const files = Array.from(fileInput.files ?? []);
      fileInput.value = '';
      this.handleSelectedFiles(files);
    }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-composer [name="model"]').addEventListener('change', () => {
      this.renderConversationEffortOptions();
      this.submitConversationSettings();
    }, { signal: this.abort.signal });
    this.$<HTMLSelectElement>('.cw-composer [name="effort"]').addEventListener('change', () => this.submitConversationSettings(), { signal: this.abort.signal });
  }

  private $<T extends HTMLElement = HTMLElement>(selector: string): T { return this.host.querySelector<T>(selector)!; }

  private requestCloseSideChat(conversationId: string): void {
    const conversation = this.state?.conversations.find((item) => item.conversationId === conversationId);
    if (!conversation || conversation.kind !== 'side_chat') return;
    if (!window.confirm(
      'Close this side chat? This permanently discards its temporary thread, messages, unsent draft, and staged attachments, including unsent work in another open tab. This cannot be undone.',
    )) return;
    this.actions.closeSideChat(conversationId);
  }

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
    for (const conversation of state.conversations) {
      if (conversation.kind === 'side_chat') this.knownSideChatIds.add(conversation.conversationId);
    }
    const current = currentConversation(state);
    const hasPursuit = !!state.selectedPursuitId;
    this.$('.cw-no-pursuit').hidden = state.managerOpen || hasPursuit || !!current;
    this.$('.cw-list-view').hidden = state.managerOpen || !hasPursuit || !!current;
    this.$('.cw-manager-view').hidden = !state.managerOpen || !!current;
    this.$('.cw-detail-view').hidden = !current;
    const managerEntry = this.$<HTMLButtonElement>('.cw-manager-entry');
    managerEntry.setAttribute('aria-pressed', String(state.managerOpen));
    managerEntry.classList.toggle('cw-active', state.managerOpen);
    this.renderError(state.error);
    this.renderConnection(state.connection);
    if (!state.managerOpen) this.renderPicker(state);
    this.renderList(state);
    if (state.managerOpen) this.renderManagerPicker(state);
    this.renderManagerList(state);
    this.renderTabs(state, current);
    this.renderDetail(state, current);
  }

  private renderTabs(state: ConversationState, conversation: ReturnType<typeof currentConversation>): void {
    const parent = conversation?.kind === 'side_chat'
      ? state.conversations.find((item) => item.conversationId === conversation.parentConversationId) ?? null
      : conversation;
    const tabs = this.$('.cw-conversation-tabs');
    const focused = tabs.contains(document.activeElement) ? document.activeElement as HTMLButtonElement : null;
    const focusKey = focused ? {
      conversationId: focused.dataset.conversationId,
      closeSideChat: focused.dataset.closeSideChat,
      newSideChat: focused.dataset.newSideChat,
    } : null;
    if (focusKey?.newSideChat && state.creatingSideChat) {
      tabs.dataset.pendingSideChatFocus = focusKey.newSideChat;
    }
    const pendingSideChatFocus = tabs.dataset.pendingSideChatFocus;
    if (!conversation || !parent) {
      delete tabs.dataset.pendingSideChatFocus;
      tabs.replaceChildren();
      return;
    }
    const nodes: HTMLElement[] = [];
    const addTab = (summary: NonNullable<ReturnType<typeof currentConversation>>, label: string, closeable: boolean): void => {
      const wrapper = element('span', 'cw-conversation-tab-wrap');
      wrapper.setAttribute('role', 'presentation');
      const tab = element('button', 'cw-conversation-tab');
      tab.type = 'button';
      tab.dataset.conversationId = summary.conversationId;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-selected', String(summary.conversationId === conversation.conversationId));
      tab.tabIndex = summary.conversationId === conversation.conversationId ? 0 : -1;
      tab.title = label;
      tab.textContent = label;
      wrapper.append(tab);
      if (closeable) {
        const close = element('button', 'cw-close-side-chat');
        close.type = 'button';
        close.dataset.closeSideChat = summary.conversationId;
        close.textContent = '×';
        close.setAttribute('aria-label', `Close ${label}`);
        close.title = `Close ${label}`;
        wrapper.append(close);
      }
      nodes.push(wrapper);
    };
    addTab(parent, parent.title.trim() || 'Conversation', false);
    if (parent.kind === 'manager') {
      tabs.replaceChildren(...nodes);
      return;
    }
    for (const conversationId of state.sessionSideChatIds) {
      const sideChat = state.conversations.find((item) => item.conversationId === conversationId);
      if (!sideChat || sideChat.kind !== 'side_chat' || sideChat.parentConversationId !== parent.conversationId) continue;
      addTab(sideChat, sideChatTitle(sideChat.title), true);
    }
    const create = element('button', 'cw-new-side-chat');
    create.type = 'button';
    create.dataset.newSideChat = parent.conversationId;
    create.textContent = state.creatingSideChat ? '…' : '+';
    create.disabled = state.creatingSideChat || parent.archived;
    create.setAttribute('aria-label', state.creatingSideChat ? 'Creating side chat' : 'New side chat');
    create.title = state.creatingSideChat ? 'Creating side chat…' : 'New side chat';
    nodes.push(create);
    tabs.replaceChildren(...nodes);
    if (focusKey || pendingSideChatFocus) {
      const buttons = [...tabs.querySelectorAll<HTMLButtonElement>('button')];
      const selected = buttons.find((button) => button.getAttribute('aria-selected') === 'true');
      const retained = buttons.find((button) =>
        button.dataset.conversationId === focusKey?.conversationId
        && button.dataset.closeSideChat === focusKey?.closeSideChat
        && button.dataset.newSideChat === focusKey?.newSideChat);
      const createFocusParent = pendingSideChatFocus ?? focusKey?.newSideChat;
      const openedFromCreate = !!createFocusParent
        && conversation.kind === 'side_chat'
        && conversation.parentConversationId === createFocusParent;
      const failedCreate = !!pendingSideChatFocus
        && !state.creatingSideChat
        && conversation.conversationId === pendingSideChatFocus;
      const restoredCreate = failedCreate
        ? buttons.find((button) => button.dataset.newSideChat === pendingSideChatFocus && !button.disabled)
        : null;
      const target = openedFromCreate || failedCreate
        ? restoredCreate ?? selected
        : retained && !retained.disabled ? retained : selected;
      target?.focus();
      if (openedFromCreate || failedCreate) delete tabs.dataset.pendingSideChatFocus;
    }
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
    this.renderNewConversationModelOptions();
    const projectHost = this.$<HTMLSelectElement>('.cw-add-project [name="host_id"]');
    const priorProjectHost = projectHost.value;
    projectHost.replaceChildren(...state.hosts.map((host) => new Option(host.displayName, host.hostId)));
    projectHost.value = state.hosts.some((host) => host.hostId === priorProjectHost) ? priorProjectHost : hostSelect.value;
    const newButton = this.$<HTMLButtonElement>('.cw-new-form button[type="submit"]');
    this.updateNewConversationButton();
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
    this.updateNewConversationButton();
  }

  private ensureModelCatalog(hostId: string): ConversationModelCatalog | null {
    const catalog = this.modelCatalogs.get(hostId) ?? null;
    if (!catalog && hostId && !this.requestedModelCatalogs.has(hostId) && !this.failedModelCatalogs.has(hostId)) {
      this.requestedModelCatalogs.add(hostId);
      this.actions.loadModelCatalog(hostId);
    }
    return catalog;
  }

  private showUnavailableSelect(select: HTMLSelectElement, label: string, value = ''): void {
    select.replaceChildren(new Option(label, value));
    select.value = value;
    select.disabled = true;
  }

  private populateModelSelect(select: HTMLSelectElement, catalog: ConversationModelCatalog, preferredModel: string): ConversationModel | null {
    const selected = catalog.models.find((model) => model.id === preferredModel) ?? catalogDefaultModel(catalog);
    select.replaceChildren(...catalog.models.map((model) => {
      const option = new Option(model.displayName, model.id);
      option.title = model.displayName === model.id ? model.id : `${model.displayName} (${model.id})`;
      return option;
    }));
    if (preferredModel && !catalog.models.some((model) => model.id === preferredModel)) {
      const unavailable = new Option(`${preferredModel} · unavailable`, preferredModel);
      unavailable.disabled = true;
      select.add(unavailable, 0);
      select.value = preferredModel;
      return null;
    }
    select.value = selected?.id ?? '';
    return selected;
  }

  private populateEffortSelect(
    select: HTMLSelectElement,
    catalog: ConversationModelCatalog,
    model: ConversationModel,
    preferredEffort: string,
  ): string {
    const supported = model.supportedReasoningEfforts;
    const selected = supported.some((option) => option.reasoningEffort === preferredEffort)
      ? preferredEffort
      : modelDefaultEffort(catalog, model);
    select.replaceChildren(...supported.map((effort) => {
      const option = new Option(reasoningEffortLabel(effort.reasoningEffort), effort.reasoningEffort);
      option.title = effort.description;
      return option;
    }));
    if (preferredEffort && !supported.some((option) => option.reasoningEffort === preferredEffort)) {
      const unavailable = new Option(`${reasoningEffortLabel(preferredEffort)} · unavailable`, preferredEffort);
      unavailable.disabled = true;
      select.add(unavailable, 0);
    }
    select.value = selected || preferredEffort;
    return select.value;
  }

  private renderNewConversationModelOptions(): void {
    const hostId = this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value;
    const modelSelect = this.$<HTMLSelectElement>('.cw-new-form [name="model"]');
    const effortSelect = this.$<HTMLSelectElement>('.cw-new-form [name="effort"]');
    if (!hostId) {
      this.showUnavailableSelect(modelSelect, 'Choose a host');
      this.showUnavailableSelect(effortSelect, 'Reasoning');
      this.updateNewConversationButton();
      return;
    }
    const catalog = this.ensureModelCatalog(hostId);
    if (!catalog) {
      this.showUnavailableSelect(modelSelect, 'Loading models…');
      this.showUnavailableSelect(effortSelect, 'Loading…');
      this.updateNewConversationButton();
      return;
    }
    const previousModel = modelSelect.value;
    this.populateModelSelect(modelSelect, catalog, previousModel);
    modelSelect.disabled = catalog.models.length === 0;
    this.renderNewConversationEffortOptions();
  }

  private renderNewConversationEffortOptions(): void {
    const hostId = this.$<HTMLSelectElement>('.cw-new-form [name="host"]').value;
    const modelId = this.$<HTMLSelectElement>('.cw-new-form [name="model"]').value;
    const effortSelect = this.$<HTMLSelectElement>('.cw-new-form [name="effort"]');
    const catalog = this.modelCatalogs.get(hostId);
    const model = catalog?.models.find((entry) => entry.id === modelId);
    if (!catalog || !model) {
      this.showUnavailableSelect(effortSelect, catalog ? 'Reasoning unavailable' : 'Loading…');
      this.updateNewConversationButton();
      return;
    }
    this.populateEffortSelect(effortSelect, catalog, model, effortSelect.value);
    effortSelect.disabled = model.supportedReasoningEfforts.length === 0;
    this.updateNewConversationButton();
  }

  private updateNewConversationButton(): void {
    if (!this.state) return;
    const form = this.$<HTMLFormElement>('.cw-new-form');
    const hostId = form.querySelector<HTMLSelectElement>('[name="host"]')!.value;
    const projectId = form.querySelector<HTMLSelectElement>('[name="project"]')!.value;
    const model = form.querySelector<HTMLSelectElement>('[name="model"]')!;
    const effort = form.querySelector<HTMLSelectElement>('[name="effort"]')!;
    form.querySelector<HTMLButtonElement>('button[type="submit"]')!.disabled = this.state.creatingConversation
      || !this.state.selectedPursuitId || !hostId || !projectId || model.disabled || effort.disabled || !model.value || !effort.value;
  }

  private renderManagerPicker(state: ConversationState): void {
    const localHost = state.hosts.find((host) => host.kind === 'local');
    const modelSelect = this.$<HTMLSelectElement>('.cw-manager-form [name="model"]');
    const effortSelect = this.$<HTMLSelectElement>('.cw-manager-form [name="effort"]');
    if (!localHost) {
      this.showUnavailableSelect(modelSelect, 'Local host unavailable');
      this.showUnavailableSelect(effortSelect, 'Reasoning unavailable');
      this.updateManagerConversationButton();
      return;
    }
    const catalog = this.ensureModelCatalog(localHost.hostId);
    if (!catalog) {
      this.showUnavailableSelect(modelSelect, 'Loading models…');
      this.showUnavailableSelect(effortSelect, 'Loading…');
      this.updateManagerConversationButton();
      return;
    }
    const previousModel = modelSelect.value;
    this.populateModelSelect(modelSelect, catalog, previousModel);
    modelSelect.disabled = catalog.models.length === 0;
    this.renderManagerEffortOptions();
  }

  private renderManagerEffortOptions(): void {
    if (!this.state) return;
    const localHost = this.state.hosts.find((host) => host.kind === 'local');
    const modelId = this.$<HTMLSelectElement>('.cw-manager-form [name="model"]').value;
    const effortSelect = this.$<HTMLSelectElement>('.cw-manager-form [name="effort"]');
    const catalog = localHost ? this.modelCatalogs.get(localHost.hostId) : undefined;
    const model = catalog?.models.find((entry) => entry.id === modelId);
    if (!catalog || !model) {
      this.showUnavailableSelect(effortSelect, catalog ? 'Reasoning unavailable' : 'Loading…');
      this.updateManagerConversationButton();
      return;
    }
    this.populateEffortSelect(effortSelect, catalog, model, effortSelect.value);
    effortSelect.disabled = model.supportedReasoningEfforts.length === 0;
    this.updateManagerConversationButton();
  }

  private updateManagerConversationButton(): void {
    if (!this.state) return;
    const form = this.$<HTMLFormElement>('.cw-manager-form');
    const model = form.querySelector<HTMLSelectElement>('[name="model"]')!;
    const effort = form.querySelector<HTMLSelectElement>('[name="effort"]')!;
    const button = form.querySelector<HTMLButtonElement>('button[type="submit"]')!;
    button.disabled = this.state.creatingManager || model.disabled || effort.disabled || !model.value || !effort.value;
    button.textContent = this.state.creatingManager ? 'Creating…' : 'New Manager conversation';
  }

  private renderList(state: ConversationState): void {
    this.$('.cw-pursuit-label').textContent = state.selectedPursuitId ?? '';
    const status = this.$('.cw-list-status');
    status.textContent = state.loadingWorkspace || state.loadingPursuit ? 'Loading conversations…' : '';
    const list = this.$('.cw-conversation-list');
    const focusedId = list.contains(document.activeElement)
      ? (document.activeElement as HTMLElement).dataset.conversationId
      : undefined;
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
      const heading = element('span', 'cw-conversation-heading');
      const title = appendText(heading, 'span', conversation.title);
      title.className = 'cw-conversation-title';
      const unreadFinal = conversation.lastFinalEventId !== null
        && conversation.lastFinalEventId > (conversation.lastReadEventId ?? 0);
      if (unreadFinal) {
        appendText(heading, 'span', 'NEW', 'cw-conversation-new');
        button.classList.add('cw-has-unread-final');
        button.setAttribute('aria-label', `${conversation.title}, new final response`);
      }
      button.append(heading);
      const host = state.hosts.find((item) => item.hostId === conversation.hostId)?.displayName || conversation.hostId;
      appendText(button, 'small', [host, conversation.status].filter(Boolean).join(' · '));
      button.addEventListener('click', () => this.actions.openConversation(conversation.conversationId), { signal: this.abort.signal });
      list.append(button);
    }
    if (focusedId) {
      const buttons = [...list.querySelectorAll<HTMLButtonElement>('.cw-conversation')];
      (buttons.find((button) => button.dataset.conversationId === focusedId) ?? buttons[0])?.focus();
    }
  }

  private renderManagerList(state: ConversationState): void {
    const status = this.$('.cw-manager-status');
    status.textContent = state.loadingWorkspace ? 'Loading Manager conversations…' : '';
    const list = this.$('.cw-manager-list');
    const focusedId = list.contains(document.activeElement)
      ? (document.activeElement as HTMLElement).dataset.conversationId
      : undefined;
    list.replaceChildren();
    const conversations = managerConversations(state);
    if (!conversations.length && !state.loadingWorkspace) {
      appendText(list, 'p', 'No Manager conversations yet.', 'cw-empty-copy');
      return;
    }
    for (const conversation of conversations) {
      const button = element('button', 'cw-conversation');
      button.type = 'button';
      button.dataset.conversationId = conversation.conversationId;
      const heading = element('span', 'cw-conversation-heading');
      const title = appendText(heading, 'span', conversation.title || 'Manager');
      title.className = 'cw-conversation-title';
      const unreadFinal = conversation.lastFinalEventId !== null
        && conversation.lastFinalEventId > (conversation.lastReadEventId ?? 0);
      if (unreadFinal) {
        appendText(heading, 'span', 'NEW', 'cw-conversation-new');
        button.classList.add('cw-has-unread-final');
      }
      button.append(heading);
      appendText(button, 'small', ['Local', conversation.status].filter(Boolean).join(' · '));
      button.addEventListener('click', () => this.actions.openConversation(conversation.conversationId), { signal: this.abort.signal });
      list.append(button);
    }
    if (focusedId) {
      const buttons = [...list.querySelectorAll<HTMLButtonElement>('.cw-conversation')];
      (buttons.find((button) => button.dataset.conversationId === focusedId) ?? buttons[0])?.focus();
    }
  }

  private renderDetail(state: ConversationState, conversation: ReturnType<typeof currentConversation>): void {
    if (!conversation) {
      if (this.lastConversationId) this.saveDraft(this.lastConversationId);
      this.lastConversationId = null;
      this.updateElapsedTimer(false);
      return;
    }
    const changed = this.lastConversationId !== conversation.conversationId;
    if (changed) {
      if (this.lastConversationId) this.saveDraft(this.lastConversationId);
      this.lastConversationId = conversation.conversationId;
      this.loadDraft(conversation.conversationId);
      this.activitySignature = '\u0000';
      this.activityFirstEventId = null;
    }
    this.$('.cw-detail-title').textContent = conversation.kind === 'side_chat'
      ? sideChatTitle(conversation.title)
      : conversation.kind === 'manager' && !conversation.title.trim()
        ? 'Manager'
        : conversation.title;
    const host = state.hosts.find((item) => item.hostId === conversation.hostId)?.displayName || conversation.hostId;
    const project = state.projects.find((item) => item.projectId === conversation.projectId);
    this.$('.cw-detail-location').textContent = [host, conversation.executionCwd || project?.cwd || project?.label || conversation.projectId].filter(Boolean).join(' · ');
    const sending = state.sendingConversationIds.includes(conversation.conversationId);
    const interruptible = turnCanInterrupt(conversation.status);
    const interrupting = state.interruptingConversationIds.includes(conversation.conversationId);
    if (interrupting) this.observedInterruptIds.add(conversation.conversationId);
    const newInterruptError = this.observedInterruptIds.has(conversation.conversationId)
      && !interrupting
      && !!state.error
      && state.error !== this.stoppingBaselineErrors.get(conversation.conversationId);
    if ((!interruptible && !sending) || newInterruptError) {
      this.stoppingConversationIds.delete(conversation.conversationId);
      this.observedInterruptIds.delete(conversation.conversationId);
      this.stoppingBaselineErrors.delete(conversation.conversationId);
    }
    const stopping = interrupting || this.stoppingConversationIds.has(conversation.conversationId);
    const liveActive = sending || interruptible || stopping;
    if (liveActive && !this.workStartedAt.has(conversation.conversationId)) {
      this.workStartedAt.set(conversation.conversationId, Date.now());
    } else if (!liveActive) {
      this.workStartedAt.delete(conversation.conversationId);
    }
    const liveLabel = liveWorkLabel(conversation.status, stopping, sending);
    const statusNode = this.$('.cw-turn-status');
    statusNode.textContent = state.loadingConversation ? 'Loading history…' : liveActive ? liveLabel : activityStatusLabel(conversation.status || 'unknown');
    statusNode.classList.toggle('cw-working', liveActive && !stopping && !liveLabel.startsWith('Waiting'));
    statusNode.classList.toggle('cw-stopping', stopping);
    const loadEarlier = this.$<HTMLButtonElement>('.cw-load-earlier');
    const loadingEarlier = state.loadingEarlierConversationIds.includes(conversation.conversationId);
    const hasEarlierEvents = Boolean(state.hasEarlierEventsByConversation[conversation.conversationId]);
    const moveHistoryFocus = !hasEarlierEvents && !loadEarlier.hidden && document.activeElement === loadEarlier;
    loadEarlier.hidden = !hasEarlierEvents;
    loadEarlier.disabled = loadingEarlier;
    loadEarlier.textContent = loadingEarlier ? 'Loading earlier messages…' : 'Load earlier messages';
    if (moveHistoryFocus) {
      queueMicrotask(() => this.$('.cw-activity').focus({ preventScroll: true }));
    }
    this.$('.cw-side-chat-note').hidden = conversation.kind !== 'side_chat';
    const sendStop = this.$<HTMLButtonElement>('.cw-send-stop');
    sendStop.dataset.mode = interruptible || stopping ? 'stop' : 'send';
    sendStop.type = interruptible || stopping ? 'button' : 'submit';
    sendStop.textContent = stopping ? 'Stopping…' : interruptible ? 'Stop' : sending ? 'Preparing…' : 'Send';
    sendStop.classList.toggle('cw-stop', interruptible || stopping);
    sendStop.classList.toggle('cw-primary', !interruptible && !stopping);
    if (interruptible || stopping) sendStop.disabled = stopping;
    this.$<HTMLTextAreaElement>('.cw-composer textarea').disabled = conversation.archived;
    this.$<HTMLButtonElement>('.cw-attach').disabled = conversation.archived;
    if (!state.sendingConversationIds.includes(conversation.conversationId)) {
      for (const staged of this.stagedAttachments.get(conversation.conversationId) ?? []) staged.submitting = false;
    }
    this.restoreStagedAttachments(
      conversation.conversationId,
      state.attachmentsByConversation[conversation.conversationId] ?? [],
    );
    this.renderStagedAttachments(conversation.conversationId);
    this.renderManagerReference(state, conversation.kind === 'manager');
    this.updateComposerControls();
    this.renderConversationModelOptions(conversation);
    const reconnect = this.$<HTMLButtonElement>('.cw-reconnect');
    reconnect.hidden = conversation.status.toLowerCase() !== 'unknown';
    const reconciling = state.reconcilingConversationIds.includes(conversation.conversationId);
    reconnect.disabled = state.loadingConversation || reconciling;
    reconnect.textContent = reconciling ? 'Checking…' : 'Reconnect and check status';
    const archive = this.$<HTMLButtonElement>('.cw-archive');
    const sessionSideChats = state.sessionSideChatIds.map((conversationId) =>
      state.conversations.find((item) => item.conversationId === conversationId));
    const hasSessionSideChat = conversation.kind !== 'side_chat' && conversation.kind !== 'manager' && (
      state.creatingSideChat
      || sessionSideChats.some((sideChat) => !sideChat)
      || sessionSideChats.some((sideChat) => sideChat?.kind === 'side_chat'
        && sideChat.parentConversationId === conversation.conversationId)
    );
    archive.hidden = conversation.kind === 'side_chat';
    archive.disabled = hasSessionSideChat;
    archive.title = hasSessionSideChat ? 'Close side chats before archiving' : 'Archive conversation';
    this.renderActivity(
      state.eventsByConversation[conversation.conversationId] ?? [],
      state.attachmentsByConversation[conversation.conversationId] ?? [],
      changed,
      {
        active: liveActive,
        stopping,
        label: liveLabel,
        startedAt: liveLabel.startsWith('Waiting') ? null : this.workStartedAt.get(conversation.conversationId) ?? null,
      },
    );
    this.updateElapsedTimer(liveActive && !liveLabel.startsWith('Waiting'));
    this.renderPending(
      state.pendingRequests.filter((request) => request.conversationId === conversation.conversationId),
      new Set(state.respondingRequestKeys),
    );
  }

  private renderManagerReference(state: ConversationState, isManager: boolean): void {
    const container = this.$('.cw-staged-references');
    container.replaceChildren();
    const pursuitId = isManager ? state.managerReferencePursuitId : null;
    container.hidden = !pursuitId;
    if (!pursuitId) return;
    const chip = element('span', 'cw-reference-chip');
    appendText(chip, 'span', `Pursuit · ${pursuitId}`);
    const remove = element('button', 'cw-reference-remove');
    remove.type = 'button';
    remove.dataset.removeManagerReference = 'true';
    remove.textContent = '×';
    remove.setAttribute('aria-label', `Remove Pursuit reference ${pursuitId}`);
    chip.append(remove);
    container.append(chip);
  }

  private renderConversationModelOptions(conversation: NonNullable<ReturnType<typeof currentConversation>>): void {
    const modelSelect = this.$<HTMLSelectElement>('.cw-composer [name="model"]');
    const effortSelect = this.$<HTMLSelectElement>('.cw-composer [name="effort"]');
    const catalog = this.ensureModelCatalog(conversation.hostId);
    if (!catalog) {
      this.showUnavailableSelect(modelSelect, conversation.model || 'Loading models…', conversation.model);
      this.showUnavailableSelect(effortSelect, conversation.reasoningEffort ? reasoningEffortLabel(conversation.reasoningEffort) : 'Loading…', conversation.reasoningEffort);
      return;
    }
    const model = this.populateModelSelect(modelSelect, catalog, conversation.model);
    if (!model) {
      this.showUnavailableSelect(effortSelect, conversation.reasoningEffort ? reasoningEffortLabel(conversation.reasoningEffort) : 'Reasoning unavailable', conversation.reasoningEffort);
    } else {
      this.populateEffortSelect(effortSelect, catalog, model, conversation.reasoningEffort);
      effortSelect.disabled = conversation.archived || model.supportedReasoningEfforts.length === 0;
    }
    modelSelect.disabled = conversation.archived || catalog.models.length === 0;
  }

  private renderConversationEffortOptions(): void {
    if (!this.state) return;
    const conversation = currentConversation(this.state);
    if (!conversation) return;
    const modelSelect = this.$<HTMLSelectElement>('.cw-composer [name="model"]');
    const effortSelect = this.$<HTMLSelectElement>('.cw-composer [name="effort"]');
    const catalog = this.modelCatalogs.get(conversation.hostId);
    const model = catalog?.models.find((entry) => entry.id === modelSelect.value);
    if (!catalog || !model) {
      this.showUnavailableSelect(effortSelect, 'Reasoning unavailable');
      return;
    }
    this.populateEffortSelect(effortSelect, catalog, model, effortSelect.value);
    effortSelect.disabled = conversation.archived || model.supportedReasoningEfforts.length === 0;
  }

  private submitConversationSettings(): void {
    if (!this.state) return;
    const conversation = currentConversation(this.state);
    if (!conversation || conversation.archived) return;
    const model = this.$<HTMLSelectElement>('.cw-composer [name="model"]').value;
    const reasoningEffort = this.$<HTMLSelectElement>('.cw-composer [name="effort"]').value;
    if (!model || !reasoningEffort || (model === conversation.model && reasoningEffort === conversation.reasoningEffort)) return;
    this.actions.updateConversationSettings(model, reasoningEffort);
  }

  setModelCatalog(catalog: ConversationModelCatalog): void {
    if (!catalog.hostId) return;
    this.modelCatalogs.set(catalog.hostId, catalog);
    this.requestedModelCatalogs.delete(catalog.hostId);
    this.failedModelCatalogs.delete(catalog.hostId);
    if (this.state) this.render(this.state);
  }

  releaseModelCatalog(hostId: string): void {
    this.requestedModelCatalogs.delete(hostId);
    this.failedModelCatalogs.add(hostId);
  }

  retryModelCatalogs(): void {
    this.requestedModelCatalogs.clear();
    this.failedModelCatalogs.clear();
  }

  private retryModelCatalog(hostId: string): void {
    this.requestedModelCatalogs.delete(hostId);
    this.failedModelCatalogs.delete(hostId);
  }

  private renderActivity(
    events: ConversationEvent[],
    attachments: ConversationAttachment[],
    forceBottom: boolean,
    liveWork?: LiveWorkState,
  ): void {
    const liveSignature = liveWork?.active
      ? `${liveWork.label}:${liveWork.stopping ? 1 : 0}:${liveWork.startedAt ?? ''}`
      : '';
    const signature = `${events.map((event) => event.eventId).join('\u001f')}\u001e${attachments.map((attachment) => `${attachment.attachmentId}:${attachment.state}`).join('\u001f')}\u001e${liveSignature}`;
    if (signature === this.activitySignature && !forceBottom) return;
    this.activitySignature = signature;
    const activity = this.$('.cw-activity');
    const nextFirstEventId = events[0]?.eventId ?? null;
    const prepended = this.activityFirstEventId !== null
      && nextFirstEventId !== this.activityFirstEventId
      && events.some((event) => event.eventId === this.activityFirstEventId);
    this.activityFirstEventId = nextFirstEventId;
    const previousScrollHeight = activity.scrollHeight;
    const previousScrollTop = activity.scrollTop;
    const disclosureStates = new Map<string, { open: boolean; label: string }>();
    for (const details of activity.querySelectorAll<HTMLDetailsElement>('details[data-activity-key]')) {
      const key = details.dataset.activityKey;
      if (key) disclosureStates.set(key, { open: details.open, label: details.querySelector(':scope > summary')?.textContent ?? '' });
    }
    const active = document.activeElement instanceof HTMLElement && activity.contains(document.activeElement)
      ? document.activeElement
      : null;
    const activeOwner = active?.closest<HTMLElement>('[data-activity-key]') ?? null;
    const focusableSelector = 'a[href],button,summary,input,select,textarea,[tabindex]:not([tabindex="-1"])';
    const activeFocusables = activeOwner ? [...activeOwner.querySelectorAll<HTMLElement>(focusableSelector)] : [];
    const focusedActivity = activeOwner?.dataset.activityKey && active
      ? { key: activeOwner.dataset.activityKey, index: activeFocusables.indexOf(active) }
      : null;
    const reusableImages = new Map<string, HTMLElement[]>();
    for (const image of activity.querySelectorAll<HTMLElement>('.cw-sent-image[data-attachment-id]')) {
      const id = image.dataset.attachmentId;
      if (!id) continue;
      const entries = reusableImages.get(id) ?? [];
      entries.push(image);
      reusableImages.set(id, entries);
    }
    const nearBottom = forceBottom || activity.scrollHeight - activity.scrollTop - activity.clientHeight < 90;
    const hadContent = activity.childElementCount > 0;
    const nodes = activityNodes(events, attachments, reusableImages, liveWork);
    if (!nodes.length) {
      activity.replaceChildren();
      appendText(activity, 'p', 'No activity yet. Send the first message when you are ready.', 'cw-empty-copy');
    } else {
      activity.replaceChildren(...nodes);
      for (const details of activity.querySelectorAll<HTMLDetailsElement>('details[data-activity-key]')) {
        const previous = disclosureStates.get(details.dataset.activityKey ?? '');
        if (!previous) continue;
        const nextLabel = details.querySelector(':scope > summary')?.textContent ?? '';
        const commentaryJustCompleted = details.classList.contains('cw-commentary-group')
          && !previous.label.startsWith('Work details')
          && nextLabel.startsWith('Work details');
        if (!commentaryJustCompleted) details.open = previous.open;
      }
      if (focusedActivity && focusedActivity.index >= 0) {
        const nextOwner = [...activity.querySelectorAll<HTMLElement>('[data-activity-key]')]
          .find((node) => node.dataset.activityKey === focusedActivity.key);
        const nextFocusables = nextOwner ? [...nextOwner.querySelectorAll<HTMLElement>(focusableSelector)] : [];
        let nextFocus = nextFocusables[focusedActivity.index] ?? nextFocusables[0];
        let closedAncestor: HTMLDetailsElement | null = null;
        for (
          let ancestor = nextFocus?.parentElement ?? null;
          ancestor && activity.contains(ancestor);
          ancestor = ancestor.parentElement
        ) {
          if (ancestor instanceof HTMLDetailsElement && !ancestor.open) closedAncestor = ancestor;
        }
        if (closedAncestor) {
          nextFocus = closedAncestor.querySelector<HTMLElement>(':scope > summary') ?? nextFocus;
        }
        nextFocus?.focus({ preventScroll: true });
      }
    }
    if (!nodes.length) return;
    requestAnimationFrame(() => {
      if (prepended && hadContent && !forceBottom) {
        activity.scrollTop = previousScrollTop + (activity.scrollHeight - previousScrollHeight);
      } else if (nearBottom) this.scrollToBottom();
      else if (hadContent) this.$('.cw-unread').hidden = false;
    });
  }

  private refreshElapsedTimes(): void {
    const now = Date.now();
    for (const elapsed of this.host.querySelectorAll<HTMLTimeElement>('.cw-work-elapsed[data-started-at]')) {
      const startedAt = Number(elapsed.dataset.startedAt);
      if (!Number.isFinite(startedAt)) continue;
      const seconds = Math.max(0, Math.floor((now - startedAt) / 1_000));
      const minutes = Math.floor(seconds / 60);
      elapsed.textContent = minutes ? `${minutes}m ${seconds % 60}s` : `${seconds}s`;
      elapsed.dateTime = `PT${seconds}S`;
    }
  }

  private updateElapsedTimer(active: boolean): void {
    if (!active) {
      if (this.elapsedTimer !== null) clearInterval(this.elapsedTimer);
      this.elapsedTimer = null;
      return;
    }
    this.refreshElapsedTimes();
    if (this.elapsedTimer === null) {
      this.elapsedTimer = setInterval(() => this.refreshElapsedTimes(), 1_000);
    }
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

  private setComposerNotice(message: string): void {
    const notice = this.$('.cw-composer-notice');
    notice.textContent = message;
    notice.hidden = !message;
  }

  private createPreviewUrl(file: File): string {
    try { return typeof URL.createObjectURL === 'function' ? URL.createObjectURL(file) : ''; }
    catch { return ''; }
  }

  private revokePreviewUrl(value: string): void {
    if (!value) return;
    try { if (typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(value); }
    catch { /* Preview cleanup is best-effort. */ }
  }

  private handleComposerPaste(event: ClipboardEvent): void {
    const conversationId = this.lastConversationId;
    const conversation = this.state && currentConversation(this.state);
    const clipboard = event.clipboardData;
    if (!conversationId || !conversation || conversation.archived || !clipboard) return;
    const clipboardErrors: string[] = [];
    let files: File[] = [];
    try { files = Array.from(clipboard.files ?? []); }
    catch { clipboardErrors.push('Could not read clipboard attachments.'); }
    if (!files.length) {
      try {
        files = Array.from(clipboard.items ?? [])
          .filter((item) => item.kind === 'file')
          .map((item) => item.getAsFile())
          .filter((file): file is File => !!file);
      } catch { clipboardErrors.push('Could not read clipboard attachment data.'); }
    }
    let pastedText = '';
    try { pastedText = clipboard.getData('text/plain'); }
    catch { clipboardErrors.push('Could not read clipboard text.'); }
    const largeText = pastedText.length >= LARGE_PASTE_CHARACTER_LIMIT;
    if (!files.length && !largeText) {
      if (clipboardErrors.length) this.setComposerNotice([...new Set(clipboardErrors)].join(' '));
      return;
    }
    event.preventDefault();
    this.setComposerNotice('');
    const notices = [...clipboardErrors, ...this.stageUserFiles(conversationId, files)];
    if (largeText) {
      const sequence = ++this.attachmentSequence;
      const file = new File([pastedText], `pasted-text-${sequence}.txt`, { type: 'text/plain;charset=utf-8' });
      const existing = (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => !entry.removed);
      const textCount = existing.filter((entry) => entry.kind === 'pasted_text').length;
      if (file.size > MAX_PASTED_TEXT_BYTES) {
        notices.push('The pasted text was not added because it exceeds the 5 MiB text limit.');
      } else if (existing.length >= MAX_STAGED_ATTACHMENT_COUNT || textCount >= MAX_STAGED_TEXT_COUNT) {
        notices.push('Up to 8 attachments, including 4 pasted texts, can be staged for one message. Remove one before pasting another.');
      } else {
        this.stageAttachment(conversationId, file, 'pasted_text', 'Pasted text');
      }
    } else if (pastedText) {
      const input = this.$<HTMLTextAreaElement>('.cw-composer textarea');
      input.setRangeText(pastedText, input.selectionStart, input.selectionEnd, 'end');
      input.dispatchEvent(new Event('input', { bubbles: true }));
    }
    if (notices.length) this.setComposerNotice([...new Set(notices)].join(' '));
  }

  private handleSelectedFiles(files: File[]): void {
    const conversationId = this.lastConversationId;
    const conversation = this.state && currentConversation(this.state);
    if (!files.length || !conversationId || !conversation || conversation.archived) return;
    this.setComposerNotice('');
    const notices = this.stageUserFiles(conversationId, files);
    if (notices.length) this.setComposerNotice([...new Set(notices)].join(' '));
  }

  private stageUserFiles(conversationId: string, files: File[]): string[] {
    const notices: string[] = [];
    const existing = (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => !entry.removed);
    let totalCount = existing.length;
    let imageCount = existing.filter((entry) => entry.kind === 'image').length;
    let fileCount = existing.filter((entry) => entry.kind === 'file').length;
    for (const file of files) {
      let mediaType = '';
      let displayName = 'Attachment';
      let byteSize = 0;
      try {
        mediaType = String(file.type || '').split(';', 1)[0].trim().toLowerCase();
        displayName = file.name || (PASTED_IMAGE_MEDIA_TYPES.has(mediaType) ? 'Pasted image' : 'Attachment');
        byteSize = Number(file.size);
      } catch {
        notices.push('A file could not be inspected and was not added.');
        continue;
      }
      const kind: StagedAttachment['kind'] = PASTED_IMAGE_MEDIA_TYPES.has(mediaType) ? 'image' : 'file';
      if (!Number.isFinite(byteSize) || byteSize <= 0) {
        notices.push(`${displayName} was not added because it is empty or unreadable.`);
        continue;
      }
      const maximum = kind === 'image' ? MAX_PASTED_IMAGE_BYTES : MAX_GENERIC_FILE_BYTES;
      if (byteSize > maximum) {
        notices.push(`${displayName} was not added because it exceeds the 20 MiB ${kind === 'image' ? 'image' : 'file'} limit.`);
        continue;
      }
      const kindLimitReached = kind === 'image'
        ? imageCount >= MAX_STAGED_IMAGE_COUNT
        : fileCount >= MAX_STAGED_FILE_COUNT;
      if (totalCount >= MAX_STAGED_ATTACHMENT_COUNT || kindLimitReached) {
        const kindLimit = kind === 'image' ? MAX_STAGED_IMAGE_COUNT : MAX_STAGED_FILE_COUNT;
        notices.push(`Up to 8 attachments, including ${kindLimit} ${kind === 'image' ? 'images' : 'files'}, can be staged for one message. Remove one before adding another.`);
        continue;
      }
      this.stageAttachment(conversationId, file, kind, displayName);
      totalCount++;
      if (kind === 'image') imageCount++;
      else fileCount++;
    }
    return notices;
  }

  private stageAttachment(conversationId: string, file: File, kind: StagedAttachment['kind'], displayName: string): void {
    const uploadId = attachmentUploadId();
    const staged: StagedAttachment = {
      clientId: uploadId,
      uploadId,
      conversationId,
      file,
      displayName,
      kind,
      previewUrl: '',
      status: 'queued',
      attachment: null,
      error: '',
      removed: false,
      submitting: false,
      removing: false,
      snapshotSeen: false,
    };
    const current = this.stagedAttachments.get(conversationId) ?? [];
    this.stagedAttachments.set(conversationId, [...current, staged]);
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
    this.enqueueStagedAttachment(staged);
  }

  private enqueueStagedAttachment(staged: StagedAttachment): void {
    if (!staged.file || staged.removed || this.attachmentUploadQueue.includes(staged)) return;
    staged.status = 'queued';
    staged.error = '';
    staged.submitting = false;
    this.attachmentUploadQueue.push(staged);
    if (this.lastConversationId === staged.conversationId) {
      this.renderStagedAttachments(staged.conversationId);
      this.updateComposerControls();
    }
    void this.drainAttachmentUploadQueue();
  }

  private async drainAttachmentUploadQueue(): Promise<void> {
    if (this.attachmentUploadRunning) return;
    this.attachmentUploadRunning = true;
    try {
      while (this.attachmentUploadQueue.length) {
        const staged = this.attachmentUploadQueue.shift()!;
        if (staged.removed || !staged.file) continue;
        await this.uploadStagedAttachment(staged);
      }
    } finally {
      this.attachmentUploadRunning = false;
      if (this.attachmentUploadQueue.length) void this.drainAttachmentUploadQueue();
    }
  }

  private async uploadStagedAttachment(staged: StagedAttachment): Promise<void> {
    const { conversationId, file } = staged;
    if (!file || staged.removed) return;
    staged.status = 'uploading';
    staged.error = '';
    staged.submitting = false;
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
    try {
      const attachment = await this.actions.uploadAttachment(
        conversationId,
        file,
        staged.uploadId,
        staged.kind === 'file' ? 'file' : undefined,
      );
      staged.attachment = attachment;
      staged.status = 'ready';
      if (staged.removed) {
        const suppressed = this.suppressedAttachmentIds.get(conversationId) ?? new Set<string>();
        suppressed.add(attachment.attachmentId);
        this.suppressedAttachmentIds.set(conversationId, suppressed);
        try {
          await this.actions.deleteAttachment(conversationId, attachment.attachmentId);
          this.stagedAttachments.set(conversationId, (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => entry !== staged));
          this.revokePreviewUrl(staged.previewUrl);
        } catch (error: unknown) {
          suppressed.delete(attachment.attachmentId);
          if (!(this.stagedAttachments.get(conversationId) ?? []).includes(staged)) {
            this.revokePreviewUrl(staged.previewUrl);
            return;
          }
          staged.removed = false;
          staged.removing = false;
          if (staged.kind === 'image' && !staged.previewUrl) staged.previewUrl = this.createPreviewUrl(file);
          if (this.lastConversationId === conversationId) {
            this.setComposerNotice(error instanceof Error ? `Could not remove attachment: ${error.message}` : 'Could not remove attachment.');
            this.renderStagedAttachments(conversationId);
            this.updateComposerControls();
          }
        }
        return;
      }
      if (staged.kind === 'image' && !staged.previewUrl) staged.previewUrl = this.createPreviewUrl(file);
      if (this.lastConversationId === conversationId) {
        this.renderStagedAttachments(conversationId);
        this.updateComposerControls();
      }
    } catch (error: unknown) {
      if (staged.removed) {
        this.stagedAttachments.set(conversationId, (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => entry !== staged));
        this.revokePreviewUrl(staged.previewUrl);
        return;
      }
      staged.status = 'failed';
      staged.error = error instanceof Error && error.message ? error.message : String(error || 'The upload failed.');
      if (this.lastConversationId === conversationId) {
        this.setComposerNotice(`Could not upload ${staged.displayName}: ${staged.error}`);
        this.renderStagedAttachments(conversationId);
        this.updateComposerControls();
      }
    }
  }

  private retryStagedAttachment(conversationId: string, clientId: string): void {
    const staged = (this.stagedAttachments.get(conversationId) ?? [])
      .find((entry) => entry.clientId === clientId);
    if (!staged || staged.status !== 'failed' || !staged.file || staged.removed || staged.removing || staged.submitting) return;
    this.setComposerNotice('');
    this.enqueueStagedAttachment(staged);
  }

  private restoreStagedAttachments(conversationId: string, attachments: ConversationAttachment[]): void {
    const current = this.stagedAttachments.get(conversationId) ?? [];
    const suppressed = this.suppressedAttachmentIds.get(conversationId) ?? new Set<string>();
    const authoritativeStagedIds = new Set(
      attachments
        .filter((attachment) => attachment.state.toLowerCase() === 'staged')
        .map((attachment) => attachment.attachmentId),
    );
    if (!this.state?.loadingConversation) {
      for (const entry of current) {
        const attachmentId = entry.attachment?.attachmentId;
        if (!attachmentId || !entry.snapshotSeen || entry.status !== 'ready'
          || entry.submitting || entry.removing || authoritativeStagedIds.has(attachmentId)) continue;
        entry.removed = true;
        this.revokePreviewUrl(entry.previewUrl);
      }
    }
    const byAttachmentId = new Map(
      current.map((entry) => [entry.attachment?.attachmentId ?? entry.uploadId, entry] as const),
    );
    for (const attachment of attachments) {
      const existing = byAttachmentId.get(attachment.attachmentId);
      if (attachment.state.toLowerCase() !== 'staged') {
        if (existing && !existing.submitting) {
          existing.removed = true;
          this.revokePreviewUrl(existing.previewUrl);
        }
        continue;
      }
      if (suppressed.has(attachment.attachmentId)) continue;
      if (existing) {
        existing.attachment = attachment;
        existing.status = 'ready';
        existing.error = '';
        existing.snapshotSeen = true;
        continue;
      }
      const kind: StagedAttachment['kind'] = attachment.kind === 'image'
        ? 'image'
        : attachment.kind === 'pasted_text' ? 'pasted_text' : 'file';
      const restored: StagedAttachment = {
        clientId: attachment.attachmentId,
        uploadId: attachment.attachmentId,
        conversationId,
        file: null,
        displayName: attachment.kind === 'pasted_text' ? 'Pasted text' : attachment.displayName,
        kind,
        previewUrl: '',
        status: 'ready',
        attachment,
        error: '',
        removed: false,
        submitting: false,
        removing: false,
        snapshotSeen: true,
      };
      current.push(restored);
      byAttachmentId.set(attachment.attachmentId, restored);
    }
    this.stagedAttachments.set(conversationId, current.filter((entry) => !entry.removed || entry.removing));
  }

  private renderStagedAttachments(conversationId: string): void {
    const parent = this.$('.cw-staged-attachments');
    const reusableImages = new Map<string, HTMLImageElement>();
    for (const chip of parent.querySelectorAll<HTMLElement>('.cw-attachment-chip.cw-image[data-attachment-id]')) {
      const attachmentId = chip.dataset.attachmentId;
      const image = chip.querySelector<HTMLImageElement>('img');
      if (attachmentId && image) reusableImages.set(attachmentId, image);
    }
    const nodes: HTMLElement[] = [];
    for (const staged of this.stagedAttachments.get(conversationId) ?? []) {
      if (staged.removed) continue;
      const chip = element('article', `cw-attachment-chip cw-${staged.kind.replace('_', '-')}`);
      const attachmentId = staged.attachment?.attachmentId ?? staged.uploadId;
      chip.dataset.attachmentId = attachmentId;
      if (staged.kind === 'image') {
        if (staged.status === 'ready' && staged.attachment) {
          const image = reusableImages.get(attachmentId) ?? element('img');
          image.alt = '';
          image.loading = 'lazy';
          image.decoding = 'async';
          const source = staged.previewUrl || attachmentReadUrl(staged.attachment);
          if (image.getAttribute('src') !== source) image.src = source;
          chip.append(image);
        } else appendText(chip, 'span', 'IMG', 'cw-file-icon');
      } else appendText(chip, 'span', staged.kind === 'pasted_text' ? 'TXT' : 'FILE', 'cw-file-icon');
      const copy = element('span', 'cw-attachment-copy');
      appendText(copy, 'strong', staged.displayName);
      const state = staged.status === 'queued'
        ? 'Waiting to upload…'
        : staged.status === 'uploading'
          ? 'Uploading…'
          : staged.status === 'failed'
            ? `Upload failed: ${staged.error || 'The server rejected this attachment.'}`
            : [formatByteSize(staged.attachment?.byteSize ?? staged.file?.size ?? 0), staged.removing ? 'Removing…' : staged.submitting ? 'Sending…' : 'Ready'].filter(Boolean).join(' · ');
      const detail = appendText(copy, 'small', state, staged.status === 'failed' ? 'cw-attachment-error' : undefined);
      if (staged.error) detail.title = staged.error;
      const controls = element('span', 'cw-attachment-actions');
      if (staged.kind === 'file' && staged.status === 'ready' && staged.attachment) {
        const download = element('a', 'cw-attachment-download');
        download.href = attachmentReadUrl(staged.attachment);
        download.download = staged.displayName || 'attachment';
        download.textContent = '↓';
        download.setAttribute('aria-label', `Download ${staged.displayName}`);
        download.title = `Download ${staged.displayName}`;
        controls.append(download);
      }
      if (staged.status === 'failed' && staged.file) {
        const retry = element('button', 'cw-attachment-retry');
        retry.type = 'button';
        retry.textContent = 'Retry';
        retry.disabled = staged.submitting || staged.removing;
        retry.setAttribute('aria-label', `Retry ${staged.displayName}`);
        retry.addEventListener('click', () => this.retryStagedAttachment(conversationId, staged.clientId), { signal: this.abort.signal });
        controls.append(retry);
      }
      const remove = element('button', 'cw-attachment-remove');
      remove.type = 'button';
      remove.textContent = '×';
      remove.disabled = staged.submitting || staged.removing;
      remove.setAttribute('aria-label', `Remove ${staged.displayName}`);
      remove.title = `Remove ${staged.displayName}`;
      remove.addEventListener('click', () => this.removeStagedAttachment(conversationId, staged.clientId), { signal: this.abort.signal });
      controls.append(remove);
      chip.append(copy, controls);
      nodes.push(chip);
    }
    parent.replaceChildren(...nodes);
    parent.hidden = nodes.length === 0;
  }

  private removeStagedAttachment(conversationId: string, clientId: string): void {
    const current = this.stagedAttachments.get(conversationId) ?? [];
    const staged = current.find((entry) => entry.clientId === clientId);
    if (!staged || staged.submitting || staged.removing) return;
    this.setComposerNotice('');
    if (staged.attachment) {
      const suppressed = this.suppressedAttachmentIds.get(conversationId) ?? new Set<string>();
      suppressed.add(staged.attachment.attachmentId);
      this.suppressedAttachmentIds.set(conversationId, suppressed);
      staged.removing = true;
      if (this.lastConversationId === conversationId) {
        this.renderStagedAttachments(conversationId);
        this.updateComposerControls();
      }
      void this.actions.deleteAttachment(conversationId, staged.attachment.attachmentId).then(() => {
        this.stagedAttachments.set(conversationId, (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => entry !== staged));
        this.revokePreviewUrl(staged.previewUrl);
        if (this.lastConversationId === conversationId) {
          this.renderStagedAttachments(conversationId);
          this.updateComposerControls();
        }
      }).catch((error: unknown) => {
        if (attachmentAlreadyAbsent(error)) {
          this.stagedAttachments.set(conversationId, (this.stagedAttachments.get(conversationId) ?? []).filter((entry) => entry !== staged));
          this.revokePreviewUrl(staged.previewUrl);
          if (this.lastConversationId === conversationId) {
            this.setComposerNotice('');
            this.renderStagedAttachments(conversationId);
            this.updateComposerControls();
          }
          return;
        }
        suppressed.delete(staged.attachment!.attachmentId);
        staged.removed = false;
        staged.removing = false;
        if (this.lastConversationId === conversationId) {
          this.setComposerNotice(error instanceof Error ? `Could not remove attachment: ${error.message}` : 'Could not remove attachment.');
          this.renderStagedAttachments(conversationId);
          this.updateComposerControls();
        }
      });
      return;
    }
    staged.removed = true;
    staged.removing = staged.status === 'uploading';
    if (staged.status === 'queued') {
      this.attachmentUploadQueue = this.attachmentUploadQueue.filter((entry) => entry !== staged);
    }
    if (!staged.removing) {
      this.stagedAttachments.set(conversationId, current.filter((entry) => entry !== staged));
      this.revokePreviewUrl(staged.previewUrl);
    }
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
  }

  private updateComposerControls(): void {
    if (!this.state) return;
    const conversation = currentConversation(this.state);
    const button = this.$<HTMLButtonElement>('.cw-send-stop');
    if (!conversation) { button.disabled = true; return; }
    if (button.dataset.mode === 'stop') {
      button.disabled = this.stoppingConversationIds.has(conversation.conversationId)
        || this.state.interruptingConversationIds.includes(conversation.conversationId);
      return;
    }
    const staged = (this.stagedAttachments.get(conversation.conversationId) ?? []).filter((entry) => !entry.removed);
    const ready = staged.filter((entry) => entry.status === 'ready' && entry.attachment);
    const unavailable = staged.some((entry) => entry.status !== 'ready' || entry.submitting || entry.removing);
    const hasText = !!this.$<HTMLTextAreaElement>('.cw-composer textarea').value.trim();
    button.disabled = !conversationCanSend(this.state, conversation) || unavailable || !hasText && ready.length === 0;
  }

  private submitComposer(): void {
    const input = this.$<HTMLTextAreaElement>('.cw-composer textarea');
    const button = this.$<HTMLButtonElement>('.cw-send-stop');
    if (button.dataset.mode !== 'send' || button.disabled) return;
    const text = input.value.trim();
    const conversationId = this.lastConversationId;
    if (!conversationId) return;
    const staged = (this.stagedAttachments.get(conversationId) ?? [])
      .filter((entry) => !entry.removed && entry.status === 'ready' && entry.attachment);
    const attachmentIds = staged.map((entry) => entry.attachment!.attachmentId);
    if (!text && !attachmentIds.length) return;
    for (const entry of staged) entry.submitting = true;
    this.renderStagedAttachments(conversationId);
    this.updateComposerControls();
    void Promise.resolve(this.actions.sendMessage(text, attachmentIds)).then((accepted) => {
      if (!accepted) this.releaseSubmittingAttachments(conversationId, attachmentIds);
    }).catch(() => this.releaseSubmittingAttachments(conversationId, attachmentIds));
  }

  private releaseSubmittingAttachments(conversationId: string, attachmentIds: string[]): void {
    const submitted = new Set(attachmentIds);
    for (const staged of this.stagedAttachments.get(conversationId) ?? []) {
      if (staged.attachment && submitted.has(staged.attachment.attachmentId)) staged.submitting = false;
    }
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
  }

  clearComposerIfUnchanged(
    submittedText: string,
    submittedAttachmentIds: string[] = [],
    conversationId: string | null = this.lastConversationId,
  ): void {
    const input = this.$<HTMLTextAreaElement>('.cw-composer textarea');
    if (!conversationId) return;
    const sideChat = this.knownSideChatIds.has(conversationId);
    if (this.lastConversationId === conversationId) {
      if (input.value.trim() === submittedText) {
        input.value = '';
        if (sideChat) this.storeSideChatDraft(conversationId, '');
        else {
          try { localStorage.removeItem(this.draftKey(conversationId)); }
          catch { /* Storage can be unavailable in hardened browser modes. */ }
        }
      }
    } else {
      if (sideChat) {
        if (this.sideChatDraftValue(conversationId).trim() === submittedText) this.storeSideChatDraft(conversationId, '');
      } else {
        try {
          const saved = localStorage.getItem(this.draftKey(conversationId));
          if ((saved ?? '').trim() === submittedText) localStorage.removeItem(this.draftKey(conversationId));
        } catch { /* Storage can be unavailable in hardened browser modes. */ }
      }
    }
    const submitted = new Set(submittedAttachmentIds);
    const suppressed = this.suppressedAttachmentIds.get(conversationId) ?? new Set<string>();
    for (const attachmentId of submitted) suppressed.add(attachmentId);
    this.suppressedAttachmentIds.set(conversationId, suppressed);
    const remaining: StagedAttachment[] = [];
    for (const staged of this.stagedAttachments.get(conversationId) ?? []) {
      if (staged.attachment && submitted.has(staged.attachment.attachmentId)) this.revokePreviewUrl(staged.previewUrl);
      else remaining.push(staged);
    }
    this.stagedAttachments.set(conversationId, remaining);
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
  }

  clearStagedAttachments(conversationId: string): void {
    for (const staged of this.stagedAttachments.get(conversationId) ?? []) {
      staged.removed = true;
      this.revokePreviewUrl(staged.previewUrl);
    }
    this.stagedAttachments.delete(conversationId);
    this.attachmentUploadQueue = this.attachmentUploadQueue.filter((staged) => {
      if (staged.conversationId !== conversationId) return true;
      staged.removed = true;
      return false;
    });
    this.suppressedAttachmentIds.delete(conversationId);
    if (this.lastConversationId === conversationId) {
      this.renderStagedAttachments(conversationId);
      this.updateComposerControls();
    }
  }

  forgetConversation(conversationId: string): void {
    this.clearStagedAttachments(conversationId);
    this.storeSideChatDraft(conversationId, '');
    this.knownSideChatIds.delete(conversationId);
    if (this.lastConversationId === conversationId) {
      this.$<HTMLTextAreaElement>('.cw-composer textarea').value = '';
      this.lastConversationId = null;
      this.activitySignature = '';
    }
  }

  focusConversationTab(conversationId: string): void {
    queueMicrotask(() => {
      const tabs = [...this.$('.cw-conversation-tabs').querySelectorAll<HTMLButtonElement>('[role="tab"]')];
      const tab = tabs.find((candidate) => candidate.dataset.conversationId === conversationId)
        ?? tabs.find((candidate) => candidate.getAttribute('aria-selected') === 'true');
      tab?.focus();
    });
  }

  selectHost(hostId: string): void {
    this.pickerTouched = true;
    this.retryModelCatalog(hostId);
    const select = this.$<HTMLSelectElement>('.cw-new-form [name="host"]');
    select.value = hostId;
    this.renderProjectOptions();
    this.renderNewConversationModelOptions();
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

  private sideChatDraftKey(conversationId: string): string {
    return `rightmemory:side-chat-draft:${encodeURIComponent(this.rootKey)}:${encodeURIComponent(conversationId)}`;
  }

  private sideChatDraftValue(conversationId: string): string {
    try { return sessionStorage.getItem(this.sideChatDraftKey(conversationId)) ?? this.ephemeralDrafts.get(conversationId) ?? ''; }
    catch { return this.ephemeralDrafts.get(conversationId) ?? ''; }
  }

  private storeSideChatDraft(conversationId: string, value: string): void {
    if (value) this.ephemeralDrafts.set(conversationId, value);
    else this.ephemeralDrafts.delete(conversationId);
    try {
      if (value) sessionStorage.setItem(this.sideChatDraftKey(conversationId), value);
      else sessionStorage.removeItem(this.sideChatDraftKey(conversationId));
    } catch { /* Keep the in-memory fallback when session storage is unavailable. */ }
  }

  private saveDraft(conversationId = this.lastConversationId): void {
    if (!conversationId) return;
    const value = this.$<HTMLTextAreaElement>('.cw-composer textarea').value;
    if (this.knownSideChatIds.has(conversationId)) {
      this.storeSideChatDraft(conversationId, value);
      return;
    }
    try {
      if (value) localStorage.setItem(this.draftKey(conversationId), value);
      else localStorage.removeItem(this.draftKey(conversationId));
    } catch { /* Storage can be unavailable in hardened browser modes. */ }
  }

  private loadDraft(conversationId: string): void {
    if (this.knownSideChatIds.has(conversationId)) {
      this.$<HTMLTextAreaElement>('.cw-composer textarea').value = this.sideChatDraftValue(conversationId);
      return;
    }
    try { this.$<HTMLTextAreaElement>('.cw-composer textarea').value = localStorage.getItem(this.draftKey(conversationId)) ?? ''; }
    catch { this.$<HTMLTextAreaElement>('.cw-composer textarea').value = ''; }
  }

  private scrollToBottom(): void {
    const activity = this.$('.cw-activity');
    activity.scrollTop = activity.scrollHeight;
    this.$('.cw-unread').hidden = true;
    if (this.lastConversationId) this.actions.acknowledgeRead(this.lastConversationId);
  }

  isFollowingActivity(conversationId: string): boolean {
    if (this.lastConversationId !== conversationId || this.$<HTMLElement>('.cw-detail-view').hidden) return false;
    const activity = this.$('.cw-activity');
    return activity.scrollHeight - activity.scrollTop - activity.clientHeight < 90;
  }

  destroy(): void {
    this.saveDraft();
    this.updateElapsedTimer(false);
    for (const staged of this.stagedAttachments.values()) {
      for (const attachment of staged) {
        attachment.removed = true;
        this.revokePreviewUrl(attachment.previewUrl);
      }
    }
    this.attachmentUploadQueue = [];
    this.abort.abort();
  }
}

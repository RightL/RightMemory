import { ApiError, MutationQueue, type QueueChange, type Transport } from './queue.ts';
import { applyOperation, childrenOf, createSiblingBeforeOperation, deletionSelection, DRAFT_PREFIX, indexTree, moveSiblingOperation, promoteOperation,
  type MutationResult, type Operation, type Snapshot } from './tree.ts';
import { DraftBook, type TitleDraft } from './drafts.ts';
import { MapRenderer } from './renderer.ts';
import { parseWholeTitleFormat, setWholeTitleMark, titleText, type TopicMark } from './title-format.ts';
import { keyboardCommand, navigate, readView, reconcileView, reveal, writeView, type ViewState } from './view-state.ts';
import { apiContext, apiTransport, type FetchJson } from './pursuit-api.ts';
import './pursuit-map.css';

const EDIT_IDLE_MS = 3000;
const ACTIVITY_INTERVAL_MS = 1000;

const markup = `
  <div class="pm-toolbar" role="toolbar" aria-label="Pursuit Map tools">
    <div class="pm-tool-group">
      <button type="button" data-command="child" title="Add child (Tab)"><span aria-hidden="true">＋</span> Child</button>
      <button type="button" data-command="sibling" title="Add sibling (Enter)">Sibling</button>
      <span class="pm-divider"></span>
      <button type="button" data-command="undo" title="Undo (Ctrl/⌘ Z)" aria-label="Undo">↶</button>
      <button type="button" data-command="redo" title="Redo (Ctrl/⌘ Shift Z)" aria-label="Redo">↷</button>
    </div>
    <div class="pm-tool-group pm-node-tools">
      <button type="button" data-command="note" title="Edit note (N)">▤ <span>Note</span></button>
      <button type="button" data-command="focus" title="Toggle Focus (F)" aria-pressed="false">✦ <span>Focus</span></button>
      <button type="button" data-command="collapse" title="Expand or collapse (Space)" aria-label="Expand or collapse selected direction">⊟</button>
      <details class="pm-more"><summary aria-label="More node actions" title="More actions">···</summary><div class="pm-menu">
        <button type="button" data-command="copy-context">Copy context</button>
        <button type="button" data-command="rename">Rename <kbd>F2</kbd></button>
        <button type="button" data-command="promote">Promote <kbd>Shift Tab</kbd></button>
        <button type="button" data-command="root">New top-level direction</button>
        <button type="button" data-command="relations">Related directions</button>
        <button type="button" data-command="delete" class="pm-danger">Delete subtree <kbd>Del</kbd></button>
      </div></details>
    </div>
    <div class="pm-tool-group pm-view-tools">
      <span class="pm-save-status" role="status" aria-live="polite">Loading…</span>
      <button type="button" data-command="search" title="Find (Ctrl/⌘ F)" aria-label="Find directions">⌕</button>
      <button type="button" data-command="fit" title="Fit map (Ctrl/⌘ 0)">Fit</button>
      <details class="pm-help"><summary aria-label="Keyboard shortcuts" title="Keyboard shortcuts">?</summary><div>
        <strong>On the canvas</strong>
        <p>Double-click or F2 to rename. Enter adds a sibling after; Shift+Enter adds one before. Tab adds a child. Shift+Tab promotes. Alt+Up/Down reorders siblings.</p>
        <p>Ctrl/⌘ B toggles bold, Ctrl/⌘ U underline, Ctrl/⌘ Shift X strikethrough. Finish title editing before applying these whole-topic marks.</p>
        <p>Shift-drag empty space to select several directions. Ctrl/⌘+Shift-drag adds to the selection; Ctrl/⌘-click toggles one direction.</p>
        <p>Arrows move between directions. Space folds a branch. Delete removes a subtree. N opens its note; F toggles Focus.</p>
        <p>Ctrl/⌘ F finds, Ctrl/⌘ 0 fits, Ctrl/⌘ Z undoes, Ctrl/⌘ Shift Z redoes.</p>
        <p>Drag a label onto a direction to nest it; drag above or below a label to reorder. Drop on empty space to make an independent top-level direction.</p>
        <p>Drag empty space to pan. Ctrl/⌘ + wheel zooms. Touch pans from any label; pinch zooms without moving directions. Fit shows every independent map.</p>
      </div></details>
    </div>
  </div>
  <div class="pm-diagnostics" role="status" hidden></div>
  <div class="pm-stage">
    <div class="pm-canvas"></div>
    <div class="pm-topic-toolbar" role="toolbar" aria-label="Selected direction tools" hidden>
      <button type="button" data-command="bold" aria-label="Bold topic" title="Bold (Ctrl/⌘ B)" aria-pressed="false"><strong>B</strong></button>
      <button type="button" data-command="underline" aria-label="Underline topic" title="Underline (Ctrl/⌘ U)" aria-pressed="false"><u>U</u></button>
      <button type="button" data-command="strike" aria-label="Strikethrough topic" title="Strikethrough (Ctrl/⌘ Shift X)" aria-pressed="false"><s>S</s></button>
      <span class="pm-divider"></span>
      <button type="button" data-command="note">Note</button>
      <button type="button" data-command="focus" aria-pressed="false">Focus</button>
      <button type="button" data-command="context-menu" aria-label="More topic actions" aria-haspopup="menu" aria-expanded="false">More</button>
    </div>
    <div class="pm-context-menu" role="menu" aria-label="Map actions" hidden></div>
    <div class="pm-empty" hidden><h2>No directions yet</h2><p>Start with a direction you want to keep visible.</p><button type="button" data-command="root">＋ Add a direction</button></div>
    <div class="pm-search" role="search" hidden>
      <input type="search" aria-label="Find a direction" placeholder="Find a direction…" autocomplete="off">
      <span class="pm-search-count" aria-live="polite"></span>
      <button type="button" data-command="previous-result" aria-label="Previous result">↑</button>
      <button type="button" data-command="next-result" aria-label="Next result">↓</button>
      <button type="button" data-command="close-search" aria-label="Close search">×</button>
    </div>
    <aside class="pm-note" aria-label="Direction note" hidden>
      <header><div><small>NOTE · MARKDOWN</small><h2></h2></div><button type="button" data-command="close-note" aria-label="Save and close note">×</button></header>
      <textarea aria-label="Markdown note" spellcheck="false" placeholder="Add context in your own words…"></textarea>
      <div class="pm-note-status" role="status" aria-live="polite"></div>
      <footer><span>Ctrl/⌘ S saves · Close also saves</span><button type="button" data-command="discard-note" hidden>Discard changes</button><button type="button" data-command="save-note">Save</button></footer>
    </aside>
    <aside class="pm-relations" hidden><header><strong>Related directions</strong><button type="button" data-command="close-relations" aria-label="Close related directions">×</button></header><div></div></aside>
    <div class="pm-recovery" aria-label="Unsaved titles" hidden></div>
    <div class="pm-toast" role="status" aria-live="polite" hidden><span></span><button type="button" hidden></button><button type="button" class="pm-toast-close" aria-label="Dismiss message">×</button></div>
    <div class="pm-zoom" aria-label="Zoom controls"><button type="button" data-command="zoom-out" aria-label="Zoom out">−</button><span>100%</span><button type="button" data-command="zoom-in" aria-label="Zoom in">＋</button></div>
    <p id="pm-keyboard-hint" class="pm-canvas-hint">Enter · sibling <span>Tab · child</span> <span>Double-click · rename</span></p>
  </div>
`;

export interface PursuitMapController {
  refresh(): Promise<void>;
  flush(): Promise<void>;
  setActive(active: boolean): void;
  getSelectedId(): string | null;
  subscribeSelection(listener: (id: string | null) => void): () => void;
  readonly hasUnsavedChanges: boolean;
  destroy(): Promise<void>;
}

export async function mountPursuitMap(host: HTMLElement, fetchJson: FetchJson): Promise<PursuitMapController> {
  return mountMap(host, apiTransport(fetchJson), { context: apiContext(fetchJson) });
}

export interface MountMapOptions {
  context?(itemId: string, revision: string): Promise<string>;
  writeClipboard?(text: string): Promise<void>;
}

/** Exported separately so the browser fixture exercises the complete UI with disposable state. */
export async function mountMap(host: HTMLElement, transport: Transport, options: MountMapOptions = {}): Promise<PursuitMapController> {
  const snapshot = await transport.load();
  return new PursuitMap(host, snapshot, transport, options);
}

class PursuitMap implements PursuitMapController {
  private queue: MutationQueue;
  private renderer: MapRenderer;
  private view: ViewState;
  private drafts = new DraftBook();
  private abort = new AbortController();
  private active = true;
  private copyingContext = false;
  private matches: string[] = [];
  private matchIndex = -1;
  private toastTimer: ReturnType<typeof setTimeout> | undefined;
  private idleTimer: ReturnType<typeof setTimeout> | undefined;
  private activityTimer: ReturnType<typeof setTimeout> | undefined;
  private lastActivitySent = 0;
  private unsubscribe: () => void;
  private selectionListeners = new Set<(id: string | null) => void>();
  private lastNotifiedSelection: string | null;

  constructor(private host: HTMLElement, snapshot: Snapshot, transport: Transport, private options: MountMapOptions = {}) {
    host.className = 'pursuit-map';
    host.innerHTML = markup;
    this.queue = new MutationQueue(snapshot, transport);
    this.view = reconcileView(snapshot, readView(localStorage, snapshot.root_key));
    this.lastNotifiedSelection = this.view.selected ?? null;
    this.renderer = new MapRenderer(this.$('.pm-canvas'), snapshot, this.view, {
      select: (ids, primary) => {
        this.closeContextMenu();
        this.view.selectedIds = [...ids];
        this.view.selected = primary;
        this.saveView(); this.updateTools(); this.notifySelection();
      },
      collapse: (id, collapsed, preserveDrag) => this.setCollapsed(id, collapsed, preserveDrag),
      move: (operation) => { void this.mutate(operation); },
      editStart: (id, title) => this.editStarted(id, title),
      editInput: (text) => { if (this.drafts.title) this.drafts.title.text = text; },
      editEnd: (id, text, canceled) => this.editEnded(id, text, canceled),
      marker: (id, kind) => { this.select(id); void this.command(kind); },
      viewport: (viewport) => { this.view.viewport = viewport; this.$('.pm-zoom span').textContent = `${Math.round(viewport.scale * 100)}%`; this.saveView(); },
      error: (message) => this.toast(message),
      geometry: (rect) => this.positionTopicToolbar(rect),
      dismissOverlays: () => this.closeContextMenu(),
      contextMenu: (id, x, y) => this.openContextMenu(id, x, y),
    });
    this.unsubscribe = this.queue.subscribe((change) => this.changed(change));
    for (const type of ['input', 'keydown', 'pointerdown', 'pointerup', 'wheel', 'compositionupdate']) {
      host.addEventListener(type, () => this.recordActivity(), { capture: true, passive: true, signal: this.abort.signal });
    }
    host.addEventListener('pointermove', (event) => {
      if (event.buttons) this.recordActivity();
    }, { capture: true, passive: true, signal: this.abort.signal });
    host.addEventListener('click', (event) => {
      const button = (event.target as HTMLElement).closest<HTMLButtonElement>('button[data-command]');
      if (button?.disabled) return;
      if (button) { this.$<HTMLDetailsElement>('.pm-more').open = false; void this.command(button.dataset.command!); }
    }, { signal: this.abort.signal });
    // Opening Copy context must not blur and silently save an unfinished title.
    this.$('.pm-more').addEventListener('pointerdown', (event) => {
      if (this.renderer.editing && event.button === 0) event.preventDefault();
    }, { signal: this.abort.signal });
    this.$('.pm-topic-toolbar').addEventListener('pointerdown', (event) => {
      if (event.button === 0) event.preventDefault(); // Retain canvas keyboard focus after formatting.
    }, { signal: this.abort.signal });
    document.addEventListener('pointerdown', (event) => {
      if (!this.active) return;
      const target = event.target as Node;
      if (!this.host.contains(target)) this.select(null);
      if (!(event.target as HTMLElement).closest('.pm-context-menu, [data-command="context-menu"]')) this.closeContextMenu();
    }, { signal: this.abort.signal });
    host.addEventListener('contextmenu', (event) => {
      if (!(event.target as HTMLElement).closest('input, textarea, [contenteditable]')) event.preventDefault();
    }, { signal: this.abort.signal });
    document.addEventListener('scroll', (event) => {
      if (!(event.target instanceof Element) || !event.target.closest('.pm-context-menu')) this.closeContextMenu();
    }, { capture: true, signal: this.abort.signal });
    this.$<HTMLInputElement>('.pm-search input').addEventListener('input', () => this.search(), { signal: this.abort.signal });
    this.$<HTMLInputElement>('.pm-search input').addEventListener('keydown', (event) => {
      if (event.key === 'Enter') { event.preventDefault(); this.nextMatch(event.shiftKey ? -1 : 1); }
      if (event.key === 'Escape') { event.preventDefault(); this.closeSearch(); }
    }, { signal: this.abort.signal });
    this.$<HTMLTextAreaElement>('.pm-note textarea').addEventListener('input', (event) => {
      if (this.drafts.note) this.drafts.note.text = (event.target as HTMLTextAreaElement).value;
      this.updateNote(false);
    }, { signal: this.abort.signal });
    document.addEventListener('keydown', (event) => this.keydown(event), { capture: true, signal: this.abort.signal });
    window.addEventListener('beforeunload', (event) => {
      if (this.queue.pendingCount || this.drafts.dirty) { event.preventDefault(); event.returnValue = ''; }
    }, { signal: this.abort.signal });
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) this.flushInBackground();
    }, { signal: this.abort.signal });
    window.addEventListener('pagehide', () => this.flushInBackground(), { signal: this.abort.signal });
    window.addEventListener('focus', () => {
      if (this.active && !this.renderer.editing) void this.refresh().catch((error) => this.toast(error.message));
    }, { signal: this.abort.signal });
    this.$('.pm-toast-close').addEventListener('click', () => { this.$('.pm-toast').hidden = true; }, { signal: this.abort.signal });
    this.updateTools();
    this.renderDiagnostics(snapshot);
    requestAnimationFrame(() => this.renderer.focus());
  }

  private $<T extends HTMLElement = HTMLElement>(selector: string): T { return this.host.querySelector<T>(selector)!; }
  private saveView(): void { writeView(localStorage, this.queue.snapshot.root_key, this.view); }
  private get snapshot(): Snapshot { return this.queue.snapshot; }
  private get selected() { return indexTree(this.snapshot).get(this.view.selected ?? ''); }
  private get selectedItems() {
    const items = indexTree(this.snapshot);
    return this.view.selectedIds.map((id) => items.get(id)).filter((item) => item !== undefined);
  }

  private displaySnapshot(): Snapshot {
    const draft = this.drafts.title;
    if (draft?.operation.type === 'create') {
      try { return applyOperation(this.snapshot, { ...draft.operation, title: draft.text || 'New direction' }, draft.id); }
      catch { return this.snapshot; }
    }
    return this.snapshot;
  }

  private render(preserveDrag = false): void {
    const display = this.displaySnapshot();
    this.view = reconcileView(display, this.view);
    this.renderer.render(display, this.view, preserveDrag);
    this.renderer.highlight(new Set(this.matches));
    this.updateTools();
    this.saveView();
    this.notifySelection();
  }

  private notifySelection(): void {
    const selected = this.view.selected ?? null;
    if (selected === this.lastNotifiedSelection) return;
    this.lastNotifiedSelection = selected;
    for (const listener of this.selectionListeners) listener(selected);
  }

  private changed(change: QueueChange): void {
    this.closeContextMenu();
    for (const { from, to } of change.remapped ?? []) {
      if (this.view.selected === from) this.view.selected = to;
      this.view.selectedIds = this.view.selectedIds.map((id) => id === from ? to : id);
      this.view.collapsed = this.view.collapsed.map((id) => id === from ? to : id);
      this.drafts.remap(from, to);
    }
    this.drafts.reconcile(change.snapshot);
    this.render();
    this.updateNote();
    this.renderDiagnostics(change.snapshot);
    if (change.error) this.toast(`${change.error.message} Unsaved text is kept.`, undefined, 0);
    else if (change.snapshot.pending && change.snapshot.writable && !change.flushing && !this.idleTimer) this.scheduleFlush();
  }

  private scheduleFlush(): void {
    clearTimeout(this.idleTimer);
    this.idleTimer = setTimeout(() => {
      this.idleTimer = undefined;
      void this.queue.flush().catch(() => { /* The queue exposes checkpoint failures in the map. */ });
    }, EDIT_IDLE_MS);
  }

  private recordActivity(): void {
    if (!this.active || this.abort.signal.aborted) return;
    if (this.snapshot.writable) this.scheduleFlush();
    const delay = ACTIVITY_INTERVAL_MS - (Date.now() - this.lastActivitySent);
    if (delay <= 0) void this.sendActivity();
    else if (!this.activityTimer) this.activityTimer = setTimeout(() => { void this.sendActivity(); }, delay);
  }

  private async sendActivity(): Promise<void> {
    clearTimeout(this.activityTimer);
    this.activityTimer = undefined;
    if (!this.active || this.abort.signal.aborted) return;
    this.lastActivitySent = Date.now();
    try { await this.queue.activity(); }
    catch (error) {
      if (this.abort.signal.aborted) return;
      this.toast((error as Error).message, undefined, 0);
      try { await this.refresh(); } catch { /* Preserve the last safely acknowledged map. */ }
    }
  }

  private flushInBackground(): void {
    clearTimeout(this.idleTimer);
    this.idleTimer = undefined;
    void this.queue.flush(true).catch(() => { /* The server retains saved edits if closing cannot flush. */ });
  }

  private updateTools(): void {
    const item = this.selected;
    const selectedItems = this.selectedItems;
    const writable = this.snapshot.writable;
    this.$<HTMLButtonElement>('[data-command="undo"]').disabled = !this.queue.canUndo;
    this.$<HTMLButtonElement>('[data-command="redo"]').disabled = !this.queue.canRedo;
    for (const button of this.host.querySelectorAll<HTMLButtonElement>('button[data-command]')) {
      const command = button.dataset.command!;
      if (['rename', 'delete', 'focus'].includes(command)) button.disabled = !writable || !item?.editable;
      if (['bold', 'underline', 'strike'].includes(command)) button.disabled = !writable || !selectedItems.length || selectedItems.some((entry) => !entry.editable);
      if (command === 'promote') button.disabled = !writable || !item?.editable || !item.parent_id;
      if (command === 'note' || command === 'context-menu') button.disabled = !item;
      if (command === 'copy-context') button.disabled = !item || this.copyingContext;
      if (command === 'child') button.disabled = !writable || !!item && !item.editable;
      if (['root', 'sibling', 'sibling-before'].includes(command)) button.disabled = !writable || command !== 'root' && !!item?.parent_id && !indexTree(this.snapshot).get(item.parent_id)?.editable;
      if (command === 'collapse') button.disabled = !item?.child_ids.length;
      if (['bold', 'underline', 'strike', 'focus'].includes(command)) {
        let pressed: boolean | 'mixed' = !!item?.focused;
        if (command !== 'focus') {
          const states = selectedItems.map((entry) => parseWholeTitleFormat(entry.title).marks.has(command as TopicMark));
          pressed = states.length > 0 && states.every(Boolean) ? true : states.some(Boolean) ? 'mixed' : false;
        }
        button.setAttribute(button.getAttribute('role') === 'menuitemcheckbox' ? 'aria-checked' : 'aria-pressed', String(pressed));
      }
    }
    this.$('.pm-empty').hidden = this.displaySnapshot().root_ids.length > 0;
    const status = this.$('.pm-save-status');
    status.textContent = this.queue.pendingCount || this.queue.flushing ? 'Saving…' : writable ? 'Saved' : this.snapshot.pending ? 'Needs attention' : 'Read-only';
    status.classList.toggle('pm-saving', !!this.queue.pendingCount || this.queue.flushing);
    this.positionTopicToolbar(this.renderer?.selectedTopicRect() ?? null);
  }

  private positionTopicToolbar(rect: DOMRect | null): void {
    const toolbar = this.$('.pm-topic-toolbar');
    toolbar.hidden = !rect || !this.view.selected || this.view.selected.startsWith(DRAFT_PREFIX) || !this.active || !this.$('.pm-context-menu').hidden;
    if (toolbar.hidden || !rect) return;
    const stage = this.$('.pm-stage').getBoundingClientRect();
    const width = toolbar.offsetWidth;
    const height = toolbar.offsetHeight;
    const top = rect.top - stage.top - height - 10;
    toolbar.style.left = `${Math.max(6, Math.min(stage.width - width - 6, rect.left - stage.left + (rect.width - width) / 2))}px`;
    toolbar.style.top = `${Math.max(6, Math.min(stage.height - height - 6, top >= 6 ? top : rect.bottom - stage.top + 10))}px`;
  }

  private closeContextMenu(focus = false): void {
    this.$('.pm-context-menu').hidden = true;
    this.$('[data-command="context-menu"]').setAttribute('aria-expanded', 'false');
    if (focus) this.renderer.focus();
    this.positionTopicToolbar(this.renderer?.selectedTopicRect() ?? null);
  }

  private openContextMenu(id: string | null, x: number, y: number): void {
    const menu = this.$('.pm-context-menu');
    menu.replaceChildren();
    const item = id ? indexTree(this.snapshot).get(id) : undefined;
    const entries: Array<[string, string] | null> = item ? [
      ['child', 'Add child'], ['sibling', 'Add sibling after'], ['sibling-before', 'Add sibling before'], ['rename', 'Rename'], null,
      ['bold', 'Bold'], ['underline', 'Underline'], ['strike', 'Strikethrough'], null,
      ['copy-context', 'Copy context'], ['note', 'Note'], ['focus', 'Toggle Focus'],
      ...(item.child_ids.length ? [['collapse', this.view.collapsed.includes(item.id) ? 'Expand' : 'Collapse'] as [string, string]] : []),
      ...(item.parent_id ? [['promote', 'Promote'] as [string, string]] : []), null, ['delete', 'Delete subtree'],
    ] : [['root', 'Add top-level direction'], ['fit', 'Fit map']];
    for (const entry of entries) {
      if (!entry) { const line = document.createElement('hr'); line.setAttribute('role', 'separator'); menu.append(line); continue; }
      const [command, label] = entry;
      const button = document.createElement('button');
      button.type = 'button'; button.textContent = label; button.dataset.command = command; button.tabIndex = -1;
      button.setAttribute('role', ['bold', 'underline', 'strike', 'focus'].includes(command) ? 'menuitemcheckbox' : 'menuitem');
      if (command === 'delete') button.className = 'pm-danger';
      menu.append(button);
    }
    menu.hidden = false;
    this.updateTools();
    const stage = this.$('.pm-stage').getBoundingClientRect();
    menu.style.maxHeight = `${Math.max(0, stage.height - 12)}px`;
    menu.style.left = `${Math.max(6, Math.min(stage.width - menu.offsetWidth - 6, x - stage.left))}px`;
    menu.style.top = `${Math.max(6, Math.min(stage.height - menu.offsetHeight - 6, y - stage.top))}px`;
    this.$('[data-command="context-menu"]').setAttribute('aria-expanded', 'true');
    menu.querySelector<HTMLButtonElement>('button:not(:disabled)')?.focus();
  }

  private menuKeydown(event: KeyboardEvent): void {
    const menu = this.$('.pm-context-menu');
    const buttons = [...menu.querySelectorAll<HTMLButtonElement>('button:not(:disabled)')];
    const index = buttons.indexOf(document.activeElement as HTMLButtonElement);
    if (['ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key)) {
      event.preventDefault(); event.stopPropagation();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? buttons.length - 1
        : (index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length;
      buttons[next]?.focus();
    } else if (event.key === 'Escape' || event.key === 'Tab') {
      event.preventDefault(); event.stopPropagation(); this.closeContextMenu(true);
    }
  }

  private renderDiagnostics(snapshot: Snapshot): void {
    const banner = this.$('.pm-diagnostics');
    banner.replaceChildren();
    banner.hidden = snapshot.writable && !snapshot.diagnostics.length;
    if (banner.hidden) return;
    const title = document.createElement('strong');
    title.textContent = snapshot.writable ? 'Map notices' : snapshot.pending ? 'Saved edits need attention' : 'This map is read-only';
    banner.append(title);
    const details = document.createElement('details');
    const summary = document.createElement('summary');
    summary.textContent = 'Show diagnostics';
    details.append(summary);
    details.open = snapshot.pending && !snapshot.writable;
    for (const diagnostic of snapshot.diagnostics) {
      const paragraph = document.createElement('p');
      paragraph.textContent = typeof diagnostic === 'string' ? diagnostic : diagnostic.message ?? JSON.stringify(diagnostic);
      details.append(paragraph);
    }
    banner.append(details);
    if (!snapshot.writable) {
      const refresh = document.createElement('button');
      refresh.type = 'button'; refresh.textContent = 'Refresh';
      refresh.addEventListener('click', () => { void this.refresh().catch((error) => this.toast(error.message, undefined, 0)); });
      banner.append(refresh);
    }
  }

  private select(id: string | null, center = false): void {
    this.closeContextMenu();
    const next = id === null ? { ...this.view, selected: null, selectedIds: [] } : reveal(this.displaySnapshot(), this.view, id);
    const unfolded = next.collapsed.length !== this.view.collapsed.length;
    this.view = next;
    if (unfolded) this.render();
    this.renderer.selectMany(next.selectedIds, next.selected, center);
    this.updateTools();
    this.saveView();
    this.notifySelection();
  }

  private setCollapsed(id: string, collapsed: boolean, preserveDrag = false): void {
    this.view.collapsed = this.view.collapsed.filter((entry) => entry !== id);
    if (collapsed) this.view.collapsed.push(id);
    this.render(preserveDrag);
  }

  private async command(command: string): Promise<void> {
    if (command !== 'context-menu') this.closeContextMenu(!this.$('.pm-context-menu').hidden);
    try {
      switch (command) {
        case 'child': this.create('child'); break;
        case 'sibling': this.create('sibling'); break;
        case 'sibling-before': this.create('sibling-before'); break;
        case 'sibling-up': case 'sibling-down': {
          if (!this.snapshot.writable || !this.selected?.editable) break;
          const operation = moveSiblingOperation(this.snapshot, this.selected.id, command === 'sibling-up' ? -1 : 1);
          if (operation) {
            const result = await this.mutate(operation);
            if (result?.selected_id) this.renderer.ensureVisible(result.selected_id);
          }
          this.renderer.focus();
          break;
        }
        case 'bold': case 'underline': case 'strike': await this.format(command); break;
        case 'context-menu': {
          const menu = this.$('.pm-context-menu');
          if (!menu.hidden) { this.closeContextMenu(true); break; }
          const rect = this.renderer.selectedTopicRect();
          if (rect) this.openContextMenu(this.view.selected, rect.left, rect.bottom + 8);
          break;
        }
        case 'root': this.create('root'); break;
        case 'rename': if (this.selected?.editable) await this.renderer.beginEdit(this.selected.id); break;
        case 'promote': {
          const operation = this.view.selected ? promoteOperation(this.snapshot, this.view.selected) : null;
          if (operation) await this.mutate(operation); break;
        }
        case 'delete': await this.remove(); break;
        case 'focus': if (this.selected?.editable) await this.mutate({ type: 'set_focus', id: this.selected.id, focused: !this.selected.focused }); break;
        case 'collapse': if (this.selected?.child_ids.length) this.setCollapsed(this.selected.id, !this.view.collapsed.includes(this.selected.id)); break;
        case 'copy-context': await this.copyContext(); break;
        case 'note': this.openNote(); break;
        case 'save-note': await this.saveNote(); break;
        case 'close-note': if (await this.saveNote()) { this.$('.pm-note').hidden = true; this.drafts.note = null; this.renderer.focus(); } break;
        case 'discard-note': this.drafts.note = null; this.$('.pm-note').hidden = true; this.renderer.focus(); break;
        case 'relations': this.openRelations(); break;
        case 'close-relations': this.$('.pm-relations').hidden = true; this.renderer.focus(); break;
        case 'search': this.$('.pm-search').hidden = false; this.$<HTMLInputElement>('.pm-search input').focus(); this.$<HTMLInputElement>('.pm-search input').select(); break;
        case 'close-search': this.closeSearch(); break;
        case 'next-result': this.nextMatch(1); break;
        case 'previous-result': this.nextMatch(-1); break;
        case 'fit': this.renderer.fit(); this.renderer.focus(); break;
        case 'zoom-in': this.renderer.zoom(0.1); break;
        case 'zoom-out': this.renderer.zoom(-0.1); break;
        case 'undo': case 'redo': {
          const result = await this.queue.history(command);
          if (result?.selected_id) this.select(result.selected_id, true);
          if (result) this.toast(command === 'undo' ? 'Undone.' : 'Redone.');
          break;
        }
      }
    } catch (error) { this.toast((error as Error).message, undefined, 0); }
  }

  private keydown(event: KeyboardEvent): void {
    if (!this.active) return;
    const target = event.target as HTMLElement;
    const isInput = target.matches('input,textarea,[contenteditable]');
    if (target.closest('.pm-context-menu')) { this.menuKeydown(event); return; }
    if (target.id === 'input-box') {
      // Raw title editing must never enable the browser's contenteditable rich-text commands.
      const command = keyboardCommand(event);
      if (command && ['bold', 'underline', 'strike'].includes(command)) { event.preventDefault(); event.stopImmediatePropagation(); }
      return; // The library owns IME, Enter and Escape while renaming.
    }
    const command = keyboardCommand(event, isInput);
    if (!command) return;
    if (isInput && command === 'save' && target.closest('.pm-note')) { event.preventDefault(); void this.command('save-note'); return; }
    if (isInput) return;
    // Shortcuts operate on the canvas, not the surrounding Web Studio navigation.
    if (!target.closest('.pm-canvas') && !['search', 'undo', 'redo', 'fit', 'escape'].includes(command)) return;
    event.preventDefault();
    event.stopPropagation();
    if (command === 'navigate') {
      const next = navigate(this.snapshot, this.view, event.key);
      const folded = next.collapsed.join('\n') !== this.view.collapsed.join('\n');
      this.view = next;
      if (folded) this.render();
      this.renderer.selectMany(this.view.selectedIds, this.view.selected, true);
      this.updateTools(); this.saveView(); this.notifySelection();
    } else if (command === 'escape') {
      this.renderer.cancelGesture();
      this.closeContextMenu();
      this.closeSearch();
      this.$('.pm-relations').hidden = true;
      this.$<HTMLDetailsElement>('.pm-more').open = false;
      this.$<HTMLDetailsElement>('.pm-help').open = false;
    } else void this.command(command);
  }

  private create(kind: 'child' | 'sibling' | 'sibling-before' | 'root', previous?: TitleDraft): void {
    if (!this.snapshot.writable || this.renderer.editing) return;
    const selected = this.selected;
    const parent = kind === 'root' ? null : kind === 'child' ? selected?.id ?? null : selected?.parent_id ?? null;
    const siblings = childrenOf(this.snapshot, parent);
    const after = kind === 'sibling' && selected ? selected.id : siblings.at(-1) ?? null;
    const id = previous?.id ?? `${DRAFT_PREFIX}${crypto.randomUUID()}`;
    const operation = previous?.operation.type === 'create' ? previous.operation : kind === 'sibling-before' && selected
      ? createSiblingBeforeOperation(this.snapshot, selected.id, '') : { type: 'create' as const, parent_id: parent, after_id: after, title: '' };
    this.drafts.title = previous ?? { id, temporaryId: id, text: '', operation };
    if (operation.parent_id) this.view = reveal(this.snapshot, this.view, operation.parent_id);
    this.view.collapsed = this.view.collapsed.filter((entry) => entry !== operation.parent_id);
    this.view.selected = id;
    this.view.selectedIds = [id];
    this.render();
    void this.renderer.beginEdit(id, this.drafts.title.text);
  }

  private editStarted(id: string, text: string): void {
    if (this.drafts.title?.id !== id) this.drafts.title = { id, text, operation: { type: 'rename', id, title: text } };
  }

  private editEnded(id: string, text: string, canceled: boolean): void {
    const draft = this.drafts.title;
    if (!draft || draft.id !== id) return;
    this.drafts.title = null;
    if (canceled || !text) { this.render(); return; }
    if (draft.operation.type === 'rename' && indexTree(this.snapshot).get(id)?.title === text) { this.render(); return; }
    draft.text = text;
    draft.operation = { ...draft.operation, title: text };
    this.drafts.savingTitles.push(draft);
    void this.queue.enqueue(draft.operation, draft.temporaryId).then(() => {
      this.drafts.savingTitles = this.drafts.savingTitles.filter((saving) => saving !== draft);
    }).catch((error: Error) => {
      this.drafts.failedTitle(draft, error.message);
      this.render();
      this.renderRecovery();
    });
    this.renderer.focus();
  }

  private renderRecovery(): void {
    const panel = this.$('.pm-recovery');
    panel.replaceChildren();
    panel.hidden = !this.drafts.failedTitles.length;
    if (panel.hidden) return;
    const title = document.createElement('strong');
    title.textContent = 'Unsaved titles kept here';
    panel.append(title);
    for (const draft of this.drafts.failedTitles) {
      const row = document.createElement('div');
      const text = document.createElement('span');
      text.textContent = titleText(draft.text);
      text.title = draft.error ?? '';
      const retry = document.createElement('button');
      retry.type = 'button'; retry.textContent = 'Edit again';
      retry.addEventListener('click', () => {
        if (this.renderer.editing) return;
        const operation = draft.operation;
        const items = indexTree(this.snapshot);
        if (operation.type === 'create' && operation.parent_id && !items.has(operation.parent_id) || operation.type === 'rename' && !items.has(operation.id)) {
          this.toast('That direction was removed elsewhere. The unsaved title is still here for you to copy.', undefined, 0); return;
        }
        this.drafts.failedTitles = this.drafts.failedTitles.filter((entry) => entry !== draft);
        if (operation.type === 'create') this.create('child', draft);
        else { this.drafts.title = draft; this.select(draft.id, true); void this.renderer.beginEdit(draft.id, draft.text); }
        this.renderRecovery();
      });
      const discard = document.createElement('button');
      discard.type = 'button'; discard.textContent = 'Discard';
      discard.addEventListener('click', () => { this.drafts.failedTitles = this.drafts.failedTitles.filter((entry) => entry !== draft); this.renderRecovery(); });
      row.append(text, retry, discard); panel.append(row);
    }
  }

  private async format(mark: TopicMark): Promise<void> {
    const selected = this.selectedItems;
    if (!selected.length || selected.some((item) => !item.editable) || !this.snapshot.writable || this.renderer.editing) return;
    const enabled = !selected.every((item) => parseWholeTitleFormat(item.title).marks.has(mark));
    const primary = this.selected;
    const ordered = primary ? [primary, ...selected.filter((item) => item.id !== primary.id)] : selected;
    const renames = ordered.map((item) => ({ id: item.id, title: setWholeTitleMark(item.title, mark, enabled) }))
      .filter((rename, index) => rename.title !== ordered[index].title);
    this.renderer.focus();
    if (renames.length) await this.mutate({ type: 'rename_many', renames });
  }

  private async mutate(operation: Operation): Promise<MutationResult | null> {
    this.closeContextMenu();
    try { return await this.queue.enqueue(operation); }
    catch (error) { this.toast((error as Error).message, undefined, 0); return null; }
  }

  private async remove(): Promise<void> {
    const item = this.selected;
    if (!item?.editable || !this.snapshot.writable) return;
    this.view.selected = deletionSelection(this.snapshot, item.id);
    this.view.selectedIds = this.view.selected ? [this.view.selected] : [];
    const promise = this.queue.enqueue({ type: 'delete', id: item.id });
    const undo = async () => {
      try {
        const result = await promise;
        if (this.queue.canUndo && this.queue.lastUndoId === result.operation_id) await this.command('undo');
        else this.toast('Newer edits exist. Use the toolbar Undo to undo them in order.');
      } catch { /* Failure already restored the deleted direction. */ }
    };
    this.toast(`“${titleText(item.title)}” removed.`, { label: 'Undo', action: () => { void undo(); } }, 0);
    try {
      const result = await promise;
      if (result.repaired_references.length) this.toast(`“${titleText(item.title)}” removed; broken references repaired.`, { label: 'Undo', action: () => { void undo(); } }, 0);
    } catch { /* The queue reports failure and restores the authoritative snapshot. */ }
  }

  private async copyContext(): Promise<void> {
    const item = this.selected;
    if (!item || item.id.startsWith(DRAFT_PREFIX)) { this.toast('Save the direction before copying its context.'); return; }
    if (this.copyingContext) return;
    if (this.queue.pendingCount) { this.toast('Wait for the current save to finish, then copy context.'); return; }
    if (this.drafts.dirty) { this.toast('Save or discard unsaved edits before copying context.'); return; }
    if (!this.options.context) { this.toast('Context copying is unavailable in this view.'); return; }
    this.copyingContext = true;
    this.updateTools();
    this.toast('Preparing context…', undefined, 0);
    try {
      await this.flush();
      const revision = this.snapshot.revision;
      const text = await this.options.context(item.id, revision);
      if (this.abort.signal.aborted) return;
      if (this.hasUnsavedChanges || this.snapshot.revision !== revision || !indexTree(this.snapshot).has(item.id)) {
        this.toast('The map changed while preparing context. Finish your edits and copy again.', undefined, 0);
        return;
      }
      try {
        if (this.options.writeClipboard) await this.options.writeClipboard(text);
        else {
          if (!navigator.clipboard?.writeText) throw new Error('Clipboard access is unavailable.');
          await navigator.clipboard.writeText(text);
        }
      } catch {
        this.toast('Could not copy context. Allow clipboard access in your browser, then try again.', undefined, 0);
        return;
      }
      if (!this.abort.signal.aborted) this.toast('Context copied. Paste it into Codex App.');
    } catch (error) {
      if (this.abort.signal.aborted) return;
      if (error instanceof ApiError && error.status === 409) {
        try {
          await this.refresh();
          this.toast('The map changed elsewhere and has been refreshed. Review the direction and copy again.', undefined, 0);
        } catch {
          this.toast('The map changed elsewhere. Refresh the map before copying context.', undefined, 0);
        }
      } else this.toast(`Could not prepare context: ${(error as Error).message}`, undefined, 0);
    } finally {
      this.copyingContext = false;
      if (!this.abort.signal.aborted) this.updateTools();
    }
  }

  private openNote(): void {
    const item = this.selected;
    if (!item) return;
    const existing = this.drafts.note;
    if (existing && existing.id !== item.id && (existing.saving || existing.text !== existing.savedText)) {
      this.toast('Save or discard the open note before opening another.'); this.$<HTMLTextAreaElement>('.pm-note textarea').focus(); return;
    }
    if (existing?.id !== item.id) this.drafts.openNote(item.id, item.body);
    this.$('.pm-note').hidden = false;
    this.updateNote();
    this.$<HTMLTextAreaElement>('.pm-note textarea').focus();
  }

  private updateNote(updateText = true): void {
    const note = this.drafts.note;
    if (!note) return;
    const item = indexTree(this.snapshot).get(note.id);
    this.$('.pm-note h2').textContent = item ? titleText(item.title) : 'Direction removed elsewhere';
    const textarea = this.$<HTMLTextAreaElement>('.pm-note textarea');
    if (updateText && textarea.value !== note.text) textarea.value = note.text;
    textarea.readOnly = !item?.editable || !this.snapshot.writable;
    const status = this.$('.pm-note-status');
    status.textContent = note.error ? `${note.error} Your text is kept. Review the map, then save again.` : note.saving ? 'Saving…' : note.text !== note.savedText ? 'Unsaved changes' : 'Saved';
    status.classList.toggle('pm-error', !!note.error);
    this.$<HTMLButtonElement>('[data-command="save-note"]').disabled = note.saving || !item?.editable || !this.snapshot.writable;
    this.$('[data-command="discard-note"]').hidden = note.text === note.savedText && !note.error;
  }

  private async saveNote(): Promise<boolean> {
    const note = this.drafts.note;
    if (!note) return true;
    if (note.saving) return false;
    if (note.text === note.savedText && !note.error) return true;
    const submitted = note.text;
    note.saving = true;
    this.updateNote(false);
    try {
      const result = await this.queue.enqueue({ type: 'edit_body', id: note.id, body: submitted });
      this.drafts.noteSaved(result.selected_id ?? note.id, submitted);
      this.updateNote(false);
      return this.drafts.note?.text === submitted;
    } catch (error) {
      this.drafts.noteFailed(note.id, (error as Error).message);
      this.updateNote(false);
      return false;
    }
  }

  private openRelations(): void {
    const item = this.selected;
    if (!item) return;
    const panel = this.$('.pm-relations');
    const content = this.$('.pm-relations > div');
    content.replaceChildren();
    const labels: Record<string, string> = { ref: 'Reference', depends: 'Depends on', supports: 'Supports', contradicts: 'Contradicts', related: 'Related to' };
    const items = indexTree(this.snapshot);
    for (const [kind, target] of item.edges) {
      const linked = items.get(target);
      const line = document.createElement(linked ? 'button' : 'p');
      line.textContent = `${labels[kind] ?? 'Related'} · ${linked ? titleText(linked.title) : 'Memory reference'}`;
      if (linked) line.addEventListener('click', () => this.select(linked.id, true));
      content.append(line);
    }
    if (!item.edges.length) content.textContent = 'No related directions.';
    panel.hidden = false;
  }

  private search(): void {
    const query = this.$<HTMLInputElement>('.pm-search input').value.trim().toLocaleLowerCase();
    this.matches = query ? this.snapshot.items.filter((item) => `${titleText(item.title)}\n${item.body}`.toLocaleLowerCase().includes(query)).map((item) => item.id) : [];
    this.matchIndex = -1;
    this.nextMatch(1);
  }

  private nextMatch(delta: number): void {
    if (this.matches.length) {
      this.matchIndex = (this.matchIndex + delta + this.matches.length) % this.matches.length;
      this.select(this.matches[this.matchIndex], true);
    }
    this.$('.pm-search-count').textContent = this.matches.length ? `${this.matchIndex + 1} / ${this.matches.length}` : this.$<HTMLInputElement>('.pm-search input').value ? 'No results' : '';
    this.renderer.highlight(new Set(this.matches));
  }

  private closeSearch(): void {
    this.$('.pm-search').hidden = true;
    this.matches = [];
    this.renderer.highlight(new Set());
    this.renderer.focus();
  }

  private toast(message: string, action?: { label: string; action(): void }, duration = 6000): void {
    clearTimeout(this.toastTimer);
    const toast = this.$('.pm-toast');
    toast.hidden = false;
    this.$('.pm-toast > span').textContent = message;
    const button = this.$<HTMLButtonElement>('.pm-toast > button:not(.pm-toast-close)');
    button.hidden = !action;
    button.textContent = action?.label ?? '';
    button.onclick = action?.action ?? null;
    if (duration) this.toastTimer = setTimeout(() => { toast.hidden = true; }, duration);
  }

  async refresh(): Promise<void> { await this.queue.reload(); }
  async flush(): Promise<void> {
    do { await this.queue.flush(); }
    while (this.queue.pendingCount || this.snapshot.pending);
  }
  getSelectedId(): string | null { return this.view.selected ?? null; }
  subscribeSelection(listener: (id: string | null) => void): () => void {
    this.selectionListeners.add(listener);
    listener(this.getSelectedId());
    return () => this.selectionListeners.delete(listener);
  }
  get hasUnsavedChanges(): boolean { return this.queue.pendingCount > 0 || this.drafts.dirty; }
  setActive(active: boolean): void {
    this.active = active;
    this.queue.setActive(active);
    if (active) { this.render(); this.renderer.focus(); }
    else { this.closeContextMenu(); this.renderer.cancelGesture(); this.saveView(); this.flushInBackground(); }
  }
  async destroy(): Promise<void> {
    this.queue.setActive(false);
    try { await this.flush(); }
    catch (error) { this.queue.setActive(this.active); throw error; }
    this.unsubscribe();
    this.selectionListeners.clear();
    clearTimeout(this.toastTimer);
    clearTimeout(this.idleTimer);
    clearTimeout(this.activityTimer);
    this.abort.abort();
    this.renderer.destroy();
  }
}

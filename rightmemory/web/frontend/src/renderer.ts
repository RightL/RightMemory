import MindElixir, { type MindElixirData, type Topic } from 'mind-elixir';
import 'mind-elixir/style.css';
import { assertMove, dropOperation, indexTree, type Operation, type Snapshot } from './tree.ts';
import { forestData, palette, stackMaps, titleMarkup, titleText } from './canvas-data.ts';
import { CanvasGestures, edgePanVelocity, type PointerSample } from './gestures.ts';
import { type ViewState } from './view-state.ts';

interface Callbacks {
  select(ids: string[], primary: string | null): void;
  collapse(id: string, collapsed: boolean, preserveDrag?: boolean): void;
  move(operation: Operation): void;
  editStart(id: string, title: string): void;
  editInput(text: string): void;
  editEnd(id: string, text: string, canceled: boolean): void;
  marker(id: string, kind: 'note' | 'focus' | 'relations'): void;
  viewport(viewport: NonNullable<ViewState['viewport']>): void;
  error(message: string): void;
  geometry(rect: DOMRect | null): void;
  dismissOverlays(): void;
  contextMenu(id: string | null, x: number, y: number): void;
}

interface RootMap { id: string; host: HTMLDivElement; mind: MindElixir; x: number; y: number; width: number; height: number; rootX: number }

/** Mind Elixir lays out each real tree; this adapter owns the shared surface. */
export class MapRenderer {
  private maps = new Map<string, RootMap>();
  private forest = document.createElement('div');
  private snapshot: Snapshot;
  private view: ViewState;
  private viewport: NonNullable<ViewState['viewport']>;
  private bounds = { width: 0, height: 0 };
  private needsFit: boolean;
  private editingId: string | null = null;
  private editText: string | undefined;
  private abort = new AbortController();
  private resize: ResizeObserver;
  private viewportTimer: ReturnType<typeof setTimeout> | undefined;
  private renderDeferred = false;
  private gestures = new CanvasGestures();
  private captures = new Map<number, HTMLElement>();
  private drop: Operation | null = null;
  private dropError: string | null = null;
  private ghost: HTMLDivElement | null = null;
  private moving = false;
  private suppressCanvasClick = false;
  private wheelTimer: ReturnType<typeof setTimeout> | undefined;
  private dragPointer: { id: string; x: number; y: number } | null = null;
  private panFrame: number | null = null;
  private panTime: number | null = null;
  private hoverId: string | null = null;
  private hoverTimer: ReturnType<typeof setTimeout> | undefined;
  private marquee: {
    pointer: number;
    start: { x: number; y: number };
    current: { x: number; y: number };
    additive: boolean;
    baseIds: string[];
    basePrimary: string | null;
    previewIds: string[];
    previewPrimary: string | null;
    moved: boolean;
    element: HTMLDivElement | null;
  } | null = null;
  private marqueeFrame: number | null = null;

  constructor(private host: HTMLElement, snapshot: Snapshot, view: ViewState, private callbacks: Callbacks) {
    this.snapshot = snapshot;
    this.view = { ...view, collapsed: [...view.collapsed], selectedIds: [...view.selectedIds] };
    this.viewport = { ...(view.viewport ?? { x: 0, y: 0, scale: 1 }) };
    this.needsFit = !view.viewport;
    this.forest.className = 'pm-forest';
    host.append(this.forest);
    host.tabIndex = 0;
    host.setAttribute('role', 'tree');
    host.setAttribute('aria-multiselectable', 'true');
    host.setAttribute('aria-label', 'Pursuit directions. Enter adds a sibling; Tab adds a child.');
    host.setAttribute('aria-describedby', 'pm-keyboard-hint');
    const events = { signal: this.abort.signal };
    host.addEventListener('click', (event) => {
      const clicked = event.target as HTMLElement;
      const target = clicked.closest<HTMLElement>('[data-map-marker], me-epd');
      if (!target) {
        if (!this.suppressCanvasClick && !clicked.closest('me-tpc, #input-box')) {
          this.commitSelection([], null);
        }
        return;
      }
      const topic = target.closest('me-tpc') as Topic | null ?? target.parentElement?.querySelector<Topic>('me-tpc');
      if (!topic) return;
      event.stopPropagation();
      const id = topic.nodeObj.id;
      if (target.dataset.mapMarker === 'collapse' || target.tagName === 'ME-EPD') {
        this.callbacks.collapse(id, !this.view.collapsed.includes(id));
        this.focus();
      } else this.callbacks.marker(id, target.dataset.mapMarker as 'note' | 'focus' | 'relations');
    }, events);
    host.addEventListener('dblclick', (event) => {
      if ((event.target as HTMLElement).closest('[data-map-marker], me-epd, #input-box')) return;
      const topic = (event.target as HTMLElement).closest<Topic>('me-tpc');
      if (topic) { event.preventDefault(); void this.beginEdit(topic.nodeObj.id); }
    }, events);
    // The app owns cross-root selection and gestures. Capture prevents Mind
    // Elixir's per-tree selection and box-selection handlers from racing it.
    host.addEventListener('pointerdown', (event) => this.pointerDown(event), { ...events, capture: true });
    host.addEventListener('pointermove', (event) => this.pointerMove(event), { ...events, capture: true });
    host.addEventListener('pointerup', (event) => this.pointerUp(event), { ...events, capture: true });
    host.addEventListener('pointercancel', () => this.cancelGesture(), events);
    host.addEventListener('lostpointercapture', (event) => {
      if (this.captures.get(event.pointerId) === event.target) this.cancelGesture();
    }, events);
    window.addEventListener('blur', () => this.cancelGesture(), events);
    document.addEventListener('visibilitychange', () => { if (document.hidden) this.cancelGesture(); }, events);
    host.addEventListener('contextmenu', (event) => {
      const target = event.target as HTMLElement;
      if (target.closest('input, textarea, [contenteditable]')) return;
      event.preventDefault();
      this.cancelGesture();
      const id = target.closest<Topic>('me-tpc')?.nodeObj.id ?? null;
      if (id) this.commitSelection([id], id);
      this.callbacks.contextMenu(id, event.clientX, event.clientY);
    }, events);
    host.addEventListener('wheel', (event) => {
      if ((event.target as HTMLElement).closest('#input-box')) return;
      event.preventDefault();
      this.beginViewMotion();
      clearTimeout(this.wheelTimer);
      this.wheelTimer = setTimeout(() => {
        this.wheelTimer = undefined;
        if (!this.gestures.active) this.moving = false;
        this.notifyGeometry();
      }, 160);
      const factor = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? host.clientHeight : 1;
      if (event.ctrlKey || event.metaKey) this.setScale(this.viewport.scale * Math.exp(-event.deltaY * factor * 0.002), event.clientX, event.clientY);
      else {
        this.viewport.x -= (event.shiftKey ? event.deltaY : event.deltaX) * factor;
        this.viewport.y -= (event.shiftKey ? 0 : event.deltaY) * factor;
        this.applyViewport();
      }
    }, { ...events, passive: false });
    this.resize = new ResizeObserver(() => { if (this.needsFit) this.fit(); else this.notifyGeometry(); });
    this.resize.observe(host);
    this.render(snapshot, view);
  }

  get editing(): boolean { return this.editingId !== null; }

  selectedTopicRect(): DOMRect | null {
    if (this.editing || this.moving) return null;
    return this.topic(this.view.selected)?.getBoundingClientRect() ?? null;
  }

  private notifyGeometry(): void { this.callbacks.geometry(this.selectedTopicRect()); }

  private beginViewMotion(): void {
    this.moving = true;
    this.callbacks.dismissOverlays();
    this.notifyGeometry();
  }

  private createMap(data: MindElixirData): RootMap {
    const host = document.createElement('div');
    host.className = 'pm-root-map';
    host.dataset.rootId = data.nodeData.id;
    this.forest.append(host);
    const mind = new MindElixir({
      el: host, direction: data.direction, contextMenu: false, toolBar: false,
      keypress: false, allowUndo: false, alignment: 'nodes',
      // Disable per-tree pointer handlers: pan, zoom, and cross-tree drops share one surface.
      overflowHidden: true, markdown: titleMarkup,
      theme: {
        name: 'Pursuit', type: 'light', palette,
        cssVar: {
          '--bgcolor': '#fdfdfb', '--color': '#35453f', '--selected': '#3d8172',
          '--main-gap-x': '25px', '--main-gap-y': '13px', '--node-gap-x': '18px', '--node-gap-y': '2px',
          '--main-bgcolor': '#f5f6f1', '--main-color': '#35453f', '--main-border': 'none',
          '--root-bgcolor': '#ffffff', '--root-color': '#243633', '--root-border-color': 'transparent',
          '--root-radius': '7px', '--main-radius': '6px', '--topic-padding': '5px 8px', '--map-padding': '32px',
        },
      },
      before: {
        beginEdit: (topic) => !!topic && this.canEdit(topic.nodeObj.id),
        // The independent reducer and save queue own every structural operation.
        moveNodeIn: () => false, moveNodeBefore: () => false, moveNodeAfter: () => false,
        addChild: () => false, insertSibling: () => false, insertParent: () => false,
        removeNodes: () => false, copyNode: () => false, copyNodes: () => false,
      },
    });
    mind.bus.addListener('operation', (operation) => {
      if (operation.name === 'beginEdit') this.attachInlineEditor(operation.obj.id);
    });
    mind.init(data);
    mind.container.tabIndex = -1;
    mind.container.setAttribute('role', 'group');
    mind.container.setAttribute('aria-labelledby', `pm-node-${data.nodeData.id}`);
    mind.map.style.transform = 'none';
    return { id: data.nodeData.id, host, mind, x: 0, y: 0, width: 0, height: 0, rootX: 0 };
  }

  private canEdit(id: string): boolean {
    return this.snapshot.writable && !!indexTree(this.snapshot).get(id)?.editable;
  }

  private topic(id: string | null): Topic | undefined {
    if (!id) return;
    for (const topic of this.host.querySelectorAll<Topic>('me-tpc')) if (topic.nodeObj.id === id) return topic;
  }

  render(snapshot: Snapshot, view: ViewState, preserveDrag = false): void {
    this.snapshot = snapshot;
    if (this.editing) {
      this.view = { ...view, collapsed: [...view.collapsed], selectedIds: [...view.selectedIds] };
      this.renderDeferred = true;
      return;
    }
    if (!preserveDrag) this.cancelGesture();
    this.view = { ...view, collapsed: [...view.collapsed], selectedIds: [...view.selectedIds] };
    const anchor = this.topic(view.selected)?.getBoundingClientRect();
    const roots = new Set(snapshot.root_ids);
    for (const [id, map] of this.maps) {
      if (!roots.has(id)) { map.mind.destroy(); map.host.remove(); this.maps.delete(id); }
    }
    for (const data of forestData(snapshot, view)) {
      let map = this.maps.get(data.nodeData.id);
      if (!map) { map = this.createMap(data); this.maps.set(map.id, map); }
      else { map.mind.direction = data.direction!; map.mind.refresh(data); }
      map.mind.editable = snapshot.writable;
      this.forest.append(map.host);
    }
    this.decorate();
    this.layoutMaps();
    const placed = this.topic(view.selected)?.getBoundingClientRect();
    if (anchor && placed) {
      this.viewport.x += anchor.left + anchor.width / 2 - placed.left - placed.width / 2;
      this.viewport.y += anchor.top + anchor.height / 2 - placed.top - placed.height / 2;
    }
    if (this.needsFit) this.fit();
    else this.applyViewport();
    this.selectMany(view.selectedIds, view.selected);
  }

  private layoutMaps(): void {
    const sizes = this.snapshot.root_ids.map((id) => {
      const map = this.maps.get(id)!;
      map.mind.linkDiv();
      const root = map.mind.nodes.querySelector<HTMLElement>('me-root')!;
      return { id, width: map.mind.nodes.offsetWidth, height: map.mind.nodes.offsetHeight, rootX: root.offsetLeft + root.offsetWidth / 2 };
    });
    const layout = stackMaps(sizes);
    for (const placement of layout.maps) {
      const map = this.maps.get(placement.id)!;
      Object.assign(map, placement);
      map.host.style.left = `${map.x}px`;
      map.host.style.top = `${map.y}px`;
      map.host.style.width = `${map.width}px`;
      map.host.style.height = `${map.height}px`;
    }
    this.bounds = { width: layout.width, height: layout.height };
    this.forest.style.width = `${layout.width}px`;
    this.forest.style.height = `${layout.height}px`;
    this.forest.hidden = !layout.maps.length;
  }

  private decorate(): void {
    const items = indexTree(this.snapshot);
    const selected = new Set(this.view.selectedIds);
    for (const topic of this.host.querySelectorAll<Topic>('me-tpc')) {
      const id = topic.nodeObj.id;
      const item = items.get(id)!;
      topic.setAttribute('role', 'treeitem');
      topic.setAttribute('aria-label', titleText(item.title));
      topic.classList.remove('selected');
      topic.setAttribute('aria-selected', String(selected.has(id)));
      topic.id = `pm-node-${id}`;
      topic.tabIndex = -1;
      topic.classList.toggle('pm-readonly', !item.editable);
      let level = 1;
      let parent = item.parent_id;
      while (parent) { level++; parent = items.get(parent)?.parent_id ?? null; }
      topic.setAttribute('aria-level', String(level));
      if (item.child_ids.length) topic.setAttribute('aria-expanded', String(!this.view.collapsed.includes(id)));
      topic.querySelectorAll('[data-map-marker]').forEach((marker) => marker.remove());
      const markers: Array<['note' | 'focus' | 'relations', string, string]> = [];
      if (item.body) markers.push(['note', '▤', 'Open note']);
      if (item.focused) markers.push(['focus', '✦', 'Remove Focus']);
      if (item.edges.length) markers.push(['relations', '↗', 'Show related directions']);
      for (const [kind, icon, label] of markers) {
        const marker = document.createElement('button');
        marker.type = 'button'; marker.tabIndex = -1;
        marker.dataset.mapMarker = kind; marker.className = 'pm-node-marker';
        marker.textContent = icon; marker.title = label;
        marker.setAttribute('aria-label', `${label}: ${titleText(item.title)}`);
        topic.append(marker);
      }
      if (!item.parent_id && item.child_ids.length) {
        const fold = document.createElement('button');
        fold.type = 'button'; fold.tabIndex = -1;
        fold.dataset.mapMarker = 'collapse'; fold.className = 'pm-node-marker';
        fold.textContent = this.view.collapsed.includes(id) ? '⊕' : '⊖';
        fold.setAttribute('aria-label', `${this.view.collapsed.includes(id) ? 'Expand' : 'Collapse'} ${titleText(item.title)}`);
        topic.append(fold);
      }
      const control = topic.parentElement?.querySelector<HTMLElement>('me-epd');
      if (control) {
        control.setAttribute('role', 'button');
        control.setAttribute('aria-label', `${this.view.collapsed.includes(id) ? 'Expand' : 'Collapse'} ${titleText(item.title)}`);
        control.setAttribute('aria-expanded', String(!this.view.collapsed.includes(id)));
      }
    }
  }

  select(id: string | null, center = false): void {
    this.selectMany(id ? [id] : [], id, center);
  }

  selectMany(ids: readonly string[], primary: string | null, center = false): void {
    const selected = [...new Set(ids)];
    const selectedIds = new Set(selected);
    if (!primary || !selectedIds.has(primary)) primary = selected[0] ?? null;
    this.view.selectedIds = selected;
    this.view.selected = primary;
    let activeTopic: Topic | undefined;
    for (const topic of this.host.querySelectorAll<Topic>('me-tpc')) {
      const id = topic.nodeObj.id;
      const isSelected = selectedIds.has(id);
      topic.setAttribute('aria-selected', String(isSelected));
      topic.classList.remove('selected');
      topic.classList.toggle('pm-selected', isSelected);
      topic.classList.toggle('pm-active', id === primary);
      if (!activeTopic && id === primary) activeTopic = topic;
    }
    if (!activeTopic) { this.host.removeAttribute('aria-activedescendant'); this.notifyGeometry(); return; }
    this.host.setAttribute('aria-activedescendant', activeTopic.id);
    if (center) this.centerReadableNode(activeTopic);
    this.notifyGeometry();
  }

  private commitSelection(ids: readonly string[], primary: string | null, center = false): void {
    this.selectMany(ids, primary, center);
    this.callbacks.select([...this.view.selectedIds], this.view.selected);
  }

  ensureVisible(id: string): void {
    const topic = this.topic(id);
    if (!topic) return;
    const node = topic.getBoundingClientRect();
    const canvas = this.host.getBoundingClientRect();
    if (node.left < canvas.left + 12 || node.right > canvas.right - 12 || node.top < canvas.top + 12 || node.bottom > canvas.bottom - 12) {
      this.centerReadableNode(topic);
    }
  }

  private centerReadableNode(topic: Topic): void {
    if (this.viewport.scale < 1) this.setScale(1);
    const canvas = this.host.getBoundingClientRect();
    const node = topic.getBoundingClientRect();
    this.viewport.x += canvas.left + canvas.width / 2 - node.left - node.width / 2;
    this.viewport.y += canvas.top + canvas.height / 2 - node.top - node.height / 2;
    this.applyViewport();
  }

  async beginEdit(id: string, text?: string): Promise<void> {
    if (this.editing || !this.canEdit(id)) return;
    const topic = this.topic(id);
    const root = topic?.closest<HTMLElement>('.pm-root-map')?.dataset.rootId;
    if (!topic || !root) return;
    this.editText = text;
    this.commitSelection([id], id, true);
    await this.maps.get(root)!.mind.beginEdit(topic);
  }

  private attachInlineEditor(id: string): void {
    const editor = this.host.querySelector<HTMLElement>('#input-box');
    if (!editor) return;
    this.editingId = id;
    this.callbacks.dismissOverlays();
    this.notifyGeometry();
    // Mind Elixir edits nodeObj.topic, which retains the original Markdown.
    if (this.editText !== undefined) editor.textContent = this.editText;
    this.editText = undefined;
    editor.setAttribute('role', 'textbox');
    editor.setAttribute('aria-label', 'Direction title');
    editor.setAttribute('aria-multiline', 'false');
    this.callbacks.editStart(id, editor.textContent ?? '');
    let canceled = false;
    editor.addEventListener('input', () => this.callbacks.editInput(editor.textContent ?? ''));
    editor.addEventListener('keydown', (event) => {
      if (event.isComposing) return;
      if (event.key === 'Escape') canceled = true;
      if (event.key === 'Enter' && event.shiftKey) { event.preventDefault(); event.stopImmediatePropagation(); editor.blur(); }
    }, { capture: true });
    editor.addEventListener('blur', () => {
      const text = editor.innerText?.trim() ?? editor.textContent?.trim() ?? '';
      queueMicrotask(() => {
        this.editingId = null;
        this.callbacks.editEnd(id, text, canceled);
        if (this.renderDeferred) { this.renderDeferred = false; this.render(this.snapshot, this.view); }
        this.notifyGeometry();
      });
    }, { once: true });
    queueMicrotask(() => {
      editor.focus();
      const range = document.createRange();
      range.selectNodeContents(editor);
      window.getSelection()?.removeAllRanges();
      window.getSelection()?.addRange(range);
    });
  }

  private pointerDown(event: PointerEvent): void {
    this.suppressCanvasClick = this.gestures.active || !!this.marquee;
    const target = event.target as HTMLElement;
    if (this.editing || target.closest('[data-map-marker], me-epd, #input-box')) return;
    const topic = target.closest<Topic>('me-tpc');
    const id = event.button === 0 ? topic?.nodeObj.id ?? null : null;
    if (this.gestures.has(event.pointerId)) this.cancelGesture();
    if (this.marquee) this.cancelGesture();
    if (event.pointerType === 'touch' && !this.gestures.touching) this.cancelGesture();
    if (event.pointerType !== 'touch' && this.gestures.active) { event.stopPropagation(); return; }
    event.stopPropagation();
    if (id) {
      const modified = event.pointerType !== 'touch' && (event.ctrlKey || event.metaKey);
      if (modified) {
        const ids = [...this.view.selectedIds];
        const index = ids.indexOf(id);
        if (index < 0) this.commitSelection([...ids, id], this.view.selected ?? id);
        else {
          ids.splice(index, 1);
          this.commitSelection(ids, this.view.selected === id ? ids.at(-1) ?? null : this.view.selected);
        }
      } else this.commitSelection([id], id);
    }
    this.focus();
    if (!id && event.pointerType !== 'touch' && event.button === 0 && event.shiftKey) {
      const sample = this.pointerSample(event);
      this.marquee = {
        pointer: event.pointerId,
        start: { x: sample.x, y: sample.y },
        current: { x: sample.x, y: sample.y },
        additive: event.ctrlKey || event.metaKey,
        baseIds: [...this.view.selectedIds],
        basePrimary: this.view.selected,
        previewIds: [...this.view.selectedIds],
        previewPrimary: this.view.selected,
        moved: false,
        element: null,
      };
      this.host.setPointerCapture(event.pointerId);
      this.captures.set(event.pointerId, this.host);
      event.preventDefault();
      return;
    }
    if (event.pointerType !== 'touch' && id && !this.canEdit(id)) return;
    if (!this.gestures.start(this.pointerSample(event), id, this.viewport)) return;
    if (event.pointerType === 'touch') this.beginViewMotion();
    // Capturing the original topic retains click/double-click targeting while
    // still receiving a release outside the label or canvas.
    const capture = topic ?? this.host;
    capture.setPointerCapture(event.pointerId);
    this.captures.set(event.pointerId, capture);
    if (!topic) event.preventDefault();
  }

  private pointerMove(event: PointerEvent): void {
    if (this.marquee?.pointer === event.pointerId) {
      event.stopPropagation();
      const sample = this.pointerSample(event);
      this.marquee.current = { x: sample.x, y: sample.y };
      if (!event.buttons) { this.cancelGesture(); return; }
      if (!this.marquee.moved && Math.hypot(sample.x - this.marquee.start.x, sample.y - this.marquee.start.y) < 5) return;
      if (!this.marquee.moved) {
        this.marquee.moved = true;
        this.suppressCanvasClick = true;
        this.marquee.element = document.createElement('div');
        this.marquee.element.className = 'pm-selection-rectangle';
        this.host.append(this.marquee.element);
        this.beginViewMotion();
      }
      event.preventDefault();
      if (this.marqueeFrame === null) this.marqueeFrame = requestAnimationFrame(() => {
        this.marqueeFrame = null;
        this.updateMarquee();
      });
      return;
    }
    if (!this.gestures.has(event.pointerId)) return;
    event.stopPropagation();
    const motion = this.gestures.move(this.pointerSample(event));
    if (motion.kind === 'idle') return;
    // A drag can end in a browser click event; it must not clear the selection.
    this.suppressCanvasClick = true;
    if (motion.kind === 'cancel') { this.cancelGesture(); return; }
    event.preventDefault();
    this.beginViewMotion();
    if (motion.kind === 'view') {
      this.host.classList.add('pm-panning');
      this.viewport = motion.viewport;
      this.applyViewport();
      return;
    }
    const id = motion.id;
    if (this.view.selected !== id || this.view.selectedIds.length !== 1) this.commitSelection([id], id);
    this.dragPointer = { id, x: event.clientX, y: event.clientY };
    this.updateDrop();
    this.updateAutoPan();
  }

  private updateMarquee(): void {
    const marquee = this.marquee;
    if (!marquee?.moved || !marquee.element) return;
    const left = Math.min(marquee.start.x, marquee.current.x);
    const top = Math.min(marquee.start.y, marquee.current.y);
    const right = Math.max(marquee.start.x, marquee.current.x);
    const bottom = Math.max(marquee.start.y, marquee.current.y);
    marquee.element.style.left = `${left}px`;
    marquee.element.style.top = `${top}px`;
    marquee.element.style.width = `${right - left}px`;
    marquee.element.style.height = `${bottom - top}px`;
    const canvas = this.host.getBoundingClientRect();
    const selection = { left: canvas.left + left, top: canvas.top + top, right: canvas.left + right, bottom: canvas.top + bottom };
    const hits: string[] = [];
    for (const topic of this.host.querySelectorAll<Topic>('me-tpc')) {
      const rect = topic.getBoundingClientRect();
      if (rect.width && rect.height && rect.left >= selection.left && rect.right <= selection.right
        && rect.top >= selection.top && rect.bottom <= selection.bottom) hits.push(topic.nodeObj.id);
    }
    const baseIds = new Set(marquee.baseIds);
    const hitIds = new Set(hits);
    const ids = marquee.additive ? [...marquee.baseIds, ...hits.filter((id) => !baseIds.has(id))] : hits;
    const primary = marquee.additive && marquee.basePrimary && baseIds.has(marquee.basePrimary)
      ? marquee.basePrimary
      : marquee.basePrimary && hitIds.has(marquee.basePrimary) ? marquee.basePrimary : ids[0] ?? null;
    marquee.previewIds = ids;
    marquee.previewPrimary = primary;
    this.selectMany(ids, primary);
  }

  private updateDrop(): void {
    if (!this.dragPointer) return;
    const { id, x, y } = this.dragPointer;
    if (!this.ghost) { this.ghost = document.createElement('div'); this.ghost.className = 'pm-drag-ghost'; this.host.append(this.ghost); }
    const canvas = this.host.getBoundingClientRect();
    this.ghost.style.left = `${x - canvas.left + 16}px`;
    this.ghost.style.top = `${y - canvas.top + 16}px`;
    let label = titleText(indexTree(this.snapshot).get(id)?.title ?? 'Direction');
    this.clearDrop();
    if (x < canvas.left || x > canvas.right || y < canvas.top || y > canvas.bottom) {
      this.updateHover(null); this.ghost.textContent = label; return;
    }
    const target = document.elementFromPoint(x, y)?.closest<Topic>('me-tpc');
    let hover: string | null = null;
    try {
      if (target && this.host.contains(target)) {
        const rect = target.getBoundingClientRect();
        const fraction = (y - rect.top) / rect.height;
        const position = fraction < 0.25 ? 'before' : fraction > 0.75 ? 'after' : 'in';
        this.drop = dropOperation(this.snapshot, id, target.nodeObj.id, position);
        target.classList.add(`pm-drop-${position}`);
        if (position === 'in' && this.view.collapsed.includes(target.nodeObj.id)) hover = target.nodeObj.id;
      } else {
        const worldY = (y - canvas.top - this.viewport.y) / this.viewport.scale;
        const roots = this.snapshot.root_ids.filter((root) => root !== id);
        const after = roots.filter((id) => { const map = this.maps.get(id)!; return map.y + map.height / 2 < worldY; }).at(-1) ?? null;
        assertMove(this.snapshot, id, null, after);
        this.drop = { type: 'move', id, parent_id: null, after_id: after };
        label += ' · Top level';
      }
    } catch (error) { this.dropError = (error as Error).message; label += ' · Cannot move here'; }
    if (this.ghost.textContent !== label) this.ghost.textContent = label;
    this.updateHover(hover);
  }

  private updateAutoPan(): void {
    const canvas = this.host.getBoundingClientRect();
    const pointer = this.dragPointer;
    const velocity = pointer ? edgePanVelocity(pointer.x - canvas.left, pointer.y - canvas.top, canvas.width, canvas.height) : { x: 0, y: 0 };
    if (!velocity.x && !velocity.y) {
      if (this.panFrame !== null) cancelAnimationFrame(this.panFrame);
      this.panFrame = null; this.panTime = null; return;
    }
    if (this.panFrame !== null) return;
    this.panFrame = requestAnimationFrame((time) => {
      this.panFrame = null;
      if (!this.dragPointer) return;
      const elapsed = this.panTime === null ? 1 : Math.min(2, (time - this.panTime) / (1000 / 60));
      this.panTime = time;
      this.viewport.x += velocity.x * elapsed;
      this.viewport.y += velocity.y * elapsed;
      this.applyViewport();
      this.updateDrop();
      this.updateAutoPan();
    });
  }

  private updateHover(id: string | null): void {
    if (this.hoverId === id) return;
    clearTimeout(this.hoverTimer);
    this.hoverId = id;
    if (!id) return;
    this.hoverTimer = setTimeout(() => {
      if (!this.dragPointer || this.hoverId !== id) return;
      this.hoverId = null;
      // Layout replaces topics; transfer capture to the stable shared canvas first.
      for (const [pointer, capture] of this.captures) {
        if (capture === this.host) continue;
        this.captures.delete(pointer);
        if (capture.hasPointerCapture(pointer)) capture.releasePointerCapture(pointer);
        this.host.setPointerCapture(pointer);
        this.captures.set(pointer, this.host);
      }
      this.callbacks.collapse(id, false, true);
      this.updateDrop();
    }, 625);
  }

  private pointerUp(event: PointerEvent): void {
    if (this.marquee?.pointer === event.pointerId) {
      event.stopPropagation();
      const marquee = this.marquee;
      const sample = this.pointerSample(event);
      marquee.current = { x: sample.x, y: sample.y };
      if (this.marqueeFrame !== null) cancelAnimationFrame(this.marqueeFrame);
      this.marqueeFrame = null;
      if (marquee.moved) this.updateMarquee();
      this.marquee = null;
      marquee.element?.remove();
      this.releasePointer(event.pointerId);
      this.moving = !!this.wheelTimer;
      if (marquee.moved) this.callbacks.select([...marquee.previewIds], marquee.previewPrimary);
      else this.selectMany(marquee.baseIds, marquee.basePrimary);
      this.notifyGeometry();
      return;
    }
    if (!this.gestures.has(event.pointerId)) return;
    event.stopPropagation();
    if (this.dragPointer) { this.dragPointer.x = event.clientX; this.dragPointer.y = event.clientY; this.updateDrop(); }
    const moved = this.gestures.end(event.pointerId, this.viewport);
    const operation = moved ? this.drop : null;
    const error = moved ? this.dropError : null;
    this.releasePointer(event.pointerId);
    if (!this.gestures.active) this.cancelGesture();
    if (operation) this.callbacks.move(operation);
    else if (error) this.callbacks.error(error);
  }

  private pointerSample(event: PointerEvent): PointerSample {
    const canvas = this.host.getBoundingClientRect();
    return { pointerId: event.pointerId, pointerType: event.pointerType, button: event.button, buttons: event.buttons, x: event.clientX - canvas.left, y: event.clientY - canvas.top };
  }

  private releasePointer(pointer: number): void {
    const capture = this.captures.get(pointer);
    // Remove ownership before release can dispatch lostpointercapture.
    this.captures.delete(pointer);
    if (capture?.hasPointerCapture(pointer)) capture.releasePointerCapture(pointer);
  }

  private clearDrop(): void {
    this.drop = null; this.dropError = null;
    this.host.querySelectorAll('.pm-drop-in, .pm-drop-before, .pm-drop-after').forEach((topic) => topic.classList.remove('pm-drop-in', 'pm-drop-before', 'pm-drop-after'));
  }

  cancelGesture(): void {
    if (this.panFrame !== null) cancelAnimationFrame(this.panFrame);
    if (this.marqueeFrame !== null) cancelAnimationFrame(this.marqueeFrame);
    this.panFrame = null; this.panTime = null; this.marqueeFrame = null; this.dragPointer = null;
    const marquee = this.marquee;
    this.marquee = null;
    marquee?.element?.remove();
    if (marquee) this.selectMany(marquee.baseIds, marquee.basePrimary);
    this.updateHover(null);
    this.gestures.cancel();
    for (const pointer of this.captures.keys()) this.releasePointer(pointer);
    this.clearDrop();
    this.ghost?.remove(); this.ghost = null;
    this.host.classList.remove('pm-panning');
    this.moving = !!this.wheelTimer;
    this.notifyGeometry();
  }

  highlight(ids: Set<string>): void {
    this.host.querySelectorAll<Topic>('me-tpc').forEach((topic) => topic.classList.toggle('pm-search-hit', ids.has(topic.nodeObj.id)));
  }

  focus(): void { this.host.focus({ preventScroll: true }); }

  fit(): void {
    const width = this.host.clientWidth;
    const height = this.host.clientHeight;
    if (!width || !height) { this.needsFit = true; return; }
    this.needsFit = false;
    const scale = Math.max(0.1, Math.min(1, (width - 48) / Math.max(1, this.bounds.width), (height - 72) / Math.max(1, this.bounds.height)));
    this.viewport = { x: (width - this.bounds.width * scale) / 2, y: (height - this.bounds.height * scale) / 2, scale };
    this.applyViewport();
  }

  zoom(delta: number): void { this.setScale(this.viewport.scale + delta); }

  private setScale(scale: number, clientX?: number, clientY?: number): void {
    const canvas = this.host.getBoundingClientRect();
    const x = clientX === undefined ? canvas.width / 2 : clientX - canvas.left;
    const y = clientY === undefined ? canvas.height / 2 : clientY - canvas.top;
    const next = Math.max(0.1, Math.min(2, scale));
    const ratio = next / this.viewport.scale;
    this.viewport = { x: x - (x - this.viewport.x) * ratio, y: y - (y - this.viewport.y) * ratio, scale: next };
    this.applyViewport();
  }

  private applyViewport(): void {
    const { x, y, scale } = this.viewport;
    this.forest.style.transform = `translate3d(${x}px, ${y}px, 0) scale(${scale})`;
    this.notifyGeometry();
    clearTimeout(this.viewportTimer);
    this.viewportTimer = setTimeout(() => this.callbacks.viewport({ ...this.viewport }), 150);
  }

  destroy(): void {
    clearTimeout(this.viewportTimer);
    clearTimeout(this.wheelTimer);
    this.cancelGesture();
    this.abort.abort();
    this.resize.disconnect();
    this.maps.forEach((map) => map.mind.destroy());
    this.maps.clear();
    this.forest.remove();
  }
}

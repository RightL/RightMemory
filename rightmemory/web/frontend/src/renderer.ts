import MindElixir, { type MindElixirData, type NodeObj, type Topic } from 'mind-elixir';
import 'mind-elixir/style.css';
import { assertMove, childrenOf, indexTree, VIRTUAL_ROOT, visualRoot, type Operation, type Snapshot } from './tree.ts';
import { type ViewState } from './view-state.ts';

const palette = ['#d58375', '#c79561', '#79a494', '#9694b9', '#80a7b8', '#b895ae'];

export function canvasData(snapshot: Snapshot, view: ViewState): MindElixirData {
  const items = indexTree(snapshot);
  const folded = new Set(view.collapsed);
  const build = (id: string, depth: number, color: string): NodeObj => {
    const item = items.get(id)!;
    return {
      id, topic: item.title, expanded: !folded.has(id), branchColor: color,
      style: depth === 0
        ? { color: '#243633', fontSize: '25px', background: '#ffffff', border: 'none', fontWeight: '650' }
        : depth === 1
          ? { color: '#293c37', background: `${color}26`, border: 'none', fontSize: '16px', fontWeight: '550' }
          : { color: '#35453f', background: depth === 2 ? `${color}14` : 'transparent', border: 'none', fontSize: '14px' },
      children: depth === 0 && folded.has(id) ? [] : item.child_ids.map((child, childIndex) => build(child, depth + 1, depth === 0 ? palette[childIndex % palette.length] : color)),
    };
  };
  const root = visualRoot(snapshot);
  return {
    nodeData: root === VIRTUAL_ROOT
      ? { id: root, topic: 'Pursuits', children: snapshot.root_ids.map((id, index) => build(id, 1, palette[index % palette.length])) }
      : build(root, 0, palette[0]),
    direction: MindElixir.RIGHT,
  };
}

interface Callbacks {
  select(id: string): void;
  collapse(id: string, collapsed: boolean): void;
  move(operation: Operation): void;
  editStart(id: string, title: string): void;
  editInput(text: string): void;
  editEnd(id: string, text: string, canceled: boolean): void;
  marker(id: string, kind: 'note' | 'focus' | 'relations'): void;
  viewport(viewport: NonNullable<ViewState['viewport']>): void;
  error(message: string): void;
}

/** All library-specific DOM, layout, drag and viewport behavior stays here. */
export class MapRenderer {
  private mind: MindElixir;
  private snapshot: Snapshot;
  private view: ViewState;
  private rendering = false;
  private editingId: string | null = null;
  private editText: string | undefined;
  private abort = new AbortController();
  private viewportTimer: ReturnType<typeof setTimeout> | undefined;
  private renderDeferred = false;

  constructor(private host: HTMLElement, snapshot: Snapshot, view: ViewState, private callbacks: Callbacks) {
    this.snapshot = snapshot;
    this.view = view;
    this.mind = new MindElixir({
      el: host, direction: MindElixir.RIGHT, contextMenu: false, toolBar: false,
      keypress: false, allowUndo: false, alignment: 'nodes', scaleMin: 0.1, scaleMax: 2,
      // In Mind Elixir this option also gates pointer handlers; clip the stage with CSS instead.
      overflowHidden: false,
      mouseSelectionButton: 0,
      theme: {
        name: 'Pursuit', type: 'light', palette,
        cssVar: {
          '--bgcolor': '#fdfdfb', '--color': '#35453f', '--selected': '#3d8172',
          '--main-gap-x': '25px', '--main-gap-y': '13px', '--node-gap-x': '18px', '--node-gap-y': '2px',
          '--main-bgcolor': '#f5f6f1', '--main-color': '#35453f', '--main-border': 'none',
          '--root-bgcolor': '#ffffff', '--root-color': '#243633', '--root-border-color': 'transparent',
          '--root-radius': '7px', '--main-radius': '6px', '--topic-padding': '5px 8px', '--map-padding': '60px',
        },
      },
      before: {
        beginEdit: (topic) => !!topic && this.canEdit(topic.nodeObj.id),
        moveNodeIn: (from, to) => this.allowDrop(from, to, 'in'),
        moveNodeBefore: (from, to) => this.allowDrop(from, to, 'before'),
        moveNodeAfter: (from, to) => this.allowDrop(from, to, 'after'),
        // Creation and deletion go through the independent reducer, including a real visual root.
        addChild: () => false, insertSibling: () => false, insertParent: () => false,
        removeNodes: () => false, copyNode: () => false, copyNodes: () => false,
      },
    });
    this.mind.bus.addListener('selectNodes', (nodes) => {
      if (this.rendering || !nodes.length) return;
      const selected = nodes.at(-1)!;
      if (nodes.length > 1) this.mind.selectNode(this.mind.findEle(selected.id));
      this.callbacks.select(selected.id);
      this.decorate();
    });
    this.mind.bus.addListener('expandNode', (node) => {
      this.callbacks.collapse(node.id, node.expanded === false);
      this.decorate();
    });
    this.mind.bus.addListener('operation', (operation) => {
      if (operation.name === 'beginEdit') this.attachInlineEditor(operation.obj.id);
      else if (['moveNodeIn', 'moveNodeBefore', 'moveNodeAfter'].includes(operation.name) && 'objs' in operation) {
        for (const node of operation.objs) {
          const parent = node.parent?.id === VIRTUAL_ROOT ? null : node.parent?.id ?? null;
          const siblings = node.parent?.children ?? [];
          const after = siblings[siblings.indexOf(node) - 1]?.id ?? null;
          this.callbacks.move({ type: 'move', id: node.id, parent_id: parent, after_id: after });
        }
      }
    });
    this.mind.bus.addListener('scale', () => this.saveViewport());
    this.mind.bus.addListener('move', () => this.saveViewport());
    this.mind.init(canvasData(snapshot, view));
    this.mind.container.setAttribute('role', 'tree');
    this.mind.container.setAttribute('aria-label', 'Pursuit directions. Enter adds a sibling; Tab adds a child.');
    this.mind.container.setAttribute('aria-describedby', 'pm-keyboard-hint');
    host.addEventListener('click', (event) => {
      const target = (event.target as HTMLElement).closest<HTMLElement>('[data-map-marker]');
      if (!target) return;
      event.stopPropagation();
      const topic = target.closest('me-tpc') as Topic;
      if (target.dataset.mapMarker === 'collapse') {
        this.callbacks.collapse(topic.nodeObj.id, !this.view.collapsed.includes(topic.nodeObj.id));
        return;
      }
      this.callbacks.marker(topic.nodeObj.id, target.dataset.mapMarker as 'note' | 'focus' | 'relations');
    }, { capture: true, signal: this.abort.signal });
    host.addEventListener('pointerdown', (event) => {
      if ((event.target as HTMLElement).closest('[data-map-marker]')) event.stopPropagation();
    }, { capture: true, signal: this.abort.signal });
    this.restoreViewport(view.viewport);
    this.decorate();
    this.select(view.selected);
  }

  get editing(): boolean { return this.editingId !== null; }

  private canEdit(id: string): boolean {
    return this.snapshot.writable && id !== VIRTUAL_ROOT && !!indexTree(this.snapshot).get(id)?.editable;
  }

  private allowDrop(from: Topic[], to: Topic, position: 'in' | 'before' | 'after'): boolean {
    if (from.length !== 1 || !this.snapshot.writable || this.editing) return false;
    const id = from[0].nodeObj.id;
    const target = to.nodeObj.id;
    const items = indexTree(this.snapshot);
    const parent = position === 'in' ? (target === VIRTUAL_ROOT ? null : target) : items.get(target)?.parent_id ?? null;
    const siblings = childrenOf(this.snapshot, parent).filter((entry) => entry !== id);
    const after = position === 'in' ? siblings.at(-1) ?? null : position === 'after' ? target : siblings[siblings.indexOf(target) - 1] ?? null;
    try { assertMove(this.snapshot, id, parent, after); return true; }
    catch (error) { this.callbacks.error((error as Error).message); return false; }
  }

  render(snapshot: Snapshot, view: ViewState): void {
    this.snapshot = snapshot;
    this.view = view;
    if (this.editing) { this.renderDeferred = true; return; }
    this.rendering = true;
    const viewport = this.getViewport();
    this.mind.refresh(canvasData(snapshot, view));
    this.mind.editable = snapshot.writable;
    this.restoreViewport(viewport);
    this.decorate();
    this.select(view.selected);
    this.rendering = false;
  }

  private decorate(): void {
    const items = indexTree(this.snapshot);
    for (const topic of this.host.querySelectorAll<Topic>('me-tpc')) {
      const id = topic.nodeObj.id;
      const item = items.get(id);
      topic.setAttribute('role', 'treeitem');
      topic.setAttribute('aria-label', item?.title ?? 'Pursuits');
      topic.setAttribute('aria-selected', String(this.view.selected === id));
      topic.id = `pm-node-${id}`;
      topic.tabIndex = -1;
      topic.classList.toggle('pm-readonly', item?.editable === false);
      const level = (node: NodeObj): number => node.parent ? 1 + level(node.parent) : 1;
      topic.setAttribute('aria-level', String(level(topic.nodeObj)));
      if (item?.child_ids.length) topic.setAttribute('aria-expanded', String(!this.view.collapsed.includes(id)));
      topic.querySelectorAll('[data-map-marker]').forEach((marker) => marker.remove());
      const markers: Array<['note' | 'focus' | 'relations', string, string]> = [];
      if (item?.body) markers.push(['note', '▤', 'Open note']);
      if (item?.focused) markers.push(['focus', '✦', 'Remove Focus']);
      if (item?.edges.length) markers.push(['relations', '↗', 'Show related directions']);
      for (const [kind, icon, label] of markers) {
        const marker = document.createElement('button');
        marker.type = 'button';
        marker.tabIndex = -1;
        marker.dataset.mapMarker = kind;
        marker.className = 'pm-node-marker';
        marker.textContent = icon;
        marker.title = label;
        marker.setAttribute('aria-label', `${label}: ${item!.title}`);
        topic.append(marker);
      }
      if (topic.parentElement?.tagName === 'ME-ROOT' && item?.child_ids.length) {
        const fold = document.createElement('button');
        fold.type = 'button'; fold.tabIndex = -1;
        fold.dataset.mapMarker = 'collapse'; fold.className = 'pm-node-marker';
        fold.textContent = this.view.collapsed.includes(id) ? '⊕' : '⊖';
        fold.setAttribute('aria-label', `${this.view.collapsed.includes(id) ? 'Expand' : 'Collapse'} ${item.title}`);
        topic.append(fold);
      }
      const control = topic.parentElement?.querySelector<HTMLElement>('me-epd');
      if (control) {
        control.setAttribute('role', 'button');
        control.setAttribute('aria-label', `${this.view.collapsed.includes(id) ? 'Expand' : 'Collapse'} ${item?.title ?? 'branch'}`);
        control.setAttribute('aria-expanded', String(!this.view.collapsed.includes(id)));
      }
    }
  }

  select(id: string | null, center = false): void {
    if (!id) return;
    try {
      const topic = this.mind.findEle(id);
      const wasRendering = this.rendering;
      this.rendering = true;
      this.mind.selectNode(topic);
      this.rendering = wasRendering;
      this.view.selected = id;
      this.mind.container.setAttribute('aria-activedescendant', topic.id);
      this.host.querySelectorAll('[aria-selected="true"]').forEach((entry) => entry.setAttribute('aria-selected', 'false'));
      topic.setAttribute('aria-selected', 'true');
      if (center) this.centerReadableNode(topic);
    } catch { /* A collapsed ancestor can hide a previously selected node. */ }
  }

  private centerReadableNode(topic: Topic): void {
    // Fit may use a tiny scale for a large map. A revealed label must be readable,
    // and repeated search/navigation must not be skipped by the library's smooth-pan guard.
    this.mind.map.style.transition = 'none';
    if (this.mind.scaleVal < 1) this.mind.scale(1);
    const canvas = this.mind.container.getBoundingClientRect();
    const node = topic.getBoundingClientRect();
    this.mind.move(
      canvas.left + canvas.width / 2 - node.left - node.width / 2,
      canvas.top + canvas.height / 2 - node.top - node.height / 2,
      false,
    );
    this.saveViewport();
  }

  async beginEdit(id: string, text?: string): Promise<void> {
    if (this.editing || !this.canEdit(id)) return;
    this.editText = text;
    this.select(id, true);
    await this.mind.beginEdit(this.mind.findEle(id));
  }

  private attachInlineEditor(id: string): void {
    const editor = this.host.querySelector<HTMLElement>('#input-box');
    if (!editor) return;
    this.editingId = id;
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
      // Node titles are single lines. Native Enter/Tab ends editing; IME Enter is untouched.
      if (event.key === 'Enter' && event.shiftKey) { event.preventDefault(); event.stopImmediatePropagation(); editor.blur(); }
    }, { capture: true });
    editor.addEventListener('blur', () => {
      const text = editor.innerText?.trim() ?? editor.textContent?.trim() ?? '';
      queueMicrotask(() => {
        this.editingId = null;
        this.callbacks.editEnd(id, text, canceled);
        if (this.renderDeferred) { this.renderDeferred = false; this.render(this.snapshot, this.view); }
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

  highlight(ids: Set<string>): void {
    this.host.querySelectorAll<Topic>('me-tpc').forEach((topic) => topic.classList.toggle('pm-search-hit', ids.has(topic.nodeObj.id)));
  }

  focus(): void { this.mind.container.focus({ preventScroll: true }); }
  fit(): void { this.mind.scaleFit(); this.saveViewport(); }
  zoom(delta: number): void { this.mind.scale(Math.max(0.1, Math.min(2, this.mind.scaleVal + delta))); }

  private getViewport(): NonNullable<ViewState['viewport']> {
    const transform = new DOMMatrixReadOnly(getComputedStyle(this.mind.map).transform);
    return { x: transform.m41, y: transform.m42, scale: this.mind.scaleVal };
  }
  private restoreViewport(viewport?: ViewState['viewport']): void {
    if (!viewport) { this.mind.scaleFit(); return; }
    this.mind.scaleVal = viewport.scale;
    this.mind.map.style.transformOrigin = '50% 50%';
    this.mind.map.style.transform = `translate3d(${viewport.x}px, ${viewport.y}px, 0) scale(${viewport.scale})`;
  }
  private saveViewport(): void {
    clearTimeout(this.viewportTimer);
    this.viewportTimer = setTimeout(() => this.callbacks.viewport(this.getViewport()), 150);
  }
  destroy(): void {
    clearTimeout(this.viewportTimer);
    this.abort.abort();
    this.mind.destroy();
  }
}

import { indexTree, type Snapshot } from './tree.ts';

export interface ViewState {
  selected: string | null;
  collapsed: string[];
  viewport?: { x: number; y: number; scale: number };
}

export function visibleNodes(snapshot: Snapshot, collapsed: Iterable<string>): string[] {
  const folded = new Set(collapsed);
  const items = indexTree(snapshot);
  const result: string[] = [];
  const visit = (id: string) => {
    if (!items.has(id)) return;
    result.push(id);
    if (!folded.has(id)) items.get(id)!.child_ids.forEach(visit);
  };
  snapshot.root_ids.forEach(visit);
  return result;
}

export function reconcileView(snapshot: Snapshot, view: ViewState): ViewState {
  const items = indexTree(snapshot);
  const collapsed = view.collapsed.filter((id) => items.get(id)?.child_ids.length);
  let selected = view.selected;
  if (!selected || !items.has(selected)) selected = snapshot.root_ids[0] ?? null;
  let parent = selected ? items.get(selected)?.parent_id : null;
  while (parent) {
    if (collapsed.includes(parent)) selected = parent;
    parent = items.get(parent)?.parent_id;
  }
  return { ...view, selected, collapsed };
}

export function rootBranchSide(index: number, count: number): 'left' | 'right' {
  return count > 1 && index % 2 === 0 ? 'left' : 'right';
}

export function navigate(snapshot: Snapshot, view: ViewState, key: string): ViewState {
  const state = reconcileView(snapshot, view);
  const selected = state.selected;
  if (!selected) return state;
  const items = indexTree(snapshot);
  const item = items.get(selected)!;
  const collapsed = new Set(state.collapsed);
  const visible = visibleNodes(snapshot, collapsed);
  if (key === 'ArrowDown' || key === 'ArrowUp') {
    const offset = key === 'ArrowDown' ? 1 : -1;
    state.selected = visible[Math.max(0, Math.min(visible.length - 1, visible.indexOf(selected) + offset))] ?? selected;
  } else if (key === 'ArrowRight' || key === 'ArrowLeft') {
    const side = key === 'ArrowLeft' ? 'left' : 'right';
    if (!item.parent_id) {
      const child = item.child_ids.find((_, index) => rootBranchSide(index, item.child_ids.length) === side);
      if (child && collapsed.has(selected)) collapsed.delete(selected);
      else if (child) state.selected = child;
    } else {
      let branch = item;
      while (branch.parent_id && items.get(branch.parent_id)?.parent_id) branch = items.get(branch.parent_id)!;
      const root = items.get(branch.parent_id!)!;
      const outward = rootBranchSide(root.child_ids.indexOf(branch.id), root.child_ids.length);
      if (side === outward) {
        if (collapsed.has(selected)) collapsed.delete(selected);
        else state.selected = item.child_ids[0] ?? selected;
      } else if (item.child_ids.length && !collapsed.has(selected)) collapsed.add(selected);
      else state.selected = item.parent_id;
    }
  } else if (key === 'Home') state.selected = snapshot.root_ids[0] ?? null;
  else if (key === 'End') state.selected = visible.at(-1) ?? selected;
  return { ...state, collapsed: [...collapsed] };
}

export type Command = 'rename' | 'sibling' | 'child' | 'promote' | 'delete' | 'collapse' | 'search' |
  'fit' | 'undo' | 'redo' | 'note' | 'focus' | 'navigate' | 'escape' | 'save' |
  'bold' | 'underline' | 'strike' | 'sibling-before' | 'sibling-up' | 'sibling-down';

export function keyboardCommand(event: Pick<KeyboardEvent, 'key' | 'ctrlKey' | 'metaKey' | 'shiftKey' | 'altKey' | 'isComposing'>, editing = false): Command | null {
  if (event.isComposing) return null;
  const modified = event.ctrlKey || event.metaKey;
  const key = event.key.toLowerCase();
  if (editing) {
    if (modified && key === 's') return 'save';
    if (key === 'escape') return 'escape';
    return null;
  }
  if (modified) {
    if (!event.altKey && key === 'b' && !event.shiftKey) return 'bold';
    if (!event.altKey && key === 'u' && !event.shiftKey) return 'underline';
    if (!event.altKey && key === 'x' && event.shiftKey) return 'strike';
    if (key === 'z') return event.shiftKey ? 'redo' : 'undo';
    if (key === 'y') return 'redo';
    if (key === 'f') return 'search';
    if (key === '0') return 'fit';
    return null;
  }
  if (event.altKey) return !event.shiftKey && key === 'arrowup' ? 'sibling-up' : !event.shiftKey && key === 'arrowdown' ? 'sibling-down' : null;
  if (event.key.startsWith('Arrow') || key === 'home' || key === 'end') return 'navigate';
  if (key === 'f2') return 'rename';
  if (key === 'enter') return event.shiftKey ? 'sibling-before' : 'sibling';
  if (key === 'tab') return event.shiftKey ? 'promote' : 'child';
  if (key === 'delete' || key === 'backspace') return 'delete';
  if (key === ' ') return 'collapse';
  if (key === 'n') return 'note';
  if (key === 'f') return 'focus';
  if (key === 'escape') return 'escape';
  return null;
}

export function reveal(snapshot: Snapshot, view: ViewState, id: string): ViewState {
  const items = indexTree(snapshot);
  const folded = new Set(view.collapsed);
  let parent = items.get(id)?.parent_id;
  while (parent) {
    folded.delete(parent);
    parent = items.get(parent)?.parent_id;
  }
  return { ...view, selected: id, collapsed: [...folded] };
}

export function readView(storage: Pick<Storage, 'getItem'>, rootKey: string): ViewState {
  try {
    const saved = JSON.parse(storage.getItem(`rightmemory:pursuit-map:${rootKey}`) ?? 'null');
    const viewport = saved?.viewport;
    return {
      selected: typeof saved?.selected === 'string' ? saved.selected : null,
      collapsed: Array.isArray(saved?.collapsed) ? saved.collapsed.filter((id: unknown) => typeof id === 'string') : [],
      ...(saved?.layout === 'independent-roots' && viewport && [viewport.x, viewport.y, viewport.scale].every(Number.isFinite) && viewport.scale >= 0.1 && viewport.scale <= 2
        ? { viewport } : {}),
    };
  } catch { return { selected: null, collapsed: [] }; }
}

export function writeView(storage: Pick<Storage, 'setItem'>, rootKey: string, view: ViewState): void {
  try { storage.setItem(`rightmemory:pursuit-map:${rootKey}`, JSON.stringify({ ...view, layout: 'independent-roots' })); } catch { /* Private browsing may disable storage. */ }
}

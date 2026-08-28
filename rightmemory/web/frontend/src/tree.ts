/** The editor's tree is deliberately independent of the canvas library. */
export interface PursuitItem {
  id: string;
  title: string;
  body: string;
  parent_id: string | null;
  child_ids: string[];
  edges: [string, string][];
  focused: boolean;
  editable: boolean;
  source_path?: string;
  source_line?: number;
  anchor_kind?: string;
}

export interface Snapshot {
  items: PursuitItem[];
  root_ids: string[];
  focus_ids: string[];
  revision: string;
  git_head: string;
  root_key: string;
  valid: boolean;
  writable: boolean;
  diagnostics: (string | { message?: string; [key: string]: unknown })[];
}

export type Operation =
  | { type: 'create'; parent_id: string | null; after_id: string | null; title: string }
  | { type: 'rename'; id: string; title: string }
  | { type: 'move'; id: string; parent_id: string | null; after_id: string | null }
  | { type: 'delete'; id: string }
  | { type: 'edit_body'; id: string; body: string }
  | { type: 'set_focus'; id: string; focused: boolean };

export interface MutationResult {
  snapshot: Snapshot;
  commit: string | null;
  operation_id: string;
  repaired_references: unknown[];
  undoable: boolean;
  selected_id: string | null;
}

export const VIRTUAL_ROOT = '__pursuit_virtual_root__';
export const DRAFT_PREFIX = '__pursuit_draft_';

export function indexTree(snapshot: Snapshot): Map<string, PursuitItem> {
  return new Map(snapshot.items.map((item) => [item.id, item]));
}

export function visualRoot(snapshot: Snapshot): string {
  return snapshot.root_ids.length === 1 ? snapshot.root_ids[0] : VIRTUAL_ROOT;
}

export function childrenOf(snapshot: Snapshot, parent: string | null): string[] {
  return parent === null || parent === VIRTUAL_ROOT
    ? snapshot.root_ids
    : indexTree(snapshot).get(parent)?.child_ids ?? [];
}

export function descendants(snapshot: Snapshot, id: string): Set<string> {
  const items = indexTree(snapshot);
  const found = new Set<string>();
  const todo = [id];
  while (todo.length) {
    const next = todo.pop()!;
    if (found.has(next)) continue;
    found.add(next);
    todo.push(...(items.get(next)?.child_ids ?? []));
  }
  return found;
}

export function assertMove(snapshot: Snapshot, id: string, parent: string | null, after: string | null): void {
  const items = indexTree(snapshot);
  if (!items.get(id)?.editable) throw new Error('This item cannot be moved.');
  if (parent && !items.get(parent)?.editable) throw new Error('Choose an editable destination.');
  if (parent && descendants(snapshot, id).has(parent)) throw new Error('A direction cannot be moved inside itself.');
  if (after === id) throw new Error('Choose a different insertion position.');
  if (after && !childrenOf(snapshot, parent).includes(after)) throw new Error('The insertion point has changed.');
}

/** Null after_id means the first position; callers pass the last sibling to append. */
export function applyOperation(snapshot: Snapshot, operation: Operation, temporaryId?: string): Snapshot {
  const next: Snapshot = {
    ...snapshot,
    items: snapshot.items.map((item) => ({ ...item, child_ids: [...item.child_ids], edges: item.edges.map((edge) => [...edge]) })),
    root_ids: [...snapshot.root_ids],
    focus_ids: [...snapshot.focus_ids],
  };
  const items = indexTree(next);
  const siblings = (parent: string | null) => parent === null ? next.root_ids : items.get(parent)!.child_ids;
  const insert = (id: string, parent: string | null, after: string | null) => {
    const list = siblings(parent);
    if (after && !list.includes(after)) throw new Error('The insertion point has changed.');
    list.splice(after ? list.indexOf(after) + 1 : 0, 0, id);
  };
  if (operation.type === 'create') {
    if (!temporaryId) throw new Error('A local identity is needed while saving a new direction.');
    if (operation.parent_id && !items.get(operation.parent_id)?.editable) throw new Error('Choose an editable parent.');
    const item: PursuitItem = {
      id: temporaryId, title: operation.title, body: '', parent_id: operation.parent_id,
      child_ids: [], edges: [], focused: false, editable: true,
    };
    next.items.push(item);
    items.set(item.id, item);
    insert(item.id, item.parent_id, operation.after_id);
    return next;
  }
  const item = items.get(operation.id);
  if (!item?.editable) throw new Error('This item is no longer editable. Reload the map.');
  switch (operation.type) {
    case 'rename': item.title = operation.title; break;
    case 'edit_body': item.body = operation.body; break;
    case 'set_focus':
      item.focused = operation.focused;
      next.focus_ids = next.focus_ids.filter((id) => id !== item.id);
      if (item.focused) next.focus_ids.push(item.id);
      break;
    case 'move': {
      assertMove(snapshot, item.id, operation.parent_id, operation.after_id);
      const old = siblings(item.parent_id);
      old.splice(old.indexOf(item.id), 1);
      item.parent_id = operation.parent_id;
      insert(item.id, item.parent_id, operation.after_id);
      break;
    }
    case 'delete': {
      const removed = descendants(next, item.id);
      const old = siblings(item.parent_id);
      old.splice(old.indexOf(item.id), 1);
      next.items = next.items.filter((candidate) => !removed.has(candidate.id));
      next.focus_ids = next.focus_ids.filter((id) => !removed.has(id));
      next.items.forEach((candidate) => { candidate.edges = candidate.edges.filter((edge) => !removed.has(edge[1])); });
      break;
    }
  }
  return next;
}

export function remapOperation(operation: Operation, temporaryId: string, id: string): Operation {
  const mapped = { ...operation };
  if ('id' in mapped && mapped.id === temporaryId) mapped.id = id;
  if ('parent_id' in mapped && mapped.parent_id === temporaryId) mapped.parent_id = id;
  if ('after_id' in mapped && mapped.after_id === temporaryId) mapped.after_id = id;
  return mapped;
}

export function promoteOperation(snapshot: Snapshot, id: string): Operation | null {
  const items = indexTree(snapshot);
  const item = items.get(id);
  if (!item?.parent_id) return null;
  const parent = items.get(item.parent_id)!;
  return { type: 'move', id, parent_id: parent.parent_id, after_id: parent.id };
}

export function deletionSelection(snapshot: Snapshot, id: string): string | null {
  const item = indexTree(snapshot).get(id);
  if (!item) return null;
  const siblings = childrenOf(snapshot, item.parent_id);
  const position = siblings.indexOf(id);
  return siblings[position + 1] ?? siblings[position - 1] ?? item.parent_id ?? VIRTUAL_ROOT;
}

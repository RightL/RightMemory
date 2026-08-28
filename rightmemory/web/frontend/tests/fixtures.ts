import { type PursuitItem, type Snapshot } from '../src/tree.ts';

export function fixture(count = 22): Snapshot {
  const items: PursuitItem[] = [];
  const add = (id: string, title: string, parent: string | null) => {
    const item: PursuitItem = { id, title, parent_id: parent, child_ids: [], body: '', edges: [], focused: false, editable: true };
    items.push(item);
    if (parent) items.find((candidate) => candidate.id === parent)!.child_ids.push(id);
    return item;
  };
  add('directions', 'Directions 方向', null);
  add('research', 'Research 研究', 'directions');
  add('design', 'Design 设计', 'directions');
  add('writing', 'Writing 写作', 'directions');
  let parent = 'research';
  for (let depth = 1; depth <= 7; depth++) {
    const child = add(`level-${depth}`, `Explore ${depth} 探索`, parent);
    add(`alternative-${depth}`, `Alternative ${depth}`, parent);
    parent = child.id;
  }
  add('interaction', 'Interaction study', 'design').body = 'Observe how a new reader understands the map.\n\nKeep the labels short.';
  add('visual', 'Visual language', 'design');
  add('essays', 'Essays 随笔', 'writing').focused = true;
  add('drafts', 'Ideas', 'writing');
  while (items.length < count) {
    const index = items.length;
    const parentId = index % 6 === 0 ? 'directions' : items[Math.max(1, Math.floor(index / 5))].id;
    add(`generated-${index}`, `Direction ${index} 方向`, parentId);
  }
  return { items, root_ids: ['directions'], focus_ids: ['essays'], revision: 'r0', git_head: 'c0', root_key: 'browser-fixture', valid: true, writable: true, diagnostics: [] };
}

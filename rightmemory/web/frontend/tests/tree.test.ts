import assert from 'node:assert/strict';
import test from 'node:test';
import { applyOperation, assertMove, descendants, indexTree, promoteOperation, visualRoot, VIRTUAL_ROOT } from '../src/tree.ts';
import { fixture } from './fixtures.ts';

test('create inserts at the requested sibling position without mutating the source tree', () => {
  const original = fixture();
  const before = structuredClone(original);
  const created = applyOperation(original, { type: 'create', parent_id: 'design', after_id: 'interaction', title: 'Prototype 原型' }, 'temporary');
  assert.deepEqual(indexTree(created).get('design')!.child_ids, ['interaction', 'temporary', 'visual']);
  assert.equal(indexTree(created).get('temporary')!.title, 'Prototype 原型');
  assert.deepEqual(original, before);
});

test('rename, note, and Focus preserve identity and subtree order', () => {
  const original = fixture();
  const renamed = applyOperation(original, { type: 'rename', id: 'design', title: 'Design 设计 <b>plain text</b>' });
  const noted = applyOperation(renamed, { type: 'edit_body', id: 'design', body: '# Notes\n\n- free form\n' });
  const focused = applyOperation(noted, { type: 'set_focus', id: 'design', focused: true });
  assert.equal(indexTree(focused).get('design')!.id, 'design');
  assert.deepEqual(indexTree(focused).get('design')!.child_ids, indexTree(original).get('design')!.child_ids);
  assert.equal(indexTree(focused).get('design')!.body, '# Notes\n\n- free form\n');
  assert.deepEqual(focused.focus_ids, ['essays', 'design']);
});

test('move and promote keep a deep subtree intact while changing sibling order', () => {
  const original = fixture();
  const subtree = descendants(original, 'level-1');
  const moved = applyOperation(original, { type: 'move', id: 'level-1', parent_id: 'writing', after_id: null });
  assert.equal(indexTree(moved).get('level-1')!.parent_id, 'writing');
  assert.deepEqual(descendants(moved, 'level-1'), subtree);
  assert.equal(indexTree(moved).get('writing')!.child_ids[0], 'level-1');
  const promoted = applyOperation(moved, promoteOperation(moved, 'level-1')!);
  assert.deepEqual(indexTree(promoted).get('directions')!.child_ids, ['research', 'design', 'writing', 'level-1']);
});

test('cycle and missing destination rejection leave source unchanged', () => {
  const original = fixture();
  assert.throws(() => assertMove(original, 'research', 'level-7', null), /inside itself/);
  assert.throws(() => applyOperation(original, { type: 'move', id: 'research', parent_id: 'missing', after_id: null }), /destination/);
  assert.equal(indexTree(original).get('research')!.parent_id, 'directions');
});

test('delete removes logical descendants, Focus, and inbound edges; a real visual root can be removed', () => {
  const original = fixture();
  indexTree(original).get('research')!.edges = [['ref', 'essays'], ['ref', 'interaction']];
  const removed = applyOperation(original, { type: 'delete', id: 'writing' });
  assert(!indexTree(removed).has('essays'));
  assert.deepEqual(removed.focus_ids, []);
  assert.deepEqual(indexTree(removed).get('research')!.edges, [['ref', 'interaction']]);
  const empty = applyOperation(removed, { type: 'delete', id: 'directions' });
  assert.equal(visualRoot(empty), VIRTUAL_ROOT);
  assert.deepEqual(empty.items, []);
});

test('a second top-level item changes the visual root without inventing a semantic node', () => {
  const original = fixture();
  const plural = applyOperation(original, { type: 'create', parent_id: null, after_id: 'directions', title: 'Another direction' }, 'second');
  assert.equal(visualRoot(original), 'directions');
  assert.equal(visualRoot(plural), VIRTUAL_ROOT);
  assert.equal(plural.items.length, original.items.length + 1);
});

test('500-item edits preserve every unrelated item', () => {
  const original = fixture(500);
  const next = applyOperation(original, { type: 'rename', id: 'generated-499', title: '五百 / Five hundred' });
  assert.equal(next.items.length, 500);
  assert.deepEqual(next.items.slice(0, 499), original.items.slice(0, 499));
});

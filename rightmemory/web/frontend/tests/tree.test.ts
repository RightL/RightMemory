import assert from 'node:assert/strict';
import test from 'node:test';
import { applyOperation, assertMove, createSiblingBeforeOperation, deletionSelection, descendants, dropOperation, indexTree, moveSiblingOperation, promoteOperation } from '../src/tree.ts';
import { forestData, titleMarkup, titleText } from '../src/canvas-data.ts';
import { fixture, forestFixture } from './fixtures.ts';

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
  assert.deepEqual(forestData(empty, { selected: null, collapsed: [] }), []);
  assert.equal(deletionSelection(removed, 'directions'), null);
  assert.deepEqual(empty.items, []);
});

test('each top-level direction remains its own canvas root', () => {
  const original = fixture();
  const plural = applyOperation(original, { type: 'create', parent_id: null, after_id: 'directions', title: 'Another direction' }, 'second');
  const data = forestData(plural, { selected: 'second', collapsed: [] });
  assert.deepEqual(data.map((map) => map.nodeData.id), ['directions', 'second']);
  assert.deepEqual(data[0].nodeData.children!.map((node) => node.direction), [0, 1, 0]);
  assert.equal(data[1].nodeData.topic, 'Another direction');
  assert.equal(deletionSelection(plural, 'directions'), 'second');
  assert.equal(plural.items.length, original.items.length + 1);
});

test('independent roots collapse separately and a single child extends right', () => {
  const data = forestData(forestFixture(), { selected: 'directions', collapsed: ['directions'] });
  assert.deepEqual(data[0].nodeData.children, []);
  assert.equal(data[1].direction, 1);
  assert.equal(data[1].nodeData.children![0].direction, 1);
  assert.equal(data[1].nodeData.children![0].children![0].id, 'caption');
});

test('cross-map drops preserve subtrees and support top-level ordering', () => {
  const original = forestFixture();
  const moved = applyOperation(original, dropOperation(original, 'research', 'sessions', 'in'));
  assert.equal(indexTree(moved).get('research')!.parent_id, 'sessions');
  assert.deepEqual(descendants(moved, 'research'), descendants(original, 'research'));
  assert.equal(indexTree(moved).get('sessions')!.child_ids.at(-1), 'research');
  const root = applyOperation(moved, dropOperation(moved, 'research', 'practice', 'before'));
  assert.deepEqual(root.root_ids, ['directions', 'research', 'practice']);
  const reordered = applyOperation(root, dropOperation(root, 'practice', 'directions', 'before'));
  assert.deepEqual(reordered.root_ids, ['practice', 'directions', 'research']);
  assert.throws(() => dropOperation(original, 'practice', 'caption', 'in'), /inside itself/);
  for (const position of ['in', 'before', 'after'] as const) assert.throws(() => dropOperation(original, 'practice', 'practice', position), /different destination/);
});

test('strikethrough display escapes HTML and preserves the raw title for editing', () => {
  const raw = '~~Earlier <img src=x onerror="alert(1)">~~ & Current';
  const snapshot = applyOperation(fixture(), { type: 'rename', id: 'research', title: raw });
  const data = forestData(snapshot, { selected: 'research', collapsed: [] });
  assert.equal(data[0].nodeData.children![0].topic, raw);
  assert.equal(titleMarkup(raw), '<s>Earlier &lt;img src=x onerror=&quot;alert(1)&quot;&gt;</s> &amp; Current');
  assert.equal(titleText(raw), 'Earlier <img src=x onerror="alert(1)"> & Current');
  assert.equal(titleMarkup('Unpaired ~~marker'), 'Unpaired ~~marker');
});

test('500-item edits preserve every unrelated item', () => {
  const original = fixture(500);
  const next = applyOperation(original, { type: 'rename', id: 'generated-499', title: '五百 / Five hundred' });
  assert.equal(next.items.length, 500);
  assert.deepEqual(next.items.slice(0, 499), original.items.slice(0, 499));
});

test('sibling-before creates immediately before the selection, including first children and roots', () => {
  for (const [id, parent, expected] of [
    ['design', 'directions', ['research', 'new', 'design', 'writing']],
    ['research', 'directions', ['new', 'research', 'design', 'writing']],
    ['directions', null, ['new', 'directions', 'practice']],
    ['practice', null, ['directions', 'new', 'practice']],
  ] as const) {
    const next = applyOperation(forestFixture(), createSiblingBeforeOperation(forestFixture(), id, 'New'), 'new');
    assert.deepEqual(parent ? indexTree(next).get(parent)!.child_ids : next.root_ids, expected);
  }
});

test('sibling moves work in both directions at every depth and are no-ops at boundaries', () => {
  const original = forestFixture();
  for (const [id, delta, parent, expected] of [
    ['design', -1, 'directions', ['design', 'research', 'writing']],
    ['design', 1, 'directions', ['research', 'writing', 'design']],
    ['practice', -1, null, ['practice', 'directions']],
    ['directions', 1, null, ['practice', 'directions']],
    ['visual', -1, 'design', ['visual', 'interaction']],
  ] as const) {
    const next = applyOperation(original, moveSiblingOperation(original, id, delta)!);
    assert.deepEqual(parent ? indexTree(next).get(parent)!.child_ids : next.root_ids, expected);
    assert.deepEqual(descendants(next, id), descendants(original, id));
  }
  for (const [id, delta] of [['research', -1], ['writing', 1], ['directions', -1], ['practice', 1]] as const) {
    assert.equal(moveSiblingOperation(original, id, delta), null);
  }
});

test('visible-empty create and rename are refused without changing the snapshot', () => {
  const original = fixture();
  const before = structuredClone(original);
  for (const title of ['****', '~~~~', '<u></u>', '**<u>~~ ~~</u>**']) {
    assert.throws(() => applyOperation(original, { type: 'rename', id: 'design', title }), /visible title/);
    assert.throws(() => applyOperation(original, { type: 'create', title, parent_id: null, after_id: null }, 'new'), /visible title/);
  }
  assert.deepEqual(original, before);
});

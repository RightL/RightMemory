import assert from 'node:assert/strict';
import test from 'node:test';
import { navigate, keyboardCommand, readView, reconcileView, reveal, visibleNodes, writeView, type ViewState } from '../src/view-state.ts';
import { applyOperation } from '../src/tree.ts';
import { DraftBook, type TitleDraft } from '../src/drafts.ts';
import { stackMaps } from '../src/canvas-data.ts';
import { CanvasGestures, type PointerSample } from '../src/gestures.ts';
import { fixture, forestFixture } from './fixtures.ts';

const key = (key: string, changes: Partial<KeyboardEvent> = {}) => ({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, ...changes });
const pointer = (pointerType: string, changes: Partial<PointerSample> = {}): PointerSample => ({ pointerId: 1, pointerType, button: 0, buttons: 1, x: 100, y: 100, ...changes });

test('touch beginning on a node pans without producing a structural drop', () => {
  const gestures = new CanvasGestures();
  const viewport = { x: 10, y: 20, scale: 0.8 };
  assert(gestures.start(pointer('touch'), 'research', viewport));
  const motion = gestures.move(pointer('touch', { x: 240, y: 165 }));
  assert(motion.kind === 'view');
  assert.deepEqual(motion.viewport, { x: 150, y: 85, scale: 0.8 });
  assert.equal(gestures.end(1, motion.viewport), null);
  assert(!gestures.active);
});

test('two touches pinch around their midpoint and continue panning after one lifts', () => {
  const gestures = new CanvasGestures();
  const viewport = { x: 10, y: 20, scale: 0.8 };
  gestures.start(pointer('touch'), 'research', viewport);
  gestures.start(pointer('touch', { pointerId: 2, x: 200 }), 'practice', viewport);
  const pinch = gestures.move(pointer('touch', { pointerId: 2, x: 300 }));
  assert(pinch.kind === 'view');
  assert.deepEqual(pinch.viewport, { x: -80, y: -60, scale: 1.6 });
  assert.equal(gestures.end(1, pinch.viewport), null);
  const pan = gestures.move(pointer('touch', { pointerId: 2, x: 320, y: 130 }));
  assert(pan.kind === 'view');
  assert.deepEqual(pan.viewport, { x: -60, y: -30, scale: 1.6 });
  assert.equal(gestures.end(2, pan.viewport), null);
  assert(!gestures.active);
});

test('mouse and pen require a held press and threshold before a node can drop', () => {
  for (const type of ['mouse', 'pen']) {
    const gestures = new CanvasGestures();
    const viewport = { x: 0, y: 0, scale: 1 };
    gestures.start(pointer(type), 'research', viewport);
    assert.deepEqual(gestures.move(pointer(type, { x: 102 })), { kind: 'idle' });
    assert.equal(gestures.end(1, viewport), null);
    gestures.start(pointer(type), 'research', viewport);
    assert.deepEqual(gestures.move(pointer(type, { x: 120 })), { kind: 'drag', id: 'research' });
    assert.equal(gestures.end(1, viewport), 'research');
    gestures.start(pointer(type), 'research', viewport);
    gestures.move(pointer(type, { x: 120 }));
    assert.deepEqual(gestures.move(pointer(type, { x: 140, buttons: 0 })), { kind: 'cancel' });
    assert.equal(gestures.end(1, viewport), null);
    assert.deepEqual(gestures.move(pointer(type, { x: 160, buttons: 1 })), { kind: 'idle' });
    gestures.start(pointer(type), 'research', viewport);
    gestures.move(pointer(type, { x: 120 }));
    gestures.start(pointer(type, { x: 300 }), 'practice', viewport);
    assert.deepEqual(gestures.move(pointer(type, { x: 302 })), { kind: 'idle' });
    assert.equal(gestures.end(1, viewport), null);
  }
});

test('canceled or lost gestures cannot leave a pending drop or a stale pinch', () => {
  const gestures = new CanvasGestures();
  const viewport = { x: 0, y: 0, scale: 1 };
  gestures.start(pointer('mouse'), 'research', viewport);
  gestures.move(pointer('mouse', { x: 120 }));
  gestures.cancel();
  assert.equal(gestures.end(1, viewport), null);
  gestures.start(pointer('touch'), 'research', viewport);
  gestures.start(pointer('touch', { pointerId: 2, x: 200 }), 'practice', viewport);
  gestures.cancel();
  assert(!gestures.touching);
  assert.deepEqual(gestures.move(pointer('touch', { pointerId: 2, x: 250 })), { kind: 'idle' });
  assert.equal(gestures.end(2, viewport), null);
  gestures.start(pointer('touch', { pointerId: 3 }), 'practice', viewport);
  const motion = gestures.move(pointer('touch', { pointerId: 3, x: 120 }));
  assert(motion.kind === 'view');
  assert.deepEqual(motion.viewport, { x: 20, y: 0, scale: 1 });
});

test('keyboard mapping covers direct editing and respects input composition', () => {
  assert.equal(keyboardCommand(key('Enter')), 'sibling');
  assert.equal(keyboardCommand(key('Tab')), 'child');
  assert.equal(keyboardCommand(key('Tab', { shiftKey: true })), 'promote');
  assert.equal(keyboardCommand(key('z', { ctrlKey: true, shiftKey: true })), 'redo');
  assert.equal(keyboardCommand(key('F2')), 'rename');
  assert.equal(keyboardCommand(key('Enter', { isComposing: true })), null);
  assert.equal(keyboardCommand(key('Backspace'), true), null);
  assert.equal(keyboardCommand(key('z', { metaKey: true }), true), null);
  assert.equal(keyboardCommand(key('s', { metaKey: true }), true), 'save');
});

test('arrow navigation folds branches and selects visible neighbors', () => {
  const snapshot = fixture();
  let view: ViewState = { selected: 'research', collapsed: [] };
  view = navigate(snapshot, view, 'ArrowRight');
  assert.deepEqual(view.collapsed, ['research']);
  assert(!visibleNodes(snapshot, view.collapsed).includes('level-7'));
  view = navigate(snapshot, view, 'ArrowDown');
  assert.equal(view.selected, 'design');
  view = navigate(snapshot, view, 'ArrowRight');
  assert.equal(view.selected, 'interaction');
  view = navigate(snapshot, view, 'ArrowLeft');
  assert.equal(view.selected, 'design');
});

test('horizontal navigation follows both sides of each independent map', () => {
  const snapshot = forestFixture();
  const root: ViewState = { selected: 'directions', collapsed: [] };
  assert.equal(navigate(snapshot, root, 'ArrowLeft').selected, 'research');
  assert.equal(navigate(snapshot, root, 'ArrowRight').selected, 'design');
  assert.equal(navigate(snapshot, { ...root, selected: 'research' }, 'ArrowLeft').selected, 'level-1');
  assert.equal(navigate(snapshot, { ...root, selected: 'practice' }, 'ArrowLeft').selected, 'practice');
  assert.equal(navigate(snapshot, { ...root, selected: 'practice' }, 'ArrowRight').selected, 'sessions');
  assert.equal(navigate(snapshot, { ...root, selected: 'drafts' }, 'ArrowDown').selected, 'practice');
  assert.equal(navigate(snapshot, { ...root, selected: 'practice' }, 'ArrowUp').selected, 'drafts');
  assert.equal(navigate(snapshot, { ...root, selected: 'practice' }, 'Home').selected, 'directions');
  assert.equal(navigate(snapshot, root, 'End').selected, 'caption');
});

test('an empty map has no selectable placeholder and remains keyboard safe', () => {
  const snapshot = applyOperation(fixture(), { type: 'delete', id: 'directions' });
  const view = reconcileView(snapshot, { selected: 'directions', collapsed: ['research'] });
  assert.deepEqual(view, { selected: null, collapsed: [] });
  assert.deepEqual(visibleNodes(snapshot, []), []);
  for (const key of ['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End']) assert.deepEqual(navigate(snapshot, view, key), view);
});

test('independent map placement aligns roots and reserves nonoverlapping rows', () => {
  const layout = stackMaps([
    { id: 'one', width: 1000, height: 480, rootX: 650 },
    { id: 'two', width: 500, height: 180, rootX: 100 },
    { id: 'three', width: 800, height: 320, rootX: 450 },
  ]);
  assert.deepEqual(layout.maps.map((map) => map.id), ['one', 'two', 'three']);
  assert.equal(new Set(layout.maps.map((map) => map.x + map.rootX)).size, 1);
  for (let index = 1; index < layout.maps.length; index++) assert(layout.maps[index].y > layout.maps[index - 1].y + layout.maps[index - 1].height);
  assert.equal(layout.width, 1050);
  assert.equal(layout.height, 1108);
  assert.deepEqual(stackMaps([]), { maps: [], width: 0, height: 0 });
});

test('selection reconciliation never leaves a hidden or deleted node selected', () => {
  const snapshot = fixture();
  const view = reconcileView(snapshot, { selected: 'level-7', collapsed: ['research', 'missing'] });
  assert.equal(view.selected, 'research');
  assert.deepEqual(view.collapsed, ['research']);
  const removed = applyOperation(snapshot, { type: 'delete', id: 'research' });
  assert.equal(reconcileView(removed, view).selected, 'directions');
  assert.deepEqual(reveal(snapshot, view, 'level-7').collapsed, []);
});

test('browser view state is isolated by root', () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) ?? null, setItem: (key: string, value: string) => { values.set(key, value); } };
  const view = { selected: 'research', collapsed: ['design'], viewport: { x: 10, y: 20, scale: 0.8 } };
  writeView(storage, 'root-a', view);
  assert.deepEqual(readView(storage, 'root-a'), view);
  assert.deepEqual(readView(storage, 'root-b'), { selected: null, collapsed: [] });
  storage.setItem('rightmemory:pursuit-map:broken', '{bad');
  assert.deepEqual(readView(storage, 'broken'), { selected: null, collapsed: [] });
  storage.setItem('rightmemory:pursuit-map:old-layout', JSON.stringify({ ...view, selected: '__pursuit_virtual_root__' }));
  const old = reconcileView(forestFixture(), readView(storage, 'old-layout'));
  assert.deepEqual(old, { selected: 'directions', collapsed: ['design'] });
});

test('note conflict keeps unsaved text even when an authoritative snapshot changes its body', () => {
  const book = new DraftBook();
  const note = book.openNote('design', 'Original');
  note.text = 'My unsaved 中文 note';
  note.saving = true;
  book.noteFailed('design', 'Conflict');
  const external = applyOperation(fixture(), { type: 'edit_body', id: 'design', body: 'Another writer' });
  book.reconcile(external);
  assert.equal(book.note!.text, 'My unsaved 中文 note');
  assert.equal(book.note!.savedText, 'Original');
  assert.equal(book.note!.error, 'Conflict');
});

test('a note typed during saving stays dirty after the older request succeeds', () => {
  const book = new DraftBook();
  const note = book.openNote('design', 'Original');
  note.text = 'Submitted'; note.saving = true;
  note.text = 'Typed while saving';
  book.noteSaved('design', 'Submitted');
  assert.equal(book.note!.text, 'Typed while saving');
  assert.equal(book.note!.savedText, 'Submitted');
  assert(book.dirty);
});

test('failed creation retains its title and remaps a just-saved parent', () => {
  const book = new DraftBook();
  const draft: TitleDraft = { id: 'temp-child', temporaryId: 'temp-child', text: 'Child 子方向', operation: { type: 'create', title: 'Child 子方向', parent_id: 'temp-parent', after_id: null } };
  book.savingTitles.push(draft);
  book.remap('temp-parent', 'saved-parent');
  book.failedTitle(draft, 'Conflict');
  assert.equal(book.failedTitles[0].text, 'Child 子方向');
  assert.equal(book.failedTitles[0].operation.type === 'create' && book.failedTitles[0].operation.parent_id, 'saved-parent');
  assert(book.dirty);
});

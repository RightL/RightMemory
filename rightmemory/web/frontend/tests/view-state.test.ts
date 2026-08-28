import assert from 'node:assert/strict';
import test from 'node:test';
import { navigate, keyboardCommand, readView, reconcileView, reveal, visibleNodes, writeView, type ViewState } from '../src/view-state.ts';
import { applyOperation } from '../src/tree.ts';
import { DraftBook, type TitleDraft } from '../src/drafts.ts';
import { fixture } from './fixtures.ts';

const key = (key: string, changes: Partial<KeyboardEvent> = {}) => ({ key, ctrlKey: false, metaKey: false, shiftKey: false, altKey: false, isComposing: false, ...changes });

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
  view = navigate(snapshot, view, 'ArrowLeft');
  assert.deepEqual(view.collapsed, ['research']);
  assert(!visibleNodes(snapshot, view.collapsed).includes('level-7'));
  view = navigate(snapshot, view, 'ArrowDown');
  assert.equal(view.selected, 'design');
  view = navigate(snapshot, view, 'ArrowRight');
  assert.equal(view.selected, 'interaction');
  view = navigate(snapshot, view, 'ArrowLeft');
  assert.equal(view.selected, 'design');
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

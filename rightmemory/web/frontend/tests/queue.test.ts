import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, MutationQueue, type Transport } from '../src/queue.ts';
import { applyOperation, indexTree, type MutationResult, type Operation } from '../src/tree.ts';
import { fixture } from './fixtures.ts';

function harness() {
  let current = fixture();
  const requests: Array<{ revision: string; operation: Operation; resolve(value: MutationResult): void; reject(error: Error): void }> = [];
  const history: Array<{ kind: string; revision: string; commit: string }> = [];
  const transport: Transport = {
    load: async () => current,
    mutate: (revision, operation) => new Promise((resolve, reject) => requests.push({ revision, operation, resolve, reject })),
    history: async (kind, revision, commit) => {
      history.push({ kind, revision, commit });
      const landed = `history-${history.length}`;
      current = { ...current, revision: landed, git_head: landed };
      return { snapshot: current, commit: landed, operation_id: landed, repaired_references: [], undoable: true, selected_id: null };
    },
  };
  const respond = (index: number, id?: string) => {
    const request = requests[index];
    const next = applyOperation(current, request.operation, id);
    current = { ...next, revision: `r${index + 1}`, git_head: `c${index + 1}` };
    request.resolve({ snapshot: current, commit: current.git_head, operation_id: `o${index}`, repaired_references: [], undoable: true, selected_id: id ?? ('id' in request.operation ? request.operation.id : null) });
  };
  return { queue: new MutationQueue(current, transport), requests, history, respond, get current() { return current; } };
}

test('optimistic edits are immediate and server requests run in order with the returned revision', async () => {
  const { queue, requests, respond } = harness();
  const first = queue.enqueue({ type: 'rename', id: 'design', title: 'New title' });
  const second = queue.enqueue({ type: 'set_focus', id: 'design', focused: true });
  assert.equal(requests.length, 1);
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'New title');
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, true);
  respond(0); await first;
  assert.equal(requests.length, 2);
  assert.equal(requests[1].revision, 'r1');
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, true);
  respond(1); await second;
  assert.equal(queue.pendingCount, 0);
  assert.equal(queue.snapshot.revision, 'r2');
});

test('rapid parent then child creation remaps pending parent, rename, move and sibling references', async () => {
  const { queue, requests, respond } = harness();
  const parent = queue.enqueue({ type: 'create', parent_id: 'design', after_id: null, title: 'Parent 父' }, 'temp-parent');
  const child = queue.enqueue({ type: 'create', parent_id: 'temp-parent', after_id: null, title: 'Child 子' }, 'temp-child');
  const rename = queue.enqueue({ type: 'rename', id: 'temp-child', title: 'Renamed 子方向' });
  const move = queue.enqueue({ type: 'move', id: 'visual', parent_id: 'temp-parent', after_id: 'temp-child' });
  const remaps: unknown[] = [];
  queue.subscribe((change) => { if (change.remapped) remaps.push(change.remapped); });
  respond(0, 'saved-parent'); await parent;
  assert.deepEqual(requests[1].operation, { type: 'create', parent_id: 'saved-parent', after_id: null, title: 'Child 子' });
  respond(1, 'saved-child'); await child;
  assert.deepEqual(requests[2].operation, { type: 'rename', id: 'saved-child', title: 'Renamed 子方向' });
  respond(2); await rename;
  assert.deepEqual(requests[3].operation, { type: 'move', id: 'visual', parent_id: 'saved-parent', after_id: 'saved-child' });
  respond(3); await move;
  assert.deepEqual(indexTree(queue.snapshot).get('saved-parent')!.child_ids, ['saved-child', 'visual']);
  assert.deepEqual(remaps, [{ from: 'temp-parent', to: 'saved-parent' }, { from: 'temp-child', to: 'saved-child' }]);
});

test('a revision conflict rolls back queued changes to the server snapshot without replaying them', async () => {
  const { queue, requests } = harness();
  const first = queue.enqueue({ type: 'rename', id: 'design', title: 'Unsaved title' });
  const second = queue.enqueue({ type: 'edit_body', id: 'design', body: 'Unsaved note' });
  const settled = Promise.allSettled([first, second]);
  const external = { ...applyOperation(fixture(), { type: 'rename', id: 'design', title: 'External title' }), revision: 'external' };
  requests[0].reject(new ApiError('Conflict', 409, external));
  const results = await settled;
  assert(results.every((result) => result.status === 'rejected'));
  assert.equal(requests.length, 1);
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'External title');
  assert.equal(indexTree(queue.snapshot).get('design')!.body, '');
  assert.equal(queue.pendingCount, 0);
  assert.equal(queue.canUndo, false);
});

test('undo uses landed commits and redo uses the returned revert commit', async () => {
  const { queue, respond, history } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Edited' });
  assert.equal(queue.canUndo, false);
  respond(0); await edit;
  await queue.history('undo');
  assert.deepEqual(history[0], { kind: 'undo', revision: 'r1', commit: 'c1' });
  assert.equal(queue.canRedo, true);
  await queue.history('redo');
  assert.deepEqual(history[1], { kind: 'redo', revision: 'history-1', commit: 'history-1' });
  await queue.history('undo');
  assert.equal(history[2].commit, 'history-2');
});

test('a no-op response creates no undo entry', async () => {
  const { queue, requests, current } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Design 设计' });
  requests[0].resolve({ snapshot: current, commit: null, operation_id: 'noop', repaired_references: [], undoable: false, selected_id: 'design' });
  await edit;
  assert.equal(queue.canUndo, false);
});

test('authoritative plain-heading ID promotion remaps queued references', async () => {
  const { queue, requests, current } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Addressable' });
  const note = queue.enqueue({ type: 'edit_body', id: 'design', body: 'Still my note' });
  const canonical = structuredClone(current);
  canonical.items.forEach((item) => {
    if (item.id === 'design') item.id = 'design-stable';
    if (item.parent_id === 'design') item.parent_id = 'design-stable';
    item.child_ids = item.child_ids.map((id) => id === 'design' ? 'design-stable' : id);
  });
  requests[0].resolve({ snapshot: canonical, commit: 'promoted', operation_id: 'promoted', repaired_references: [], undoable: true, selected_id: 'design-stable' });
  await edit;
  assert.deepEqual(requests[1].operation, { type: 'edit_body', id: 'design-stable', body: 'Still my note' });
  requests[1].resolve({ snapshot: canonical, commit: 'note', operation_id: 'note', repaired_references: [], undoable: true, selected_id: 'design-stable' });
  await note;
});

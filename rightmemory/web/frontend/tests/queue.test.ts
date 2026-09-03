import assert from 'node:assert/strict';
import test from 'node:test';
import { ApiError, MutationQueue, type Transport } from '../src/queue.ts';
import { applyOperation, indexTree, type MutationResult, type Operation, type Snapshot } from '../src/tree.ts';
import { fixture } from './fixtures.ts';

function remapSnapshot(snapshot: Snapshot, remaps: Array<{ from: string; to: string }>, revision: string, gitHead: string): Snapshot {
  const names = new Map(remaps.map(({ from, to }) => [from, to]));
  const mapped = (id: string) => names.get(id) ?? id;
  return {
    ...snapshot,
    revision,
    git_head: gitHead,
    root_ids: snapshot.root_ids.map(mapped),
    focus_ids: snapshot.focus_ids.map(mapped),
    items: snapshot.items.map((item) => ({
      ...item,
      id: mapped(item.id),
      parent_id: item.parent_id ? mapped(item.parent_id) : null,
      child_ids: item.child_ids.map(mapped),
      edges: item.edges.map(([kind, target]) => [kind, mapped(target)]),
    })),
  };
}

function harness() {
  let current = fixture();
  const requests: Array<{ revision: string; operation: Operation; resolve(value: MutationResult): void; reject(error: Error): void }> = [];
  const history: Array<{ kind: string; revision: string; operationId: string }> = [];
  const checkpoints: string[] = [];
  const actions = new Map<string, { before: Snapshot; after: Snapshot }>();
  const transport: Transport = {
    load: async () => current,
    mutate: (revision, operation) => new Promise((resolve, reject) => requests.push({ revision, operation, resolve, reject })),
    history: async (kind, revision, operationId) => {
      history.push({ kind, revision, operationId });
      const landed = `history-${history.length}`;
      const saved = actions.get(operationId)!;
      const nextHistory = structuredClone(current.history);
      assert.equal(nextHistory[kind].pop(), operationId);
      nextHistory[kind === 'undo' ? 'redo' : 'undo'].push(operationId);
      current = { ...(kind === 'undo' ? saved.before : saved.after), revision: landed, git_head: current.git_head, pending: true, history: nextHistory };
      return { snapshot: current, commit: null, operation_id: operationId, repaired_references: [], undoable: true, selected_id: null };
    },
    flush: async (revision) => {
      assert.equal(revision, current.revision);
      const commit = `checkpoint-${checkpoints.length + 1}`;
      checkpoints.push(commit);
      current = { ...current, pending: false, git_head: commit };
      return { snapshot: current, commit, operation_id: '', repaired_references: [], undoable: false, selected_id: null };
    },
    activity: async () => {},
  };
  const respond = (index: number, id?: string) => {
    const request = requests[index];
    const before = current;
    const next = applyOperation(current, request.operation, id);
    current = { ...next, revision: `r${index + 1}`, pending: true, history: { undo: [...current.history.undo, `o${index}`], redo: [] } };
    actions.set(`o${index}`, { before, after: current });
    request.resolve({ snapshot: current, commit: null, operation_id: `o${index}`, repaired_references: [], undoable: true, selected_id: id ?? ('id' in request.operation ? request.operation.id : null) });
  };
  return { queue: new MutationQueue(current, transport), transport, requests, history, checkpoints, respond, get current() { return current; } };
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

test('rename_many is one optimistic request and creates one undo entry', async () => {
  const { queue, requests, respond, history } = harness();
  const edit = queue.enqueue({
    type: 'rename_many',
    renames: [{ id: 'design', title: 'Product design' }, { id: 'writing', title: 'Long-form writing' }],
  });
  assert.equal(requests.length, 1);
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'Product design');
  assert.equal(indexTree(queue.snapshot).get('writing')!.title, 'Long-form writing');
  respond(0);
  await edit;
  assert.equal(requests.length, 1);
  assert.equal(queue.canUndo, true);
  await queue.history('undo');
  assert.equal(history.length, 1);
  assert.equal(queue.canUndo, false);
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
  assert.deepEqual(remaps, [[{ from: 'temp-parent', to: 'saved-parent' }], [{ from: 'temp-child', to: 'saved-child' }]]);
});

test('server remaps and a create fallback are combined before remapping later work', async () => {
  const { queue, requests, current } = harness();
  const createOperation: Operation = { type: 'create', parent_id: 'design', after_id: null, title: 'Created' };
  const created = queue.enqueue(createOperation, 'temp-created');
  const later = queue.enqueue({
    type: 'rename_many',
    renames: [
      { id: 'design', title: 'Design revised' },
      { id: 'writing', title: 'Writing revised' },
      { id: 'temp-created', title: 'Created revised' },
    ],
  });
  const serverRemaps = [{ from: 'design', to: 'design-stable' }, { from: 'writing', to: 'writing-stable' }];
  const canonical = remapSnapshot(applyOperation(current, createOperation, 'saved-created'), serverRemaps, 'r1', 'c1');
  const changes: unknown[] = [];
  queue.subscribe((change) => { if (change.remapped) changes.push(change.remapped); });
  requests[0].resolve({
    snapshot: canonical, commit: 'c1', operation_id: 'create', repaired_references: [], undoable: true,
    selected_id: 'saved-created', id_remaps: serverRemaps,
  });
  await created;
  assert.deepEqual(changes, [[...serverRemaps, { from: 'temp-created', to: 'saved-created' }]]);
  assert.deepEqual(requests[1].operation, {
    type: 'rename_many',
    renames: [
      { id: 'design-stable', title: 'Design revised' },
      { id: 'writing-stable', title: 'Writing revised' },
      { id: 'saved-created', title: 'Created revised' },
    ],
  });
  const final = { ...applyOperation(canonical, requests[1].operation), revision: 'r2', git_head: 'c2' };
  requests[1].resolve({
    snapshot: final, commit: 'c2', operation_id: 'batch', repaired_references: [], undoable: true,
    selected_id: 'design-stable',
  });
  await later;
});

test('a revision conflict rolls back an optimistic rename_many without replaying it', async () => {
  const { queue, requests } = harness();
  const first = queue.enqueue({
    type: 'rename_many',
    renames: [{ id: 'design', title: 'Unsaved design' }, { id: 'writing', title: 'Unsaved writing' }],
  });
  const second = queue.enqueue({ type: 'edit_body', id: 'design', body: 'Unsaved note' });
  const settled = Promise.allSettled([first, second]);
  const external = { ...applyOperation(fixture(), { type: 'rename', id: 'design', title: 'External title' }), revision: 'external' };
  requests[0].reject(new ApiError('Conflict', 409, external));
  const results = await settled;
  assert(results.every((result) => result.status === 'rejected'));
  assert.equal(requests.length, 1);
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'External title');
  assert.equal(indexTree(queue.snapshot).get('writing')!.title, 'Writing 写作');
  assert.equal(indexTree(queue.snapshot).get('design')!.body, '');
  assert.equal(queue.pendingCount, 0);
  assert.equal(queue.canUndo, false);
});

test('undo and redo use the same action identity before and after a checkpoint', async () => {
  const { queue, respond, history } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Edited' });
  assert.equal(queue.canUndo, false);
  respond(0); await edit;
  await queue.history('undo');
  assert.deepEqual(history[0], { kind: 'undo', revision: 'r1', operationId: 'o0' });
  assert.equal(queue.canRedo, true);
  await queue.flush();
  await queue.history('redo');
  assert.deepEqual(history[1], { kind: 'redo', revision: 'history-1', operationId: 'o0' });
  await queue.flush();
  await queue.history('undo');
  assert.equal(history[2].operationId, 'o0');
});

test('undo and redo emit ordered plural identity remaps in both directions', async () => {
  const initial = fixture();
  const operation: Operation = {
    type: 'rename_many',
    renames: [{ id: 'writing', title: 'Writing renamed' }, { id: 'design', title: 'Design renamed' }],
  };
  const forward = [{ from: 'writing', to: 'writing-stable' }, { from: 'design', to: 'design-stable' }];
  const inverse = forward.map(({ from, to }) => ({ from: to, to: from }));
  const renamed = { ...remapSnapshot(applyOperation(initial, operation), forward, 'r1', 'c1'), pending: true, history: { undo: ['batch'], redo: [] } };
  const undone = { ...structuredClone(initial), revision: 'u1', history: { undo: [], redo: ['batch'] } };
  const redone = { ...structuredClone(renamed), revision: 'r2', history: { undo: ['batch'], redo: [] } };
  const history: Array<{ kind: string; revision: string; operationId: string }> = [];
  let mutations = 0;
  const transport: Transport = {
    load: async () => initial,
    mutate: async () => {
      mutations += 1;
      return {
        snapshot: renamed, commit: 'c1', operation_id: 'batch', repaired_references: [], undoable: true,
        selected_id: 'writing-stable', id_remaps: forward,
      };
    },
    history: async (kind, revision, operationId) => {
      history.push({ kind, revision, operationId });
      return kind === 'undo'
        ? {
            snapshot: undone, commit: null, operation_id: 'batch', repaired_references: [], undoable: true,
            selected_id: null, id_remaps: inverse,
          }
        : {
            snapshot: redone, commit: null, operation_id: 'batch', repaired_references: [], undoable: true,
            selected_id: null, id_remaps: forward,
          };
    },
    flush: async () => { throw new Error('This remap scenario does not checkpoint.'); },
    activity: async () => {},
  };
  const queue = new MutationQueue(initial, transport);
  const changes: Array<Array<{ from: string; to: string }>> = [];
  queue.subscribe((change) => { if (change.remapped) changes.push(change.remapped); });

  await queue.enqueue(operation);
  assert.equal(mutations, 1);
  assert.deepEqual(changes, [forward]);
  assert.equal(queue.canUndo, true);

  await queue.history('undo');
  assert.deepEqual(changes, [forward, inverse]);
  assert.equal(queue.canUndo, false);
  assert.equal(queue.canRedo, true);

  await queue.history('redo');
  assert.deepEqual(changes, [forward, inverse, forward]);
  assert.equal(queue.canUndo, true);
  assert.equal(queue.canRedo, false);
  assert.deepEqual(history, [
    { kind: 'undo', revision: 'r1', operationId: 'batch' },
    { kind: 'redo', revision: 'u1', operationId: 'batch' },
  ]);
});

test('a no-op response creates no undo entry', async () => {
  const { queue, requests, current } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Design 设计' });
  requests[0].resolve({ snapshot: current, commit: null, operation_id: 'noop', repaired_references: [], undoable: false, selected_id: 'design' });
  await edit;
  assert.equal(queue.canUndo, false);
});

test('durable actions are saved before checkpointing and undo remains one action across batches', async () => {
  const { queue, requests, respond, checkpoints, transport } = harness();
  const rename = queue.enqueue({ type: 'rename', id: 'design', title: 'Revised design' });
  const focus = queue.enqueue({ type: 'set_focus', id: 'design', focused: true });
  respond(0); await rename;
  respond(1); await focus;
  assert.equal(queue.pendingCount, 0);
  assert.equal(queue.snapshot.pending, true);
  assert.equal(queue.snapshot.git_head, 'c0');
  assert.equal(checkpoints.length, 0);
  await queue.history('undo');
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'Revised design');
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, false);
  await queue.history('redo');
  await queue.flush();
  assert.equal(checkpoints.length, 1);
  assert.equal(queue.snapshot.pending, false);
  const reloaded = new MutationQueue(await transport.load(), transport);
  assert.equal(reloaded.canUndo, true);
  await reloaded.history('undo');
  assert.equal(indexTree(reloaded.snapshot).get('design')!.title, 'Revised design');
  assert.equal(indexTree(reloaded.snapshot).get('design')!.focused, false);
  await reloaded.flush();
  assert.equal(checkpoints.length, 2);
  await reloaded.history('undo');
  assert.equal(indexTree(reloaded.snapshot).get('design')!.title, 'Design 设计');
  await reloaded.history('redo');
  await reloaded.history('redo');
  assert.equal(indexTree(reloaded.snapshot).get('design')!.focused, true);
  assert.equal(requests.length, 2);
});

test('flush waits for acknowledgement and later optimistic edits wait for its authoritative response', async () => {
  const { queue, respond, requests, transport } = harness();
  const savedFlush = transport.flush;
  let release!: () => void;
  const gate = new Promise<void>((resolve) => { release = resolve; });
  let started = false;
  transport.flush = async (...args) => { started = true; await gate; return savedFlush(...args); };
  const rename = queue.enqueue({ type: 'rename', id: 'design', title: 'First' });
  const flush = queue.flush();
  assert.equal(queue.flush(), flush);
  assert.equal(started, false);
  respond(0); await rename;
  await Promise.resolve();
  assert.equal(started, true);
  assert.equal(queue.pendingCount, 0); // The checkpoint itself cannot lose an acknowledged edit.
  assert.equal(queue.flushing, true);
  const second = queue.enqueue({ type: 'set_focus', id: 'design', focused: true });
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, true);
  assert.equal(requests.length, 1);
  release(); await flush;
  assert.equal(requests.length, 2);
  assert.equal(requests[1].revision, 'r1');
  respond(1); await second;
  assert.equal(queue.snapshot.pending, true);
});

test('a rejected checkpoint keeps saved recovery state and never sends optimistic edits over an external change', async () => {
  const { queue, respond, requests, transport } = harness();
  const rename = queue.enqueue({ type: 'rename', id: 'design', title: 'Durably saved title' });
  respond(0); await rename;
  const recovery = { ...queue.snapshot, writable: false, diagnostics: ['External state changed. Review the saved recovery record.'] };
  let rejectFlush!: (error: Error) => void;
  transport.flush = () => new Promise((_resolve, reject) => { rejectFlush = reject; });
  const flush = queue.flush();
  const later = queue.enqueue({ type: 'set_focus', id: 'design', focused: true });
  const rejected = Promise.allSettled([flush, later]);
  rejectFlush(new ApiError('Saved edits need review', 409, recovery));
  const results = await rejected;
  assert(results.every((result) => result.status === 'rejected'));
  assert.equal(queue.snapshot.pending, true);
  assert.equal(queue.snapshot.writable, false);
  assert.deepEqual(queue.snapshot.history.undo, ['o0']);
  assert.equal(indexTree(queue.snapshot).get('design')!.title, 'Durably saved title');
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, false);
  assert.equal(queue.pendingCount, 0);
  assert.equal(queue.canUndo, false);
  assert.equal(requests.length, 1);
});

test('a load started before a checkpoint cannot replace its newer authoritative response', async () => {
  const { queue, respond, transport } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Saved' });
  respond(0); await edit;
  const old = queue.snapshot;
  let release!: (snapshot: Snapshot) => void;
  transport.load = () => new Promise((resolve) => { release = resolve; });
  const read = queue.reload();
  await queue.flush();
  release(old); await read;
  assert.equal(queue.snapshot.pending, false);
  assert.equal(queue.snapshot.git_head, 'checkpoint-1');
});

test('leaving closes the edit boundary while acknowledging and checkpointing already queued operations', async () => {
  const { queue, respond, requests, checkpoints } = harness();
  const edit = queue.enqueue({ type: 'rename', id: 'design', title: 'Finish before leaving' });
  queue.setActive(false);
  const flush = queue.flush();
  await assert.rejects(queue.enqueue({ type: 'set_focus', id: 'design', focused: true }), /Reopen/);
  respond(0); await edit; await flush;
  assert.equal(checkpoints.length, 1);
  assert.equal(requests.length, 1);
  assert.equal(queue.canUndo, false);
  assert.equal(await queue.history('undo'), null);
  queue.setActive(true);
  assert.equal(queue.canUndo, true);
});

test('flush rechecks acknowledgement work added by the completed operation callback', async () => {
  const { queue, respond, requests, checkpoints } = harness();
  const first = queue.enqueue({ type: 'rename', id: 'design', title: 'First' });
  const next = first.then(() => queue.enqueue({ type: 'set_focus', id: 'design', focused: true }));
  const flush = queue.flush();
  respond(0); await first;
  await Promise.resolve();
  assert.equal(requests.length, 2);
  assert.equal(checkpoints.length, 0);
  respond(1); await next; await flush;
  assert.equal(checkpoints.length, 1);
  assert.equal(queue.snapshot.pending, false);
  assert.equal(indexTree(queue.snapshot).get('design')!.focused, true);
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

test('plural server remaps update every nested target in later queued work', async () => {
  const { queue, requests, current } = harness();
  const firstOperation: Operation = {
    type: 'rename_many',
    renames: [{ id: 'design', title: 'Design system' }, { id: 'writing', title: 'Writing system' }],
  };
  const first = queue.enqueue(firstOperation);
  const second = queue.enqueue({
    type: 'rename_many',
    renames: [{ id: 'design', title: 'Design system revised' }, { id: 'writing', title: 'Writing system revised' }],
  });
  const idRemaps = [{ from: 'design', to: 'design-stable' }, { from: 'writing', to: 'writing-stable' }];
  const canonical = remapSnapshot(applyOperation(current, firstOperation), idRemaps, 'r1', 'c1');
  const changes: unknown[] = [];
  queue.subscribe((change) => { if (change.remapped) changes.push(change.remapped); });
  requests[0].resolve({
    snapshot: canonical, commit: 'c1', operation_id: 'batch-1', repaired_references: [], undoable: true,
    selected_id: 'design-stable', id_remaps: idRemaps,
  });
  await first;
  assert.deepEqual(changes, [idRemaps]);
  assert.deepEqual(requests[1].operation, {
    type: 'rename_many',
    renames: [{ id: 'design-stable', title: 'Design system revised' }, { id: 'writing-stable', title: 'Writing system revised' }],
  });
  const final = { ...applyOperation(canonical, requests[1].operation), revision: 'r2', git_head: 'c2' };
  requests[1].resolve({
    snapshot: final, commit: 'c2', operation_id: 'batch-2', repaired_references: [], undoable: true,
    selected_id: 'design-stable',
  });
  await second;
  assert.equal(indexTree(queue.snapshot).get('design-stable')!.title, 'Design system revised');
  assert.equal(indexTree(queue.snapshot).get('writing-stable')!.title, 'Writing system revised');
});

import { mountMap } from '../src/pursuit-map.ts';
import { ApiError, type Transport } from '../src/queue.ts';
import { applyOperation, type MutationResult, type Snapshot } from '../src/tree.ts';
import { fixture, forestFixture } from './fixtures.ts';
import { runBrowserChecks } from './browser-checks.ts';

// Disposable browser fixture. It never contacts a Memory root or persists semantic data.
const parameters = new URL(location.href).searchParams;
const requested = Number(parameters.get('nodes') ?? 22);
const layout = parameters.get('layout') ?? 'forest';
let current = (layout === 'single' ? fixture : forestFixture)(Math.min(1000, Math.max(22, requested)));
if (layout === 'empty') current = { ...current, root_key: 'browser-empty-fixture', items: [], root_ids: [], focus_ids: [] };
let serial = 0;
let failNext = false;
let slow = false;
const actions = new Map<string, { before: Snapshot; after: Snapshot }>();
const clone = <T>(value: T): T => structuredClone(value);
const latency = () => new Promise((resolve) => setTimeout(resolve, slow ? 1800 : 100));
const result = (before: Snapshot, next: Snapshot, selected_id: string | null): MutationResult => {
  serial++;
  const operation_id = `operation-${serial}`;
  current = {
    ...next, revision: `r${serial}`, pending: true,
    history: { undo: [...before.history.undo, operation_id], redo: [] },
  };
  actions.set(operation_id, { before: clone(before), after: clone(current) });
  return { snapshot: clone(current), commit: null, operation_id, repaired_references: [], undoable: true, selected_id, id_remaps: [] };
};
const transport: Transport = {
  load: async () => clone(current),
  mutate: async (revision, operation) => {
    await latency();
    if (failNext) {
      failNext = false;
      current = { ...current, revision: `external-${++serial}` };
      throw new ApiError('The map changed in another window.', 409, clone(current));
    }
    if (revision !== current.revision) throw new ApiError('The map changed in another window.', 409, clone(current));
    const id = operation.type === 'create' ? `created-${serial + 1}`
      : operation.type === 'rename_many' ? operation.renames[0]?.id ?? null : operation.id;
    return result(current, applyOperation(current, operation, id), operation.type === 'delete' ? null : id);
  },
  history: async (kind, revision, operation_id) => {
    await latency();
    const entry = actions.get(operation_id);
    if (!entry || revision !== current.revision || current.history[kind].at(-1) !== operation_id) {
      throw new ApiError('History changed elsewhere.', 409, clone(current));
    }
    const history = clone(current.history);
    history[kind].pop();
    history[kind === 'undo' ? 'redo' : 'undo'].push(operation_id);
    current = {
      ...clone(kind === 'undo' ? entry.before : entry.after),
      revision: `r${++serial}`, git_head: current.git_head, pending: true, history,
    };
    return { snapshot: clone(current), commit: null, operation_id, repaired_references: [], undoable: true, selected_id: null, id_remaps: [] };
  },
  flush: async (revision) => {
    if (revision !== current.revision) throw new ApiError('The map changed in another window.', 409, clone(current));
    const commit = current.pending ? `c${++serial}` : null;
    if (commit) current = { ...current, revision: `r${serial}`, git_head: commit, pending: false };
    return { snapshot: clone(current), commit, operation_id: '', repaired_references: [], undoable: false, selected_id: null, id_remaps: [] };
  },
  activity: async () => {},
};
const host = document.querySelector<HTMLElement>('#fixture')!;
host.style.height = 'calc(100vh - 40px)';
let controller = await mountMap(host, transport, {
  context: async (itemId, revision) => {
    await latency();
    if (revision !== current.revision) throw new ApiError('The fixture changed. Review it and copy again.', 409, clone(current));
    const item = current.items.find((entry) => entry.id === itemId);
    if (!item) throw new ApiError('The fixture direction no longer exists.', 404);
    return `${item.title}\n\n${item.body}`;
  },
});
const tools = document.createElement('div');
tools.style.cssText = 'height:40px;padding:6px 12px;box-sizing:border-box;display:flex;gap:14px;align-items:center;background:#edf0e9;font:12px system-ui;';
tools.innerHTML = '<strong>Disposable fixture</strong><label><input type="checkbox"> Slow saves</label><button type="button">Conflict next save</button><a href="/?layout=forest">Independent maps</a><a href="/?layout=single">Single map</a><a href="/?layout=empty">Empty map</a><a href="/?nodes=500">500 directions</a><span>No real Memory data is used.</span>';
tools.querySelector('input')!.addEventListener('change', (event) => { slow = (event.target as HTMLInputElement).checked; });
tools.querySelector('button')!.addEventListener('click', () => { failNext = true; });
document.body.append(tools);
const run = document.createElement('button');
run.type = 'button'; run.textContent = 'Run interaction checks';
tools.append(run);
const results = document.createElement('pre');
results.setAttribute('aria-label', 'Interaction check results');
results.style.cssText = 'white-space:pre-wrap;padding:12px;font:12px/1.6 system-ui;';
results.hidden = true;
document.body.append(results);
run.addEventListener('click', async () => {
  run.disabled = true;
  tools.querySelectorAll<HTMLInputElement | HTMLButtonElement>('input,button').forEach((control) => { control.disabled = true; });
  results.hidden = false; results.textContent = 'Running disposable browser checks…\n';
  await controller.destroy();
  const checked = await runBrowserChecks(host, (line) => { results.textContent += line + '\n'; });
  if (checked) controller = checked;
  run.disabled = false;
});

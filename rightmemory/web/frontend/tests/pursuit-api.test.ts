import assert from 'node:assert/strict';
import test from 'node:test';
import { apiContext, apiTransport, type FetchJson } from '../src/pursuit-api.ts';
import { ApiError } from '../src/queue.ts';
import { fixture } from './fixtures.ts';

test('context uses a read request with the selected item and displayed revision', async () => {
  const calls: Array<{ path: string; options?: RequestInit }> = [];
  const token = crypto.randomUUID();
  const fetchJson: FetchJson = async (path, options) => {
    calls.push({ path, options });
    return { data: { text: token } };
  };
  assert.equal(await apiContext(fetchJson)('plain:with / punctuation', 'revision + token'), token);
  assert.equal(calls.length, 1);
  const url = new URL(calls[0].path, 'http://localhost');
  assert.equal(url.pathname, '/api/pursuit-map/context');
  assert.equal(url.searchParams.get('item_id'), 'plain:with / punctuation');
  assert.equal(url.searchParams.get('expected_revision'), 'revision + token');
  assert.equal(calls[0].options, undefined);
});

test('context surfaces revision conflicts and the returned authoritative snapshot', async () => {
  const snapshot = fixture();
  const fetchJson: FetchJson = async () => {
    throw Object.assign(new Error('Changed elsewhere'), { status: 409, detail: { snapshot } });
  };
  await assert.rejects(apiContext(fetchJson)('root', 'stale'), (error: unknown) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.status, 409);
    assert.equal(error.snapshot, snapshot);
    return true;
  });
});

test('missing context data is rejected before clipboard handling', async () => {
  for (const data of [null, {}, { text: 42 }, { text: '' }]) {
    await assert.rejects(apiContext(async () => ({ data }))('root', 'r0'));
  }
});

test('map reads and mutations retain their transport boundaries', async () => {
  const calls: Array<{ path: string; options?: RequestInit }> = [];
  const snapshot = fixture();
  const api = apiTransport(async (path, options) => {
    calls.push({ path, options });
    return { data: snapshot };
  });
  assert.equal(await api.load(), snapshot);
  assert.equal(calls[0].path, '/api/pursuit-map');
  assert.equal(calls[0].options, undefined);
  const operation = { type: 'set_focus' as const, id: 'root', focused: true };
  await api.mutate('r0', operation);
  assert.equal(calls[1].path, '/api/pursuit-map/operations');
  assert.equal(calls[1].options?.method, 'POST');
  assert.deepEqual(JSON.parse(String(calls[1].options?.body)), { expected_revision: 'r0', operation });
  await api.history('undo', 'r1', 'action-1');
  assert.equal(calls[2].path, '/api/pursuit-map/undo');
  assert.deepEqual(JSON.parse(String(calls[2].options?.body)), { expected_revision: 'r1', operation_id: 'action-1' });
  await api.flush('r2', true);
  assert.equal(calls[3].path, '/api/pursuit-map/flush');
  assert.deepEqual(JSON.parse(String(calls[3].options?.body)), { expected_revision: 'r2' });
  assert.equal(calls[3].options?.keepalive, true);
  await api.activity();
  assert.equal(calls[4].path, '/api/pursuit-map/activity');
  assert.deepEqual(JSON.parse(String(calls[4].options?.body)), {});
});

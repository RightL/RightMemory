import assert from 'node:assert/strict';
import test from 'node:test';
import {
  ManagerTerminalRefreshGate,
  managerAcceptedPendingSend,
  managerProviderEventAcceptsPendingSend,
  managerReconcileRefreshStatus,
  managerTerminalSignalFromEvent,
  managerTerminalStatus,
  managerTerminalTransitions,
} from '../src/conversation-workspace.ts';
import { normalizeEvent, normalizeWorkspace } from '../src/conversation-state.ts';

test('Manager terminal refresh signals coalesce across fallback state, reconciliation, and the late turn event', () => {
  const gate = new ManagerTerminalRefreshGate();

  assert.equal(gate.acceptTerminal('manager-1', 'completed', null), true);
  assert.equal(gate.acceptTerminal('manager-1', 'completed', null), false);
  assert.equal(gate.acceptTerminal('manager-1', 'completed', 'turn-1'), false);
  assert.equal(gate.acceptTerminal('manager-1', 'failed', 'turn-1'), false);
});

test('Manager refresh coalescing resets for active work and distinguishes a new terminal turn id', () => {
  const gate = new ManagerTerminalRefreshGate();

  assert.equal(gate.acceptTerminal('manager-1', 'failed', 'turn-1'), true);
  gate.observeStatus('manager-1', 'running');
  assert.equal(gate.acceptTerminal('manager-1', 'interrupted', 'turn-2'), true);
  assert.equal(gate.acceptTerminal('manager-1', 'interrupted', 'turn-2'), false);
  assert.equal(gate.acceptTerminal('manager-1', 'completed', 'turn-3'), true);

  gate.beginTurn('manager-1');
  assert.equal(gate.acceptTerminal('manager-1', 'completed', null), true);
});

test('only completion, failure, and interruption are Manager mutation terminal states', () => {
  assert.equal(managerTerminalStatus('completed'), 'completed');
  assert.equal(managerTerminalStatus('failed'), 'failed');
  assert.equal(managerTerminalStatus('interrupted'), 'interrupted');
  assert.equal(managerTerminalStatus('cancelled'), 'interrupted');
  assert.equal(managerTerminalStatus('idle'), null);
  assert.equal(managerTerminalStatus('unknown'), null);
});

test('a fallback conversation state recovers a missed Manager terminal notification without retriggering an established terminal state', () => {
  const fallback = normalizeEvent({
    event_id: 8,
    conversation_id: 'manager-1',
    kind: 'conversation.state',
    payload: { conversation: { status: 'completed' } },
  });
  const explicit = normalizeEvent({
    event_id: 9,
    conversation_id: 'manager-1',
    turn_id: 'turn-1',
    kind: 'turn.completed',
    payload: { turn: { status: 'completed' } },
  });
  assert(fallback && explicit);
  assert.deepEqual(managerTerminalSignalFromEvent(fallback, 'unknown', 'completed'), {
    status: 'completed', turnId: null,
  });
  assert.equal(managerTerminalSignalFromEvent(fallback, 'completed', 'completed'), null);
  assert.deepEqual(managerTerminalSignalFromEvent(explicit, 'completed', 'completed'), {
    status: 'completed', turnId: 'turn-1',
  });
});

test('an authoritative reconnect snapshot detects Manager completion before replayed terminal events', () => {
  const workspace = (status: string, cursor: number) => normalizeWorkspace({
    root_key: 'fixture-root',
    conversations: [{
      conversation_id: 'manager-1', pursuit_id: null, kind: 'manager', host_id: 'local', project_id: 'local-root',
      status, updated_at: `2026-01-01T00:00:0${cursor}Z`,
    }],
    cursor,
  });
  const previous = workspace('unknown', 1);
  const terminal = workspace('completed', 2);
  const transitions = managerTerminalTransitions(previous.conversations, terminal.conversations);
  assert.deepEqual(transitions, [{ conversationId: 'manager-1', status: 'completed' }]);

  const gate = new ManagerTerminalRefreshGate();
  assert.equal(gate.acceptTerminal('manager-1', transitions[0].status, null), true);
  const replay = normalizeEvent({
    event_id: 2,
    conversation_id: 'manager-1',
    turn_id: 'turn-1',
    kind: 'conversation.state',
    payload: { conversation: { status: 'completed' } },
  });
  const explicit = normalizeEvent({
    event_id: 3,
    conversation_id: 'manager-1',
    turn_id: 'turn-1',
    kind: 'turn.completed',
    payload: { turn: { status: 'completed' } },
  });
  assert(replay && explicit);
  assert.equal(managerTerminalSignalFromEvent(replay, 'completed', 'completed'), null);
  const explicitSignal = managerTerminalSignalFromEvent(explicit, 'completed', 'completed');
  assert(explicitSignal);
  assert.equal(gate.acceptTerminal('manager-1', explicitSignal.status, explicitSignal.turnId), false);
});

test('resolved Manager reconciliation treats only unknown to provider-confirmed idle as a refresh signal', () => {
  assert.equal(managerReconcileRefreshStatus('unknown', 'idle', true), 'completed');
  assert.equal(managerReconcileRefreshStatus('unknown', 'idle', false), null);
  assert.equal(managerReconcileRefreshStatus('completed', 'idle', true), null);
  assert.equal(managerReconcileRefreshStatus('unknown', 'failed', true), 'failed');
});

test('first- and later-turn Manager sends resolve only from attempt-specific acceptance evidence', () => {
  assert.equal(managerAcceptedPendingSend('10', '11'), true);
  assert.equal(managerAcceptedPendingSend('20', '21'), true);
  assert.equal(managerAcceptedPendingSend('20', null), false);
  assert.equal(managerAcceptedPendingSend('20', '19'), false);
});

test('an accepted first send cannot clear a later unaccepted send before raw context state refreshes', () => {
  assert.equal(managerAcceptedPendingSend('10', '11'), true);
  assert.equal(managerAcceptedPendingSend('20', '11'), false);
  assert.equal(managerAcceptedPendingSend('20', null), false);
});

test('a provider notification after the send baseline resolves the pending Manager send without reconnect', () => {
  const accepted = normalizeEvent({
    event_id: 11,
    conversation_id: 'manager-1',
    turn_id: 'turn-2',
    kind: 'turn.started',
    payload: { turn: { id: 'turn-2', status: 'running' }, accepted_user_event_id: 11 },
  });
  const oldHistory = normalizeEvent({
    event_id: 9,
    conversation_id: 'manager-1',
    turn_id: 'old-turn-outside-the-visible-window',
    kind: 'turn.completed',
    payload: { turn: { status: 'completed' } },
  });
  assert(accepted && oldHistory);
  assert.equal(managerProviderEventAcceptsPendingSend('10', accepted), true);
  assert.equal(managerProviderEventAcceptsPendingSend('10', oldHistory), false);
});

import assert from 'node:assert/strict';
import test from 'node:test';
import {
  aggregateConversationIndicators,
  conversationIndicatorLabel,
  type OperationalConversationIndicatorInput,
} from '../src/conversation-indicators.ts';

function indicators(...conversations: OperationalConversationIndicatorInput[]) {
  return aggregateConversationIndicators(conversations);
}

test('conversation indicators keep stable pursuit ids and exclude side chats and archives', () => {
  const result = indicators(
    { pursuitId: 'design/中文', status: 'idle' },
    { pursuitId: 'design/中文', status: 'running', sideChat: true },
    { pursuitId: 'design/中文', status: 'completed', archived: true },
    { pursuitId: null, status: 'running' },
    { pursuitId: '', status: 'running' },
  );
  assert.deepEqual([...result.keys()], ['design/中文']);
  assert.deepEqual(result.get('design/中文'), {
    pursuitId: 'design/中文', state: 'idle', conversationCount: 1, stateCount: 1,
  });
});

test('waiting, attention, working, unread final, completed, and idle use operational precedence', () => {
  const result = indicators(
    { pursuitId: 'waiting', status: 'completed', unreadFinal: true },
    { pursuitId: 'waiting', status: 'running' },
    { pursuitId: 'waiting', status: 'waiting_input' },
    { pursuitId: 'working', status: 'completed', unreadFinal: true },
    { pursuitId: 'working', status: 'starting' },
    { pursuitId: 'unread', status: 'idle' },
    { pursuitId: 'unread', status: 'completed', unreadFinal: true },
    { pursuitId: 'completed', status: 'completed' },
    { pursuitId: 'attention', status: 'failed' },
    { pursuitId: 'working-hyphenated', status: 'in_progress' },
    { pursuitId: 'idle', status: 'interrupted' },
  );
  assert.equal(result.get('waiting')?.state, 'waiting-for-user');
  assert.equal(result.get('working')?.state, 'working');
  assert.equal(result.get('unread')?.state, 'unread-final');
  assert.equal(result.get('completed')?.state, 'completed');
  assert.equal(result.get('attention')?.state, 'needs-attention');
  assert.equal(result.get('working-hyphenated')?.state, 'working');
  assert.equal(result.get('idle')?.state, 'idle');
  assert.deepEqual(result.get('waiting'), {
    pursuitId: 'waiting', state: 'waiting-for-user', conversationCount: 3, stateCount: 1,
  });
});

test('indicator labels describe conversations without implying Pursuit completion', () => {
  const indicator = indicators(
    { pursuitId: 'p1', status: 'completed' },
    { pursuitId: 'p1', status: 'idle' },
  ).get('p1')!;
  assert.equal(conversationIndicatorLabel(indicator), 'Conversation completed; 2 conversations in this direction');
  assert(!conversationIndicatorLabel(indicator).includes('Pursuit'));
});

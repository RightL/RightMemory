export const conversationIndicatorStates = [
  'waiting-for-user',
  'needs-attention',
  'working',
  'unread-final',
  'completed',
  'idle',
] as const;

export type ConversationIndicatorState = typeof conversationIndicatorStates[number];

/** The small operational subset needed by the map; side chats never enter it. */
export interface OperationalConversationIndicatorInput {
  readonly pursuitId: string | null;
  readonly status: string;
  readonly unreadFinal?: boolean;
  readonly sideChat?: boolean;
  readonly archived?: boolean;
}

export interface PursuitConversationIndicator {
  readonly pursuitId: string;
  readonly state: ConversationIndicatorState;
  readonly conversationCount: number;
  readonly stateCount: number;
}

const precedence: Readonly<Record<ConversationIndicatorState, number>> = {
  'waiting-for-user': 6,
  'needs-attention': 5,
  working: 4,
  'unread-final': 3,
  completed: 2,
  idle: 1,
};

function stateOf(conversation: OperationalConversationIndicatorInput): ConversationIndicatorState {
  const status = conversation.status.trim().toLowerCase().replace(/[\s_]+/g, '-');
  if (status === 'waiting-input' || status === 'waiting-approval' || status === 'waiting-for-user') {
    return 'waiting-for-user';
  }
  if (status === 'failed' || status === 'unknown' || status === 'system-error') return 'needs-attention';
  if (status === 'starting' || status === 'running' || status === 'working' || status === 'in-progress') return 'working';
  if (conversation.unreadFinal) return 'unread-final';
  if (status === 'completed') return 'completed';
  return 'idle';
}

/** Aggregate operational conversation activity without changing the Pursuit snapshot. */
export function aggregateConversationIndicators(
  conversations: readonly OperationalConversationIndicatorInput[],
): ReadonlyMap<string, PursuitConversationIndicator> {
  const aggregates = new Map<string, { counts: Record<ConversationIndicatorState, number>; total: number }>();
  for (const conversation of conversations) {
    const pursuitId = conversation.pursuitId;
    if (!pursuitId || conversation.sideChat || conversation.archived) continue;
    let aggregate = aggregates.get(pursuitId);
    if (!aggregate) {
      aggregate = {
        counts: { 'waiting-for-user': 0, 'needs-attention': 0, working: 0, 'unread-final': 0, completed: 0, idle: 0 },
        total: 0,
      };
      aggregates.set(pursuitId, aggregate);
    }
    aggregate.counts[stateOf(conversation)]++;
    aggregate.total++;
  }

  const indicators = new Map<string, PursuitConversationIndicator>();
  for (const [pursuitId, aggregate] of aggregates) {
    const state = conversationIndicatorStates.reduce((current, candidate) =>
      aggregate.counts[candidate] && precedence[candidate] > precedence[current] ? candidate : current,
    'idle' as ConversationIndicatorState);
    indicators.set(pursuitId, {
      pursuitId,
      state,
      conversationCount: aggregate.total,
      stateCount: aggregate.counts[state],
    });
  }
  return indicators;
}

export function conversationIndicatorLabel(indicator: PursuitConversationIndicator): string {
  const count = indicator.stateCount;
  const total = indicator.conversationCount;
  const subject = count === 1 ? 'Conversation' : `${count} conversations`;
  const suffix = total > count ? `; ${total} conversations in this direction` : '';
  switch (indicator.state) {
    case 'waiting-for-user': return `${subject} waiting for you${suffix}`;
    case 'needs-attention': return `${subject} needing attention${suffix}`;
    case 'working': return `${subject} working${suffix}`;
    case 'unread-final': return `${subject} with an unread final response${suffix}`;
    case 'completed': return `${subject} completed${suffix}`;
    case 'idle': return `${subject} idle${suffix}`;
  }
}

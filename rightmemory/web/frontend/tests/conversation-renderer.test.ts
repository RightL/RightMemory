import assert from 'node:assert/strict';
import test from 'node:test';
import { isPairedProviderUserEcho } from '../src/conversation-renderer.ts';

test('a projected prefix with the explicit truncation suffix pairs with its structured local Manager event', () => {
  const openingContext = 'opaque-context-segment-'.repeat(3_500);
  const projectedPrefix = openingContext.slice(0, 65_536 - 14);

  assert.equal(
    isPairedProviderUserEcho(`${projectedPrefix}...[truncated]`, 'opaque user input', openingContext),
    true,
  );
});

test('a projected echo truncated after its full opening context still pairs when the original user input is oversized', () => {
  const openingContext = 'opaque opening context';
  const providerCopy = `${openingContext}\n\n${'opaque-source-segment-'.repeat(3_500)}`;
  const projectedPrefix = providerCopy.slice(0, 65_536 - 14);

  assert.equal(
    isPairedProviderUserEcho(`${projectedPrefix}...[truncated]`, 'opaque-source-segment-'.repeat(3_500), openingContext),
    true,
  );
  assert.equal(
    isPairedProviderUserEcho(projectedPrefix, 'opaque-source-segment-'.repeat(3_500), openingContext),
    false,
  );
});

test('a projection cutoff after the first context separator newline still pairs with the local Manager event', () => {
  const openingContext = 'opaque-context-segment-'.repeat(3_000);

  assert.equal(
    isPairedProviderUserEcho(`${openingContext}\n...[truncated]`, 'opaque user input', openingContext),
    true,
  );
});

test('an unrelated truncated provider message is not paired with a structured local Manager event', () => {
  const openingContext = 'opaque-context-segment-'.repeat(3_500);

  assert.equal(
    isPairedProviderUserEcho(`${'different-prefix-'.repeat(4_100)}...[truncated]`, 'opaque user input', openingContext),
    false,
  );
});

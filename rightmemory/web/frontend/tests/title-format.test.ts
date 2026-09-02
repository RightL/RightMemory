import assert from 'node:assert/strict';
import test from 'node:test';
import { parseWholeTitleFormat, serializeWholeTitleFormat, setWholeTitleMark, titleMarkup, titleText, toggleWholeTitleMark, type TopicMark } from '../src/title-format.ts';

test('every whole-title mark combination has deterministic order and toggles independently', () => {
  const marks: TopicMark[] = ['bold', 'underline', 'strike'];
  const expected = ['Direction', '**Direction**', '<u>Direction</u>', '**<u>Direction</u>**',
    '~~Direction~~', '**~~Direction~~**', '<u>~~Direction~~</u>', '**<u>~~Direction~~</u>**'];
  const rendered = ['Direction', '<strong>Direction</strong>', '<u>Direction</u>', '<strong><u>Direction</u></strong>',
    '<s>Direction</s>', '<strong><s>Direction</s></strong>', '<u><s>Direction</s></u>', '<strong><u><s>Direction</s></u></strong>'];
  for (let mask = 0; mask < 8; mask++) {
    const state = { inner: 'Direction', marks: new Set(marks.filter((_, bit) => mask & (1 << bit)).reverse()) };
    const raw = serializeWholeTitleFormat(state);
    assert.equal(raw, expected[mask]);
    assert.deepEqual(parseWholeTitleFormat(raw), state);
    assert.equal(titleMarkup(raw), rendered[mask]);
    assert.equal(titleText(raw), 'Direction');
    marks.forEach((mark, bit) => assert.equal(toggleWholeTitleMark(raw, mark), expected[mask ^ (1 << bit)]));
  }
  assert.equal(serializeWholeTitleFormat(parseWholeTitleFormat('~~<u>**Direction**</u>~~')), expected[7]);
});

test('partial marks survive whole-title wrapping, including the same mark', () => {
  for (const raw of ['A ~~deprecated~~ branch', '~~one~~ and ~~two~~', 'A **part** only']) {
    assert.equal(parseWholeTitleFormat(raw).marks.size, 0);
    for (const mark of ['bold', 'underline', 'strike'] as const) {
      const wrapped = toggleWholeTitleMark(raw, mark);
      assert.equal(titleText(wrapped), titleText(raw));
      assert.equal(toggleWholeTitleMark(wrapped, mark), raw);
    }
  }
  assert.equal(titleMarkup('A **bold <u>under ~~old~~</u>** branch'), 'A <strong>bold <u>under <s>old</s></u></strong> branch');
});

test('setWholeTitleMark makes mixed title states uniformly enabled or disabled', () => {
  const mixed = ['Direction', '**Already bold**', 'A **partly bold** direction'];
  const enabled = mixed.map((raw) => setWholeTitleMark(raw, 'bold', true));
  assert.deepEqual(enabled, ['**Direction**', '**Already bold**', '**A **partly bold** direction**']);
  assert(enabled.every((raw) => parseWholeTitleFormat(raw).marks.has('bold')));
  assert.deepEqual(enabled.map((raw) => setWholeTitleMark(raw, 'bold', true)), enabled);

  const disabled = enabled.map((raw) => setWholeTitleMark(raw, 'bold', false));
  assert.deepEqual(disabled, ['Direction', 'Already bold', 'A **partly bold** direction']);
  assert(disabled.every((raw) => !parseWholeTitleFormat(raw).marks.has('bold')));
  assert.deepEqual(disabled.map((raw) => setWholeTitleMark(raw, 'bold', false)), disabled);

  for (const mark of ['underline', 'strike'] as const) {
    const marked = setWholeTitleMark('Already marked', mark, true);
    const uniformlyEnabled = ['Direction', marked].map((raw) => setWholeTitleMark(raw, mark, true));
    assert(uniformlyEnabled.every((raw) => parseWholeTitleFormat(raw).marks.has(mark)));
    assert.deepEqual(uniformlyEnabled.map((raw) => setWholeTitleMark(raw, mark, true)), uniformlyEnabled);
    const uniformlyDisabled = uniformlyEnabled.map((raw) => setWholeTitleMark(raw, mark, false));
    assert(uniformlyDisabled.every((raw) => !parseWholeTitleFormat(raw).marks.has(mark)));
    assert.deepEqual(uniformlyDisabled.map((raw) => setWholeTitleMark(raw, mark, false)), uniformlyDisabled);
  }
});

test('unmatched and crossed delimiters stay literal; only exact allowed tags render', () => {
  for (const raw of ['A **unfinished', '~~unfinished', '**A ~~B** C~~', '<u>unfinished', '</u>', '<U>text</U>',
    '<u onclick=alert(1)>text</u>', '<img src=x onerror=alert(1)>', '<script>alert(1)</script>']) {
    assert.equal(titleText(raw), raw);
    assert.equal(titleMarkup(raw), raw.replaceAll('<', '&lt;').replaceAll('>', '&gt;'));
  }
  assert.equal(titleMarkup('**<svg onload=alert(1)>**'), '<strong>&lt;svg onload=alert(1)&gt;</strong>');
  assert.equal(titleMarkup('<u>A & "B"</u>'), '<u>A &amp; &quot;B&quot;</u>');
  assert.equal(titleMarkup('**unfinished <u>valid</u>'), '**unfinished <u>valid</u>');
});

test('visible-empty markup is distinguishable from literal punctuation', () => {
  for (const raw of ['', ' ', '****', '~~~~', '<u></u>', '**<u>~~ ~~</u>**']) assert.equal(titleText(raw).trim(), '');
  for (const raw of ['**', '~~', '<u>', '<u class=x></u>']) assert.notEqual(titleText(raw).trim(), '');
  const deep = '<u>'.repeat(3000) + 'Text' + '</u>'.repeat(3000);
  assert.equal(titleText(deep), 'Text');
  assert.equal(parseWholeTitleFormat(deep).inner, 'Text');
});

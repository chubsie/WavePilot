import test from 'node:test';
import assert from 'node:assert/strict';
import {
  applyContinuedSelection, createDecisionCard, createDecisionLog, DECISION_TTL_MS,
  formatCheckAge, formatCheckSummary, formatSwitchLine, isContinuedSelection,
} from '../tools/mission_demo/llm-decisions.js';

const searchPayload = {
  time_sec: 10, event_kind: 'tick', rule_mode: 'search', params: {}, source: 'd1', confidence: .6,
  probabilities: { search: .6, approach: .3, hold: .1 }, fallback_reason: 'none', options: ['search', 'approach', 'hold'],
  latency_ms: 40, mode_result: null, continued: false,
};

const recheck = (overrides) => ({ ...searchPayload, continued: true, ...overrides });

test('continued re-judgement updates the current card instead of adding one', () => {
  const log = createDecisionLog();
  log.addSelection({ seq: 1, payload: searchPayload }, 0);

  const updated = log.addSelection({ seq: 2, payload: recheck({ time_sec: 11, confidence: .9, probabilities: { search: .9, approach: .05, hold: .05 }, latency_ms: 12.4 }) }, 1000);
  log.addSelection({ seq: 3, payload: recheck({ time_sec: 12, confidence: .95 }) }, 2000);

  assert.ok(updated);
  assert.equal(log.entries.length, 1, 'card count is unchanged');
  const card = log.entries[0];
  assert.equal(card.key, 'selection-1', 'the card keeps its key so the view patches it in place');
  assert.equal(card.checkCount, 3);
  assert.equal(card.revision, 3);
  assert.equal(card.lastSeq, 3);
  assert.equal(card.lastTimeSec, 12);
  assert.equal(card.confidence, '95%');
  assert.equal(card.checkedAtMs, 2000);
  assert.equal(formatCheckSummary(card), '3 checks · last t=12.0 s');
});

test('continued payload carries the latest probabilities and latency', () => {
  const card = createDecisionCard(searchPayload, 1);
  const next = applyContinuedSelection(card, recheck({ probabilities: { search: .2, approach: .7, hold: .1 }, latency_ms: 12.4 }), 2);

  assert.deepEqual(next.bars.map((bar) => bar.mode), ['approach', 'search', 'hold']);
  assert.equal(next.latency, '12 ms');
  assert.equal(card.checkCount, 1, 'the original card object is not mutated');
});

test('a switch (continued:false) adds a new card with a SWITCH line', () => {
  const log = createDecisionLog();
  log.addSelection({ seq: 1, payload: searchPayload }, 0);
  log.addSelection({ seq: 2, payload: recheck({}) }, 1000);
  log.addSelection({ seq: 3, payload: { ...searchPayload, rule_mode: 'approach', continued: false } }, 2000);

  assert.deepEqual(log.entries.map((entry) => entry.key), ['selection-3', 'selection-1']);
  assert.equal(log.entries[0].switchLine, 'SWITCH search → approach');
  assert.equal(log.entries[0].checkCount, 1);
  assert.equal(log.entries[1].switchLine, null, 'the first decision has nothing to switch from');
});

test('continued without a matching running card falls back to a new card', () => {
  const log = createDecisionLog();
  log.addSelection({ seq: 5, payload: recheck({}) }, 0);
  assert.equal(log.entries.length, 1, 'page opened mid-run: first continued payload still shows a card');

  log.addSelection({ seq: 6, payload: recheck({ rule_mode: 'hold' }) }, 1000);
  assert.deepEqual(log.entries.map((entry) => entry.key), ['selection-6', 'selection-5'], 'mode mismatch is treated as a switch');
  assert.equal(log.entries[0].switchLine, 'SWITCH search → hold');
});

test('continued re-judgement refreshes the TTL clock of the card', () => {
  const log = createDecisionLog();
  log.addSelection({ seq: 1, payload: searchPayload }, 0);
  log.addSelection({ seq: 2, payload: recheck({}) }, DECISION_TTL_MS);
  assert.equal(log.entries[0].atMs, DECISION_TTL_MS);

  log.addSelection({ seq: 3, payload: { ...searchPayload, rule_mode: 'approach' } }, DECISION_TTL_MS + 100);
  log.prune(DECISION_TTL_MS * 2 - 1);
  assert.deepEqual(log.entries.map((entry) => entry.key), ['selection-3', 'selection-1'], 'refreshed card has not expired yet');
  log.prune(DECISION_TTL_MS * 2 + 1);
  assert.deepEqual(log.entries.map((entry) => entry.key), ['selection-3']);
});

test('same seq polled again is still ignored for continued payloads', () => {
  const log = createDecisionLog();
  log.addSelection({ seq: 1, payload: searchPayload }, 0);
  log.addSelection({ seq: 2, payload: recheck({}) }, 1000);
  assert.equal(log.addSelection({ seq: 2, payload: recheck({}) }, 1100), null);
  assert.equal(log.entries[0].checkCount, 2);
});

test('helpers: continued detection, switch line, check summary and age', () => {
  const card = createDecisionCard(searchPayload, 1);
  assert.equal(isContinuedSelection(recheck({}), card), true);
  assert.equal(isContinuedSelection(recheck({ rule_mode: 'SEARCH' }), card), true, 'mode match is case-insensitive');
  assert.equal(isContinuedSelection(searchPayload, card), false);
  assert.equal(isContinuedSelection(recheck({ continued: 'true' }), card), false, 'only a real boolean true counts');
  assert.equal(isContinuedSelection(recheck({}), null), false);

  assert.equal(formatSwitchLine('search', 'approach'), 'SWITCH search → approach');
  assert.equal(formatSwitchLine('search', 'search'), null);
  assert.equal(formatSwitchLine(null, 'search'), null);

  assert.equal(formatCheckSummary(card), '1 check · last t=10.0 s');
  assert.equal(formatCheckSummary(createDecisionCard({ rule_mode: 'hold' }, 2)), '1 check');
  assert.equal(formatCheckAge(1300, 1000), 'last check 0.3 s ago');
  assert.equal(formatCheckAge(900, 1000), 'last check 0.0 s ago', 'clock skew never shows a negative age');
});

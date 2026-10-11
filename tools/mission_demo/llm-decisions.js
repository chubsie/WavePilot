// LLM Decisions: Python 선택기(D1/규칙/강제)가 고른 rule mode 를 오른쪽 패널 카드로 보여준다.
// 입력은 sim_link 의 state.pipeline.selection / mode_end ({seq, payload}). 위쪽은 순수 함수(node 테스트 대상),
// 아래 createLlmDecisionView 만 DOM 을 만진다. 서버 문자열은 textContent 로만 넣는다.
import { LOG_TTL_MS } from './yolo-log.js';

export const DECISION_TTL_MS = LOG_TTL_MS;  // YOLO log 와 같은 10 초 TTL
export const MAX_DECISION_ENTRIES = 30;
export const MAX_PROBABILITY_BARS = 4;      // 막대는 상위 몇 개만 짧게
const KNOWN_SOURCES = new Set(['d1', 'rule', 'forced']);

// ---------- 포맷 ----------
const isPresent = (value) => value !== null && value !== undefined && value !== '';
const toModeName = (mode) => (isPresent(mode) ? String(mode).toUpperCase() : '–');

export function formatAiBadge(mission) {
  const runningMode = mission && isPresent(mission.running_mode) ? toModeName(mission.running_mode) : 'WAITING';
  return `AI · ${runningMode}`;
}

export function formatConfidence(confidence) {
  return Number.isFinite(confidence) ? `${Math.round(confidence * 100)}%` : '–';
}

// options 순서를 기준으로 확률을 붙이고, 높은 순으로 상위 maxBars 개만 남긴다. options 가 없으면 probabilities 키를 쓴다
export function formatProbabilityBars(probabilities, options, maxBars = MAX_PROBABILITY_BARS) {
  const table = probabilities && typeof probabilities === 'object' ? probabilities : {};
  const modes = Array.isArray(options) && options.length ? options : Object.keys(table);

  return modes
    .map((mode) => {
      const probability = Number(table[mode]);
      const ratio = Number.isFinite(probability) ? Math.min(1, Math.max(0, probability)) : 0;
      return { mode: String(mode), percent: Math.round(ratio * 100) };
    })
    .sort((a, b) => b.percent - a.percent)
    .slice(0, maxBars);
}

// 직전 모드 결과 — 예: "search → success T3"
export function formatModeResult(result) {
  if (!result || typeof result !== 'object' || !isPresent(result.rule_mode)) return null;

  const outcome = isPresent(result.outcome) ? ` → ${String(result.outcome)}` : '';
  const target = isPresent(result.target_track) ? ` ${String(result.target_track)}` : '';
  return `${String(result.rule_mode)}${outcome}${target}`;
}

export function formatFallbackReason(reason) {
  if (!isPresent(reason) || String(reason).toLowerCase() === 'none') return null;
  return String(reason);
}

// selection.payload → 카드 모델
export function createDecisionCard(payload, seq) {
  const source = isPresent(payload.source) ? String(payload.source).toLowerCase() : 'unknown';
  const params = payload.params && typeof payload.params === 'object' ? payload.params : {};

  return {
    kind: 'selection', key: `selection-${seq}`, seq,
    mode: toModeName(payload.rule_mode),
    source,
    sourceClass: KNOWN_SOURCES.has(source) ? source : 'unknown',
    confidence: formatConfidence(payload.confidence),
    fallbackReason: formatFallbackReason(payload.fallback_reason),
    eventKind: isPresent(payload.event_kind) ? String(payload.event_kind) : '–',
    target: isPresent(params.target_track) ? String(params.target_track) : null,
    bars: formatProbabilityBars(payload.probabilities, payload.options),
    previousResult: formatModeResult(payload.mode_result),
    latency: Number.isFinite(payload.latency_ms) ? `${Math.round(payload.latency_ms)} ms` : null,
  };
}

// mode_end.payload → 작은 한 줄
export function createModeEndLine(payload, seq) {
  return { kind: 'mode_end', key: `mode-end-${seq}`, seq, text: `END ${formatModeResult(payload) ?? '–'}` };
}

// ---------- 결정 로그 (최신이 앞) ----------
export function createDecisionLog({ maxEntries = MAX_DECISION_ENTRIES, ttlMs = DECISION_TTL_MS } = {}) {
  const entries = [];
  let lastSelectionSeq = null, lastModeEndSeq = null;

  function push(entry, nowMs) {
    entries.unshift({ ...entry, atMs: nowMs });
    entries.length = Math.min(entries.length, maxEntries);
    return entries[0];
  }

  // pipeline 은 최신 하나만 들고 있어 폴링마다 같은 seq 가 다시 온다 — 직전 seq 와 같으면 버린다
  function addSelection(pipelineSelection, nowMs) {
    if (!pipelineSelection || !pipelineSelection.payload || pipelineSelection.seq === lastSelectionSeq) return null;
    lastSelectionSeq = pipelineSelection.seq;
    return push(createDecisionCard(pipelineSelection.payload, pipelineSelection.seq), nowMs);
  }

  function addModeEnd(pipelineModeEnd, nowMs) {
    if (!pipelineModeEnd || !pipelineModeEnd.payload || pipelineModeEnd.seq === lastModeEndSeq) return null;
    lastModeEndSeq = pipelineModeEnd.seq;
    return push(createModeEndLine(pipelineModeEnd.payload, pipelineModeEnd.seq), nowMs);
  }

  // 지금 실행 중인 결정(가장 최신 selection)은 모드가 몇 분씩 돌아도 남긴다 — 그보다 오래된 항목만 TTL 로 지운다
  function prune(nowMs) {
    const current = entries.find((entry) => entry.kind === 'selection');
    while (entries.length && entries[entries.length - 1] !== current && nowMs - entries[entries.length - 1].atMs > ttlMs) entries.pop();
  }

  return { entries, addSelection, addModeEnd, prune };
}

// ---------- DOM ----------
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function renderBar(bar) {
  const row = element('div', 'decision-bar');
  const track = element('span', 'decision-bar-track');
  const fill = element('i');

  fill.style.width = `${bar.percent}%`;
  track.append(fill);
  row.append(element('span', 'decision-bar-mode', bar.mode), track, element('b', null, `${bar.percent}%`));
  return row;
}

function renderCard(card) {
  const article = element('article', 'decision-card');
  const header = element('header');
  header.append(element('b', 'decision-mode', card.mode), element('span', `decision-source ${card.sourceClass}`, card.source.toUpperCase()));
  header.append(element('span', 'decision-confidence', card.confidence));
  article.append(header);

  const facts = [card.eventKind, card.target ? `target ${card.target}` : null, card.latency].filter(Boolean).join(' · ');
  article.append(element('p', 'decision-facts', facts));
  if (card.fallbackReason) article.append(element('p', 'decision-fallback', `fallback: ${card.fallbackReason}`));
  if (card.previousResult) article.append(element('p', 'decision-previous', `prev ${card.previousResult}`));
  if (card.bars.length) {
    const bars = element('div', 'decision-bars');
    bars.append(...card.bars.map(renderBar));
    article.append(bars);
  }
  return article;
}

function renderEntry(entry) {
  return entry.kind === 'selection' ? renderCard(entry) : element('p', 'decision-end', entry.text);
}

// 새 항목만 위에 끼우고, 모델에서 빠진 항목은 지운다(비면 CSS :empty 가 대기 문구를 띄운다)
export function createLlmDecisionView(container) {
  const rendered = new Map();

  function render(entries) {
    const keys = new Set(entries.map((entry) => entry.key));
    for (const [key, node] of rendered) {
      if (!keys.has(key)) { node.remove(); rendered.delete(key); }
    }
    for (let i = entries.length - 1; i >= 0; i--) {
      const entry = entries[i];
      if (rendered.has(entry.key)) continue;
      const node = renderEntry(entry);
      rendered.set(entry.key, node);
      container.prepend(node);
    }
  }

  return { render };
}

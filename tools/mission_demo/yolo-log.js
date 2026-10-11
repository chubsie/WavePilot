// YOLO log: 브라우저 프레임 → Python YOLO 탐지 → state JSON → 보고 문장 흐름을 오른쪽 패널에 보여준다.
// 위쪽은 순수 함수(node 테스트 대상), 아래 createYoloLogView 만 DOM 을 만진다. 서버 문자열은 textContent 로만 넣는다.

export const MAX_LOG_ENTRIES = 150;  // TTL 안에서도 폭주하지 않게 거는 상한
export const LOG_TTL_MS = 10000;     // 모든 로그 항목(FRAME·STATE·REPORT)은 10 초 뒤 사라진다

// ---------- 포맷 ----------
const hasTrackId = (detection) => detection.track_id !== null && detection.track_id !== undefined;
const fixed = (value, digits) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '–';

// 원본 이미지 픽셀 xyxy → 화면 박스(FPV 캔버스가 곧 YOLO 입력이라 단순 비율 스케일)
export function scaleBox(xyxy, imageWidth, imageHeight, viewWidth, viewHeight) {
  const [x1, y1, x2, y2] = xyxy.map(Number);
  const scaleX = viewWidth / imageWidth, scaleY = viewHeight / imageHeight;

  return { left: x1 * scaleX, top: y1 * scaleY, width: (x2 - x1) * scaleX, height: (y2 - y1) * scaleY };
}

// 캡처(16:9 YOLO 입력) 픽셀 → 드론 시점 화면(인셋) 픽셀. 둘은 같은 카메라·같은 세로 FOV 라 중심 기준 세로 비율 하나로 스케일한다.
// 화면이 16:9 보다 좁으면 캡처 좌우가 화면 밖(음수·폭 초과)으로 나간다 — 잘라 내지 않는다
export function mapCapturePointToView(x, y, captureWidth, captureHeight, viewWidth, viewHeight) {
  const scale = viewHeight / captureHeight;

  return { x: (x - captureWidth / 2) * scale + viewWidth / 2, y: (y - captureHeight / 2) * scale + viewHeight / 2 };
}

export function mapCaptureBoxToView(xyxy, captureWidth, captureHeight, viewWidth, viewHeight) {
  const [x1, y1, x2, y2] = xyxy.map(Number);
  const topLeft = mapCapturePointToView(x1, y1, captureWidth, captureHeight, viewWidth, viewHeight);
  const bottomRight = mapCapturePointToView(x2, y2, captureWidth, captureHeight, viewWidth, viewHeight);

  return { left: topLeft.x, top: topLeft.y, width: bottomRight.x - topLeft.x, height: bottomRight.y - topLeft.y };
}

// FPV 박스 색 키 — 클래스마다 색을 조금씩 다르게. fallback 트랙의 VEHICLE 은 car 색, 모르는 클래스는 object
const COLORED_CLASS_NAMES = new Set(['person', 'car', 'bus', 'truck']);
export function getObjectCategory(className) {
  const name = String(className).toLowerCase();
  if (name === 'vehicle') return 'car';
  return COLORED_CLASS_NAMES.has(name) ? name : 'object';
}

// FPV 네임 태그는 객체와 신뢰도만 — 트랙 번호·박스 좌표는 YOLO log 에 있다
export function formatDetectionLabel(detection) {
  return `${String(detection.class_name).toLowerCase()} ${Math.round(Number(detection.confidence) * 100)}%`;
}

export function formatDetectionLine(detection) {
  const id = hasTrackId(detection) ? ` #${detection.track_id}` : '';
  const box = (detection.xyxy || []).map((value) => Math.round(Number(value))).join(',');
  const color = detection.upper_color ? ` ${String(detection.upper_color)}` : '';
  return `${String(detection.class_name)}${id} ${fixed(detection.confidence, 2)} [${box}]${color}`;
}

export function formatFrameHeader(frame, latencyMs) {
  const latency = Number.isFinite(latencyMs) ? `${Math.round(latencyMs)} ms` : '– ms';
  const count = (frame.detections || []).length;
  return `frame #${frame.frame_id} · seq ${frame.seq ?? '–'} · ${frame.image_width}×${frame.image_height} · YOLO ${latency} · ${count} det`;
}

// 탐지 내용이 같은지 보는 키 — 클래스·트랙만 본다(박스 몇 픽셀 흔들림은 무시)
export function detectionSignature(frame) {
  return (frame.detections || []).map((d) => `${d.class_name}:${hasTrackId(d) ? d.track_id : '-'}`).sort().join('|');
}

function countByClass(state) {
  if (state.counts_by_class && typeof state.counts_by_class === 'object') return state.counts_by_class;
  const counts = {};
  for (const track of state.tracks || []) counts[track.class] = (counts[track.class] || 0) + 1;
  return counts;
}

export function summarizeState(state) {
  const counts = Object.entries(countByClass(state)).map(([name, count]) => `${name} ${count}`).join(', ');
  const trackCount = (state.tracks || []).length;
  return `STATE JSON t=${fixed(state.time_sec, 1)}s · tracks ${trackCount}${counts ? ` (${counts})` : ''}`;
}

export function formatTrackLine(track) {
  const position = track.position_ned_m || {};
  return `${track.track_id} ${track.class} conf ${fixed(track.mean_confidence, 2)} obs ${track.observation_count ?? '–'}`
    + ` N ${fixed(position.north, 1)} E ${fixed(position.east, 1)}${track.is_active === false ? ' (inactive)' : ''}`;
}

export function formatReportHeader(payload) {
  const decision = payload.decision || {};
  const verdict = decision.verdict ? String(decision.verdict).toUpperCase() : '–';
  return `REPORT t=${fixed(payload.time_sec, 1)}s · ${verdict}${decision.reason ? ` · ${String(decision.reason)}` : ''}`;
}

export function reportSentences(payload) {
  const sentences = payload.report && Array.isArray(payload.report.sentences) ? payload.report.sentences : [];
  return sentences.map((sentence) => ({
    kind: String(sentence.kind || ''),
    text: String(sentence.text || ''),
    trackIds: Array.isArray(sentence.track_ids) ? sentence.track_ids.map(String) : [],
  }));
}

// ---------- 로그 모델 (최신이 앞) ----------
export function createYoloLog({ maxEntries = MAX_LOG_ENTRIES, ttlMs = LOG_TTL_MS } = {}) {
  const entries = [];
  const seenFrameIds = new Set();
  let lastStateSeq = null, lastReportSeq = null;

  function dropOldest() {
    const dropped = entries.pop();
    if (dropped.kind === 'frame') seenFrameIds.delete(dropped.frame.frame_id);
  }

  function push(entry, nowMs) {
    entries.unshift({ ...entry, atMs: nowMs });
    while (entries.length > maxEntries) dropOldest();
    return entries[0];
  }

  // 최신이 앞이므로 뒤에서부터 TTL 지난 항목을 버린다
  function prune(nowMs) {
    while (entries.length && nowMs - entries[entries.length - 1].atMs > ttlMs) dropOldest();
  }

  // YOLO 가 낸 프레임마다 한 줄 — 폴링마다 최근 결과가 겹쳐 오므로 frame_id 로 중복을 거른다.
  // timestamp_sec 은 페이지가 보낸 performance.now() 초라, TTL 이 지난 옛 프레임은 다시 들이지 않는다
  function addFrame(frame, latencyMs, nowMs) {
    if (!frame || seenFrameIds.has(frame.frame_id)) return null;
    if (Number.isFinite(frame.timestamp_sec) && nowMs - frame.timestamp_sec * 1000 > ttlMs) return null;

    seenFrameIds.add(frame.frame_id);
    return push({
      kind: 'frame', key: `frame-${frame.frame_id}`, seq: frame.seq, frame,
      header: formatFrameHeader(frame, latencyMs),
      lines: (frame.detections || []).map(formatDetectionLine),
    }, nowMs);
  }

  function addState(pipelineState, nowMs) {
    if (!pipelineState || !pipelineState.payload || pipelineState.seq === lastStateSeq) return null;
    lastStateSeq = pipelineState.seq;
    const state = pipelineState.payload;
    return push({
      kind: 'state', key: `state-${pipelineState.seq}`, seq: pipelineState.seq,
      header: summarizeState(state),
      lines: (state.tracks || []).map(formatTrackLine),
      json: JSON.stringify(state, null, 2),
    }, nowMs);
  }

  function addReport(pipelineReport, nowMs) {
    if (!pipelineReport || !pipelineReport.payload || pipelineReport.seq === lastReportSeq) return null;
    lastReportSeq = pipelineReport.seq;
    const payload = pipelineReport.payload;
    return push({
      kind: 'report', key: `report-${pipelineReport.seq}`, seq: pipelineReport.seq,
      header: formatReportHeader(payload),
      sentences: reportSentences(payload),
      error: payload.report_error ? String(payload.report_error) : null,
    }, nowMs);
  }

  return { entries, addFrame, addState, addReport, prune };
}

// ---------- DOM ----------
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

export function createTrackChip(trackId) {
  return element('span', 'track-chip', `[${trackId}]`);
}

function renderEntry(entry) {
  const article = element('article', `yolo-entry ${entry.kind}`);
  const header = element('header');
  header.append(element('span', 'yolo-kind', entry.kind.toUpperCase()), element('span', 'yolo-title', entry.header));
  article.append(header);

  if (entry.lines && entry.lines.length) article.append(element('pre', 'yolo-lines', entry.lines.join('\n')));
  if (entry.json) {
    const details = element('details');
    details.append(element('summary', null, 'raw JSON'), element('pre', 'yolo-json', entry.json));
    article.append(details);
  }
  for (const sentence of entry.sentences || []) {
    const line = element('p', `yolo-sentence ${sentence.kind}`);
    line.append(...sentence.trackIds.map(createTrackChip), document.createTextNode(sentence.text));
    article.append(line);
  }
  if (entry.error) article.append(element('p', 'yolo-error', `report_error: ${entry.error}`));
  return article;
}

// 새 항목만 위에 끼우고, 모델에서 빠진 항목은 지운다
export function createYoloLogView(container) {
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

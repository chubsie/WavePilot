// YOLO log: 브라우저 프레임 → Python YOLO 탐지 → state JSON → 보고 문장 흐름을 오른쪽 패널에 보여준다.
// 위쪽은 순수 함수(node 테스트 대상), 아래 createYoloLogView 만 DOM 을 만진다. 서버 문자열은 textContent 로만 넣는다.

export const MAX_LOG_ENTRIES = 30;
export const FRAME_LOG_INTERVAL_MS = 1000;

// ---------- 포맷 ----------
const hasTrackId = (detection) => detection.track_id !== null && detection.track_id !== undefined;
const fixed = (value, digits) => Number.isFinite(Number(value)) ? Number(value).toFixed(digits) : '–';

// 원본 이미지 픽셀 xyxy → 화면 박스(FPV 캔버스가 곧 YOLO 입력이라 단순 비율 스케일)
export function scaleBox(xyxy, imageWidth, imageHeight, viewWidth, viewHeight) {
  const [x1, y1, x2, y2] = xyxy.map(Number);
  const scaleX = viewWidth / imageWidth, scaleY = viewHeight / imageHeight;

  return { left: x1 * scaleX, top: y1 * scaleY, width: (x2 - x1) * scaleX, height: (y2 - y1) * scaleY };
}

export function formatDetectionLabel(detection) {
  const id = hasTrackId(detection) ? ` #${detection.track_id}` : '';
  return `${String(detection.class_name)}${id} ${Math.round(Number(detection.confidence) * 100)}%`;
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
export function createYoloLog({ maxEntries = MAX_LOG_ENTRIES, frameIntervalMs = FRAME_LOG_INTERVAL_MS } = {}) {
  const entries = [];
  const seenFrameIds = new Set();
  let lastFrame = { atMs: -Infinity, signature: null };
  let lastStateSeq = null, lastReportSeq = null;

  function push(entry) {
    entries.unshift(entry);
    while (entries.length > maxEntries) {
      const dropped = entries.pop();
      if (dropped.kind === 'frame') seenFrameIds.delete(dropped.frame.frame_id);
    }
    return entry;
  }

  // 같은 frame_id 는 한 번만, 그리고 탐지 내용이 바뀌었거나 frameIntervalMs 가 지났을 때만 남긴다
  function addFrame(frame, latencyMs, nowMs) {
    if (!frame || seenFrameIds.has(frame.frame_id)) return null;
    const signature = detectionSignature(frame);
    if (signature === lastFrame.signature && nowMs - lastFrame.atMs < frameIntervalMs) return null;

    seenFrameIds.add(frame.frame_id);
    lastFrame = { atMs: nowMs, signature };
    return push({
      kind: 'frame', key: `frame-${frame.frame_id}`, seq: frame.seq, frame,
      header: formatFrameHeader(frame, latencyMs),
      lines: (frame.detections || []).map(formatDetectionLine),
    });
  }

  function addState(pipelineState) {
    if (!pipelineState || !pipelineState.payload || pipelineState.seq === lastStateSeq) return null;
    lastStateSeq = pipelineState.seq;
    const state = pipelineState.payload;
    return push({
      kind: 'state', key: `state-${pipelineState.seq}`, seq: pipelineState.seq,
      header: summarizeState(state),
      lines: (state.tracks || []).map(formatTrackLine),
      json: JSON.stringify(state, null, 2),
    });
  }

  function addReport(pipelineReport) {
    if (!pipelineReport || !pipelineReport.payload || pipelineReport.seq === lastReportSeq) return null;
    lastReportSeq = pipelineReport.seq;
    const payload = pipelineReport.payload;
    return push({
      kind: 'report', key: `report-${pipelineReport.seq}`, seq: pipelineReport.seq,
      header: formatReportHeader(payload),
      sentences: reportSentences(payload),
      error: payload.report_error ? String(payload.report_error) : null,
    });
  }

  return { entries, addFrame, addState, addReport };
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

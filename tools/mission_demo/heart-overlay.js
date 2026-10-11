// fire_heart 를 화면에서 확실히 보이게 하는 부분 — 하트 빔 펄스 계산 + 메인(1인칭) 화면 위 'FIRE ♥ T48 ×3' 연사 표시.
// 하트 모양 자체는 world.js 가 3D 스프라이트로 메인 렌더에 그린다.
// 이 2D 오버레이도 world.fpvCanvas(YOLO 로 올라가는 JPEG)와 다른 캔버스에만 그린다 — 하트가 YOLO 입력에 섞이면 안 된다.
// 위쪽은 순수 함수(node 테스트 대상), createHeartOverlay 만 canvas 를 만진다.

export const HEART_PULSE_MS = 400;       // fire_heart 한 발이 chase 하트 빔을 켜 두는 시간
export const HEART_SHOT_LABEL_MS = 800;  // 마지막 발 이후 연사 표시('FIRE ♥ T48 ×3')를 남겨 두는 시간
export const BURST_GAP_MS = 1500;        // 이보다 오래 쉬면 다음 발은 새 연사로 센다
export const HEART_TARGET_MAX_AGE_SEC = 0.5;   // fire_heart 때 이보다 오래된 YOLO 박스는 조준에 쓰지 않는다
export const BODY_POINT_FROM_TOP = 0.4;        // 박스 위에서 40% — 머리 위가 아니라 몸통을 노린다
export const HEART_FLIGHT_MIN_SEC = 0.5, HEART_FLIGHT_MAX_SEC = 0.8;

// ---------- 순수 계산 ----------
// 연속 발사면 끊김 없이 이어지게 — 앞 펄스가 더 길게 남아 있으면 줄이지 않는다
export function extendHeartPulse(untilMs, nowMs, pulseMs = HEART_PULSE_MS) {
  return Math.max(untilMs, nowMs + pulseMs);
}

export function isHeartPulseOn(untilMs, nowMs) {
  return nowMs < untilMs;
}

// 같은 트랙을 BURST_GAP_MS 안에 다시 쏘면 연사 카운트를 잇고, 아니면 1 부터
export function countBurstShot(burst, trackId, nowMs) {
  const isSameBurst = burst !== null && burst.trackId === trackId && nowMs - burst.lastMs <= BURST_GAP_MS;
  return { trackId, count: isSameBurst ? burst.count + 1 : 1, lastMs: nowMs };
}

export function formatHeartShot(trackId, count) {
  return `FIRE ♥ T${trackId} ×${count}`;
}

// ---------- 표적 조준 (fire_heart track_id → YOLO 박스 → 화면 지점) ----------
// 프레임들(detectionLog + 최신 detections) 중 maxAgeSec 안에서 그 track_id 가 있는 가장 최근 박스
export function findTargetBox(frames, trackId, nowSec, maxAgeSec = HEART_TARGET_MAX_AGE_SEC) {
  let best = null;
  for (const frame of frames) {
    if (!frame || !Number.isFinite(frame.timestamp_sec) || nowSec - frame.timestamp_sec > maxAgeSec) continue;
    if (!(frame.image_width > 0 && frame.image_height > 0)) continue;
    if (best && frame.timestamp_sec <= best.timestampSec) continue;

    const detection = (frame.detections || []).find((item) => item.track_id === trackId && item.xyxy?.length === 4);
    if (detection) best = { xyxy: detection.xyxy, imageWidth: frame.image_width, imageHeight: frame.image_height, timestampSec: frame.timestamp_sec };
  }
  return best;
}

// 박스 가로 가운데, 위에서 BODY_POINT_FROM_TOP 지점(픽셀)
export function getBodyAimPixel([x1, y1, x2, y2]) {
  return { x: (x1 + x2) / 2, y: y1 + (y2 - y1) * BODY_POINT_FROM_TOP };
}

// 이미지 픽셀(원점 왼쪽 위, y 아래로) → three.js NDC(-1..1, y 위로)
export function pixelToNdc(x, y, width, height) {
  return { x: (x / width) * 2 - 1, y: 1 - (y / height) * 2 };
}

// 하트가 쏠 지점: ndc 는 FPV 카메라 unproject 용. 박스가 없으면 null(=레티클)
export function getHeartTarget(frames, trackId, nowSec, maxAgeSec = HEART_TARGET_MAX_AGE_SEC) {
  const box = findTargetBox(frames, trackId, nowSec, maxAgeSec);
  if (!box) return null;

  const pixel = getBodyAimPixel(box.xyxy);
  return { ndc: pixelToNdc(pixel.x, pixel.y, box.imageWidth, box.imageHeight) };
}

// 표적까지 거리(m) → 비행 시간. 순항 속도로 걸리는 시간을 0.5~0.8 s 로 묶어, 가까워도 보이고 멀어도 바로 닿게
export function getHeartFlightSec(distanceM, cruiseMps) {
  return Math.min(HEART_FLIGHT_MAX_SEC, Math.max(HEART_FLIGHT_MIN_SEC, distanceM / cruiseMps));
}

// ---------- 오버레이 ----------
export function createHeartOverlay({ canvas, uploadCanvas }) {
  if (canvas === uploadCanvas) throw new Error('heart overlay must not draw on the YOLO upload canvas');

  const ctx = canvas.getContext('2d');
  let burst = null;
  let latestShot = null;

  function fire(trackId, nowMs) {
    burst = countBurstShot(burst, trackId, nowMs);
    latestShot = { trackId, count: burst.count, startMs: nowMs };
    return burst.count;
  }

  function fitToDisplay() {
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(canvas.clientWidth * dpr), height = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  }

  function drawCounter(nowMs) {
    if (!latestShot || nowMs - latestShot.startMs > HEART_SHOT_LABEL_MS) return;

    // 메인 화면 전체를 덮는 캔버스라 인셋 시절(높이의 7%)보다 작게, 상한도 둔다
    const dpr = window.devicePixelRatio || 1;
    const fontPx = Math.round(Math.min(32 * dpr, Math.max(10 * dpr, canvas.height * 0.04)));
    ctx.font = `700 ${fontPx}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = 'rgba(15,17,21,.7)';
    const text = formatHeartShot(latestShot.trackId, latestShot.count);
    const textWidth = ctx.measureText(text).width + fontPx;
    ctx.fillRect((canvas.width - textWidth) / 2, fontPx * 0.4, textWidth, fontPx * 1.4);
    ctx.fillStyle = '#ff7a93';
    ctx.fillText(text, canvas.width / 2, fontPx * 0.6);
  }

  function draw(nowMs) {
    fitToDisplay();
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    drawCounter(nowMs);
  }

  return { fire, draw, canvas };
}

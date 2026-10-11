// fire_heart 를 화면에서 확실히 보이게 하는 부분 — chase 빔 펄스 계산 + FPV 위 2D 하트 오버레이.
// FPV 오버레이는 world.fpvCanvas(YOLO 로 올라가는 JPEG)와 다른 캔버스에만 그린다 — 하트가 YOLO 입력에 섞이면 안 된다.
// 위쪽은 순수 함수(node 테스트 대상), createHeartOverlay 만 canvas 를 만진다.

export const HEART_PULSE_MS = 400;       // fire_heart 한 발이 chase 하트 빔을 켜 두는 시간
export const FPV_HEART_FLIGHT_MS = 800;  // FPV 하트 한 발이 아래에서 표적(화면 중앙)까지 날아가는 시간
export const BURST_GAP_MS = 1500;        // 이보다 오래 쉬면 다음 발은 새 연사로 센다
export const HEART_TARGET_MAX_AGE_SEC = 0.5;   // fire_heart 때 이보다 오래된 YOLO 박스는 조준에 쓰지 않는다
export const BODY_POINT_FROM_TOP = 0.4;        // 박스 위에서 40% — 머리 위가 아니라 몸통을 노린다
export const HEART_FLIGHT_MIN_SEC = 0.5, HEART_FLIGHT_MAX_SEC = 0.8;
const RETICLE = { x: 0.5, y: 0.5 };            // 화면 비율 좌표(0..1)의 FPV 중앙

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

// 하트가 쏠 지점: ndc 는 FPV 카메라 unproject 용, screen 은 오버레이용 화면 비율(0..1). 박스가 없으면 null(=레티클)
export function getHeartTarget(frames, trackId, nowSec, maxAgeSec = HEART_TARGET_MAX_AGE_SEC) {
  const box = findTargetBox(frames, trackId, nowSec, maxAgeSec);
  if (!box) return null;

  const pixel = getBodyAimPixel(box.xyxy);
  return {
    ndc: pixelToNdc(pixel.x, pixel.y, box.imageWidth, box.imageHeight),
    screen: { x: pixel.x / box.imageWidth, y: pixel.y / box.imageHeight },
  };
}

// 표적까지 거리(m) → 비행 시간. 순항 속도로 걸리는 시간을 0.5~0.8 s 로 묶어, 가까워도 보이고 멀어도 바로 닿게
export function getHeartFlightSec(distanceM, cruiseMps) {
  return Math.min(HEART_FLIGHT_MAX_SEC, Math.max(HEART_FLIGHT_MIN_SEC, distanceM / cruiseMps));
}

// 진행률 0..1 → 하트 위치·크기·투명도. 화면 아래 가운데서 target(화면 비율, 기본 레티클)으로 날아가며 커졌다 사라진다
export function getHeartFlight(progress, width, height, lane = 0, target = RETICLE) {
  const travel = Math.min(1, progress * 1.4);
  const size = height * (0.12 + 0.28 * progress);
  const startX = width / 2, endX = width * target.x;
  const startY = height, endY = height * target.y;

  return {
    x: startX + (endX - startX) * travel + lane * width * 0.06 * (1 - travel),
    y: startY + (endY - startY) * travel,
    size,
    alpha: progress < 0.65 ? 1 : Math.max(0, 1 - (progress - 0.65) / 0.35),
  };
}

// 발마다 조금씩 다른 줄(-1..1)로 날려 연사가 한 덩어리로 겹쳐 보이지 않게
export function getShotLane(count) {
  return [0, -1, 1, -0.5, 0.5][count % 5];
}

// ---------- 오버레이 ----------
function traceHeartPath(ctx, x, y, size) {
  ctx.beginPath();
  ctx.moveTo(x, y + size * 0.3);
  ctx.bezierCurveTo(x, y, x - size * 0.5, y, x - size * 0.5, y + size * 0.3);
  ctx.bezierCurveTo(x - size * 0.5, y + size * 0.6, x, y + size * 0.8, x, y + size);
  ctx.bezierCurveTo(x, y + size * 0.8, x + size * 0.5, y + size * 0.6, x + size * 0.5, y + size * 0.3);
  ctx.bezierCurveTo(x + size * 0.5, y, x, y, x, y + size * 0.3);
  ctx.closePath();
}

export function createHeartOverlay({ canvas, uploadCanvas }) {
  if (canvas === uploadCanvas) throw new Error('heart overlay must not draw on the YOLO upload canvas');

  const ctx = canvas.getContext('2d');
  const shots = [];
  let burst = null;

  // target = 화면 비율 지점(getHeartTarget().screen). 없으면 레티클로
  function fire(trackId, nowMs, target = null) {
    burst = countBurstShot(burst, trackId, nowMs);
    shots.push({ trackId, count: burst.count, startMs: nowMs, lane: getShotLane(burst.count), target: target ?? RETICLE });
    return burst.count;
  }

  function fitToDisplay() {
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(canvas.clientWidth * dpr), height = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  }

  function drawShot(shot, progress) {
    const { width, height } = canvas;
    const flight = getHeartFlight(progress, width, height, shot.lane, shot.target);

    ctx.globalAlpha = flight.alpha;
    ctx.fillStyle = '#ff3b5c';
    ctx.strokeStyle = '#ffffff';
    ctx.lineWidth = Math.max(1, height * 0.006);
    traceHeartPath(ctx, flight.x, flight.y - flight.size / 2, flight.size);
    ctx.fill();
    ctx.stroke();
  }

  function drawCounter(nowMs) {
    const latest = shots[shots.length - 1];
    if (!latest || nowMs - latest.startMs > FPV_HEART_FLIGHT_MS) return;

    const fontPx = Math.max(10, Math.round(canvas.height * 0.07));
    ctx.globalAlpha = 1;
    ctx.font = `700 ${fontPx}px system-ui, sans-serif`;
    ctx.textAlign = 'center';
    ctx.textBaseline = 'top';
    ctx.fillStyle = 'rgba(15,17,21,.7)';
    const text = formatHeartShot(latest.trackId, latest.count);
    const textWidth = ctx.measureText(text).width + fontPx;
    ctx.fillRect((canvas.width - textWidth) / 2, fontPx * 0.4, textWidth, fontPx * 1.4);
    ctx.fillStyle = '#ff7a93';
    ctx.fillText(text, canvas.width / 2, fontPx * 0.6);
  }

  function draw(nowMs) {
    fitToDisplay();
    ctx.clearRect(0, 0, canvas.width, canvas.height);

    for (let i = shots.length - 1; i >= 0; i--) {
      const progress = (nowMs - shots[i].startMs) / FPV_HEART_FLIGHT_MS;
      if (progress >= 1) { shots.splice(i, 1); continue; }
      if (progress >= 0) drawShot(shots[i], progress);
    }
    drawCounter(nowMs);
    ctx.globalAlpha = 1;
  }

  return { fire, draw, canvas };
}

// fire_heart 를 화면에서 확실히 보이게 하는 부분 — chase 빔 펄스 계산 + FPV 위 2D 하트 오버레이.
// FPV 오버레이는 world.fpvCanvas(YOLO 로 올라가는 JPEG)와 다른 캔버스에만 그린다 — 하트가 YOLO 입력에 섞이면 안 된다.
// 위쪽은 순수 함수(node 테스트 대상), createHeartOverlay 만 canvas 를 만진다.

export const HEART_PULSE_MS = 400;       // fire_heart 한 발이 chase 하트 빔을 켜 두는 시간
export const FPV_HEART_FLIGHT_MS = 800;  // FPV 하트 한 발이 아래에서 표적(화면 중앙)까지 날아가는 시간
export const BURST_GAP_MS = 1500;        // 이보다 오래 쉬면 다음 발은 새 연사로 센다

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

// 진행률 0..1 → 하트 위치·크기·투명도. 화면 아래 가운데서 표적(가운데 레티클)으로 날아가며 커졌다 사라진다
export function getHeartFlight(progress, width, height, lane = 0) {
  const travel = Math.min(1, progress * 1.4);
  const size = height * (0.12 + 0.28 * progress);

  return {
    x: width / 2 + lane * width * 0.06 * (1 - travel),
    y: height - (height / 2) * travel,
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

  function fire(trackId, nowMs) {
    burst = countBurstShot(burst, trackId, nowMs);
    shots.push({ trackId, count: burst.count, startMs: nowMs, lane: getShotLane(burst.count) });
    return burst.count;
  }

  function fitToDisplay() {
    const dpr = window.devicePixelRatio || 1;
    const width = Math.round(canvas.clientWidth * dpr), height = Math.round(canvas.clientHeight * dpr);
    if (canvas.width !== width || canvas.height !== height) { canvas.width = width; canvas.height = height; }
  }

  function drawShot(shot, progress) {
    const { width, height } = canvas;
    const flight = getHeartFlight(progress, width, height, shot.lane);

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

// 브라우저 시뮬레이터 ↔ Python 양방향 링크 (Phase 2, D-01 / X-09).
// - 와이어 프로토콜 클라이언트: POST /sim/frame (JPEG + X-Telemetry), POST /sim/telemetry (JSON)
// - GPS/배터리 헬퍼, AI 모드 명령 실행기(autopilot), YOLO 박스 오버레이 + 하트 효과, 우측 패널 렌더러
// 모듈이 아닌 classic script 다. 브라우저에선 window.SimLink, Node 에선 require() 로 쓴다(스모크 테스트용).
// 로드 시점에는 DOM 에 접근하지 않는다 — DOM 은 요소를 인자로 받는 함수 안에서만 만진다.
(function (root) {
  'use strict';

  // ---------- 상수 ----------
  // X-06: 평면 지구 근사 GPS, 홈 = 서울시청
  const HOME_LATITUDE_DEG = 37.5665;
  const HOME_LONGITUDE_DEG = 126.9780;
  const METERS_PER_DEG_LAT = 111320;

  // D-12: 프레임·텔레메트리 전송 주기와 백프레셔
  const FRAME_HZ = 10;
  const TELEMETRY_HZ = 10;
  const MAX_PENDING_FRAMES = 2;
  const JPEG_QUALITY = 0.85;
  const MAX_SEQ = 2 ** 31;
  const LINK_TIMEOUT_SEC = 1.0;

  // X-08: 배터리는 시간에 비례해 600 s 에 100% 소모
  const BATTERY_FULL_SEC = 600;

  // X-02: set_velocity 클램프
  const MAX_HORIZONTAL_MPS = 8;
  const MAX_VERTICAL_MPS = 3;
  const MAX_VELOCITY_DURATION_SEC = 2;

  // X-03 / X-07: 짐벌 범위와 기본 각
  const GIMBAL_PITCH_MIN_DEG = -90;
  const GIMBAL_PITCH_MAX_DEG = 0;
  const GIMBAL_YAW_MIN_DEG = -90;
  const GIMBAL_YAW_MAX_DEG = 90;
  const DEFAULT_GIMBAL_PITCH_DEG = -20;

  // 자동비행 동역학
  const GOTO_SPEED_MPS = 6;
  const CLIMB_SPEED_MPS = 2;
  const LAND_SPEED_MPS = 1;
  const GOTO_GAIN_PER_SEC = 1.0;
  const GOAL_REACHED_HORIZONTAL_M = 1.0;
  const GOAL_REACHED_VERTICAL_M = 0.5;
  const YAW_RATE_DPS = 90;
  const GIMBAL_RATE_DPS = 120;

  // 오버레이·패널
  const HEART_DURATION_SEC = 1.2;
  const MAX_RECENT_ITEMS = 8;
  const DETECTION_STALE_SEC = 1.0;
  const MAX_PENDING_EVENTS = 50;

  // ---------- 순수 헬퍼 ----------
  const clamp = (value, min, max) => Math.min(max, Math.max(min, value));

  function wrapYawDeg(deg) {
    // [-180, 180) 로 감는다
    return ((((deg + 180) % 360) + 360) % 360) - 180;
  }

  function nedToGps(northM, eastM, downM) {
    // X-06 평면 지구 근사, D-13 잡음 없음, 홈 고도 0
    const metersPerDegLon = METERS_PER_DEG_LAT * Math.cos(HOME_LATITUDE_DEG * Math.PI / 180);

    return {
      latitude_deg: HOME_LATITUDE_DEG + northM / METERS_PER_DEG_LAT,
      longitude_deg: HOME_LONGITUDE_DEG + eastM / metersPerDegLon,
      altitude_m: -downM,
    };
  }

  function parseBatteryStartPct(search) {
    // URL 형식: ?battery=<0..100> (D-15). 없거나 잘못되면 100
    const raw = new URLSearchParams(search || '').get('battery');
    const value = raw === null || raw.trim() === '' ? NaN : Number(raw);

    return Number.isFinite(value) ? clamp(value, 0, 100) : 100;
  }

  function batteryPctAt(startPct, elapsedSec) {
    // D-14: 움직임과 무관하게 시간에 선형으로 소모
    return Math.max(0, startPct - 100 * elapsedSec / BATTERY_FULL_SEC);
  }

  function slewToward(current, target, maxStep) {
    return current + clamp(target - current, -maxStep, maxStep);
  }

  // ---------- AI 모드 명령 실행기 ----------
  // motion 종류: hold(정지 유지) / goto(take_off·go_to 공용) / velocity / land / return
  function createAutopilot(options = {}) {
    const onHeart = options.onHeart || (() => {});
    const events = [];
    const gimbalTarget = { pitchDeg: DEFAULT_GIMBAL_PITCH_DEG, yawDeg: 0 };
    let motion = null;
    let yawTargetDeg = null;
    let lastPose = { northM: 0, eastM: 0, downM: 0, yawDeg: 0, onGround: true };

    const autopilot = {
      flightMode: 'hover',
      gimbal: { pitchDeg: DEFAULT_GIMBAL_PITCH_DEG, yawDeg: 0 },
      enterAi,
      cancel,
      apply,
      step,
      takeEvents,
    };

    function holdAt(pose) {
      motion = { kind: 'hold', northM: pose.northM, eastM: pose.eastM, downM: pose.downM };
    }

    function enterAi(pose) {
      lastPose = { ...pose };
      holdAt(pose);
      yawTargetDeg = pose.yawDeg;
      autopilot.flightMode = pose.onGround ? 'grounded' : 'hover';
    }

    function cancel() {
      // 수동으로 돌아갈 때 남은 명령이 나중에 되살아나지 않도록 버린다
      motion = null;
      yawTargetDeg = null;
    }

    function apply(command, nowSec) {
      const isNumber = (key) => Number.isFinite(command[key]);

      switch (command && command.kind) {
        case 'take_off': {
          if (!isNumber('altitude_m')) return false;
          motion = { kind: 'goto', northM: lastPose.northM, eastM: lastPose.eastM, downM: -command.altitude_m, commandId: null };
          autopilot.flightMode = 'moving';
          return true;
        }
        case 'go_to': {
          if (!isNumber('north_m') || !isNumber('east_m') || !isNumber('altitude_m')) return false;
          motion = { kind: 'goto', northM: command.north_m, eastM: command.east_m, downM: -command.altitude_m, commandId: command.id };
          autopilot.flightMode = 'moving';
          return true;
        }
        case 'set_velocity': {
          if (!isNumber('north_mps') || !isNumber('east_mps') || !isNumber('down_mps') || !isNumber('duration_sec')) return false;
          if (command.duration_sec <= 0) return false;
          const horizontalMps = Math.hypot(command.north_mps, command.east_mps);
          const scale = horizontalMps > MAX_HORIZONTAL_MPS ? MAX_HORIZONTAL_MPS / horizontalMps : 1;
          const durationSec = Math.min(command.duration_sec, MAX_VELOCITY_DURATION_SEC);
          motion = {
            kind: 'velocity',
            northMps: command.north_mps * scale,
            eastMps: command.east_mps * scale,
            downMps: clamp(command.down_mps, -MAX_VERTICAL_MPS, MAX_VERTICAL_MPS),
            untilSec: nowSec + durationSec,
          };
          autopilot.flightMode = 'moving';
          return true;
        }
        case 'set_yaw': {
          if (!isNumber('yaw_deg')) return false;
          yawTargetDeg = wrapYawDeg(command.yaw_deg);
          return true;
        }
        case 'set_gimbal': {
          if (!isNumber('pitch_deg') || !isNumber('yaw_deg')) return false;
          gimbalTarget.pitchDeg = clamp(command.pitch_deg, GIMBAL_PITCH_MIN_DEG, GIMBAL_PITCH_MAX_DEG);
          gimbalTarget.yawDeg = clamp(command.yaw_deg, GIMBAL_YAW_MIN_DEG, GIMBAL_YAW_MAX_DEG);
          return true;
        }
        case 'land': {
          motion = { kind: 'land' };
          autopilot.flightMode = 'landing';
          return true;
        }
        case 'return_to_home': {
          motion = { kind: 'return', phase: 'fly', downM: lastPose.downM };
          autopilot.flightMode = 'returning';
          return true;
        }
        case 'fire_heart': {
          // X-05: 같은 트랙에 한 번만 쏘는 규칙은 Phase 4 몫이다
          if (!Number.isInteger(command.track_id)) return false;
          onHeart(command.track_id);
          return true;
        }
        default:
          return false;
      }
    }

    function velocityToward(pose, northM, eastM, downM) {
      const dNorth = northM - pose.northM;
      const dEast = eastM - pose.eastM;
      const horizontalM = Math.hypot(dNorth, dEast);
      const horizontalMps = Math.min(horizontalM * GOTO_GAIN_PER_SEC, GOTO_SPEED_MPS);
      const horizontalScale = horizontalM > 0 ? horizontalMps / horizontalM : 0;

      return {
        velNorthMps: dNorth * horizontalScale,
        velEastMps: dEast * horizontalScale,
        velDownMps: clamp((downM - pose.downM) * GOTO_GAIN_PER_SEC, -CLIMB_SPEED_MPS, CLIMB_SPEED_MPS),
        horizontalM,
        verticalM: Math.abs(downM - pose.downM),
      };
    }

    function stepDescent(pose) {
      if (pose.onGround) {
        holdAt(pose);
        autopilot.flightMode = 'grounded';
        return { velNorthMps: 0, velEastMps: 0, velDownMps: 0 };
      }
      return { velNorthMps: 0, velEastMps: 0, velDownMps: LAND_SPEED_MPS };
    }

    function stepMotion(nowSec, pose) {
      const stopped = { velNorthMps: 0, velEastMps: 0, velDownMps: 0 };

      if (!motion) return stopped;

      if (motion.kind === 'hold') {
        return velocityToward(pose, motion.northM, motion.eastM, motion.downM);
      }

      if (motion.kind === 'goto') {
        const toward = velocityToward(pose, motion.northM, motion.eastM, motion.downM);
        const isReached = toward.horizontalM <= GOAL_REACHED_HORIZONTAL_M && toward.verticalM <= GOAL_REACHED_VERTICAL_M;
        if (!isReached) return toward;
        // D-09: go_to 도착 시 goal_reached 이벤트를 한 번만 보낸다
        if (motion.commandId !== null && motion.commandId !== undefined) {
          events.push({ kind: 'goal_reached', command_id: motion.commandId, time_sec: nowSec });
          if (events.length > MAX_PENDING_EVENTS) events.shift();
        }
        motion = { kind: 'hold', northM: motion.northM, eastM: motion.eastM, downM: motion.downM };
        autopilot.flightMode = 'hover';
        return velocityToward(pose, motion.northM, motion.eastM, motion.downM);
      }

      if (motion.kind === 'velocity') {
        if (nowSec < motion.untilSec) {
          return { velNorthMps: motion.northMps, velEastMps: motion.eastMps, velDownMps: motion.downMps };
        }
        // X-02 자동 정지: 지금 위치에서 멈춰 선다
        holdAt(pose);
        autopilot.flightMode = 'hover';
        return stopped;
      }

      if (motion.kind === 'land') return stepDescent(pose);

      if (motion.kind === 'return') {
        if (motion.phase === 'fly') {
          const toward = velocityToward(pose, 0, 0, motion.downM);
          if (toward.horizontalM > GOAL_REACHED_HORIZONTAL_M) return toward;
          motion.phase = 'descend';
        }
        return stepDescent(pose);
      }

      return stopped;
    }

    function stepYaw(dtSec, pose) {
      if (yawTargetDeg === null) return pose.yawDeg;
      // 최단 호를 따라 YAW_RATE_DPS 로 돈다
      const errorDeg = wrapYawDeg(yawTargetDeg - pose.yawDeg);
      return wrapYawDeg(pose.yawDeg + clamp(errorDeg, -YAW_RATE_DPS * dtSec, YAW_RATE_DPS * dtSec));
    }

    function stepGimbal(dtSec) {
      const maxStepDeg = GIMBAL_RATE_DPS * dtSec;
      autopilot.gimbal.pitchDeg = slewToward(autopilot.gimbal.pitchDeg, gimbalTarget.pitchDeg, maxStepDeg);
      autopilot.gimbal.yawDeg = slewToward(autopilot.gimbal.yawDeg, gimbalTarget.yawDeg, maxStepDeg);
    }

    function step(dtSec, nowSec, pose) {
      lastPose = { ...pose };

      const velocity = stepMotion(nowSec, pose);
      const yawDeg = stepYaw(dtSec, pose);
      stepGimbal(dtSec);

      return {
        velNorthMps: velocity.velNorthMps,
        velEastMps: velocity.velEastMps,
        velDownMps: velocity.velDownMps,
        yawDeg,
      };
    }

    function takeEvents() {
      return events.splice(0, events.length);
    }

    return autopilot;
  }

  // ---------- 와이어 프로토콜 클라이언트 ----------
  function createLink(options) {
    const { canvas, getTelemetry, isAiMode, onCommand, takeEvents, onPaired, log } = options;
    const nowSec = () => performance.now() / 1000;
    const state = {
      connected: false,
      isPaired: false,
      lastOkSec: -Infinity,
      frameFps: 0,
      yoloLatencyMs: null,
      roundTripMs: null,
      detections: null,
      detectionsAtSec: -Infinity,
      recentCommands: [],
      discardedCount: 0,
    };

    function refreshConnected() {
      state.connected = nowSec() - state.lastOkSec <= LINK_TIMEOUT_SEC;
    }

    // --- 텔레메트리: 10 Hz, 한 번에 하나만 in-flight ---
    let isTelemetryInFlight = false;
    let carriedEvents = [];
    let telemetryFailCount = 0;

    function handleCommands(commands) {
      if (!Array.isArray(commands) || commands.length === 0) return;
      // D-05: 수동 모드에선 받은 명령을 실행하지 않고 버린다(명령은 한 번만 배달된다)
      if (!isAiMode()) {
        state.discardedCount += commands.length;
        log(`Discarded ${commands.length} command(s): manual mode`);
        return;
      }
      for (const command of commands) {
        const accepted = onCommand(command) !== false;
        state.recentCommands.unshift({ id: command.id, kind: String(command.kind), accepted });
      }
      state.recentCommands.length = Math.min(state.recentCommands.length, MAX_RECENT_ITEMS);
    }

    function handleTelemetryResponse(body, sentAtSec) {
      const status = body.status || {};

      state.lastOkSec = nowSec();
      state.roundTripMs = (state.lastOkSec - sentAtSec) * 1000;
      // 첫 응답 = 초기 페어링. 홈을 여기서 잡아야 같은 응답의 명령부터 새 홈 기준으로 실행된다
      if (!state.isPaired) {
        state.isPaired = true;
        if (onPaired) onPaired();
      }
      if (Number.isFinite(status.frame_fps)) state.frameFps = status.frame_fps;
      state.yoloLatencyMs = Number.isFinite(status.yolo_latency_ms) ? status.yolo_latency_ms : null;
      if (body.detections) {
        state.detections = body.detections;
        state.detectionsAtSec = state.lastOkSec;
      }
      handleCommands(body.commands);
    }

    async function postTelemetry() {
      if (isTelemetryInFlight) return;
      isTelemetryInFlight = true;

      const events = carriedEvents.concat(takeEvents()).slice(-MAX_PENDING_EVENTS);
      const sentAtSec = nowSec();
      carriedEvents = [];

      try {
        const response = await fetch('/sim/telemetry', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ telemetry: getTelemetry(), ai_mode: isAiMode(), events }),
        });
        if (response.status !== 200) throw new Error('HTTP ' + response.status);
        handleTelemetryResponse(await response.json(), sentAtSec);
        telemetryFailCount = 0;
      } catch (error) {
        // 실패한 이벤트(goal_reached 등)는 다음 전송에 다시 싣는다
        carriedEvents = events.concat(carriedEvents).slice(-MAX_PENDING_EVENTS);
        if (telemetryFailCount++ === 0) log('Telemetry upload failed: ' + error.message);
      } finally {
        isTelemetryInFlight = false;
        refreshConnected();
      }
    }

    function start() {
      if (!/^https?:$/.test(location.protocol)) {
        log('Open via python -m modules.simulation.browser.server');
        return;
      }
      setInterval(postTelemetry, 1000 / TELEMETRY_HZ);
    }

    // --- 프레임: 10 Hz, 최대 MAX_PENDING_FRAMES 개 in-flight, 넘치면 버린다(D-12) ---
    const frameIntervalSec = 1 / FRAME_HZ;
    let sinceLastFrameSec = 0;
    let pendingFrames = 0;
    let seq = 0;
    let frameFailCount = 0;

    async function uploadFrame(blob, frameSeq, snapshot) {
      try {
        if (!blob) throw new Error('toBlob returned null');
        const response = await fetch('/sim/frame?seq=' + frameSeq, {
          method: 'POST',
          headers: { 'Content-Type': 'image/jpeg', 'X-Telemetry': JSON.stringify(snapshot) },
          body: blob,
        });
        if (response.status !== 204) throw new Error('HTTP ' + response.status);
        frameFailCount = 0;
      } catch (error) {
        if (frameFailCount++ === 0) log('Frame upload failed: ' + error.message);
      } finally {
        pendingFrames--;
      }
    }

    function afterRender(dtSec) {
      refreshConnected();
      if (!/^https?:$/.test(location.protocol)) return;

      sinceLastFrameSec += dtSec;
      if (sinceLastFrameSec < frameIntervalSec) return;
      sinceLastFrameSec = Math.min(sinceLastFrameSec - frameIntervalSec, frameIntervalSec);   // 느린 프레임 뒤 몰아 보내지 않는다

      if (pendingFrames >= MAX_PENDING_FRAMES) return;

      // D-11: 텔레메트리 스냅샷은 렌더 직후(캡처 시점)에 뜬다
      const snapshot = getTelemetry();
      const frameSeq = seq = (seq + 1) % MAX_SEQ;
      pendingFrames++;
      canvas.toBlob((blob) => uploadFrame(blob, frameSeq, snapshot), 'image/jpeg', JPEG_QUALITY);
    }

    return { start, afterRender, state };
  }

  // ---------- 오버레이: YOLO 박스 + 하트 ----------
  const hearts = [];

  function startHeart(trackId, nowSec) {
    hearts.push({ trackId, startSec: nowSec });
  }

  function drawDetections(ctx, frame) {
    const scaleX = ctx.canvas.width / frame.image_width;
    const scaleY = ctx.canvas.height / frame.image_height;

    ctx.lineWidth = 3;
    ctx.font = '600 22px system-ui, sans-serif';
    ctx.textBaseline = 'bottom';
    for (const detection of frame.detections || []) {
      const [x1, y1, x2, y2] = detection.xyxy.map(Number);
      const label = `${detection.class_name} ${Number(detection.confidence).toFixed(2)}`
        + (detection.track_id !== null && detection.track_id !== undefined ? ` #${detection.track_id}` : '');
      const left = x1 * scaleX, top = y1 * scaleY;
      const labelWidth = ctx.measureText(label).width + 10;
      ctx.strokeStyle = '#4fd1a5';
      ctx.strokeRect(left, top, (x2 - x1) * scaleX, (y2 - y1) * scaleY);
      ctx.fillStyle = 'rgba(15,17,21,.75)';
      ctx.fillRect(left, Math.max(0, top - 28), labelWidth, 28);
      ctx.fillStyle = '#4fd1a5';
      ctx.fillText(label, left + 5, Math.max(28, top) - 3);
    }
  }

  function traceHeartPath(ctx, x, y, size) {
    ctx.beginPath();
    ctx.moveTo(x, y + size * 0.3);
    ctx.bezierCurveTo(x, y, x - size * 0.5, y, x - size * 0.5, y + size * 0.3);
    ctx.bezierCurveTo(x - size * 0.5, y + size * 0.6, x, y + size * 0.8, x, y + size);
    ctx.bezierCurveTo(x, y + size * 0.8, x + size * 0.5, y + size * 0.6, x + size * 0.5, y + size * 0.3);
    ctx.bezierCurveTo(x + size * 0.5, y, x, y, x, y + size * 0.3);
    ctx.closePath();
  }

  function drawHearts(ctx, nowSec) {
    const width = ctx.canvas.width, height = ctx.canvas.height;

    for (let i = hearts.length - 1; i >= 0; i--) {
      const progress = (nowSec - hearts[i].startSec) / HEART_DURATION_SEC;
      if (progress >= 1 || progress < 0) {
        if (progress >= 1) hearts.splice(i, 1);
        continue;
      }
      // X-05: 화면 아래 가운데에서 중앙으로 날아가며 커졌다가 사라진다
      const x = width / 2;
      const y = height - (height / 2) * Math.min(1, progress * 1.5);
      const size = 40 + 120 * progress;
      ctx.globalAlpha = progress < 0.6 ? 1 : 1 - (progress - 0.6) / 0.4;
      ctx.fillStyle = '#ff3b5c';
      traceHeartPath(ctx, x, y - size / 2, size);
      ctx.fill();
      ctx.fillStyle = '#ffffff';
      ctx.font = '700 28px system-ui, sans-serif';
      ctx.textAlign = 'center';
      ctx.fillText(`#${hearts[i].trackId}`, x, y + size / 2 + 32);
      ctx.textAlign = 'start';
      ctx.globalAlpha = 1;
    }
  }

  function drawOverlay(ctx, linkState, nowSec) {
    ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);

    const isFresh = performance.now() / 1000 - linkState.detectionsAtSec < DETECTION_STALE_SEC;
    if (linkState.detections && isFresh) drawDetections(ctx, linkState.detections);

    drawHearts(ctx, nowSec);
  }

  // ---------- 우측 패널 ----------
  // 서버 데이터는 textContent 로만 넣는다 — HTML 파싱 경로 금지(T-02-04-02)
  function fillList(listElement, lines) {
    const items = lines.map((line) => {
      const item = document.createElement('li');
      item.textContent = line;
      return item;
    });
    listElement.replaceChildren(...items);
  }

  function formatTelemetry(telemetry) {
    const fixed = (value, digits) => Number(value).toFixed(digits);
    const gps = telemetry.gps;

    return [
      `NED      N ${fixed(telemetry.north_m, 1)}  E ${fixed(telemetry.east_m, 1)}  D ${fixed(telemetry.down_m, 1)} m`,
      `GPS      ${fixed(gps.latitude_deg, 6)}, ${fixed(gps.longitude_deg, 6)}`,
      `Altitude ${fixed(gps.altitude_m, 1)} m`,
      `Attitude R ${fixed(telemetry.roll_deg, 1)}  P ${fixed(telemetry.pitch_deg, 1)}  Y ${fixed(telemetry.yaw_deg, 1)}°`,
      `Gimbal   P ${fixed(telemetry.gimbal_pitch_deg, 1)}  Y ${fixed(telemetry.gimbal_yaw_deg, 1)}°`,
      `Battery  ${fixed(telemetry.battery_pct, 1)} %`,
      `Mode     ${telemetry.flight_mode}`,
    ].join('\n');
  }

  function renderPanel(elements, telemetry, linkState, homeGps) {
    const yoloText = linkState.yoloLatencyMs === null ? '–' : linkState.yoloLatencyMs.toFixed(0);
    const roundTripText = linkState.roundTripMs === null ? '–' : linkState.roundTripMs.toFixed(0);
    elements.link.textContent = `Python: ${linkState.connected ? 'connected' : 'disconnected'}`
      + ` · frames ${linkState.frameFps.toFixed(1)} fps · YOLO ${yoloText} ms · RTT ${roundTripText} ms`;

    const homeText = homeGps
      ? `Home     ${Number(homeGps.latitude_deg).toFixed(6)}, ${Number(homeGps.longitude_deg).toFixed(6)}`
      : 'Home     (set when Python pairs)';
    elements.telemetry.textContent = formatTelemetry(telemetry) + '\n' + homeText;

    const commandLines = linkState.recentCommands.map((c) => `#${c.id} ${c.kind}${c.accepted ? '' : ' (rejected)'}`);
    if (linkState.discardedCount > 0) commandLines.push(`${linkState.discardedCount} discarded (manual mode)`);
    fillList(elements.commands, commandLines);

    const isFresh = performance.now() / 1000 - linkState.detectionsAtSec < DETECTION_STALE_SEC;
    const detectionList = isFresh && linkState.detections ? linkState.detections.detections || [] : [];
    const detectionLines = detectionList.slice(0, MAX_RECENT_ITEMS).map((d) => `${d.class_name} ${Number(d.confidence).toFixed(2)}`
      + (d.track_id !== null && d.track_id !== undefined ? ` #${d.track_id}` : ''));
    fillList(elements.detections, detectionLines);
  }

  // ---------- 공개 API ----------
  const api = {
    HOME_LATITUDE_DEG, HOME_LONGITUDE_DEG, METERS_PER_DEG_LAT,
    FRAME_HZ, TELEMETRY_HZ, MAX_PENDING_FRAMES, JPEG_QUALITY, MAX_SEQ, LINK_TIMEOUT_SEC,
    BATTERY_FULL_SEC,
    MAX_HORIZONTAL_MPS, MAX_VERTICAL_MPS, MAX_VELOCITY_DURATION_SEC,
    GIMBAL_PITCH_MIN_DEG, GIMBAL_PITCH_MAX_DEG, GIMBAL_YAW_MIN_DEG, GIMBAL_YAW_MAX_DEG, DEFAULT_GIMBAL_PITCH_DEG,
    GOTO_SPEED_MPS, CLIMB_SPEED_MPS, LAND_SPEED_MPS, GOAL_REACHED_HORIZONTAL_M, GOAL_REACHED_VERTICAL_M,
    YAW_RATE_DPS, GIMBAL_RATE_DPS, HEART_DURATION_SEC, MAX_RECENT_ITEMS, DETECTION_STALE_SEC, MAX_PENDING_EVENTS,
    wrapYawDeg, nedToGps, parseBatteryStartPct, batteryPctAt,
    createAutopilot, createLink,
    startHeart, drawOverlay, renderPanel,
  };

  root.SimLink = api;
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
})(typeof window !== 'undefined' ? window : globalThis);

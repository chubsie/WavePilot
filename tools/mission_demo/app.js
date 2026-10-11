import { KeyboardInput, GloveInput, AutoPilotInput } from "./input.js";
import { AnalysisEngine } from "./analysis.js";
import { DroneWorld } from "./world.js";
import { TestFlight, COURSE } from './test-flight.js';
import { terrainHeight } from './mountain-course.js';
import { createYoloLog, createYoloLogView, formatDetectionLabel, getObjectCategory, scaleBox } from './yolo-log.js';
import { createDecisionLog, createLlmDecisionView, formatAiBadge } from './llm-decisions.js';

// TODO: 임시 — 장갑 수동(IMU)·산악 코스에서 비행 키를 누르는 동안은 키보드가 우선한다. 장갑 검증 끝나면 false로.
const ALLOW_KEYBOARD_OVERRIDE = true;

const $ = (id) => document.getElementById(id);
const startedAt = performance.now();
let batteryStartedAt = performance.now();  // AI 임무를 켤 때마다 다시 채운다 — 탭을 오래 열어 두면 0% 로 시작해 즉시 강제 복귀했다
let mode = "keyboard";
let lastFrame = performance.now();
let lastTracks = [];
let blockedUntil = 0;
let auxHeld = false, keyFlash = false;

const world = new DroneWorld($("viewport"), (status) => $("asset-status").textContent = status, $("fpv-view"));
const keyboard = new KeyboardInput(() => { if (mode === "auto" || mode === "ai") setMode("keyboard"); });
const glove = new GloveInput({
  onStatus: ({ connected, kind, error }) => {
    $("connect-ble").classList.toggle("connected", connected && kind === "ble");
    $("connect-ble").textContent = connected && kind === "ble" ? "PAIRED" : "PAIRING";
    $('connect-ble').disabled = connected && kind === 'usb';
    for (const id of ['zero-imu','gyro-imu','info-imu','flex-flat','flex-bent']) $(id).disabled = !connected;
    connectionNotice(error || '');
    if (!connected && mode === "imu") setMode("keyboard");
  },
  onModeToggle: () => toggleMainMode(),
  onAuxAction: (pressed) => { auxHeld = pressed; },
  onLog: (line) => { const log=$('glove-log'); log.textContent=(log.textContent+'\n'+line).split('\n').slice(-80).join('\n'); log.scrollTop=log.scrollHeight; },
});
const autopilot = new AutoPilotInput();
const analysis = new AnalysisEngine(() => {});  // 정답 트랙 fallback 만 쓴다 — Scene report 섹션은 없앴다
const testFlight=new TestFlight();

// ---------- Python 링크 (sim_link.js) ----------
// FPV 캔버스(world.fpvCanvas)를 JPEG 로 올리고, 응답의 detections·pipeline 을 패널에 보여준다.
// AI 모드에서만 Python 비행 명령을 받아 pyAutopilot 이 실행한다(로컬 'auto' 관찰 모드는 받지 않는다).
const SimLink = window.SimLink;
const RAD_TO_DEG = 180 / Math.PI;
const HEART_PULSE_MS = 600;  // fire_heart 한 발 = 기존 하트 빔을 잠깐 켠다
const DEFAULT_AI_GIMBAL_PITCH_DEG = -20;
const nowSec = () => performance.now() / 1000;
let heartPulseUntilMs = 0;
const YOLO_PANEL_REFRESH_MS = 1000 / 30;
const YOLO_FPS_WINDOW_MS = 1000;
const yoloFrameTimes = [];  // 로그에 새로 들어온 YOLO 프레임 시각 — 실측 fps 용
const yoloLog = createYoloLog();
const yoloView = createYoloLogView($('yolo-log'));
const decisionLog = createDecisionLog();
const decisionView = createLlmDecisionView($('decision-list'));

const pyAutopilot = SimLink ? SimLink.createAutopilot({
  onHeart: (trackId) => {
    heartPulseUntilMs = performance.now() + HEART_PULSE_MS;
    pyAutopilot.recordEvent({ kind: 'heart_shot', track_id: trackId, time_sec: nowSec(), actor_id: null });
  },
}) : null;

// 좌표: north=-z, east=x, down=-y, yaw = 북 기준 시계방향 deg (getSimTelemetry 와 같은 규약)
function getPose() {
  const flight = world.telemetry(), position = flight.position;
  return {
    northM: -position.z, eastM: position.x, downM: -position.y,
    yawDeg: SimLink.wrapYawDeg(flight.heading), onGround: Boolean(flight.onGround),
  };
}

// AI 모드 입력 — Python 명령을 따르는 속도·기수·짐벌. 링크 스크립트가 없으면 제자리 정지
function getAiInput(delta) {
  if (!pyAutopilot) {
    return { source: 'python', velNorthMps: 0, velEastMps: 0, velDownMps: 0, yawDeg: world.telemetry().heading, gimbalPitchDeg: DEFAULT_AI_GIMBAL_PITCH_DEG };
  }
  const command = pyAutopilot.step(delta, nowSec(), getPose());
  return { source: 'python', ...command, gimbalPitchDeg: pyAutopilot.gimbal.pitchDeg };
}

// 좌표: world x→east, -z→north, y→up(down=-y). 카메라 기울기는 짐벌(-90..0°)로, 남는 기수 들림만 body pitch 로 보낸다
function getSimTelemetry() {
  const flight = world.telemetry(), position = flight.position;
  const northM = -position.z, eastM = position.x, downM = -position.y;
  const isPythonFlying = mode === 'ai' && pyAutopilot;
  const cameraPitchDeg = world.camera.rotation.x * RAD_TO_DEG;
  const gimbalPitchDeg = isPythonFlying ? pyAutopilot.gimbal.pitchDeg : Math.min(0, Math.max(-90, cameraPitchDeg));
  const groundM = world.training ? terrainHeight(position.x, position.z) : 0;
  const timeSec = nowSec();
  const isMoving = flight.speed > .5 || Math.abs(flight.climb) > .5;
  const manualFlightMode = flight.onGround ? 'grounded' : isMoving ? 'moving' : 'hover';

  return {
    time_sec: timeSec,
    north_m: northM, east_m: eastM, down_m: downM,
    roll_deg: -world.camera.rotation.z * RAD_TO_DEG,
    pitch_deg: cameraPitchDeg - gimbalPitchDeg,
    yaw_deg: SimLink.wrapYawDeg(flight.heading),
    gimbal_pitch_deg: gimbalPitchDeg,
    gimbal_yaw_deg: isPythonFlying ? pyAutopilot.gimbal.yawDeg : 0,
    battery_pct: SimLink.batteryPctAt(100, (performance.now() - batteryStartedAt) / 1000),
    flight_mode: isPythonFlying ? pyAutopilot.flightMode : manualFlightMode,
    gps: SimLink.nedToGps(northM, eastM, downM),
    height_above_ground_m: SimLink.heightAboveGroundM(position.y, groundM),
  };
}

const link = SimLink && world.fpvCanvas ? SimLink.createLink({
  canvas: world.fpvCanvas,
  getTelemetry: getSimTelemetry,
  isAiMode: () => mode === 'ai',
  onCommand: (command) => pyAutopilot.apply(command, nowSec()),
  takeEvents: () => pyAutopilot.takeEvents(),
  // 업로드 상한을 렌더 속도로 열어 두고 in-flight 2 장 제한을 역압으로 쓴다 — YOLO 는 자기 처리 속도 그대로 돈다
  frameHz: 60, telemetryHz: 60,  // 결과 폴링도 같이 열어 FPV 박스가 YOLO 속도대로 갱신되게
  log: (line) => console.info('[sim-link]', line),
}) : null;
link?.start();

function getFreshDetections() {
  if (!link || !link.state.detections || mode === 'test') return null;
  const ageSec = performance.now() / 1000 - link.state.detectionsAtSec;
  return ageSec < SimLink.DETECTION_STALE_SEC ? link.state.detections : null;
}

function startTest(){
  if(world.loading)return;
  world.setTraining(true);
  autopilot.reset();blockedUntil=0;testFlight.start();setMode('test');
}

function setMode(next) {
  if(mode==='test' && next!=='test'){
    world.setTraining(false);
  }
  if (next !== mode) autopilot.resetObservation();
  if (next === 'ai' && mode !== 'ai') batteryStartedAt = performance.now();
  if (pyAutopilot && next === 'ai' && mode !== 'ai') pyAutopilot.enterAi(getPose());  // 명령이 올 때까지 제자리 유지
  if (pyAutopilot && next !== 'ai' && mode === 'ai') pyAutopilot.cancel();          // 남은 명령이 나중에 되살아나지 않게
  mode = next;
  world.testCourse.visible=mode==='test';
  $('test-hud').hidden=mode!=='test';
  for(const id of ['normal-tab','participate-tab']){
    const selected=(id==='participate-tab')===(mode==='test');
    $(id).setAttribute('aria-selected',String(selected));$(id).tabIndex=selected?0:-1;
  }
  $('mode-screen').setAttribute('aria-labelledby',mode==='test'?'participate-tab':'normal-tab');
  document.querySelector('.compact-flex').hidden = ['ai','auto'].includes(mode);
  for (const id of ['flex-bent', 'flex-flat']) $(id).hidden = ['ai','auto'].includes(mode);
  renderFlightModeBadge();
  $('flight-mode-badge').classList.toggle('automatic',mode==='ai'||mode==='auto');
  $('observation-toggle').textContent=mode==='auto'?'Manual control':'Start observation';
  $('main-mode-toggle').textContent=mode==='ai'?'Mode: AI (Python)':'Mode: Manual';
  $('main-mode-toggle').classList.toggle('active',mode==='ai');
  document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("flight-hint").textContent = '';
}

// AI 배지는 Python 이 지금 돌리는 rule mode 를 보여 준다 — 링크 상태가 바뀌므로 패널 갱신 때도 다시 그린다
function renderFlightModeBadge() {
  const text = mode === 'ai' ? formatAiBadge(link?.state.mission) : mode === 'auto' ? 'AUTO · OBSERVE' : 'MANUAL';
  if ($('flight-mode-text').textContent !== text) $('flight-mode-text').textContent = text;
}

function toggleMainMode() { if(mode!=='test')setMode(mode === 'ai' ? (glove.link?'imu':'keyboard') : 'ai'); }

function connectionNotice(message) {
  $('glove-status').textContent=message;
  $('glove-status').hidden=!message;
}


// 정답 트랙(fallback)으로 FPV 박스를 그린다
function renderTrackBoxes(visibleTracks) {
  const overlay = $("detection-overlay");
  const visibleIds = new Set();
  visibleTracks.forEach((track) => {
    const screen = world.project(track.position);
    if (!screen.visible) return;
    visibleIds.add(track.id);
    const box = upsertTargetBox(overlay, track.id);
    box.dataset.kind = getObjectCategory(track.kind);
    box.style.cssText = `left:${screen.x-screen.width/2}px;top:${screen.y-screen.height/2}px;width:${screen.width}px;height:${screen.height}px`;
    box.querySelector(".target-label").textContent = formatDetectionLabel({ class_name: track.kind, confidence: track.confidence / 100 });
    box.querySelector(".target-distance").textContent = "";
  });
  overlay.querySelectorAll(".target-box").forEach((box) => { if (!visibleIds.has(box.dataset.track)) box.remove(); });
}

function upsertTargetBox(overlay, key) {
  let box = overlay.querySelector(`[data-track="${key}"]`);
  if (!box) {
    box = document.createElement("div"); box.className = "target-box"; box.dataset.track = key;
    box.innerHTML = `<span class="target-label"></span><span class="target-distance"></span>`; overlay.appendChild(box);
  }
  return box;
}

// YOLO 결과로 FPV 박스를 그린다. 서버 문자열은 textContent 로만
function renderYoloBoxes(frame) {
  const detections = frame.detections || [];
  const overlay = $("detection-overlay"), view = $("fpv-view");
  const boxKeys = new Set();
  detections.forEach((detection, index) => {
    const key = `yolo-${index}`, box = upsertTargetBox(overlay, key);
    const rect = scaleBox(detection.xyxy, frame.image_width, frame.image_height, view.clientWidth, view.clientHeight);
    boxKeys.add(key);
    box.dataset.kind = getObjectCategory(detection.class_name);
    box.style.cssText = `left:${rect.left}px;top:${rect.top}px;width:${rect.width}px;height:${rect.height}px`;
    box.querySelector(".target-label").textContent = formatDetectionLabel(detection);
    box.querySelector(".target-distance").textContent = "";
  });
  overlay.querySelectorAll(".target-box").forEach((box) => { if (!boxKeys.has(box.dataset.track)) box.remove(); });
}

function updateYoloPanel() {
  if (!link) return;
  if (mode !== 'test') {
    for (const frame of link.state.detectionLog) {
      if (yoloLog.addFrame(frame, frame.latency_ms, performance.now())) yoloFrameTimes.push(performance.now());
    }
  }
  while (yoloFrameTimes.length && performance.now() - yoloFrameTimes[0] > YOLO_FPS_WINDOW_MS) yoloFrameTimes.shift();
  $("yolo-fps").textContent = String(yoloFrameTimes.length);
  const pipeline = link.state.pipeline || {};
  yoloLog.addState(pipeline.state, performance.now());
  yoloLog.addReport(pipeline.report, performance.now());
  yoloLog.prune(performance.now());
  yoloView.render(yoloLog.entries);
  updateDecisionPanel(pipeline);
}

function updateDecisionPanel(pipeline) {
  decisionLog.addSelection(pipeline.selection, performance.now());
  decisionLog.addModeEnd(pipeline.mode_end, performance.now());
  decisionLog.prune(performance.now());
  decisionView.render(decisionLog.entries);
  const runningMode = link.state.mission?.running_mode;
  $('decision-running-mode').textContent = runningMode ? String(runningMode).toUpperCase() : '–';
  renderFlightModeBadge();
}

function updateTelemetry() {
  const telemetry = world.telemetry();
  $("altitude").textContent = telemetry.altitude.toFixed(1);
  $("speed").textContent = telemetry.speed.toFixed(1);
  $("heading").textContent = String(Math.round(telemetry.heading)).padStart(3,"0");
  $("coord-n").textContent = (-telemetry.position.z).toFixed(1).padStart(5,"0");
  $("coord-e").textContent = telemetry.position.x.toFixed(1).padStart(5,"0");
  const m = glove.mapped();
  const bend=glove.flexReady()?Math.round(glove.flexBend()*100):0;
  for(const [id,value] of [['roll',m.rollDegrees.toFixed(1)+'°'],['pitch',m.pitchDegrees.toFixed(1)+'°'],['yaw',m.yawDegrees.toFixed(0)+'°'],['rate',m.yawRate.toFixed(0)+'°/s'],['adc',glove.flex0.raw],['bend',glove.flexReady()?bend+'%':'UNCAL']]) $('sensor-'+id).textContent=glove.last?value:'—';
  $('sensor-hz').textContent=`${glove.hz} Hz`;
  $('flat-value').textContent=glove.flex0.flat ?? '—';
  $('bent-value').textContent=glove.flex0.bent ?? '—';
  $('flex-calibration').textContent = glove.flexReady() ? 'Flat 0% → Bent 100% · Level hover at 50%' : 'Save FLAT with finger straight, then BENT with finger bent';
  $('bend-fill').style.height = `${bend}%`;
  $('flex-gauge').setAttribute('aria-valuenow',String(bend));
  $('flight-details').textContent = `Thrust ${Math.round(telemetry.throttle*100)}%${telemetry.autoHover ? ' · Auto hover' : ''}${telemetry.onGround ? ' · Grounded' : ''}\nClimb ${telemetry.climb.toFixed(1)} m/s · ${Math.hypot(telemetry.position.x,telemetry.position.z-54).toFixed(0)} m from home\nMotors FL/FR/RL/RR ${telemetry.motors.map(value=>Math.round(value*100)+'%').join(' / ')}`;
  return telemetry;
}

function drawSensorChart() {
  if (!$('advanced-controls').open) return;
  const canvas = $('sensor-chart'), ctx = canvas.getContext('2d');
  const width = canvas.clientWidth, height = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
  if (canvas.width !== width*dpr || canvas.height !== height*dpr) { canvas.width=width*dpr; canvas.height=height*dpr; }
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height);
  ctx.strokeStyle='#364036'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(0,height/2); ctx.lineTo(width,height/2); ctx.stroke();
  [[0,90,'#f0674d'],[1,90,'#c9ef58'],[2,270,'#79bbc0']].forEach(([index,range,color])=>{
    ctx.strokeStyle=color; ctx.lineWidth=1.5; ctx.beginPath();
    glove.history.forEach((sample,i)=>{
      const x=i/499*width, y=height/2-Math.max(-1,Math.min(1,sample[index]/range))*(height/2-4);
      if(i) ctx.lineTo(x,y); else ctx.moveTo(x,y);
    }); ctx.stroke();
  });
}

function frame(now) {
  const delta = Math.min((now-lastFrame)/1000, .05); lastFrame = now;
  const telemetry = world.telemetry();
  lastTracks = mode==='test'?[]:analysis.tracks((now-startedAt)/1000);
  world.syncTargets(lastTracks);
  const visibleTracks = lastTracks.filter(t => world.canObserve(t.position));
  // AI/AUTO keep their original path; keys there already hand control back via onTakeover.
  const keyboardOverride = ALLOW_KEYBOARD_OVERRIDE && keyboard.isActive() && (mode === 'imu' || mode === 'test');
  // AUTO 자율 접근은 3D 위치가 필요해 YOLO 박스가 아니라 정답 트랙(visibleTracks)을 계속 쓴다
  const input = keyboardOverride ? keyboard.sample()
    : mode==='test'?(glove.link?glove.sample():keyboard.sample()):mode === "auto" ? autopilot.sample(telemetry.position, world.heading, visibleTracks, now)
    : mode === 'ai' ? getAiInput(delta) : mode === "imu" ? glove.sample() : keyboard.sample();
  world.flashHeld = keyFlash || now < heartPulseUntilMs || (auxHeld && glove.sample().source !== 'imu-stale');
  if (world.update(input, delta)) {
    blockedUntil = now + 600;
    if (mode === 'auto') autopilot.notifyBlocked();
  }
  if (mode === 'auto') {
    const messages = {PATROL:'Patrol', APPROACH:'Approaching target', OBSERVE:'Holding observation', BLOCKED:'Obstacle · Stopped', LOST:'Target lost · Stopped'};
    $('flight-hint').textContent = `${messages[autopilot.status]} ${autopilot.targetId || ''} · Key input resumes manual`;
  }
  $("obstacle-warning").classList.toggle("show", now < blockedUntil);
  glove.advanceFrame(); // Main also updates flex after this frame's physics.
  updateTelemetry();
  const yoloFrame = getFreshDetections();
  if (yoloFrame) renderYoloBoxes(yoloFrame); else renderTrackBoxes(lastTracks.filter(t => world.canObserve(t.position)));
  // 10 Hz 업로드 스로틀은 sim_link 가 한다. 끊겨 있으면 JPEG 인코딩도 하지 않는다(텔레메트리 POST 가 연결을 되살린다)
  if (link?.state.connected) link.afterRender(delta);
  drawSensorChart();
  if(mode==='test'){
    testFlight.update(world.camera.position.toArray(),now);
    const seconds=testFlight.elapsed;
    const time=`${String(Math.floor(seconds/60)).padStart(2,'0')}:${(seconds%60).toFixed(1).padStart(4,'0')}`;
    $('test-state').textContent=testFlight.state==='FLYING'?`NEXT RING ${testFlight.index+1} / 10`:testFlight.state==='READY'?'PASS RING 1 TO START':testFlight.state;
    $('test-score').textContent=`${testFlight.passed.size} / ${COURSE.length}`;
    $('test-time').textContent=time;
    $('test-result').textContent=testFlight.state==='COMPLETE'?`SUCCESS · ${COURSE.length} rings in ${time}`:'';
    for(const ring of world.testCourse.children){
      const color=testFlight.passed.has(ring.userData.index)?0x627346:ring.userData.index===testFlight.index?0xc9ef58:0xe7b94d;
      ring.material.color.setHex(color);ring.material.emissive.setHex(color);
    }
  }
  requestAnimationFrame(frame);
}

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.mode === 'imu' && !glove.link) connectGlove('usb');
  else setMode(button.dataset.mode);
}));
let connecting = false;
const settings = $('advanced-controls');
function closeSettings() { settings.open = false; settings.querySelector('summary').focus({preventScroll:true}); }
$('close-settings').addEventListener('click', closeSettings);
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && settings.open) closeSettings();
});
document.addEventListener('pointerdown', event => {
  if (settings.open && !settings.contains(event.target)) settings.open = false;
});
async function connectGlove(kind) {
  if (connecting) return;
  if (glove.link?.kind === kind) { await glove.disconnect(); return; }
  connecting = true;
  connectionNotice('');
  try { await glove.connect(kind); if(mode!=='test')setMode('imu'); }
  catch (error) {
    connectionNotice(error.name === 'NotFoundError' ? '' : `Connection failed: ${error.message}`);
  } finally { connecting = false; }
}
$('observation-toggle').addEventListener('click',()=>setMode(mode==='auto'?(glove.link?'imu':'keyboard'):'auto'));
 $('main-mode-toggle').addEventListener('click',toggleMainMode);
$('flight-mode-badge').addEventListener('click',toggleMainMode);  // 우상단 MANUAL 배지를 눌러 Manual / AI 전환
$("connect-ble").addEventListener("click", () => connectGlove('ble'));
$("flex-flat").addEventListener("click", () => { $("flight-hint").textContent = glove.calibrateFlex("flat") ? "FLAT saved" : "Waiting for flex samples"; });
$("flex-bent").addEventListener("click", () => { $("flight-hint").textContent = glove.calibrateFlex("bent") ? "BENT saved" : "Waiting for flex samples"; });
$("zero-imu").addEventListener("click", () => glove.send("c"));
for(const [id,command] of [['gyro-imu','g'],['info-imu','i']]) $(id).addEventListener('click',()=>glove.send(command).catch(error=>{$('glove-log').textContent=error.message;}));
for(const id of ['invRoll','invPitch','invYaw','swapRP']) {
  const element = $(id);
  element.checked=Boolean(glove.axes[id]);
  element.addEventListener('change',()=>{glove.axes[id]=element.checked;try{localStorage.setItem('gloveVizOpts.v2',JSON.stringify(glove.axes));}catch{}});
}
window.addEventListener('keydown',event=>{
  if(event.target.matches('input,textarea,select') || event.repeat) return;
  if(event.code==='KeyM') toggleMainMode();
  if(event.code==='KeyL') keyFlash=true;
});
window.addEventListener('keyup',event=>{if(event.code==='KeyL') keyFlash=false;});
window.addEventListener('blur',()=>{keyFlash=false;});
setInterval(()=>{glove.hz=glove.packetCount;glove.packetCount=0;},1000);
setInterval(updateYoloPanel, YOLO_PANEL_REFRESH_MS);
$("reset-flight").addEventListener("click", () => {
  if(mode==='test'){startTest();return;}
  world.reset(); autopilot.reset(); blockedUntil = 0;
  if (mode === 'ai' && pyAutopilot) pyAutopilot.enterAi(getPose());  // 리셋 위치에서 다시 제자리 유지
});
$('normal-tab').addEventListener('click',()=>{if(mode==='test')setMode(glove.link?'imu':'keyboard');});
$('participate-tab').addEventListener('click',()=>{if(mode!=='test')startTest();});
document.querySelector('.experience-tabs').addEventListener('keydown',event=>{
  if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return;
  event.preventDefault();
  const id=event.key==='Home'?'normal-tab':event.key==='End'?'participate-tab':event.target.id==='normal-tab'?'participate-tab':'normal-tab';
  $(id).click();$(id).focus();
});
$('test-restart').addEventListener('click',startTest);

world.setQuality("cinematic");
await world.loadEnvironment();
requestAnimationFrame(frame);

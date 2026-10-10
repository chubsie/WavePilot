import { KeyboardInput, GloveInput, AutoPilotInput } from "./input.js";
import { AnalysisEngine } from "./analysis.js";
import { DroneWorld } from "./world.js";

const $ = (id) => document.getElementById(id);
const startedAt = performance.now();
let mode = "keyboard";
let lastFrame = performance.now();
let lastTracks = [];
let initialReportRequested = false;
let quality = localStorage.getItem("wavepilot-quality") || "cinematic";
let blockedUntil = 0;

const world = new DroneWorld($("viewport"), (status) => $("asset-status").textContent = status);
const keyboard = new KeyboardInput(() => { if (mode === "auto") setMode("keyboard"); });
const glove = new GloveInput({
  onStatus: ({ connected, kind, error }) => {
    $("connect-imu").classList.toggle("connected", connected && kind === "usb");
    $("connect-ble").classList.toggle("connected", connected && kind === "ble");
    $("connect-imu").textContent = connected && kind === "usb" ? "USB 해제" : "USB 연결";
    $("connect-ble").textContent = connected && kind === "ble" ? "Bluetooth 해제" : "Bluetooth 연결";
    $('glove-status').textContent = error || (connected ? `${kind.toUpperCase()} 장갑 연결됨` : '장갑 미연결 · USB 또는 Bluetooth 연결을 선택하세요');
    if (!connected && mode === "imu") setMode("keyboard");
  },
  onModeToggle: () => setMode(mode === "auto" ? "imu" : "auto"),
  onAuxAction: (pressed) => { if (pressed) $("flight-hint").textContent = "장갑 보조 버튼 입력 감지"; },
});
const autopilot = new AutoPilotInput();
const analysis = new AnalysisEngine(renderReportState);

function setMode(next) {
  if (next !== mode) autopilot.resetObservation();
  mode = next;
  document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("flight-hint").textContent = mode === "keyboard" ? "WASD 이동 · Q/E 회전 · R/F 고도" : mode === "auto" ? "자동 경로 비행 중 · 키 입력 시 수동 복귀" : "장갑 기울기 조종 · 입력 손실 시 정지";
}

function setQuality(next) {
  quality = next;
  localStorage.setItem("wavepilot-quality", quality);
  document.body.classList.toggle("performance", quality === "performance");
  document.querySelectorAll("[data-quality]").forEach((button) => button.classList.toggle("active", button.dataset.quality === quality));
  world.setQuality(quality);
}

function renderTracks(tracks, visibleTracks) {
  $("track-count").textContent = String(tracks.length).padStart(2, "0");
  const list = $("detection-list");
  const trackKey = tracks.map(track => track.id).join(",");
  if (list.dataset.tracks !== trackKey) {
    list.dataset.tracks = trackKey;
    list.innerHTML = tracks.map((track, index) => `
    <article class="detection-card" style="animation-delay:${index*55}ms">
      <span class="detection-id">${track.id}</span>
      <div class="detection-main"><b>${track.kind}</b><span>${track.zone} · ${track.movement}</span></div>
      <div class="detection-side"><b>${track.confidence}</b><small>CONFIDENCE</small></div>
    </article>`).join("");
  }
  tracks.forEach((track, index) => {
    list.children[index].querySelector(".detection-side b").textContent = track.confidence;
  });

  const overlay = $("detection-overlay");
  const visibleIds = new Set();
  visibleTracks.forEach((track) => {
    const screen = world.project(track.position);
    if (!screen.visible) return;
    visibleIds.add(track.id);
    let box = overlay.querySelector(`[data-track="${track.id}"]`);
    if (!box) {
      box = document.createElement("div"); box.className = "target-box"; box.dataset.track = track.id;
      box.innerHTML = `<span class="target-label"></span><span class="target-distance"></span>`; overlay.appendChild(box);
    }
    box.style.cssText = `left:${screen.x-screen.width/2}px;top:${screen.y-screen.height/2}px;width:${screen.width}px;height:${screen.height}px`;
    box.querySelector(".target-label").textContent = `${track.id} / ${track.kind} ${track.confidence}`;
    box.querySelector(".target-distance").textContent = `${screen.distance.toFixed(1)} M`;
  });
  overlay.querySelectorAll(".target-box").forEach((box) => { if (!visibleIds.has(box.dataset.track)) box.remove(); });
}

function renderReportState(state) {
  if (state.status === "loading") {
    $("llm-status").textContent = "ANALYZING";
    $("request-analysis").disabled = true;
    $("analysis-cooldown").textContent = "WORKING";
    return;
  }
  $("llm-status").textContent = "READY";
  $("request-analysis").disabled = false;
  $("analysis-cooldown").textContent = "08S LOCK";
  $("report-time").textContent = `${state.generatedAt.toLocaleTimeString("ko-KR", { hour12:false })} 생성`;
  $("report-state").textContent = `STATE / ${Math.round(state.stateTime)}MS`;
  $("report-content").innerHTML = state.lines.map((line, index) => `<div class="report-line" style="animation-delay:${index*85}ms"><span class="cite">${line.ids.join("+")}</span><span>${line.text}</span></div>`).join("");
}

function updateTelemetry() {
  const telemetry = world.telemetry();
  $("altitude").textContent = telemetry.altitude.toFixed(1);
  $("speed").textContent = telemetry.speed.toFixed(1);
  $("heading").textContent = String(Math.round(telemetry.heading)).padStart(3,"0");
  $("coord-n").textContent = (-telemetry.position.z).toFixed(1).padStart(5,"0");
  $("coord-e").textContent = telemetry.position.x.toFixed(1).padStart(5,"0");
  return telemetry;
}

function frame(now) {
  const delta = Math.min((now-lastFrame)/1000, .05); lastFrame = now;
  const telemetry = world.telemetry();
  lastTracks = analysis.tracks((now-startedAt)/1000);
  world.syncTargets(lastTracks);
  const visibleTracks = lastTracks.filter(t => world.canObserve(t.position));
  const input = mode === "auto" ? autopilot.sample(telemetry.position, world.heading, visibleTracks, now) : mode === "imu" ? glove.sample() : keyboard.sample();
  if (world.update(input, delta)) {
    blockedUntil = now + 600;
    if (mode === 'auto') autopilot.notifyBlocked();
  }
  if (mode === 'auto') {
    const messages = {PATROL:'자동 순찰', APPROACH:'식별 대상 접근', OBSERVE:'거리 유지 · 관측', BLOCKED:'장애물 · 접근 중단', LOST:'대상 놓침 · 정지'};
    $('flight-hint').textContent = `${messages[autopilot.status]} ${autopilot.targetId || ''} · 키 입력 시 수동 복귀`;
  }
  $("obstacle-warning").classList.toggle("show", now < blockedUntil);
  renderTracks(lastTracks, visibleTracks); updateTelemetry();
  const seconds = Math.floor((now-startedAt)/1000);
  $("mission-clock").textContent = new Date(seconds*1000).toISOString().slice(11,19);
  if (!initialReportRequested && seconds >= 2) { initialReportRequested = true; analysis.requestReport(lastTracks, true); }
  requestAnimationFrame(frame);
}

document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => {
  if (button.dataset.mode === 'imu' && !glove.link) connectGlove('usb');
  else setMode(button.dataset.mode);
}));
document.querySelectorAll("[data-quality]").forEach((button) => button.addEventListener("click", () => setQuality(button.dataset.quality)));
let connecting = false;
async function connectGlove(kind) {
  if (connecting) return;
  if (glove.link?.kind === kind) { await glove.disconnect(); return; }
  connecting = true;
  $('glove-status').textContent = `${kind === 'usb' ? 'USB' : 'Bluetooth'} 기기 선택창에서 장갑을 선택하세요`;
  try { await glove.connect(kind); setMode('imu'); }
  catch (error) {
    $('glove-status').textContent = error.name === 'NotFoundError' ? '기기 선택이 취소됐습니다. 연결 버튼을 다시 눌러주세요.' : `연결 실패: ${error.message}`;
  } finally { connecting = false; }
}
$("connect-imu").addEventListener("click", () => connectGlove('usb'));
$("connect-ble").addEventListener("click", () => connectGlove('ble'));
$("flex-flat").addEventListener("click", () => { $("flight-hint").textContent = glove.calibrateFlex("flat") ? "Flex 평평한 상태 저장" : "Flex 샘플을 먼저 수집하세요"; });
$("flex-bent").addEventListener("click", () => { $("flight-hint").textContent = glove.calibrateFlex("bent") ? "Flex 굽힌 상태 저장" : "Flex 샘플을 먼저 수집하세요"; });
$("zero-imu").addEventListener("click", () => glove.send("c"));
$("reset-flight").addEventListener("click", () => { world.reset(); autopilot.reset(); blockedUntil = 0; });
$("request-analysis").addEventListener("click", () => analysis.requestReport(lastTracks, true));

setQuality(quality);
await world.loadEnvironment();
requestAnimationFrame(frame);

import { KeyboardInput, SerialImuInput, AutoPilotInput } from "./input.js";
import { AnalysisEngine } from "./analysis.js";
import { DroneWorld } from "./world.js";

const $ = (id) => document.getElementById(id);
const startedAt = performance.now();
let scenario = "urban";
let mode = "keyboard";
let lastFrame = performance.now();
let lastTracks = [];
let initialReportRequested = false;
let quality = localStorage.getItem("wavepilot-quality") || "cinematic";

const world = new DroneWorld($("viewport"), (status) => $("asset-status").textContent = status);
const keyboard = new KeyboardInput(() => { if (mode === "auto") setMode("keyboard"); });
const imu = new SerialImuInput((status) => {
  $("connect-imu").textContent = status === "connected" ? "장갑 연결됨" : "장갑 연결";
  if (status !== "connected" && mode === "imu") setMode("keyboard");
});
const autopilot = new AutoPilotInput();
const analysis = new AnalysisEngine(renderReportState);

function setMode(next) {
  mode = next;
  document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("flight-hint").textContent = mode === "keyboard" ? "WASD 이동 · Q/E 회전 · R/F 고도" : mode === "auto" ? "자동 경로 비행 중 · 키 입력 시 수동 복귀" : "장갑 기울기 조종 · 입력 손실 시 정지";
}

async function setScenario(next) {
  scenario = next;
  $("scenario-urban").classList.toggle("active", next === "urban");
  $("scenario-forest").classList.toggle("active", next === "forest");
  $("scenario-urban").setAttribute("aria-pressed", String(next === "urban"));
  $("scenario-forest").setAttribute("aria-pressed", String(next === "forest"));
  $("scenario-code").textContent = next === "urban" ? "URBAN_RECON" : "FOREST_RECON";
  $("cinematic-effects").className = `cinematic-effects ${next}`;
  analysis.setScenario(next); autopilot.setScenario(next); initialReportRequested = false;
  $("report-content").innerHTML = '<div class="empty-report"><span>⌁</span><p>시나리오 상태를 수집하고 있습니다.</p></div>';
  $("detection-overlay").replaceChildren();
  await world.setScenario(next);
}

function setQuality(next) {
  quality = next;
  localStorage.setItem("wavepilot-quality", quality);
  document.body.classList.toggle("performance", quality === "performance");
  document.querySelectorAll("[data-quality]").forEach((button) => button.classList.toggle("active", button.dataset.quality === quality));
  world.setQuality(quality);
}

function renderTracks(tracks) {
  $("track-count").textContent = String(tracks.length).padStart(2, "0");
  $("detection-list").innerHTML = tracks.map((track, index) => `
    <article class="detection-card" style="animation-delay:${index*55}ms">
      <span class="detection-id">${track.id}</span>
      <div class="detection-main"><b>${track.kind}</b><span>${track.zone} · ${track.movement}</span></div>
      <div class="detection-side"><b>${track.confidence}</b><small>CONFIDENCE</small></div>
    </article>`).join("");

  const overlay = $("detection-overlay");
  const visibleIds = new Set();
  tracks.forEach((track) => {
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
  const input = mode === "auto" ? autopilot.sample(telemetry.position, world.heading) : mode === "imu" ? imu.sample() : keyboard.sample();
  world.update(input, delta);
  lastTracks = analysis.tracks((now-startedAt)/1000);
  renderTracks(lastTracks); updateTelemetry();
  const seconds = Math.floor((now-startedAt)/1000);
  $("mission-clock").textContent = new Date(seconds*1000).toISOString().slice(11,19);
  if (!initialReportRequested && seconds >= 2) { initialReportRequested = true; analysis.requestReport(lastTracks, true); }
  requestAnimationFrame(frame);
}

$("scenario-urban").addEventListener("click", () => setScenario("urban"));
$("scenario-forest").addEventListener("click", () => setScenario("forest"));
document.querySelectorAll("[data-mode]").forEach((button) => button.addEventListener("click", () => setMode(button.dataset.mode)));
document.querySelectorAll("[data-quality]").forEach((button) => button.addEventListener("click", () => setQuality(button.dataset.quality)));
$("connect-imu").addEventListener("click", async () => { try { await imu.connect(); setMode("imu"); } catch (error) { alert(error.message); } });
$("reset-flight").addEventListener("click", () => world.reset());
$("request-analysis").addEventListener("click", () => analysis.requestReport(lastTracks, true));

setQuality(quality);
await setScenario("urban");
requestAnimationFrame(frame);

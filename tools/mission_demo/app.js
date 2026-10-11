import { KeyboardInput, GloveInput, AutoPilotInput } from "./input.js";
import { AnalysisEngine } from "./analysis.js";
import { DroneWorld } from "./world.js?v=slope-15";
import { TestFlight, COURSE } from './test-flight.js?v=slope-15';

const $ = (id) => document.getElementById(id);
const startedAt = performance.now();
let mode = "keyboard";
let lastFrame = performance.now();
let lastTracks = [];
let reportTracks = [];
let reportContext = {};
let initialReportRequested = false;
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
const analysis = new AnalysisEngine(renderReportState);
const testFlight=new TestFlight();

function startTest(){
  if(world.loading)return;
  world.setTraining(true);
  autopilot.reset();blockedUntil=0;testFlight.start();setMode('test');
  clearTimeout(analysis.timer);analysis.pending=false;initialReportRequested=false;
  $('report-content').textContent='Mountain course · Manual flight';
}

function setMode(next) {
  if(mode==='test' && next!=='test'){
    world.setTraining(false);
    clearTimeout(analysis.timer);analysis.pending=false;initialReportRequested=false;
    $('report-content').textContent='Checking camera view';
  }
  if (next !== mode) autopilot.resetObservation();
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
  $('flight-mode-text').textContent = mode==='test'?'MANUAL':mode === 'ai' ? 'AI · CIRCLE' : mode === 'auto' ? 'AUTO · OBSERVE' : 'MANUAL';
  $('flight-mode-badge').classList.toggle('automatic',mode==='ai'||mode==='auto');
  $('observation-toggle').textContent=mode==='auto'?'Manual control':'Start observation';
  $('main-mode-toggle').textContent=mode==='ai'?'Mode: AI (circling)':'Mode: Manual';
  $('main-mode-toggle').classList.toggle('active',mode==='ai');
  document.querySelectorAll("[data-mode]").forEach((button) => button.classList.toggle("active", button.dataset.mode === mode));
  $("flight-hint").textContent = '';
}

function toggleMainMode() { if(mode!=='test')setMode(mode === 'ai' ? (glove.link?'imu':'keyboard') : 'ai'); }

function connectionNotice(message) {
  $('glove-status').textContent=message;
  $('glove-status').hidden=!message;
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
      <div class="detection-main"><b>${track.clothing?`${track.clothing} · Person`:track.kind==='PERSON'?'Person':'Vehicle'}</b><span></span></div>
      <div class="detection-side"><b></b><small>DISTANCE</small></div>
    </article>`).join("");
  }
  tracks.forEach((track, index) => {
    list.children[index].querySelector(".detection-side b").textContent = `${track.distance.toFixed(1)}m`;
    list.children[index].querySelector(".detection-main span").textContent = `${track.screenSide} of frame`;
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
    box.querySelector(".target-label").textContent = `${track.id} / ${track.clothing?track.clothing.toUpperCase():track.kind} ${track.confidence}`;
    box.querySelector(".target-distance").textContent = `${screen.distance.toFixed(1)} M`;
  });
  overlay.querySelectorAll(".target-box").forEach((box) => { if (!visibleIds.has(box.dataset.track)) box.remove(); });
}

function renderReportState(state) {
  if (state.status === "loading") {
    $("analysis-cooldown").textContent = "WORKING";
    return;
  }
  $("analysis-cooldown").textContent = "AUTO UPDATE";
  $("report-time").textContent = `${state.generatedAt.toLocaleTimeString("en-GB", { hour12:false })} captured`;
  $("report-state").textContent = "Camera snapshot";
  $("report-content").innerHTML = state.lines.map((line, index) => `<div class="report-line" style="animation-delay:${index*85}ms"><span class="cite">${line.ids.join("+")}</span><span>${line.text}</span></div>`).join("");
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
  const input = mode==='test'?(glove.link?glove.sample():keyboard.sample()):mode === "auto" ? autopilot.sample(telemetry.position, world.heading, visibleTracks, now)
    : mode === 'ai' ? {...glove.mapped(),source:'ai'} : mode === "imu" ? glove.sample() : keyboard.sample();
  world.flashHeld = keyFlash || (auxHeld && glove.sample().source !== 'imu-stale');
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
  reportTracks = lastTracks.filter(t=>world.canObserve(t.position)).map(track=>{
    const screen=world.project(track.position), ratio=screen.x/$('fpv-view').clientWidth;
    return {...track,distance:screen.distance,screenSide:ratio<.35?'Left':ratio>.65?'Right':'Center'};
  });
  const current=updateTelemetry();
  reportContext={altitude:current.altitude,speed:current.speed,mode:$('flight-mode-text').textContent};
  renderTracks(reportTracks, reportTracks);
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
  const seconds = Math.floor((now-startedAt)/1000);
  if (seconds >= 2) { analysis.requestReport(reportTracks, !initialReportRequested, reportContext); initialReportRequested = true; }
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
  if(event.key.toLowerCase()==='m') toggleMainMode();
  if(event.key.toLowerCase()==='l') keyFlash=true;
});
window.addEventListener('keyup',event=>{if(event.key.toLowerCase()==='l') keyFlash=false;});
window.addEventListener('blur',()=>{keyFlash=false;});
setInterval(()=>{glove.hz=glove.packetCount;glove.packetCount=0;},1000);
$("reset-flight").addEventListener("click", () => { if(mode==='test'){startTest();return;}world.reset(); autopilot.reset(); blockedUntil = 0; });
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

import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import {
  HEART_PULSE_MS, HEART_SHOT_LABEL_MS, BURST_GAP_MS,
  extendHeartPulse, isHeartPulseOn, countBurstShot, formatHeartShot, createHeartOverlay,
} from '../tools/mission_demo/heart-overlay.js';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');
const SimLink = require('../tools/visualizer/sim_link.js');
globalThis.window = { THREE: T };
const { HeartBeam, AIMED_HEART_SPEED } = await import('../tools/mission_demo/hearts.js');
const { DroneWorld } = await import('../tools/mission_demo/world.js');

const DEMO_URL = new URL('../tools/mission_demo/', import.meta.url);
const readDemo = (name) => fs.readFileSync(new URL(name, DEMO_URL), 'utf8');

// ---------- 세션 ----------
test('session id: randomUUID when available, Math.random fallback otherwise', () => {
  assert.equal(SimLink.createSessionId({ randomUUID: () => 'uuid-1' }), 'uuid-1');

  const first = SimLink.createSessionId(null), second = SimLink.createSessionId({});
  assert.match(first, /^[0-9a-f]{8}(-[0-9a-f]{8}){3}$/);
  assert.notEqual(first, second);
  assert.ok(first.length <= 128, 'fits the server session id limit');
});

test('session_active: missing means active, only an explicit false deactivates', () => {
  assert.equal(SimLink.parseSessionActive({}), true);
  assert.equal(SimLink.parseSessionActive({ session_active: true }), true);
  assert.equal(SimLink.parseSessionActive({ session_active: false }), false);
  assert.equal(SimLink.parseSessionActive(null), true);
});

function createFakeServer(responses) {
  const requests = [];
  globalThis.location = { protocol: 'http:' };
  globalThis.fetch = async (url, init) => {
    requests.push({ url, init });
    if (url.startsWith('/sim/frame')) {
      return { status: 204, headers: { get: (name) => (name === 'X-Session-Active' ? responses.frameActive ?? null : null) } };
    }
    const body = responses.telemetry.shift() ?? {};
    return { status: 200, json: async () => body };
  };
  return requests;
}

function createTestLink(overrides = {}) {
  const executed = [];
  const blobCalls = [];
  const link = SimLink.createLink({
    canvas: { toBlob: (callback) => { blobCalls.push(1); callback({ size: 1 }); } },
    getTelemetry: () => ({ time_sec: 1 }),
    isAiMode: () => true,
    onCommand: (command) => { executed.push(command); return true; },
    takeEvents: () => [],
    log: () => {},
    sessionId: 'tab-A',
    ...overrides,
  });
  return { link, executed, blobCalls };
}

test('link sends session_id in the telemetry body and X-Session-Id on frames', async () => {
  const requests = createFakeServer({ telemetry: [{ commands: [] }] });
  const { link, blobCalls } = createTestLink();

  await link.postTelemetry();
  assert.equal(JSON.parse(requests[0].init.body).session_id, 'tab-A');
  assert.equal(link.state.sessionId, 'tab-A');
  assert.equal(link.state.sessionActive, true, 'no session_active field → active');

  link.afterRender(1);
  await new Promise((resolve) => setImmediate(resolve));
  const frameRequest = requests.find((request) => request.url.startsWith('/sim/frame'));
  assert.equal(blobCalls.length, 1);
  assert.equal(frameRequest.init.headers['X-Session-Id'], 'tab-A');
});

test('link without a sessionId option still generates one (drone_camera.html compatibility)', async () => {
  const requests = createFakeServer({ telemetry: [{}] });
  const { link } = createTestLink({ sessionId: undefined });

  await link.postTelemetry();
  const sent = JSON.parse(requests[0].init.body).session_id;
  assert.equal(typeof sent, 'string');
  assert.ok(sent.length > 0);
  assert.equal(sent, link.state.sessionId);
});

test('session_active false stops frame uploads and command execution but keeps telemetry', async () => {
  const requests = createFakeServer({ telemetry: [
    { session_active: false, commands: [{ id: 1, kind: 'land' }] },
    { session_active: false },
  ] });
  const { link, executed, blobCalls } = createTestLink();

  await link.postTelemetry();
  assert.equal(link.state.sessionActive, false);
  assert.deepEqual(executed, [], 'inactive tab never flies the drone');

  link.afterRender(1);
  assert.equal(blobCalls.length, 0, 'no JPEG upload from the inactive tab');

  await link.postTelemetry();
  assert.equal(requests.filter((request) => request.url === '/sim/telemetry').length, 2, 'telemetry continues');
});

test('a frame response with X-Session-Active false deactivates the tab immediately', async () => {
  createFakeServer({ telemetry: [], frameActive: 'false' });
  const { link, blobCalls } = createTestLink();

  link.afterRender(1);
  await new Promise((resolve) => setImmediate(resolve));
  assert.equal(link.state.sessionActive, false);

  link.afterRender(1);
  assert.equal(blobCalls.length, 1);
});

test('link notice priority: session taken > background notice > link lost; nothing before pairing', () => {
  assert.equal(SimLink.describeLinkNotice({ sessionActive: false, isPaired: true, connected: false, isBackgroundNotice: true }).kind, 'session');
  assert.match(SimLink.describeLinkNotice({ sessionActive: false }).text, /다른 탭이 드론을 제어 중/);
  assert.equal(SimLink.describeLinkNotice({ isPaired: true, connected: false, isBackgroundNotice: true }).kind, 'background');
  const lost = SimLink.describeLinkNotice({ isPaired: true, connected: false });
  assert.equal(lost.kind, 'link');
  assert.match(lost.text, /LINK LOST/);
  assert.equal(SimLink.describeLinkNotice({ isPaired: false, connected: false }), null, 'manual use without Python shows nothing');
  assert.equal(SimLink.describeLinkNotice({ isPaired: true, connected: true }), null);
});

// ---------- 하트 펄스 ----------
test('heart pulse: a 10-shot burst at 0.2 s keeps the beam on without gaps', () => {
  let untilMs = 0;
  const shotTimes = Array.from({ length: 10 }, (_, i) => 1000 + i * 200);
  for (const shotMs of shotTimes) untilMs = extendHeartPulse(untilMs, shotMs);

  const lastShotMs = shotTimes[shotTimes.length - 1];
  for (let t = shotTimes[0]; t < lastShotMs + HEART_PULSE_MS; t += 10) assert.ok(isHeartPulseOn(untilMs, t), `on at ${t}`);
  assert.equal(isHeartPulseOn(untilMs, lastShotMs + HEART_PULSE_MS), false);
});

test('heart pulse never shrinks an already longer pulse', () => {
  assert.equal(extendHeartPulse(5000, 1000), 5000);
  assert.equal(extendHeartPulse(0, 1000), 1000 + HEART_PULSE_MS);
});

test('burst counter continues on the same track and restarts after a gap or a new track', () => {
  let burst = countBurstShot(null, 48, 0);
  burst = countBurstShot(burst, 48, 200);
  burst = countBurstShot(burst, 48, 400);
  assert.equal(burst.count, 3);
  assert.equal(formatHeartShot(48, burst.count), 'FIRE ♥ T48 ×3');
  assert.equal(countBurstShot(burst, 48, 400 + BURST_GAP_MS + 1).count, 1);
  assert.equal(countBurstShot(burst, 7, 600).count, 1);
});

test('shot label outlives the 0.2 s gap between consecutive fire_heart shots', () => {
  assert.ok(HEART_SHOT_LABEL_MS > 200, 'the FIRE ♥ counter does not flicker between shots');
});

// ---------- FPV 오버레이는 YOLO 업로드 캔버스가 아니다 ----------
test('heart overlay refuses the YOLO upload canvas', () => {
  const uploadCanvas = { getContext: () => ({}) };
  assert.throws(() => createHeartOverlay({ canvas: uploadCanvas, uploadCanvas }), /YOLO upload canvas/);
  assert.doesNotThrow(() => createHeartOverlay({ canvas: { getContext: () => ({}) }, uploadCanvas }));
});

test('page wires the heart overlay to its own canvas, not world.fpvCanvas', () => {
  const html = readDemo('index.html');
  const fpvView = html.slice(html.indexOf('<div id="fpv-view">'), html.indexOf('</section>', html.indexOf('<div id="fpv-view">')));
  assert.match(fpvView, /<canvas id="heart-overlay"/);
  assert.match(html, /id="link-banner"/);
  assert.match(readDemo('app.js'), /createHeartOverlay\(\{ canvas: \$\('heart-overlay'\), uploadCanvas: world\.fpvCanvas \}\)/);
});

function renderWorld() {
  const world = Object.create(DroneWorld.prototype);
  const camera = new T.PerspectiveCamera(58.72, 16 / 9, .1, 320);
  camera.rotation.order = 'YXZ';
  camera.position.set(0, 30, 0);
  camera.rotation.set(-45 * Math.PI / 180, 0, 0, 'YXZ');
  camera.updateMatrixWorld(true);
  const scene = new T.Scene();
  const renderer = { setScissorTest() {}, setViewport() {}, render() {} };
  const heartsSeenByFpv = [];
  Object.assign(world, {
    camera, scene, renderer, heading: 0, velocity: new T.Vector3(), chaseCamera: new T.PerspectiveCamera(60, 1, .1, 320),
    drone: new T.Object3D(), rotors: [], leds: [], container: { clientWidth: 800, clientHeight: 450 },
    heartBeam: new HeartBeam(scene, { texture: new T.Texture(), random: () => .5 }),
    flashHeld: true, heartAimActive: true,
  });
  world.fpvRenderer = { render: () => heartsSeenByFpv.push(world.heartBeam.group.visible) };
  return { world, heartsSeenByFpv };
}

test('FPV render (YOLO input) hides the heart beam; the chase view still shows it', () => {
  const { world, heartsSeenByFpv } = renderWorld();
  for (let i = 0; i < 3; i++) world.renderViews(1 / 60, { source: 'python' });   // HEART_RATE 30/s → 2 프레임마다 한 발

  assert.deepEqual(heartsSeenByFpv, [false, false, false]);
  assert.equal(world.heartBeam.group.visible, true);
  assert.equal(world.drone.visible, true);
  assert.ok(world.heartBeam.hearts.some((heart) => heart.sprite.visible), 'a heart was emitted');
});

test('fire_heart beam goes down the gimbal line of sight, not the level nose', () => {
  const { world } = renderWorld();
  for (let i = 0; i < 3; i++) world.renderViews(1 / 60, { source: 'python' });

  const heart = world.heartBeam.hearts.find((item) => item.sprite.visible);
  const aim = world.camera.getWorldDirection(new T.Vector3());
  const along = heart.vel.dot(aim);
  assert.ok(aim.y < -.6, 'camera is pitched down at the target');
  assert.ok(Math.abs(along - AIMED_HEART_SPEED) < 1.5, `speed along the aim ${along}`);
  assert.ok(heart.vel.y < -AIMED_HEART_SPEED * .5, 'the heart travels downward toward the target');
});

test('chase camera tilts toward the aim while firing and back to level after', () => {
  const { world } = renderWorld();
  for (let i = 0; i < 120; i++) world.renderViews(1 / 60, { source: 'python' });
  const firingLook = world.chaseCamera.getWorldDirection(new T.Vector3());

  world.heartAimActive = false;
  world.flashHeld = false;
  for (let i = 0; i < 240; i++) world.renderViews(1 / 60, { source: 'python' });
  const levelLook = world.chaseCamera.getWorldDirection(new T.Vector3());

  assert.ok(firingLook.y < levelLook.y - .3, `looks down while firing (${firingLook.y} vs ${levelLook.y})`);
});

import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const SimLink = require('../tools/visualizer/sim_link.js');

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

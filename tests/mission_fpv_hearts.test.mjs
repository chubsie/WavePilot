// 1인칭 메인 화면의 하트 — 메인 렌더(world.camera)에 그대로 보이고, 코앞 것만 숨기거나 작게. YOLO 캡처에는 절대 없다
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');

// ---------- 헤드리스 스텁: WebGL 렌더러·DOM 대신 호출만 기록 ----------
const renderers = [];
class FakeRenderer {
  constructor(options = {}) {
    this.options = options;
    this.domElement = { className: '' };
    this.shadowMap = {};
    this.renders = [];
    renderers.push(this);
  }
  setPixelRatio() {}
  setSize() {}
  setScissorTest() {}
  setViewport() {}
  setScissor() {}
  render(root, camera) {
    const sprites = [];
    root.traverseVisible((object) => { if (object.isSprite) sprites.push(object); });
    this.renders.push({ root, camera, sprites: sprites.map((sprite) => ({ sprite, scale: sprite.scale.x })) });
  }
}

const noop = () => {};
const fake2dContext = new Proxy({}, {
  get: (target, key) => (key === 'createRadialGradient' ? () => ({ addColorStop: noop }) : noop),
  set: () => true,
});
globalThis.document = { createElement: () => ({ getContext: () => fake2dContext }) };
globalThis.window = { THREE: { ...T, WebGLRenderer: FakeRenderer }, devicePixelRatio: 1, addEventListener: noop };

const { DroneWorld, FPV_HEART_NEAR_M, FPV_HEART_SHRINK_M, FPV_HEART_MUZZLE_M } = await import('../tools/mission_demo/world.js');

function createWorld({ withFpv = true } = {}) {
  renderers.length = 0;
  const container = { clientWidth: 800, clientHeight: 450, appendChild: noop, getBoundingClientRect: () => ({ left: 0, bottom: 450 }) };
  const fpvContainer = withFpv ? { clientWidth: 320, clientHeight: 180, getBoundingClientRect: () => ({ left: 18, bottom: 228, width: 320, height: 180 }) } : null;
  const world = new DroneWorld(container, noop, fpvContainer);
  const [mainRenderer, uploadRenderer] = renderers;
  return { world, mainRenderer, uploadRenderer };
}

function renderFrames(world, count) {
  for (let i = 0; i < count; i++) world.renderViews(1 / 60, { source: 'keyboard', roll: 0, pitch: 0 });
}

const getMainRenders = (world, renderer) => renderer.renders.filter((render) => render.camera === world.camera);

// ---------- 렌더 ----------
test('hearts are drawn in the main drone-view render and in the chase inset, never in the YOLO capture', () => {
  const { world, mainRenderer, uploadRenderer } = createWorld();
  world.flashHeld = true;
  const heartsSeenByUpload = [];
  const uploadRender = uploadRenderer.render.bind(uploadRenderer);
  uploadRenderer.render = (scene, camera) => {
    heartsSeenByUpload.push(world.heartBeam.group.visible);
    uploadRender(scene, camera);
  };

  renderFrames(world, 12);

  assert.ok(heartsSeenByUpload.length === 12 && heartsSeenByUpload.every((isVisible) => isVisible === false));
  assert.ok(uploadRenderer.renders.every((render) => render.sprites.length === 0));
  const mainRenders = getMainRenders(world, mainRenderer);
  assert.equal(mainRenders.length, 12);
  assert.ok(mainRenders.at(-1).sprites.length > 3, 'a heart stream is visible from the drone');
  const chaseRenders = mainRenderer.renders.filter((render) => render.camera === world.chaseCamera);
  assert.ok(chaseRenders.at(-1).sprites.length >= mainRenders.at(-1).sprites.length, 'chase sees every heart');
  assert.equal(world.heartBeam.group.visible, true);
});

test('hearts leave the muzzle ahead of the camera so the first frame does not cover the drone view', () => {
  const { world } = createWorld();
  world.flashHeld = true;

  renderFrames(world, 2);
  const sprites = world.heartBeam.getVisibleSprites();
  assert.ok(sprites.length > 0);
  for (const sprite of sprites) assert.ok(sprite.position.distanceTo(world.camera.position) >= FPV_HEART_MUZZLE_M - 1e-9);
  assert.ok(FPV_HEART_MUZZLE_M > 1.2 && FPV_HEART_NEAR_M < 1);
});

test('close hearts are shrunk (and ones at the lens hidden) only for the drone-view render', () => {
  const { world, mainRenderer } = createWorld();
  world.flashHeld = true;
  renderFrames(world, 2);

  const close = getMainRenders(world, mainRenderer).at(-1).sprites
    .filter(({ sprite }) => sprite.position.distanceTo(world.camera.position) < FPV_HEART_SHRINK_M);
  assert.ok(close.length > 0);
  for (const { sprite, scale } of close) {
    assert.ok(scale < sprite.scale.x, 'smaller in the drone view than its real (chase) size');
  }

  // 카메라 렌즈 바로 앞 하트는 1인칭 렌더에서 빠지고, 렌더 뒤에는 다시 보인다
  const lensHeart = world.heartBeam.getVisibleSprites()[0];
  lensHeart.position.copy(world.camera.position).add(new T.Vector3(0, 0, -.3));
  const restore = world.fitHeartsToFpv();
  assert.equal(lensHeart.visible, false);
  restore();
  assert.equal(lensHeart.visible, true);
});

test('no hearts fired → nothing heart-shaped in either view', () => {
  const { world, mainRenderer, uploadRenderer } = createWorld();
  renderFrames(world, 5);

  assert.ok(mainRenderer.renders.every((render) => render.sprites.length === 0));
  assert.ok(uploadRenderer.renders.every((render) => render.sprites.length === 0));
});

test('without an FPV container there is no capture renderer and rendering is a safe no-op', () => {
  const { world } = createWorld({ withFpv: false });
  world.flashHeld = true;

  assert.equal(world.fpvRenderer, undefined);
  assert.doesNotThrow(() => renderFrames(world, 12));
  assert.doesNotThrow(() => world.fitHeartsToFpv()());
});

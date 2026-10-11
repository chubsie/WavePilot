// FPV 하트 = chase 와 같은 3D 하트 스프라이트를, YOLO 업로드 캔버스(world.fpvCanvas)가 아닌 별도 투명 캔버스에 그린다
import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');

// ---------- 헤드리스 스텁: WebGL 렌더러·DOM 대신 호출만 기록 ----------
function createFakeElement() {
  return { className: '', placedAfter: null, after(element) { element.placedAfter = this; } };
}

const renderers = [];
class FakeRenderer {
  constructor(options = {}) {
    this.options = options;
    this.domElement = createFakeElement();
    this.shadowMap = {};
    this.renders = [];
    this.clearCount = 0;
    renderers.push(this);
  }
  setPixelRatio() {}
  setSize() {}
  setClearColor(color, alpha) { this.clearAlpha = alpha; }
  setScissorTest() {}
  setViewport() {}
  clear() { this.clearCount++; }
  render(root, camera) {
    const sprites = [];
    root.traverseVisible((object) => { if (object.isSprite) sprites.push(object); });
    this.renders.push({ root, camera, rootVisible: root.visible, sprites });
  }
}

const noop = () => {};
const fake2dContext = new Proxy({}, {
  get: (target, key) => (key === 'createRadialGradient' ? () => ({ addColorStop: noop }) : noop),
  set: () => true,
});
globalThis.document = { createElement: () => ({ getContext: () => fake2dContext }) };
globalThis.window = { THREE: { ...T, WebGLRenderer: FakeRenderer }, devicePixelRatio: 1, addEventListener: noop };

const { DroneWorld } = await import('../tools/mission_demo/world.js');

function createWorld({ withFpv = true } = {}) {
  renderers.length = 0;
  const container = { clientWidth: 800, clientHeight: 450, appendChild: noop };
  const fpvContainer = withFpv ? { clientWidth: 640, clientHeight: 360, prepend: noop } : null;
  const world = new DroneWorld(container, noop, fpvContainer);
  const [mainRenderer, uploadRenderer, heartRenderer] = renderers;
  return { world, mainRenderer, uploadRenderer, heartRenderer };
}

function renderFrames(world, count) {
  for (let i = 0; i < count; i++) world.renderViews(1 / 60, { source: 'keyboard', roll: 0, pitch: 0 });
}

function getSubtree(root) {
  const objects = [];
  root.traverse((object) => objects.push(object));
  return objects;
}

// ---------- 캔버스 분리 ----------
test('FPV heart overlay is its own transparent WebGL canvas, never the YOLO upload canvas', () => {
  const { world, uploadRenderer, heartRenderer } = createWorld();

  assert.equal(renderers.length, 3, 'chase + FPV upload + FPV heart overlay');
  assert.equal(world.fpvCanvas, uploadRenderer.domElement);
  assert.equal(world.fpvHeartCanvas, heartRenderer.domElement);
  assert.notEqual(world.fpvHeartCanvas, world.fpvCanvas);
  assert.equal(heartRenderer.options.alpha, true);
  assert.equal(heartRenderer.clearAlpha, 0, 'clears to transparent so the FPV image shows through');
  assert.equal(uploadRenderer.options.preserveDrawingBuffer, true);
  assert.equal(world.fpvHeartCanvas.placedAfter, world.fpvCanvas, 'stacked right above the FPV canvas');
  assert.equal(world.fpvHeartCanvas.className, 'fpv-heart-canvas');
});

// ---------- 렌더 ----------
test('hearts are hidden for the YOLO upload render and drawn alone on the overlay with the FPV camera', () => {
  const { world, uploadRenderer, heartRenderer } = createWorld();
  world.flashHeld = true;
  const heartsSeenByUpload = [];
  const uploadRender = uploadRenderer.render.bind(uploadRenderer);
  uploadRenderer.render = (scene, camera) => {
    heartsSeenByUpload.push(world.heartBeam.group.visible);
    uploadRender(scene, camera);
  };

  renderFrames(world, 12);

  assert.ok(heartsSeenByUpload.length === 12 && heartsSeenByUpload.every((isVisible) => isVisible === false));
  assert.ok(heartRenderer.renders.length > 0, 'hearts reached the overlay');

  const subtree = getSubtree(world.heartBeam.group);
  for (const { root, camera, rootVisible, sprites } of heartRenderer.renders) {
    assert.equal(root, world.heartBeam.group, 'overlay renders the heart group, not the scene');
    assert.equal(camera, world.camera, 'same FPV camera as the YOLO image');
    assert.equal(rootVisible, true);
    assert.ok(sprites.length > 0);
    assert.ok(subtree.every((object) => object === root || object.isSprite), 'only heart sprites are in what the overlay draws');
  }
  assert.ok(!subtree.includes(world.drone), 'the drone body is not on the overlay');
  assert.equal(world.heartBeam.group.visible, true, 'chase view keeps its hearts');
});

test('hearts right at the nose are left off the overlay so they do not cover the FPV view', () => {
  const { world, heartRenderer } = createWorld();
  world.flashHeld = true;

  renderFrames(world, 2);   // 1.2 m 에서 막 나온 하트만 있다
  assert.equal(heartRenderer.renders.length, 0);

  renderFrames(world, 10);
  const lastRender = heartRenderer.renders.at(-1);
  for (const sprite of lastRender.sprites) assert.ok(sprite.position.distanceTo(world.camera.position) >= 3);
  const nearSprites = world.heartBeam.getVisibleSprites().filter((sprite) => sprite.position.distanceTo(world.camera.position) < 3);
  assert.ok(nearSprites.length > 0, 'a near heart exists and stays visible for the chase view');
});

test('no hearts → no overlay render; when the last heart fades the overlay is cleared once, then skipped', () => {
  const { world, heartRenderer } = createWorld();

  renderFrames(world, 5);
  assert.equal(heartRenderer.renders.length, 0);
  assert.equal(heartRenderer.clearCount, 0, 'never drawn → nothing to clear');

  world.flashHeld = true;
  renderFrames(world, 12);
  const drawnFrames = heartRenderer.renders.length;
  assert.ok(drawnFrames > 0);

  world.flashHeld = false;
  renderFrames(world, 240);   // HEART_LIFE 1.8 s 보다 길게
  assert.equal(world.heartBeam.getVisibleSprites().length, 0);
  assert.equal(heartRenderer.clearCount, 1);

  const rendersAfterFade = heartRenderer.renders.length;
  renderFrames(world, 30);
  assert.equal(heartRenderer.renders.length, rendersAfterFade);
  assert.equal(heartRenderer.clearCount, 1);
});

test('without an FPV container there is no heart overlay and rendering is a safe no-op', () => {
  const { world } = createWorld({ withFpv: false });
  world.flashHeld = true;

  assert.equal(world.fpvHeartRenderer, undefined);
  assert.doesNotThrow(() => renderFrames(world, 12));
  assert.doesNotThrow(() => world.renderFpvHearts());
});

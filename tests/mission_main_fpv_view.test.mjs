// 메인 화면 = 드론 1인칭(world.camera), 작은 창 = chase, YOLO 업로드 = 화면에 없는 오프스크린 16:9 캡처
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');

// ---------- 헤드리스 스텁: WebGL 렌더러·DOM 대신 렌더 순간의 상태만 기록 ----------
const renderers = [];
class FakeRenderer {
  constructor(options = {}) {
    this.options = options;
    this.domElement = { className: '' };
    this.shadowMap = {};
    this.renders = [];
    this.scissorTest = false;
    renderers.push(this);
  }
  setPixelRatio() {}
  setSize() {}
  setViewport(x, y, width, height) { this.viewport = [x, y, width, height]; }
  setScissor(x, y, width, height) { this.scissor = [x, y, width, height]; }
  setScissorTest(isOn) { this.scissorTest = isOn; }
  render(scene, camera) {
    const world = currentWorld;
    const sprites = [];
    scene.traverseVisible((object) => { if (object.isSprite) sprites.push(object); });
    this.renders.push({
      camera, aspect: camera.aspect, fov: camera.fov, scissorTest: this.scissorTest,
      scissor: this.scissor, viewport: this.viewport,
      isDroneVisible: world.drone.visible, isHeartGroupVisible: world.heartBeam.group.visible, sprites,
    });
  }
}

const noop = () => {};
const fake2dContext = new Proxy({}, {
  get: (target, key) => (key === 'createRadialGradient' ? () => ({ addColorStop: noop }) : noop),
  set: () => true,
});
globalThis.document = { createElement: () => ({ getContext: () => fake2dContext }) };
globalThis.window = { THREE: { ...T, WebGLRenderer: FakeRenderer }, devicePixelRatio: 1, addEventListener: noop };

const { DroneWorld, CAPTURE_WIDTH, CAPTURE_HEIGHT } = await import('../tools/mission_demo/world.js');
const { mapCaptureBoxToView, mapCapturePointToView } = await import('../tools/mission_demo/yolo-log.js');

let currentWorld = null;
const MAIN = { width: 1000, height: 600 };
const INSET = { left: 18, top: 48, width: 310, height: 174.375 };

function createWorld({ withInset = true } = {}) {
  renderers.length = 0;
  const appended = [];
  const container = {
    clientWidth: MAIN.width, clientHeight: MAIN.height, appendChild: (element) => appended.push(element),
    getBoundingClientRect: () => ({ left: 0, top: 0, bottom: MAIN.height, width: MAIN.width, height: MAIN.height }),
  };
  // prepend/append 가 없다 — 캡처 캔버스를 인셋에 붙이려 하면 바로 터진다
  const fpvContainer = withInset ? {
    clientWidth: INSET.width, clientHeight: INSET.height,
    getBoundingClientRect: () => ({ ...INSET, bottom: INSET.top + INSET.height }),
  } : null;
  const world = new DroneWorld(container, noop, fpvContainer);
  currentWorld = world;
  const [mainRenderer, captureRenderer] = renderers;
  return { world, mainRenderer, captureRenderer, appended };
}

function renderFrames(world, count, input = { source: 'keyboard', roll: 0, pitch: 0 }) {
  for (let i = 0; i < count; i++) world.renderViews(1 / 60, input);
}

// ---------- 렌더러 구성 ----------
test('only the main canvas is on the page; the YOLO upload canvas is an offscreen 640x360 capture', () => {
  const { world, mainRenderer, captureRenderer, appended } = createWorld();

  assert.equal(renderers.length, 2, 'main + offscreen capture — no separate FPV heart overlay renderer');
  assert.deepEqual(appended, [mainRenderer.domElement]);
  assert.equal(world.fpvCanvas, captureRenderer.domElement);
  assert.equal(captureRenderer.options.preserveDrawingBuffer, true);
  assert.equal(world.fpvHeartRenderer, undefined);
  assert.deepEqual([CAPTURE_WIDTH, CAPTURE_HEIGHT], [640, 360]);
});

// ---------- 렌더 ----------
test('main view renders world.camera full-frame at the viewport aspect with the drone hidden', () => {
  const { world, mainRenderer } = createWorld();
  renderFrames(world, 1);

  const mainRender = mainRenderer.renders[0];
  assert.equal(mainRender.camera, world.camera);
  assert.equal(mainRender.scissorTest, false);
  assert.deepEqual(mainRender.viewport, [0, 0, MAIN.width, MAIN.height]);
  assert.equal(mainRender.aspect, MAIN.width / MAIN.height);
  assert.equal(mainRender.fov, 58.72, 'same vertical FOV as the Python DEFAULT_CAMERA');
  assert.equal(mainRender.isDroneVisible, false, 'no self-occlusion');
});

test('the inset is the chase camera in a scissor rect at #fpv-view, with the drone visible', () => {
  const { world, mainRenderer } = createWorld();
  renderFrames(world, 1);

  const insetRender = mainRenderer.renders[1];
  const insetRect = [INSET.left, MAIN.height - (INSET.top + INSET.height), INSET.width, INSET.height];
  assert.equal(mainRenderer.renders.length, 2);
  assert.equal(insetRender.camera, world.chaseCamera);
  assert.equal(insetRender.scissorTest, true);
  assert.deepEqual(insetRender.scissor, insetRect);
  assert.deepEqual(insetRender.viewport, insetRect);
  assert.equal(insetRender.isDroneVisible, true);
  assert.equal(world.chaseCamera.aspect, INSET.width / INSET.height);
  assert.equal(mainRenderer.scissorTest, false, 'scissor is switched off again for the next frame');
});

test('the YOLO capture uses the same camera at 16:9 with hearts and drone hidden', () => {
  const { world, captureRenderer } = createWorld();
  world.flashHeld = true;
  renderFrames(world, 12);

  assert.equal(captureRenderer.renders.length, 12);
  for (const render of captureRenderer.renders) {
    assert.equal(render.camera, world.camera);
    assert.equal(render.aspect, CAPTURE_WIDTH / CAPTURE_HEIGHT);
    assert.equal(render.isHeartGroupVisible, false, 'hearts must never reach YOLO');
    assert.equal(render.isDroneVisible, false);
    assert.equal(render.sprites.length, 0);
  }
  assert.equal(world.camera.aspect, CAPTURE_WIDTH / CAPTURE_HEIGHT, 'camera rests at the capture aspect for project/unproject');
  assert.equal(world.heartBeam.group.visible, true);
  assert.equal(world.drone.visible, true);
});

test('hearts are visible in the main (drone view) render for manual and aimed fire', () => {
  for (const aim of [{ heartAimActive: false }, { heartAimActive: true }]) {
    const { world, mainRenderer } = createWorld();
    Object.assign(world, aim, { flashHeld: true });
    renderFrames(world, 12, { source: aim.heartAimActive ? 'python' : 'keyboard', roll: 0, pitch: 0, gimbalPitchDeg: -20 });

    const mainRenders = mainRenderer.renders.filter((render) => render.camera === world.camera);
    assert.ok(mainRenders.every((render) => render.isHeartGroupVisible), 'heart group is on during the drone-view render');
    assert.ok(mainRenders.at(-1).sprites.length > 3, `a stream of hearts is drawn (${mainRenders.at(-1).sprites.length})`);
  }
});

// ---------- 좌표 ----------
test('capture → main view mapping scales about the centre by the height ratio', () => {
  assert.deepEqual(mapCapturePointToView(320, 180, 640, 360, 1000, 600), { x: 500, y: 300 });
  assert.deepEqual(mapCapturePointToView(0, 0, 640, 360, 1280, 720), { x: 0, y: 0 }, 'same 16:9 → plain scale');

  // 4:3 메인: 세로 600/360 배, 캡처 좌우는 화면 밖으로 나간다(잘라 내지 않음)
  const left = mapCapturePointToView(0, 360, 640, 360, 800, 600);
  assert.ok(Math.abs(left.x - (-320 * 600 / 360 + 400)) < 1e-9 && left.x < 0);
  assert.equal(left.y, 600);

  // 21:9 처럼 넓은 메인: 캡처는 가운데 띠에 들어온다
  const box = mapCaptureBoxToView([160, 90, 480, 270], 640, 360, 1400, 600);
  const scale = 600 / 360;
  assert.ok(Math.abs(box.left - ((160 - 320) * scale + 700)) < 1e-9);
  assert.ok(Math.abs(box.top - 150) < 1e-9);
  assert.ok(Math.abs(box.width - 320 * scale) < 1e-9);
  assert.ok(Math.abs(box.height - 300) < 1e-9);
});

test('project() puts the boresight at the main view centre and agrees with the YOLO box mapping', () => {
  const { world } = createWorld();
  renderFrames(world, 1);
  const forward = world.camera.getWorldDirection(new T.Vector3());
  const ahead = world.camera.position.clone().addScaledVector(forward, 20);
  const right = new T.Vector3(1, 0, 0).applyQuaternion(world.camera.quaternion);
  const offAxis = ahead.clone().addScaledVector(right, 4);

  const centre = world.project(ahead.toArray());
  assert.ok(Math.abs(centre.x - MAIN.width / 2) < 1e-6 && Math.abs(centre.y - MAIN.height / 2) < 1e-6);

  const projected = offAxis.clone().project(world.camera);
  const capturePixel = [(projected.x * .5 + .5) * CAPTURE_WIDTH, (-projected.y * .5 + .5) * CAPTURE_HEIGHT];
  const expected = mapCapturePointToView(...capturePixel, CAPTURE_WIDTH, CAPTURE_HEIGHT, MAIN.width, MAIN.height);
  const screen = world.project(offAxis.toArray());
  assert.ok(Math.abs(screen.x - expected.x) < 1e-6 && Math.abs(screen.y - expected.y) < 1e-6);
  assert.ok(screen.x > MAIN.width / 2);
});

// ---------- 페이지 ----------
const readDemo = (name) => readFileSync(new URL(`../tools/mission_demo/${name}`, import.meta.url), 'utf8');

test('page: boxes, reticle and heart text sit over the main view; the inset is labelled CHASE CAMERA', () => {
  const html = readDemo('index.html');
  const insetStart = html.indexOf('<section class="fpv-inset"');
  const inset = html.slice(insetStart, html.indexOf('</section>', insetStart));

  assert.match(inset, /CHASE CAMERA/);
  assert.match(inset, /<div id="fpv-view"><\/div>/, 'the inset holds nothing but the chase scissor area');
  for (const overlay of ['id="detection-overlay"', 'id="heart-overlay"', 'class="reticle"']) {
    const index = html.indexOf(overlay);
    assert.ok(index > html.indexOf('id="viewport"') && index < insetStart, `${overlay} is over the main view`);
  }
  assert.match(readDemo('app.js'), /mapCaptureBoxToView\(detection\.xyxy, frame\.image_width, frame\.image_height, view\.clientWidth, view\.clientHeight\)/);
  assert.match(readDemo('app.js'), /view = \$\("viewport"\)/);
});

test('without an inset container there is no capture renderer and the main view is still the drone camera', () => {
  const { world, mainRenderer } = createWorld({ withInset: false });
  world.flashHeld = true;

  assert.equal(renderers.length, 1);
  assert.equal(world.fpvCanvas, undefined);
  assert.doesNotThrow(() => renderFrames(world, 3));
  assert.ok(mainRenderer.renders.every((render) => render.camera === world.camera));
});

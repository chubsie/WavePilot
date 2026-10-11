import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import {
  HEART_TARGET_MAX_AGE_SEC, HEART_FLIGHT_MIN_SEC, HEART_FLIGHT_MAX_SEC,
  findTargetBox, getBodyAimPixel, pixelToNdc, getHeartTarget, getHeartFlightSec,
} from '../tools/mission_demo/heart-overlay.js';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');
globalThis.window = { THREE: T };
const { HeartBeam, HEART_SPEED, HEART_LIFE, AIMED_HEART_SPEED } = await import('../tools/mission_demo/hearts.js');
const { DroneWorld } = await import('../tools/mission_demo/world.js');

const person = (trackId, xyxy) => ({ class_name: 'person', confidence: .9, track_id: trackId, xyxy });
const frame = (timestampSec, detections) => ({ timestamp_sec: timestampSec, image_width: 640, image_height: 360, detections });

// ---------- 순수 함수 ----------
test('pixel → NDC: corners and centre of a 640×360 image', () => {
  assert.deepEqual(pixelToNdc(0, 0, 640, 360), { x: -1, y: 1 });
  assert.deepEqual(pixelToNdc(640, 360, 640, 360), { x: 1, y: -1 });
  assert.deepEqual(pixelToNdc(320, 180, 640, 360), { x: 0, y: 0 });
  assert.deepEqual(pixelToNdc(480, 90, 640, 360), { x: .5, y: .5 });
});

test('body aim point is the box centre column, 40% down from the top', () => {
  assert.deepEqual(getBodyAimPixel([100, 50, 140, 150]), { x: 120, y: 90 });
});

test('target box: newest frame within 0.5 s that has the fired track_id', () => {
  const frames = [
    frame(9.7, [person(48, [0, 0, 10, 10])]),
    frame(9.9, [person(48, [100, 50, 140, 150]), person(7, [300, 0, 320, 40])]),
    frame(9.95, [person(7, [300, 0, 320, 40])]),   // 더 최근이지만 48 이 없다
  ];
  const box = findTargetBox(frames, 48, 10);
  assert.deepEqual(box, { xyxy: [100, 50, 140, 150], imageWidth: 640, imageHeight: 360, timestampSec: 9.9 });
});

test('target box: stale, missing-timestamp, other-track and empty inputs give null', () => {
  assert.equal(findTargetBox([frame(10 - HEART_TARGET_MAX_AGE_SEC - .01, [person(48, [0, 0, 10, 10])])], 48, 10), null);
  assert.equal(findTargetBox([{ image_width: 640, image_height: 360, detections: [person(48, [0, 0, 10, 10])] }], 48, 10), null);
  assert.equal(findTargetBox([frame(10, [person(7, [0, 0, 10, 10])])], 48, 10), null);
  assert.equal(findTargetBox([frame(10, [person(48, [0, 0, 10])])], 48, 10), null, 'malformed xyxy');
  assert.equal(findTargetBox([{ ...frame(10, [person(48, [0, 0, 10, 10])]), image_width: 0 }], 48, 10), null);
  assert.equal(findTargetBox([null, frame(10, undefined)], 48, 10), null);
  assert.equal(findTargetBox([], 48, 10), null);
});

test('heart target: NDC for the camera ray; null means reticle', () => {
  const target = getHeartTarget([frame(9.9, [person(48, [100, 50, 140, 150])])], 48, 10);
  assert.deepEqual(target.ndc, pixelToNdc(120, 90, 640, 360));
  assert.equal(getHeartTarget([], 48, 10), null);
});

test('flight time reaches the target in 0.5–0.8 s', () => {
  assert.equal(getHeartFlightSec(1, AIMED_HEART_SPEED), HEART_FLIGHT_MIN_SEC, 'near targets still get a visible flight');
  assert.equal(getHeartFlightSec(100, AIMED_HEART_SPEED), HEART_FLIGHT_MAX_SEC, 'far targets are reached quickly');
  assert.ok(Math.abs(getHeartFlightSec(9.8, AIMED_HEART_SPEED) - .7) < 1e-9);
});

// ---------- 월드: 모든 모드가 FPV 시선으로 쏜다 ----------
function aimWorld({ heartAimActive, heartTargetNdc = null }) {
  const world = Object.create(DroneWorld.prototype);
  const camera = new T.PerspectiveCamera(58.72, 16 / 9, .1, 320);
  camera.rotation.order = 'YXZ';
  camera.position.set(0, 20, 0);
  camera.rotation.set(-30 * Math.PI / 180, 0, 0, 'YXZ');
  camera.updateMatrixWorld(true);
  const scene = new T.Scene();
  const fpvHeartVisibility = [];
  Object.assign(world, {
    camera, scene, renderer: { setScissorTest() {}, setViewport() {}, render() {} },
    heading: 0, velocity: new T.Vector3(), chaseCamera: new T.PerspectiveCamera(60, 1, .1, 320),
    drone: new T.Object3D(), rotors: [], leds: [], container: { clientWidth: 800, clientHeight: 450 },
    heartBeam: new HeartBeam(scene, { texture: new T.Texture(), random: () => .5 }),
    flashHeld: true, heartAimActive, heartTargetNdc,
  });
  world.fpvRenderer = { render: () => fpvHeartVisibility.push(world.heartBeam.group.visible) };
  for (let i = 0; i < 3; i++) world.renderViews(1 / 60, { source: 'keyboard', roll: 0, pitch: 0 });
  const heart = world.heartBeam.hearts.find((item) => item.sprite.visible);
  return { world, heart, fpvHeartVisibility };
}

test('manual hearts (key L / glove) go down the FPV line of sight at main speed, not over heads', () => {
  const { world, heart } = aimWorld({ heartAimActive: false });
  const reticle = world.camera.getWorldDirection(new T.Vector3());
  const direction = heart.vel.clone().normalize();

  assert.ok(direction.angleTo(reticle) < .01, `heart follows the reticle (${direction.angleTo(reticle)} rad)`);
  assert.ok(heart.vel.y < 0, 'drone above people → hearts head down, not level');
  assert.ok(Math.abs(heart.vel.length() - HEART_SPEED) < 1e-6);
  assert.equal(heart.life, HEART_LIFE);
  assert.equal(heart.scale, 1);
});

test('fire_heart with a target box shoots along the unprojected box ray and dies at the target', () => {
  const ndc = { x: .3, y: -.2 };
  const { world, heart } = aimWorld({ heartAimActive: true, heartTargetNdc: ndc });
  const expected = new T.Vector3(ndc.x, ndc.y, .5).unproject(world.camera).sub(world.camera.position).normalize();
  const direction = heart.vel.clone().normalize();

  assert.ok(direction.angleTo(expected) < .01, `heart follows the box ray (${direction.angleTo(expected)} rad)`);
  const groundDistance = -world.camera.position.y / expected.y;
  const reachSec = groundDistance / heart.vel.length();
  assert.ok(reachSec >= HEART_FLIGHT_MIN_SEC - 1e-6 && reachSec <= HEART_FLIGHT_MAX_SEC + 1e-6, `reaches in ${reachSec}s`);
  assert.ok(Math.abs(heart.life - reachSec) < 1e-6, 'heart fades out at the target');

  const projected = heart.sprite.position.clone().addScaledVector(heart.vel, heart.life - heart.age).project(world.camera);
  assert.ok(Math.abs(projected.x - ndc.x) < .02 && Math.abs(projected.y - ndc.y) < .02, 'lands on the box point in the FPV image');
});

test('fire_heart without a box falls back to the reticle at aimed speed', () => {
  const { world, heart } = aimWorld({ heartAimActive: true });
  const reticle = world.camera.getWorldDirection(new T.Vector3());

  assert.ok(heart.vel.clone().normalize().angleTo(reticle) < .01);
  assert.ok(Math.abs(heart.vel.dot(reticle) - AIMED_HEART_SPEED) < 1e-6);
});

test('heart sprites stay hidden from the FPV (YOLO input) render in every aim mode', () => {
  for (const options of [{ heartAimActive: false }, { heartAimActive: true }, { heartAimActive: true, heartTargetNdc: { x: 0, y: -.3 } }]) {
    const { world, fpvHeartVisibility } = aimWorld(options);
    assert.deepEqual(fpvHeartVisibility, [false, false, false]);
    assert.equal(world.heartBeam.group.visible, true);
  }
});

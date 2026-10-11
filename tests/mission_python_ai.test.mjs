import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');
const SimLink = require('../tools/visualizer/sim_link.js');
globalThis.window = { THREE: T };
const { DroneWorld, getVelocityTiltDeg } = await import('../tools/mission_demo/world.js');

// ---------- world: source 'python' ----------
function flightWorld() {
  const world = Object.create(DroneWorld.prototype);
  Object.assign(world, { camera: new T.PerspectiveCamera(68, 1, .1, 320), velocity: new T.Vector3(),
    heading: 0, colliders: [], loading: false, renderer: { render() {} }, scene: new T.Scene(),
    container: { clientWidth: 1000, clientHeight: 1000 }, targetMeshes: new Map() });
  world.reset();
  return world;
}

const pythonInput = (extra = {}) => ({ source: 'python', velNorthMps: 0, velEastMps: 0, velDownMps: 0, yawDeg: 0, gimbalPitchDeg: -20, ...extra });

// app.js getPose 와 같은 규약: north=-z, east=x, down=-y, yaw = 북 기준 시계방향
const poseOf = (world) => ({
  northM: -world.camera.position.z, eastM: world.camera.position.x, downM: -world.camera.position.y,
  yawDeg: SimLink.wrapYawDeg(world.telemetry().heading), onGround: Boolean(world.onGround),
});

test('python source lerps velocity toward the NED command in scene axes', () => {
  const world = flightWorld();
  world.update(pythonInput({ velNorthMps: 4, velEastMps: 2, velDownMps: -1 }), 1 / 60);

  const lag = 3 / 60;
  assert.ok(Math.abs(world.velocity.x - 2 * lag) < 1e-9, 'east → +x');
  assert.ok(Math.abs(world.velocity.y - 1 * lag) < 1e-9, 'down -1 → climb +y');
  assert.ok(Math.abs(world.velocity.z + 4 * lag) < 1e-9, 'north → -z');

  for (let i = 0; i < 300; i++) world.update(pythonInput({ velNorthMps: 4, velEastMps: 2, velDownMps: 0 }), 1 / 60);
  assert.ok(Math.abs(world.velocity.x - 2) < 1e-3 && Math.abs(world.velocity.z + 4) < 1e-3);
});

test('python source sets heading from yaw and camera pitch from the gimbal, level roll', () => {
  const world = flightWorld();
  world.update(pythonInput({ yawDeg: 90, gimbalPitchDeg: -45 }), 1 / 60);

  assert.ok(Math.abs(world.heading + Math.PI / 2) < 1e-9);
  assert.ok(Math.abs(world.telemetry().heading - 90) < 1e-9, 'telemetry heading is the same clockwise yaw');
  assert.ok(Math.abs(world.camera.rotation.x + Math.PI / 4) < 1e-9);
  assert.equal(world.camera.rotation.z, 0);
  assert.ok(Number.isFinite(world.camera.position.x) && Number.isFinite(world.camera.position.y));
});

test('python source keeps the ground floor and building colliders', () => {
  const world = flightWorld();
  world.camera.position.set(0, 1, 0);
  for (let i = 0; i < 120; i++) world.update(pythonInput({ velDownMps: 3 }), 1 / 60);
  assert.ok(world.camera.position.y >= .7 - 1e-9);
  assert.equal(world.onGround, true);

  const walled = flightWorld();
  walled.camera.position.set(0, 10, 0);
  walled.colliders = [{ min: [-5, 0, -8], max: [5, 20, -6] }];
  for (let i = 0; i < 240; i++) walled.update(pythonInput({ velNorthMps: 6 }), 1 / 60);
  assert.ok(walled.camera.position.z > -6, 'blocked before the wall');
});

test('velocity tilt: forward is nose down, right is right side down, clamped to 10 deg', () => {
  const forward = getVelocityTiltDeg({ x: 0, z: -4 }, 0);
  assert.ok(Math.abs(forward.pitchDeg + 6) < 1e-9 && Math.abs(forward.rollDeg) < 1e-9);
  const right = getVelocityTiltDeg({ x: 2, z: 0 }, 0);
  assert.ok(Math.abs(right.rollDeg - 3) < 1e-9 && Math.abs(right.pitchDeg) < 1e-9);
  assert.equal(getVelocityTiltDeg({ x: 0, z: -40 }, 0).pitchDeg, -10);
  // 동쪽(yaw 90)을 보고 동쪽으로 가면 앞으로 가는 것
  const east = getVelocityTiltDeg({ x: 4, z: 0 }, -Math.PI / 2);
  assert.ok(Math.abs(east.pitchDeg + 6) < 1e-9 && Math.abs(east.rollDeg) < 1e-9);
});

test('autopilot go_to drives the world drone to the commanded NED point', () => {
  const world = flightWorld();
  const autopilot = SimLink.createAutopilot();
  world.update(pythonInput(), 1 / 60);
  autopilot.enterAi(poseOf(world));
  const start = poseOf(world);
  assert.equal(autopilot.apply({ id: 7, kind: 'go_to', north_m: start.northM + 10, east_m: start.eastM - 5, altitude_m: 30 }, 0), true);
  assert.equal(autopilot.apply({ id: 8, kind: 'set_gimbal', pitch_deg: -60, yaw_deg: 0 }, 0), true);

  let timeSec = 0;
  for (let i = 0; i < 60 * 15; i++) {
    timeSec += 1 / 60;
    const command = autopilot.step(1 / 60, timeSec, poseOf(world));
    world.update({ source: 'python', ...command, gimbalPitchDeg: autopilot.gimbal.pitchDeg }, 1 / 60);
  }
  const end = poseOf(world);
  assert.ok(Math.abs(end.northM - (start.northM + 10)) < 1.5, `north ${end.northM}`);
  assert.ok(Math.abs(end.eastM - (start.eastM - 5)) < 1.5, `east ${end.eastM}`);
  assert.ok(Math.abs(-end.downM - 30) < 1, `altitude ${-end.downM}`);
  assert.equal(autopilot.flightMode, 'hover');
  assert.ok(Math.abs(world.camera.rotation.x * 180 / Math.PI + 60) < 1e-6, 'camera follows gimbal');
  assert.deepEqual(autopilot.takeEvents().map((event) => event.kind), ['goal_reached']);
});

test('fire_heart reaches onHeart and the recorded heart_shot event', () => {
  const shots = [];
  const autopilot = SimLink.createAutopilot({
    onHeart: (trackId) => { shots.push(trackId); autopilot.recordEvent({ kind: 'heart_shot', track_id: trackId, time_sec: 1, actor_id: null }); },
  });
  assert.equal(autopilot.apply({ id: 1, kind: 'fire_heart', track_id: 3 }, 1), true);
  assert.equal(autopilot.apply({ id: 2, kind: 'fire_heart', track_id: 'T3' }, 1), false);
  assert.deepEqual(shots, [3]);
  assert.deepEqual(autopilot.takeEvents(), [{ kind: 'heart_shot', track_id: 3, time_sec: 1, actor_id: null }]);
});

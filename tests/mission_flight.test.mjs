import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import { createRequire } from 'node:module';
import { resolveMovement, sphereIntersectsAabb } from '../tools/mission_demo/collision.js';
import { AutoPilotInput, GloveInput } from '../tools/mission_demo/input.js';
import { MiddleButton } from '../tools/mission_demo/middle-button.js';
import { AnalysisEngine } from '../tools/mission_demo/analysis.js';
import { PEOPLE } from '../tools/mission_demo/population.js';
import { TestFlight, COURSE, NORMALS, TEST_START } from '../tools/mission_demo/test-flight.js';
import { terrainHeight } from '../tools/mission_demo/mountain-course.js';
const require = createRequire(import.meta.url);
const T = require('../tools/visualizer/three.min.js');
globalThis.window = { THREE: T };
Object.defineProperty(globalThis, 'localStorage', { configurable: true, value: { getItem() { return null; }, setItem() {} } });
const { DroneWorld } = await import('../tools/mission_demo/world.js');
const { createDrone } = await import('../tools/mission_demo/drone-model.js');


test('scene report describes only supplied visible targets and their measured distance', () => {
  const engine = new AnalysisEngine(()=>{});
  const report = engine.buildReport([{id:'T-07',kind:'VEHICLE',distance:24.3,screenSide:'Right'}],{altitude:32,speed:0,mode:'MANUAL'});
  const text = report.map(line=>line.text).join(' ');
  assert.ok(text.includes('1 vehicles'));
  assert.ok(text.includes('Right of frame · 24.3m'));
  assert.ok(text.includes('Hovering'));
  assert.ok(!report.some(line=>line.ids.includes('T-03')));
  assert.match(engine.buildReport([])[0].text,/No targets visible/);
  assert.ok(!text.includes('moving north'));
});

test('main axis mapping, gyro/info commands, logs and yaw reset remain available', async () => {
  const logs=[],sent=[];
  const glove=new GloveInput({onLog:line=>logs.push(line)});
  glove.parse('INFO,ready');
  glove.parse('G,1,15,-6,30,500,0,0');
  glove.parse('G,2,15,-6,30,500,0,0');
  assert.ok(glove.yawDegrees>0);
  glove.axes.swapRP=true;
  glove.axes.invRoll=true;
  const mapped=glove.sample();
  assert.equal(mapped.roll,.2);
  assert.equal(mapped.pitch,-.5);
  glove.link={send:async value=>sent.push(value)};
  for(const command of ['g','i','c']) await glove.send(command);
  assert.deepEqual(sent,['g','i','c']);
  assert.equal(glove.yawDegrees,0);
  assert.deepEqual(logs,['INFO,ready','Calibrating gyro · Keep the glove still']);
});

test('main view is the drone camera without self-occlusion while the inset is the chase view with the drone', () => {
  const world = flightWorld();
  const model = createDrone();
  Object.assign(world, model, {chaseCamera:new T.PerspectiveCamera(60,1,.1,320),
    fpvContainer:{clientWidth:300,clientHeight:169,getBoundingClientRect:()=>({left:680,bottom:220,width:300,height:169})}});
  world.container.getBoundingClientRect=()=>({left:0,bottom:1000});
  const renders=[];
  world.renderer={setScissorTest(){},setViewport(){},setScissor(){},clearDepth(){},
    render(scene,camera){renders.push({camera,visible:world.drone.visible});}};
  world.renderViews(1/60,{roll:.2,pitch:.3});
  assert.equal(renders.length,2);
  assert.equal(renders[0].camera,world.camera);
  assert.equal(renders[0].visible,false);
  assert.equal(renders[1].camera,world.chaseCamera);
  assert.equal(renders[1].visible,true);
  assert.ok(world.drone.position.equals(world.camera.position));
  assert.equal(model.rotors.length,4);
  const point=world.camera.position.clone().add(world.camera.getWorldDirection(new T.Vector3()).multiplyScalar(20));
  const projected=world.project(point.toArray());
  assert.ok(Math.abs(projected.x-500)<.01, 'boresight lands at the main view centre');
  assert.ok(Math.abs(projected.y-500)<.01);
});

function flightWorld(boxes = []) {
  const world = Object.create(DroneWorld.prototype);
  Object.assign(world, { camera: new T.PerspectiveCamera(68, 1, .1, 320), velocity: new T.Vector3(),
    heading: 0, colliders: boxes, loading: false, renderer: { render() {} }, scene: new T.Scene(),
    container: { clientWidth: 1000, clientHeight: 1000 }, targetMeshes: new Map() });
  world.reset();
  return world;
}

test('added people use matching scene and tracking positions and remain stationary', () => {
  const world=flightWorld();
  world.environment=new T.Group();
  const body=new T.Mesh(new T.BoxGeometry(.5,1.5,.3));
  body.name='Person_T03_Body'; body.position.set(2,1,-22);
  world.environment.add(body);
  world.addPeople();
  const engine=new AnalysisEngine(()=>{}), tracks=engine.tracks(0);
  for(const person of PEOPLE.filter(person=>!person.route)) {
    const model=world.environment.getObjectByName(`Person_${person.id}`);
    assert.ok(model.children.length>1, 'static people are procedural humanoids, not the GLB Person_T03 clone');
    assert.deepEqual(model.position.toArray(),person.feet);
    const track=tracks.find(t=>t.id===person.id);
    assert.equal(track.position[1],person.feet[1]+1.7);
    assert.deepEqual(engine.tracks(100).find(t=>t.id===person.id).position,track.position);
  }
  world.syncTargets(tracks);
  assert.equal(world.environment.getObjectByName('Person_T-11').position.y,20.66);
});

test('red and blue walkers move on clear routes with separate clothing and matching reports',()=>{
  const engine=new AnalysisEngine(()=>{}),boxes=urbanBoxes();
  for(const person of PEOPLE.filter(p=>p.route)){
    const first=engine.tracks(0).find(t=>t.id===person.id);
    const later=engine.tracks(10).find(t=>t.id===person.id);
    assert.notDeepEqual(first.position,later.position);
    assert.equal(later.clothing,person.clothing);
    for(let time=0;time<100;time+=.5){
      const t=engine.tracks(time).find(t=>t.id===person.id);
      assert.ok(!boxes.some(box=>sphereIntersectsAabb(t.position,.4,box)),`walker ${person.id} intersects a wall`);
    }
    assert.ok(engine.buildReport([later]).some(line=>line.text.includes(`in ${person.clothing.toLowerCase()} · Walking`)));
  }
});

test('IMU 50% throttle hovers; full throttle accelerates upward with main gravity and drag', () => {
  const world = flightWorld();
  const start = world.camera.position.y;
  const command = { source: 'imu', roll: 0, pitch: 0, yaw: 0, throttle: .5, flexReady: true, flex0: 1500, fly: true };
  for (let i = 0; i < 60; i++) world.update(command, 1 / 60);
  assert.ok(Math.abs(world.camera.position.y - start) < .001, 'neutral flex must hover, not climb');
  world.update({ ...command, throttle: 1 }, 1 / 60);
  assert.ok(Math.abs(world.velocity.y - 9.81 / 60) < .001);
  const first = world.velocity.y;
  world.update({ ...command, throttle: 1 }, 1 / 60);
  assert.ok(world.velocity.y > first);
});

test('IMU tilt produces throttle-dependent acceleration and deadband prevents drift', () => {
  const low = flightWorld(), high = flightWorld(), rest = flightWorld();
  const command = { source: 'imu', roll: 0, pitch: 1, yaw: 0, throttle: .5, flexReady: true, flex0: 1500, fly: true };
  low.update(command, 1 / 60);
  high.update({ ...command, throttle: 1 }, 1 / 60);
  rest.update({ ...command, pitch: .05 }, 1 / 60);
  assert.ok(low.velocity.z < 0);
  assert.ok(Math.abs(high.velocity.z / low.velocity.z - 2) < .001);
  assert.equal(rest.velocity.z, 0);
});

test('stale IMU and lost AUTO observations stop all translational movement', () => {
  const world = flightWorld();
  for (const source of ['imu-stale', 'auto']) {
    world.velocity.set(3, 4, 5);
    const before = world.camera.position.clone();
    world.update({ source, roll: 0, pitch: 0, yaw: 0, throttle: 0, brake: true }, 1 / 60);
    assert.equal(world.velocity.length(), 0);
    assert.ok(world.camera.position.equals(before));
  }
});

test('holding glove yaw at either 30-degree limit turns 180 degrees in two seconds',()=>{
  for(const [yawDegrees,direction] of [[30,1],[-30,-1],[330,-1]]){
    for(const dt of [1/60,1/20]){
      const world=flightWorld();
      const input={source:'imu',fly:true,roll:0,pitch:0,yawDegrees,throttle:.5,flexReady:true,flex0:1500};
      for(let i=0;i<Math.round(2/dt);i++)world.update(input,dt);
      assert.ok(Math.abs(world.heading+direction*Math.PI)<1e-8);
      const heading=world.heading;
      for(let i=0;i<60;i++)world.update({...input,yawDegrees:360},1/60);
      assert.ok(Math.abs(world.heading-heading)<1e-10,'returning to neutral must hold the new heading');
      world.update({...input,source:'imu-stale'},1/60);
      assert.ok(Math.abs(world.heading-heading)<1e-10,'lost packets must not continue turning');
    }
  }
});

test('small wrist movements stay neutral and intermediate angles turn more slowly',()=>{
  const world=flightWorld();
  const input={source:'imu',fly:true,roll:0,pitch:0,throttle:.5,flexReady:true,flex0:1500};
  for(const yawDegrees of [-5,0,5,355])world.update({...input,yawDegrees},1/60);
  assert.ok(Math.abs(world.heading)<1e-10);
  for(let i=0;i<60;i++)world.update({...input,yawDegrees:19},1/60);
  assert.ok(Math.abs(world.heading+Math.PI/4)<1e-8);
});

test('sequence-integrated yaw drives parked heading and parked mode coasts while holding altitude', () => {
  const world = flightWorld(), glove = new GloveInput();
  glove.axes.fly = false;
  glove.parse('G,10,50,-45,90,1500,0,0');
  glove.parse('G,20,50,-45,90,1500,0,0');
  world.velocity.set(3,4,5);
  const altitude = world.camera.position.y;
  world.update(glove.sample(),1/60);
  assert.ok(Math.abs(world.heading - -18*.35*Math.PI/180)<1e-12);
  assert.equal(world.camera.position.y,altitude);
  assert.deepEqual(world.velocity.toArray(),[2.8499999999999996,3.8,4.75]);
  const heading = world.heading;
  glove.parse('G,20,50,-45,90,1500,0,0'); // duplicate packet must not integrate rate again
  world.update(glove.sample(),1/60);
  assert.ok(Math.abs(world.heading)<18*Math.PI/180);
  assert.ok(world.heading<heading); // continues smoothing toward the same 18° yaw
  assert.equal(glove.sample().rollDegrees,50); // no physical tilt clamp at 30°
});

test('runtime collects only explicit GLB proxies, never enclosing facade bounds', async () => {
  const world = flightWorld(), asset = new T.Group();
  const facade = new T.Mesh(new T.BoxGeometry(120, 50, 120));
  facade.name = 'Ruin_VisualOnly';
  const proxy = new T.Mesh(new T.BoxGeometry(2, 20, 20));
  proxy.name = 'COLLIDER_Wall'; proxy.position.set(20, 10, 0);
  const target = new T.Mesh(new T.BoxGeometry(2, 1, 3));
  target.name = 'Vehicle_T07'; target.position.set(-1, 1, 5);
  asset.add(facade, proxy, target);
  Object.assign(world, { loadVersion: 0, loadGlb: async () => asset, setQuality() {}, onAssetStatus() {} });
  await world.loadEnvironment();
  assert.equal(world.colliders.length, 1);
  assert.equal(proxy.visible, false);
  assert.equal(facade.visible, true);
  assert.equal(world.loading, false);
  world.syncTargets([{ id: 'T-07', position: [0, 1, 7] }]);
  assert.deepEqual(target.position.toArray(), [0, 1, 7]);
  world.syncTargets([{ id: 'T-07', position: [0, 1, 7] }]);
  assert.deepEqual(target.position.toArray(), [0, 1, 7]);
});

test('visual occlusion removes a target but invisible collision proxies do not', () => {
  const world = flightWorld();
  world.camera.position.y = 12;
  world.camera.rotation.set(0,0,0);
  world.camera.updateMatrixWorld(true);
  world.environment = new T.Group();
  const wall = new T.Mesh(new T.BoxGeometry(4, 8, 1));
  wall.position.set(0, 12, 34);
  world.environment.add(wall);
  world.environment.updateMatrixWorld(true);
  assert.equal(world.canObserve([0, 12, 14]), false);
  wall.name = 'COLLIDER_Wall'; wall.visible = false;
  assert.equal(world.canObserve([0, 12, 14]), true);
  assert.equal(world.canObserve([0, 12, 80]), false);
});

test('extended main glove packet preserves flex throttle and button edge actions', () => {
  let toggles = 0;
  const aux = [], glove = new GloveInput({ onModeToggle: () => toggles++, onAuxAction: pressed => aux.push(pressed) });
  Object.assign(glove.flex0, { flat: 1000, bent: 2000 });
  glove.feed('G,1,15,-30,45,1500,0,3\nG,2,15,-30,45,2000,0,3\nG,3,0,0,0,1000,0,0\n');
  assert.equal(toggles, 1);
  assert.deepEqual(aux, []);
  assert.equal(glove.yawDegrees,0,'short middle press re-centers yaw');
  assert.equal(glove.last.throttle, 0);
  assert.equal(glove.last.flexReady, true);
  const neutral = glove.parse('G,4,15,-30,45,1500,0,0');
  assert.equal(neutral.throttle, 0);
  glove.advanceFrame();
  assert.equal(glove.sample().throttle, .15);
  assert.equal(neutral.pitch, 1);
  assert.equal(neutral.roll, .5);
  assert.equal(neutral.yaw, -.5);
  glove.last.timestamp = performance.now() - 501;
  assert.equal(glove.sample().source, 'imu-stale');
});

test('middle tap zeros once; long hold fires until release without zeroing',()=>{
  const events=[],button=new MiddleButton(()=>events.push('zero'),held=>events.push(held));
  button.update(true,0);button.update(true,100);button.update(false,180);button.update(false,200);
  assert.deepEqual(events,['zero']);events.length=0;
  button.update(true,250);button.update(true,749);assert.deepEqual(events,[]);
  button.update(true,750);button.update(true,900);button.update(false,1000);
  assert.deepEqual(events,[true,false]);
});

test('interrupted middle presses are cancelled without accidental zero or resumed beam',()=>{
  const events=[],button=new MiddleButton(()=>events.push('zero'),held=>events.push(held));
  button.update(true,0);button.update(false,700);assert.deepEqual(events,[]);
  button.update(true,800);button.update(true,1200);button.update(true,1300);
  assert.deepEqual(events,[true]);button.cancel();assert.deepEqual(events,[true,false]);
  button.update(true,1400);button.update(false,1500);assert.deepEqual(events,[true,false]);
  button.update(true,1600);button.update(false,1700);assert.deepEqual(events,[true,false,'zero']);
});

test('short middle press sends the same firmware zero command as the Zero button',()=>{
  const sent=[],glove=new GloveInput();glove.link={send:async command=>sent.push(command)};
  glove.yawDegrees=30;
  glove.parse('G,1,0,0,0,1500,0,1');glove.parse('G,2,0,0,0,1500,0,0');
  assert.deepEqual(sent,['c']);assert.equal(glove.yawDegrees,0);
});

test('AUTO approaches stable observations, holds distance, and stops on loss or obstruction', () => {
  const pilot = new AutoPilotInput(), p={x:0,y:16,z:0};
  const tracks=[{id:'T1',confidence:90,position:[0,0,-40]}];
  pilot.sample(p,0,tracks,0);
  assert.equal(pilot.targetId,null);
  let cmd=pilot.sample(p,0,tracks,1100);
  assert.equal(pilot.targetId,'T1');
  assert.ok(cmd.pitch>0);
  assert.equal(cmd.throttle,0);
  cmd=pilot.sample({x:0,y:16,z:-27},0,tracks,1200);
  assert.equal(cmd.pitch,0);
  assert.equal(cmd.brake,true);
  pilot.notifyBlocked();
  assert.equal(pilot.sample(p,0,tracks,1300).pitch,0);
  assert.equal(pilot.status,'BLOCKED');
  assert.equal(pilot.sample(p,0,[],1400).pitch,0);
  assert.equal(pilot.status,'LOST');
  pilot.sample(p,0,[],4000);
  assert.equal(pilot.status,'PATROL');
});

test('AUTO approaches and settles at 14m using actual acceleration and GLB collision', () => {
  const world = flightWorld(urbanBoxes()), pilot = new AutoPilotInput();
  const tracks = [{ id: 'T-07', confidence: 90, position: [-1, 1.2, 5] }];
  for (let i = 0; i < 3600; i++) {
    const input = pilot.sample(world.camera.position, world.heading, tracks, i * 1000 / 60);
    assert.equal(world.update(input, 1 / 60), false);
  }
  const distance = Math.hypot(world.camera.position.x + 1, world.camera.position.z - 5);
  assert.ok(distance >= 13.8 && distance <= 14.2, `distance ${distance}`);
  assert.ok(world.velocity.length() < .01);
});

test('USB and BLE device pickers are invoked directly on connect', async () => {
  const previous=Object.getOwnPropertyDescriptor(globalThis,'navigator');
  let calls=[];
  Object.defineProperty(globalThis,'navigator',{configurable:true,value:{
    serial:{requestPort(){calls.push('usb');return Promise.reject(new Error('cancelled'));}},
    bluetooth:{requestDevice(){calls.push('ble');return Promise.reject(new Error('cancelled'));}}
  }});
  try {
    const glove=new GloveInput();
    const usb=glove.connect('usb');
    assert.deepEqual(calls,['usb']);
    await assert.rejects(usb,/cancelled/);
    const ble=glove.connect('ble');
    assert.deepEqual(calls,['usb','ble']);
    await assert.rejects(ble,/cancelled/);
  } finally {
    if(previous) Object.defineProperty(globalThis,'navigator',previous);
    else delete globalThis.navigator;
  }
});

function urbanBoxes() {
  const b = fs.readFileSync(new URL('../tools/mission_demo/assets/urban.glb', import.meta.url));
  const j = JSON.parse(b.subarray(20, 20 + b.readUInt32LE(12)));
  const boxes = [];
  function visit(i, parent) {
    const n = j.nodes[i], m = new T.Matrix4();
    if (n.matrix) m.fromArray(n.matrix);
    else m.compose(new T.Vector3(...(n.translation || [0,0,0])), new T.Quaternion(...(n.rotation || [0,0,0,1])), new T.Vector3(...(n.scale || [1,1,1])));
    m.premultiply(parent);
    if (n.name?.startsWith('COLLIDER_')) {
      const a = j.accessors[j.meshes[n.mesh].primitives[0].attributes.POSITION];
      const box = new T.Box3(new T.Vector3(...a.min), new T.Vector3(...a.max)).applyMatrix4(m);
      boxes.push({min: box.min.toArray(), max: box.max.toArray()});
    }
    for (const c of n.children || []) visit(c, m);
  }
  for (const i of j.scenes[j.scene || 0].nodes) visit(i, new T.Matrix4());
  return boxes;
}

test('fast movement cannot tunnel through a wall; backing away remains possible', () => {
  const walls = [{min:[1,-2,-2],max:[2,2,2]}];
  const hit = resolveMovement([0,0,0], [10,0,1], .7, walls);
  assert.ok(hit.blocked);
  assert.ok(hit.position[0] <= .3);
  assert.ok(hit.position[2] > .9);
  const retreat = resolveMovement(hit.position, [-1,0,0], .7, walls);
  assert.equal(retreat.blocked, false);
});

test('drone keeps two metres of side clearance from building walls', () => {
  const wall = [{min:[3,0,-100],max:[4,48,100]}];
  const world = flightWorld(wall);
  world.camera.position.set(0, 12, 0);
  const input = {source:'keyboard', roll:1, pitch:0, yaw:0, throttle:0};
  for (let frame=0; frame<240; frame++) world.update(input, 1/60);
  assert.ok(world.camera.position.x <= 1.01, `wall clearance was only ${(3-world.camera.position.x).toFixed(2)}m`);
});

test('urban AUTO completes a loop without collision against actual GLB proxies and flight physics', () => {
  const boxes = urbanBoxes(), pilot = new AutoPilotInput(), world = flightWorld(boxes);
  assert.equal(boxes.length, 14);
  assert.ok(!boxes.some(box => sphereIntersectsAabb(world.camera.position.toArray(), .7, box)));
  const visited = new Set();
  for (let frame=0; frame<60*240; frame++) {
    visited.add(pilot.index);
    const input = pilot.sample(world.camera.position, world.heading, [], frame * 1000 / 60);
    assert.equal(world.update(input, 1 / 60), false, `blocked at frame ${frame}, waypoint ${pilot.index}`);
  }
  assert.equal(visited.size,pilot.route.length);
});

test('manual mountain course scores rings in order and freezes the completed timer',()=>{
  const challenge=new TestFlight();
  challenge.update(TEST_START,5000);assert.equal(challenge.elapsed,0);
  for(let i=0;i<COURSE.length;i++){
    const c=COURSE[i],n=NORMALS[i];
    challenge.previous=c.map((v,j)=>v-n[j]*2);
    challenge.update(c.map((v,j)=>v+n[j]*2),10000+i*1000);
    assert.equal(challenge.passed.size,i+1);
  }
  assert.equal(challenge.state,'COMPLETE');assert.equal(challenge.elapsed,9);
  challenge.update(TEST_START,999999);assert.equal(challenge.elapsed,9);
  challenge.start();assert.equal(challenge.index,0);assert.equal(challenge.elapsed,0);
});

test('straight training course begins 39m ahead and all ten rings are reachable without turning',()=>{
  assert.equal(TEST_START[2]-COURSE[0][2],39);
  const world=flightWorld(),challenge=new TestFlight();world.training=true;world.reset();
  world.camera.position.set(TEST_START[0],7.85,COURSE[0][2]+1);
  world.velocity.set(0,.9,-6);
  for(let i=1;i<COURSE.length;i++)assert.ok(Math.abs((COURSE[i][1]-COURSE[i-1][1])/12-.15)<1e-10);
  for(let frame=0;frame<60*45 && challenge.state!=='COMPLETE';frame++){
    assert.equal(world.update({source:'keyboard',roll:0,pitch:6/13,yaw:0,throttle:.9/8},1/60),false);
    challenge.update(world.camera.position.toArray(),frame*1000/60);
  }
  assert.equal(challenge.passed.size,10);
  assert.equal(world.heading,0);
  assert.equal(world.camera.position.x,TEST_START[0]);
});

test('mountain training stays still without input and terrain stops low flight into hills',()=>{
  const world=flightWorld();world.training=true;world.reset();
  const rest={source:'keyboard',roll:0,pitch:0,yaw:0,throttle:0};
  for(let i=0;i<120;i++)world.update(rest,1/60);
  assert.deepEqual(world.camera.position.toArray(),TEST_START);
  const hit=resolveMovement(TEST_START,[100,0,0],.7,[],2,terrainHeight);
  assert.equal(hit.blocked,true);
  assert.ok(hit.position[1]>=terrainHeight(hit.position[0],hit.position[2])+.7-1e-6);
  for(const ring of COURSE)assert.ok(ring[1]-4>terrainHeight(ring[0],ring[2]));
});

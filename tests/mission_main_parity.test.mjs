import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import { createRequire } from 'node:module';
import { GloveInput } from '../tools/mission_demo/input.js';
const require = createRequire(import.meta.url);
const THREE = require('../tools/visualizer/three.min.js');
globalThis.window = { THREE };
const storage = new Map();
Object.defineProperty(globalThis, 'localStorage', { configurable:true, value: {
  getItem:key=>storage.get(key) ?? null, setItem:(key,value)=>storage.set(key,value),
} });
const { MainFlightCore } = await import('../tools/mission_demo/flight-core.js');
const source = fs.readFileSync(new URL('../tools/visualizer/index.html', import.meta.url), 'utf8');
function section(start, end, offset = 0) {
  const first = source.indexOf(start, offset), last = source.indexOf(end, first);
  assert.ok(first >= 0 && last > first, `original main section ${start}`);
  return source.slice(first, last);
}

// Execute original main code, not a reimplementation of the expected equations.
function originalMain() {
  const context = vm.createContext({ THREE, Math, performance:{now:()=>1000},
    state:{roll:0,pitch:0,yaw:0,yawRate:0,lastSeq:null,samples:0,hz:0},
    opts:{invRoll:false,invPitch:false,invYaw:false,swapRP:false,fly:false},
    flex:{raw:null,recent:[],flat:1000,bent:2000,bend:0}, history:[],
    buttons:{middle:false,key:false}, mode:{ai:false,aiYaw:0,aiAgl:9},
    SAMPLE_DT:.02,YAW_DEADBAND:1,HISTORY:500,log(){},onButtons(){},
    terrainHeight:()=>0,drone:new THREE.Object3D(),leds:[],
    rotors:[[-1,-1,1],[1,-1,-1],[-1,1,-1],[1,1,1]].map(([x,z,dir])=>{
      const b=new THREE.Mesh(); b.position.set(x,0,z); b.userData={dir,speed:0,disc:{material:{opacity:0}}}; return b;
    }),
  });
  vm.runInContext(section('function mapped()', '// ---------- Flex sensor')
    + section('function handleLine(line)', "$('connect').onclick")
    + section('const G = 9.81;', '// Home =')
    + section('const MAX_OMEGA = 60;', '// ---------- Render loop')
    + section('const D2R = Math.PI / 180;', 'function frame()')
    + 'function flexReady(){return flex.flat!==null && flex.bent!==null && Math.abs(flex.bent-flex.flat)>=30;}\n'
    + 'globalThis.original={flight,shown,motorSpeeds};',context);
  const frame = section('  const m = mapped();', '  flight.pos.x +=', source.indexOf('function frame()'));
  const flexFrame = section('  if (flex.raw !== null && flexReady()) {', "  $('vFlexRaw')");
  vm.runInContext(`function step(dt){${frame}\n${flexFrame}}`,context);
  return { context, read:()=>context.original, packet:line=>{context.line=line;vm.runInContext('handleLine(line)',context);},
    step:dt=>{context.dt=dt;vm.runInContext('step(dt)',context);} };
}
function near(actual, expected, label) { assert.ok(Math.abs(actual-expected)<1e-10,`${label}: ${actual} != ${expected}`); }

test('same packets and frames match original yaw, mapped axes, smoothing, thrust/drag and four motor outputs', () => {
  storage.clear();
  const ref=originalMain(), glove=new GloveInput(), core=new MainFlightCore();
  const position=new THREE.Vector3(0,12,0),velocity=new THREE.Vector3();
  Object.assign(glove.flex0,{flat:1000,bent:2000});
  ref.read().flight.alt=12;
  let seq=1;
  for(let i=0;i<240;i++) {
    if(i===90) seq=0; else seq += i%11===0 ? 4 : 1;
    const line=`G,${seq},${Math.sin(i*.08)*65},${Math.cos(i*.06)*48},${i%13===0?.8:36},${1000+(i%80)*12},0,0`;
    const options={fly:i<180,swapRP:i>=60,invRoll:i>=120,invPitch:i>=150,invYaw:i>=100};
    Object.assign(glove.axes,options);Object.assign(ref.context.opts,options);
    glove.parse(line);ref.packet(line);
    const input=glove.sample(), dt=i%3===0?1/30:1/60;
    core.step(input,dt,position,velocity);glove.advanceFrame();ref.step(dt);
    const original=ref.read();
    near(input.yawDegrees,ref.context.opts.invYaw?-ref.context.state.yaw:ref.context.state.yaw,'integrated yaw');
    for(const axis of ['roll','pitch','yaw']) near(core.shown[axis],original.shown[axis],`shown ${axis}`);
    for(let axis=0;axis<3;axis++) near(velocity.getComponent(axis),original.flight.vel.getComponent(axis),`velocity ${axis}`);
    near(core.throttle,original.flight.throttle,'throttle');
    near(glove.flex0.bend,ref.context.flex.bend,'flex smoothing');
    for(let motor=0;motor<4;motor++) near(core.motorSpeeds[motor],original.motorSpeeds[motor],`motor ${motor}`);
  }
  assert.equal(glove.history.length,240);
  assert.deepEqual(glove.history.at(-1),Array.from(ref.context.history.at(-1)));
});

test('uncalibrated hover and AI circle match original main and retain entry AGL', () => {
  const ref=originalMain(),core=new MainFlightCore(),position=new THREE.Vector3(0,17,0),velocity=new THREE.Vector3();
  ref.read().flight.alt=17;ref.context.opts.fly=true;ref.context.flex.flat=null;
  const input={source:'imu',fly:true,rollDegrees:42,pitchDegrees:-38,yawDegrees:24,flexReady:false,flex0:null};
  Object.assign(ref.context.state,{roll:42,pitch:-38,yaw:24});
  for(let i=0;i<30;i++){core.step(input,1/60,position,velocity);ref.step(1/60);}
  near(core.throttle,ref.read().flight.throttle,'uncalibrated hover');
  velocity.toArray().forEach((value,axis)=>near(value,ref.read().flight.vel.getComponent(axis),'hover velocity'));
  core.enterAi(position.y,0);ref.context.mode.ai=true;ref.context.mode.aiYaw=ref.read().shown.yaw;ref.context.mode.aiAgl=17;
  for(let i=0;i<180;i++) {
    core.step({...input,source:'ai'},1/60,position,velocity);ref.step(1/60);
    near(core.shown.yaw,ref.read().shown.yaw,'AI yaw');
    near(core.shown.roll,ref.read().shown.roll,'AI bank');
    velocity.toArray().forEach((value,axis)=>near(value,ref.read().flight.vel.getComponent(axis),'AI velocity'));
  }
  assert.equal(core.aiAgl,17);
});

test('original flex calibration takes precedence and the former mission calibration migrates', () => {
  storage.clear();storage.set('wavepilot-mission-flex.v1',JSON.stringify({flat:800,bent:1800}));
  const migrated=new GloveInput();assert.equal(migrated.flex0.flat,800);
  assert.deepEqual(JSON.parse(storage.get('gloveVizFlex.v1')),{flat:800,bent:1800});
  storage.set('gloveVizFlex.v1',JSON.stringify({flat:1100,bent:2200}));
  storage.set('gloveVizOpts.v2',JSON.stringify({fly:true,invYaw:true}));
  const original=new GloveInput();assert.equal(original.flex0.flat,1100);assert.equal(original.axes.fly,true);
  assert.equal(original.flex0.bend,0);storage.clear();
});

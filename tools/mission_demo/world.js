import { resolveMovement, sphereIntersectsAabb } from "./collision.js";
import { createDrone } from "./drone-model.js";
import { MainFlightCore, D2R, DRAG_H, DRAG_V } from "./flight-core.js";
import { HeartBeam, updateStatusLeds, HEART_SPEED, AIMED_HEART_SPEED } from "./hearts.js?v=chase-main-1";
import { getHeartFlightSec } from "./heart-overlay.js";
import { PEOPLE, createStandingPerson, createWalker } from './population.js';
import { createCourse, TEST_START } from './test-flight.js?v=slope-15';
import { createMountain, terrainHeight } from './mountain-course.js?v=slope-15';
import { gloveTurnRate } from './yaw-steering.js';
import { mapCapturePointToView } from './yolo-log.js';

const THREE = window.THREE;

const URBAN = { sky: 0x768384, fog: 0x65706b, start: [0, 32, 54], heading: 0 };
const DRONE_RADIUS = .7;
const BUILDING_SIDE_CLEARANCE = 2;
// YOLO 로 올리는 오프스크린 캡처 해상도(16:9). 드론 시점 인셋과 같은 world.camera·같은 세로 FOV 로 그린다
export const CAPTURE_WIDTH = 640, CAPTURE_HEIGHT = 360;
const CAPTURE_ASPECT = CAPTURE_WIDTH / CAPTURE_HEIGHT;
// 드론 시점(인셋) 렌더의 하트: 이보다 가까우면 숨기고(카메라 관통), FPV_HEART_SHRINK_M 안쪽은 거리에 비례해 작게 그려
// 화면을 덮지 않으면서 하트 줄기는 보이게 한다. 크기 축소는 인셋 렌더 동안만 — chase(큰 화면)는 원래 크기
export const FPV_HEART_NEAR_M = .8, FPV_HEART_SHRINK_M = 4;
const FPV_HEART_MIN_SCALE = .25;
// 하트 발사 시작점(기체 중심에서 m) — 드론 시점에서 main 의 1.2 m 는 화면을 덮어 조금 앞으로
export const FPV_HEART_MUZZLE_M = 1.8;
// 정답 트랙(fallback) 박스 크기 380/거리 는 높이 169 px 인셋 기준이다 — 그리는 화면 높이에 비례해 맞춘다
const TRACK_BOX_REFERENCE_HEIGHT_PX = 169;
// Python 명령(source 'python') 추종 — drone_camera.html 과 같은 1차 지연·속도 기반 기울기
const PYTHON_VELOCITY_LAG_PER_SEC = 3;
const PYTHON_TILT_DEG_PER_MPS = 1.5, PYTHON_MAX_TILT_DEG = 10;
// fire_heart 조준 사격 중 chase 카메라가 짐벌 시선 쪽(표적)으로 고개를 숙인다 — 하트와 표적이 같이 보이게
const CHASE_AIM_LOOK_M = 10, CHASE_AIM_BLEND_PER_SEC = 4;
// 표적 박스로 쏠 때 퍼짐(m/s) — 0.5~0.8 s 비행에서 표적 몸통 안에 모이게 main(2.4)보다 좁게
const TARGET_HEART_SPREAD = .8;

// 속도 → 표시용 기체 자세(deg). 앞으로 가면 기수 숙임(-pitch), 오른쪽으로 가면 오른쪽 내림(+roll)
export function getVelocityTiltDeg(velocity, heading) {
  const clampTilt = (value) => Math.max(-PYTHON_MAX_TILT_DEG, Math.min(PYTHON_MAX_TILT_DEG, value));
  const forwardMps = -Math.sin(heading) * velocity.x - Math.cos(heading) * velocity.z;
  const rightMps = Math.cos(heading) * velocity.x - Math.sin(heading) * velocity.z;

  return { pitchDeg: -clampTilt(forwardMps * PYTHON_TILT_DEG_PER_MPS), rollDeg: clampTilt(rightMps * PYTHON_TILT_DEG_PER_MPS) };
}

export class DroneWorld {
  constructor(container, onAssetStatus = () => {}, fpvContainer = null) {
    this.container = container;
    this.fpvContainer = fpvContainer;
    this.onAssetStatus = onAssetStatus;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.65));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputEncoding = THREE.sRGBEncoding;
    container.appendChild(this.renderer.domElement);
    if (fpvContainer) {
      // YOLO 업로드 전용 오프스크린 캡처 — 화면에 붙이지 않는다(보이는 드론 시점은 메인 렌더러의 인셋 scissor).
      // toBlob 이 마지막 프레임을 읽도록 preserveDrawingBuffer
      this.fpvRenderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
      this.fpvRenderer.setPixelRatio(1);
      this.fpvRenderer.setSize(CAPTURE_WIDTH, CAPTURE_HEIGHT, false);
      this.fpvRenderer.shadowMap.enabled = true;
      this.fpvRenderer.shadowMap.type = THREE.PCFSoftShadowMap;
      this.fpvRenderer.outputEncoding = THREE.sRGBEncoding;
      this.fpvCanvas = this.fpvRenderer.domElement;
    }

    this.scene = new THREE.Scene();
    this.testCourse=createCourse(THREE);
    this.scene.add(this.testCourse);
    // FPV = 드론 시점 인셋이자 YOLO 입력. Python DEFAULT_CAMERA(수평 90°, 16:9 → 세로 58.72°)와 맞춰 표적 투영이 어긋나지 않고 사람도 덜 작게 잡힌다
    this.camera = new THREE.PerspectiveCamera(58.72, 1, 0.1, 320);
    this.camera.rotation.order = "YXZ";
    this.chaseCamera = new THREE.PerspectiveCamera(60, 1, .1, 320);
    const model = createDrone();
    this.drone = model.drone;
    this.rotors = model.rotors;
    this.leds = model.leds;
    this.flashHeld = false;
    this.heartAimActive = false;   // true = Python fire_heart 조준 사격(느리고 크게, 표적 박스가 있으면 그쪽으로)
    this.heartTargetNdc = null;    // fire_heart 표적 박스 몸통 지점(FPV NDC). null 이면 레티클(화면 중앙)
    this.scene.add(this.drone);
    this.heartBeam = new HeartBeam(this.scene);
    this.environment = null;
    this.velocity = new THREE.Vector3();
    this.heading = Math.PI;
    this.quality = "cinematic";
    this.colliders = [];
    this.targetMeshes = new Map();
    this.loadVersion = 0;
    this.loading = false;

    this.scene.add(new THREE.HemisphereLight(0xdde1c9, 0x263128, 1.2));
    const sun = new THREE.DirectionalLight(0xffefcb, 1.45);
    sun.position.set(-35, 55, 24);
    sun.castShadow = true;
    sun.shadow.mapSize.set(1024, 1024);
    sun.shadow.camera.left = sun.shadow.camera.bottom = -75;
    sun.shadow.camera.right = sun.shadow.camera.top = 75;
    this.scene.add(sun);
    window.addEventListener("resize", () => this.resize());
    this.resize();
  }

  setQuality(quality) {
    this.quality = quality;
    const cinematic = quality === "cinematic";
    this.renderer.setPixelRatio(cinematic ? Math.min(window.devicePixelRatio, 1.65) : 1);
    this.renderer.shadowMap.enabled = cinematic;
    if (this.fpvRenderer) this.fpvRenderer.shadowMap.enabled = cinematic;
    if (this.environment) {
      this.environment.traverse((object) => {
        if (object.userData.detail_high === true || object.name.startsWith("DETAIL_HIGH")) object.visible = cinematic;
      });
    }
    this.resize();
  }

  async loadEnvironment() {
    const version = ++this.loadVersion;
    this.loading = true;
    this.colliders = [];
    this.targetMeshes.clear();
    if (this.environment) this.scene.remove(this.environment);
    this.scene.background = new THREE.Color(URBAN.sky);
    this.scene.fog = new THREE.Fog(URBAN.fog, 70, 210);
    this.reset();
    this.onAssetStatus("BLENDER ASSET · LOADING");
    try {
      const environment = await this.loadGlb("assets/urban.glb");
      if (version !== this.loadVersion) return;
      this.environment = environment;
      this.environment.updateMatrixWorld(true);
      this.environment.traverse((object) => {
        for (const [id, prefix, origin] of [["T-03", "Person_T03", [2, -22]], ["T-07", "Vehicle_T07", [-1, 5]]]) {
          if (object.name.startsWith(prefix)) {
            if (!this.targetMeshes.has(id)) this.targetMeshes.set(id, []);
            this.targetMeshes.get(id).push({ object, initial: object.position.clone(), origin });
          }
        }
        if (object.isMesh) { object.castShadow = true; object.receiveShadow = true; }
        const isProxy = object.name.startsWith("COLLIDER_");
        if (isProxy) {
          const bounds = new THREE.Box3().setFromObject(object);
          this.colliders.push({ min: bounds.min.toArray(), max: bounds.max.toArray() });
          object.visible = false;
        }
      });
      if (this.colliders.some(box => sphereIntersectsAabb(URBAN.start, BUILDING_SIDE_CLEARANCE, box, DRONE_RADIUS))) {
        throw new Error("Urban asset contains an occupied spawn point");
      }
      this.scene.add(this.environment);
      this.addPeople();
      this.setQuality(this.quality);
      this.reset();
      this.loading = false;
      this.onAssetStatus("BLENDER ASSET · URBAN / READY");
    } catch (error) {
      if (version !== this.loadVersion) return;
      console.warn("GLB unavailable, using procedural fallback", error);
      this.colliders = [];
      this.targetMeshes.clear();
      this.environment = this.createFallback();
      this.scene.add(this.environment);
      this.loading = false;
      this.onAssetStatus("PROCEDURAL FALLBACK · ACTIVE");
    }
  }

  loadGlb(url) {
    return new Promise((resolve, reject) => new THREE.GLTFLoader().load(url, (gltf) => resolve(gltf.scene), undefined, reject));
  }

  createFallback() {
    const group = new THREE.Group();
    const material = (color) => new THREE.MeshStandardMaterial({ color, roughness: 0.85 });
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(180, 180), material(0x55574e));
    ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; group.add(ground);
    [[-28,-30,16,24],[-28,28,20,31],[29,-29,17,22],[31,30,23,35]].forEach(([x,z,s,h],i) => {
      const mesh = new THREE.Mesh(new THREE.BoxGeometry(s,h,s), material(i%2 ? 0x4b504a : 0x5e5a50));
      mesh.position.set(x,h/2,z); mesh.castShadow=true; group.add(mesh);
      this.colliders.push({ min: [x-s/2,0,z-s/2], max: [x+s/2,h,z+s/2] });
    });
    return group;
  }

  reset() {
    this.camera.position.set(...(this.training?TEST_START:URBAN.start));
    this.heading = URBAN.heading;
    this.velocity.set(0, 0, 0);
    this.mainFlight ??= new MainFlightCore();
    this.hoverAltitude = URBAN.start[1];
    this.lastSource = null;
    this.camera.rotation.set(-.65, this.heading, 0, "YXZ");
    this.camera.updateMatrixWorld(true);
    this.chaseReady = false;
  }

  setTraining(active){
    this.training=active;
    if(!this.mountain){this.mountain=createMountain(THREE);this.scene.add(this.mountain);}
    this.mountain.visible=active;this.environment.visible=!active;this.testCourse.visible=active;
    this.scene.background=new THREE.Color(active?0xa7bec4:URBAN.sky);
    this.scene.fog=new THREE.Fog(active?0xa7bec4:URBAN.fog,active?110:70,active?300:210);
    this.reset();
    this.onAssetStatus(active?'MOUNTAIN TRAINING':'BLENDER ASSET · URBAN / READY');
  }

  update(input, delta) {
    if (this.loading) {
      this.renderViews(0);
      return false;
    }
    const forward = new THREE.Vector3(-Math.sin(this.heading), 0, -Math.cos(this.heading));
    const right = new THREE.Vector3(Math.cos(this.heading), 0, -Math.sin(this.heading));
    if (input.source !== this.lastSource) this.hoverAltitude = this.camera.position.y;
    this.lastSource = input.source;
    // AI 모드는 Python 명령(source 'python')만 따른다 — main 의 원 선회('ai')는 이 데모에서 쓰지 않는다
    const mainControl = ['imu', 'imu-stale'].includes(input.source);
    const isPython = input.source === 'python';
    let moveAltitude = true;
    if (isPython) {
      this.followPythonCommand(input, delta);
    } else if (mainControl) {
      if((input.source==='imu' && input.fly) || input.source==='imu-stale'){
        const rate=input.source==='imu-stale'?0:gloveTurnRate(input.yawDegrees??0);
        const yawDegrees=-this.heading/D2R+rate*delta;
        input={...input,yawDegrees};
        // The rate is already integrated in time. Don't smooth an absolute
        // wrist heading toward it, which would undo a turn on recentering.
        this.mainFlight.shown.yaw=yawDegrees;
      }
      const result = this.mainFlight.step(input, delta, this.camera.position, this.velocity,this.training?terrainHeight(this.camera.position.x,this.camera.position.z):0);
      this.heading = result.heading;
      moveAltitude = result.moveAltitude;
    } else {
      const acceleration = forward.multiplyScalar(input.pitch * 13 * DRAG_H).add(right.multiplyScalar(input.roll * 10 * DRAG_H));
      this.velocity.x += (acceleration.x - DRAG_H * this.velocity.x) * delta;
      this.velocity.y += (input.throttle * 8 * DRAG_V - DRAG_V * this.velocity.y) * delta;
      this.velocity.z += (acceleration.z - DRAG_H * this.velocity.z) * delta;
    }
    if (input.source === 'imu-stale' || input.brake) this.velocity.set(0, 0, 0);
    const movement = this.velocity.clone().multiplyScalar(delta);
    if (!moveAltitude) movement.y = 0;
    const resolved = resolveMovement(this.camera.position.toArray(), movement.toArray(), DRONE_RADIUS, this.training?[]:this.colliders, BUILDING_SIDE_CLEARANCE,this.training?terrainHeight:null);
    this.camera.position.fromArray(resolved.position);
    if (resolved.blocked) {
      for (let axis = 0; axis < 3; axis++) {
        if (resolved.blockedAxes?.includes(axis)) this.velocity.setComponent(axis, 0);
      }
    }
    if (!mainControl) {
      this.mainFlight.ai = false;
      if (!isPython) this.heading += input.yaw * delta * 1.45;
    }
    const floor=this.training?terrainHeight(this.camera.position.x,this.camera.position.z)+.7:.7;
    this.onGround = this.camera.position.y <= floor;
    if (this.onGround) {
      this.velocity.x *= Math.max(0, 1 - 4 * delta);
      this.velocity.z *= Math.max(0, 1 - 4 * delta);
    }
    for (const [axis, min, max] of [[0,-68,68], [1,floor,this.training?65:48], [2,this.training?-120:-68,68]]) {
      const before = this.camera.position.getComponent(axis);
      const bounded = THREE.MathUtils.clamp(before, min, max);
      if (bounded !== before) this.velocity.setComponent(axis, 0);
      this.camera.position.setComponent(axis, bounded);
    }
    this.camera.rotation.y = this.heading;
    const overview = THREE.MathUtils.clamp((this.camera.position.y - 12) / 20, 0, 1);
    // Python 모드의 FPV 카메라는 짐벌 각을 그대로 쓰고 수평을 유지한다
    this.camera.rotation.x = isPython ? input.gimbalPitchDeg * D2R
      : mainControl ? this.mainFlight.shown.pitch * D2R : this.training?-.08-input.pitch*.05:-.35 - overview * .3 - input.pitch * 0.09;
    this.camera.rotation.z = isPython ? 0 : mainControl ? -this.mainFlight.shown.roll * D2R : -input.roll * 0.10;
    this.camera.updateMatrixWorld(true);
    this.renderViews(delta, input);
    return resolved.blocked;
  }

  // NED 속도 명령 → 장면 축(x=east, y=up, z=south). 기수는 yaw(북 기준 시계방향 deg)를 그대로 따른다
  followPythonCommand(input, delta) {
    const target = new THREE.Vector3(input.velEastMps, -input.velDownMps, -input.velNorthMps);
    this.velocity.lerp(target, Math.min(1, delta * PYTHON_VELOCITY_LAG_PER_SEC));
    this.heading = -input.yawDeg * D2R;
  }

  // 월드 점 → 드론 시점 인셋(#fpv-view) 픽셀. 카메라는 평소 캡처 종횡비(16:9)라 캡처 픽셀로 투영한 뒤 YOLO 박스와 같은 변환으로 옮긴다
  project(position) {
    const point = new THREE.Vector3(...position);
    const distance = point.distanceTo(this.camera.position);
    const projected = point.clone().project(this.camera);
    const forward = new THREE.Vector3(); this.camera.getWorldDirection(forward);
    const visible = point.clone().sub(this.camera.position).dot(forward) > 0 && Math.abs(projected.x) < 1.05 && Math.abs(projected.y) < 1.05;

    const view = this.fpvContainer || this.container;
    const viewWidth = view.clientWidth, viewHeight = view.clientHeight;
    const screen = mapCapturePointToView((projected.x*.5+.5)*CAPTURE_WIDTH, (-projected.y*.5+.5)*CAPTURE_HEIGHT,
      CAPTURE_WIDTH, CAPTURE_HEIGHT, viewWidth, viewHeight);
    const width = THREE.MathUtils.clamp(380 / distance, 18, 70) * viewHeight / TRACK_BOX_REFERENCE_HEIGHT_PX;

    return { visible, x: screen.x, y: screen.y, width, height: width*1.22, distance };
  }

  renderViews(delta, input = {roll:0,pitch:0}) {
    // Headless physics tests do not construct presentation cameras.
    if (!this.chaseCamera) { this.renderer.render(this.scene,this.camera); return; }
    this.drone.position.copy(this.camera.position);
    const mainControl = ['imu','imu-stale'].includes(input.source);
    if (input.source === 'python') {
      const tilt = getVelocityTiltDeg(this.velocity, this.heading);
      this.drone.rotation.set(tilt.pitchDeg * D2R, this.heading, -tilt.rollDeg * D2R, 'YXZ');
    } else {
      this.drone.rotation.set(mainControl ? this.mainFlight.shown.pitch * D2R : -input.pitch*.22,
        this.heading, mainControl ? -this.mainFlight.shown.roll * D2R : -input.roll*.28, 'YXZ');
    }
    this.rotors.forEach(rotor => {
      const index = (rotor.position.z < 0 ? 0 : 2) + (rotor.position.x < 0 ? 0 : 1);
      const speed = mainControl ? this.mainFlight.motorSpeeds[index] : .5;
      rotor.rotation.y += delta * 60 * speed * rotor.userData.dir;
      for (const prop of rotor.userData.blades) prop.material.opacity = 1 - .75 * speed;
      rotor.userData.disc.material.opacity = .04 + .26 * speed;
    });
    updateStatusLeds(this.leds || []);
    const aimDirection = this.camera.getWorldDirection(new THREE.Vector3());
    this.heartBeam?.update(delta, this.drone, this.velocity, this.flashHeld, this.getHeartAim(aimDirection));
    // Rigid follow offset: altitude, turning and acceleration never change the distance.
    const followDistance = 4;
    const offset=new THREE.Vector3(Math.sin(this.heading)*followDistance,1.8,Math.cos(this.heading)*followDistance);
    const desired=this.camera.position.clone().add(offset);
    this.chaseCamera.position.copy(desired);
    this.chaseReady=true;
    this.chaseCamera.lookAt(this.getChaseLookPoint(aimDirection, delta));

    this.renderChaseMain();
    if (this.fpvContainer) this.renderDroneInset();
    if (this.fpvRenderer) this.renderCapture();
  }

  // 큰 화면 = 기체를 뒤에서 보는 chase 시점. 기체·하트 모두 원래 크기로 보인다
  renderChaseMain() {
    const width = this.container.clientWidth, height = this.container.clientHeight;

    this.renderer.setScissorTest(false);
    this.renderer.setViewport(0, 0, width, height);
    this.renderer.render(this.scene, this.chaseCamera);
  }

  // 작은 창(#fpv-view 자리) = 드론 1인칭(world.camera). 큰 화면과 같은 렌더러의 scissor 영역이라
  // pixel ratio·그림자·sRGB 가 같다. 세로 FOV 는 그대로 두고 종횡비만 인셋에 맞춘다.
  // 기체는 자기 가림이라 숨기고, 하트는 보이게 — 카메라 코앞 것만 숨기고 가까운 것은 작게(fitHeartsToFpv)
  renderDroneInset() {
    const width = this.container.clientWidth, height = this.container.clientHeight;
    const base = this.container.getBoundingClientRect(), inset = this.fpvContainer.getBoundingClientRect();
    const x = inset.left - base.left, y = base.bottom - inset.bottom;

    this.renderer.setScissor(x, y, inset.width, inset.height);
    this.renderer.setViewport(x, y, inset.width, inset.height);
    this.renderer.setScissorTest(true);
    this.setCameraAspect(inset.width / (inset.height || 1));
    this.drone.visible = false;
    const restoreHearts = this.fitHeartsToFpv();
    this.renderer.render(this.scene, this.camera);
    restoreHearts();
    this.drone.visible = true;
    this.setCameraAspect(CAPTURE_ASPECT);   // 평소엔 캡처 종횡비 — project·하트 조준 unproject 가 YOLO 픽셀과 맞는다
    this.renderer.setScissorTest(false);
    this.renderer.setViewport(0, 0, width, height);
  }

  // 드론 시점 렌더 동안만 근접 하트를 숨기거나 줄인다. 되돌리는 함수를 돌려준다
  fitHeartsToFpv() {
    if (!this.heartBeam) return () => {};

    const changed = [];
    for (const sprite of this.heartBeam.getVisibleSprites()) {
      const distanceM = sprite.position.distanceTo(this.camera.position);
      if (distanceM >= FPV_HEART_SHRINK_M) continue;
      changed.push({ sprite, scale: sprite.scale.x });
      if (distanceM < FPV_HEART_NEAR_M) sprite.visible = false;
      else sprite.scale.setScalar(sprite.scale.x * Math.max(FPV_HEART_MIN_SCALE, distanceM / FPV_HEART_SHRINK_M));
    }

    return () => {
      for (const { sprite, scale } of changed) { sprite.visible = true; sprite.scale.setScalar(scale); }
    };
  }

  // YOLO 업로드 캡처(오프스크린 16:9) — 기체는 자기 가림, 하트는 YOLO 입력 오염이라 둘 다 숨긴다
  renderCapture() {
    this.setChaseOnlyVisible(false);
    this.fpvRenderer.render(this.scene, this.camera);
    this.setChaseOnlyVisible(true);
  }

  setCameraAspect(aspect) {
    if (this.camera.aspect === aspect) return;
    this.camera.aspect = aspect;
    this.camera.updateProjectionMatrix();
  }

  // 하트는 모든 모드에서 FPV 시선으로 나간다 — 기수 수평으로 쏘면 사람보다 높은 드론에서 늘 머리 위로 지나갔다.
  // 수동(키·장갑)은 main 속도·크기 그대로 레티클로, fire_heart 는 표적 박스 몸통 광선으로 0.5~0.8 s 에 닿게
  getHeartAim(reticleDirection) {
    const muzzle = FPV_HEART_MUZZLE_M;
    if (!this.heartAimActive) return { direction: reticleDirection, speed: HEART_SPEED, scale: 1, muzzle };
    if (!this.heartTargetNdc) return { direction: reticleDirection, muzzle };

    const direction = new THREE.Vector3(this.heartTargetNdc.x, this.heartTargetNdc.y, .5)
      .unproject(this.camera).sub(this.camera.position).normalize();
    const groundDistanceM = direction.y < -1e-3 ? -this.camera.position.y / direction.y : null;
    if (groundDistanceM === null) return { direction, muzzle };

    const flightSec = getHeartFlightSec(groundDistanceM, AIMED_HEART_SPEED);
    return { direction, speed: groundDistanceM / flightSec, life: flightSec, spread: TARGET_HEART_SPREAD, muzzle };
  }

  // YOLO 캡처에서 빼는 것(기체·하트 빔). 캡처는 YOLO 로 올라가므로 여기 든 것은 절대 그리지 않는다
  setChaseOnlyVisible(isVisible) {
    this.drone.visible = isVisible;
    if (this.heartBeam) this.heartBeam.group.visible = isVisible;
  }

  // 평소엔 기수 3 m 앞(수평), 조준 사격 중엔 짐벌 시선 CHASE_AIM_LOOK_M 앞으로 부드럽게 옮긴다
  getChaseLookPoint(aimDirection, delta) {
    const blendStep = Math.min(1, delta * CHASE_AIM_BLEND_PER_SEC);
    this.chaseAimBlend = THREE.MathUtils.lerp(this.chaseAimBlend ?? 0, this.heartAimActive ? 1 : 0, blendStep);

    const ahead = new THREE.Vector3(-Math.sin(this.heading)*3, 0, -Math.cos(this.heading)*3);
    const aimed = aimDirection.clone().multiplyScalar(CHASE_AIM_LOOK_M);

    return this.camera.position.clone().add(ahead.lerp(aimed, this.chaseAimBlend));
  }

  syncTargets(tracks) {
    for (const track of tracks) {
      for (const { object, initial, origin } of this.targetMeshes.get(track.id) || []) {
        object.position.x = initial.x + track.position[0] - origin[0];
        object.position.z = initial.z + track.position[2] - origin[1];
        if(object.userData.limbs){
          object.rotation.y=track.heading;
          for(const {pivot,phase} of object.userData.limbs)pivot.rotation.x=Math.sin(track.stride)*.45*phase;
        }
        object.updateMatrixWorld(true);
      }
    }
  }

  addPeople() {
    // 절차적 휴머노이드 — GLB 의 저폴리 Person_T03 은 YOLO 가 person 으로 못 잡는다
    for (const person of PEOPLE) {
      const group=person.route?createWalker(THREE,person.color):createStandingPerson(THREE,person);
      group.name=`Person_${person.id}`;
      group.position.set(...person.feet);
      this.environment.add(group);
      this.targetMeshes.set(person.id,[{object:group,initial:group.position.clone(),origin:[person.feet[0],person.feet[2]]}]);
    }
    this.environment.updateMatrixWorld(true);
  }

  canObserve(position) {
    if (this.loading || !this.environment || !this.project(position).visible) return false;
    const destination = new THREE.Vector3(...position);
    const direction = destination.clone().sub(this.camera.position);
    const distance = direction.length();
    if (distance < 2) return true;
    const ray = new THREE.Raycaster(this.camera.position, direction.normalize(), 0, distance - 2);
    const hits = ray.intersectObject(this.environment, true);
    return !hits.some(hit => {
      for (let node = hit.object; node; node = node.parent) {
        if (!node.visible || node.name.startsWith('COLLIDER_')) return false;
      }
      return true;
    });
  }

  resize() {
    const width = this.container.clientWidth || 1, height = this.container.clientHeight || 1;
    this.renderer.setSize(width, height, false);
    this.chaseCamera.aspect = width / height;
    this.chaseCamera.updateProjectionMatrix();
    this.setCameraAspect(CAPTURE_ASPECT);
  }

  telemetry() {
    return { altitude: this.camera.position.y, speed: Math.hypot(this.velocity.x, this.velocity.z),
      climb: this.velocity.y, throttle: this.mainFlight?.throttle ?? .5, autoHover: this.mainFlight?.autoHover,
      motors: this.mainFlight?.motorSpeeds || [0,0,0,0], onGround: this.onGround,
      heading: ((-THREE.MathUtils.radToDeg(this.heading)%360)+360)%360, position: this.camera.position };
  }
}

import { resolveMovement, sphereIntersectsAabb } from "./collision.js";
import { createDrone } from "./drone-model.js";
import { MainFlightCore, D2R, DRAG_H, DRAG_V } from "./flight-core.js";
import { HeartBeam, updateStatusLeds } from "./hearts.js";
import { PEOPLE, createWalker } from './population.js';
import { createCourse, TEST_START } from './test-flight.js';
import { createMountain, terrainHeight } from './mountain-course.js';

const THREE = window.THREE;

const URBAN = { sky: 0x768384, fog: 0x65706b, start: [0, 32, 54], heading: 0 };
const DRONE_RADIUS = .7;
const BUILDING_SIDE_CLEARANCE = 2;

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

    this.scene = new THREE.Scene();
    this.testCourse=createCourse(THREE);
    this.scene.add(this.testCourse);
    this.camera = new THREE.PerspectiveCamera(68, 1, 0.1, 320);
    this.camera.rotation.order = "YXZ";
    this.chaseCamera = new THREE.PerspectiveCamera(60, 1, .1, 320);
    const model = createDrone();
    this.drone = model.drone;
    this.rotors = model.rotors;
    this.leds = model.leds;
    this.flashHeld = false;
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
    const mainControl = ['imu', 'imu-stale', 'ai'].includes(input.source);
    let moveAltitude = true;
    if (mainControl) {
      if (input.source === 'ai' && !this.mainFlight.ai) this.mainFlight.enterAi(this.camera.position.y, 0, -this.heading / D2R);
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
      this.heading += input.yaw * delta * 1.45;
    }
    const floor=this.training?terrainHeight(this.camera.position.x,this.camera.position.z)+.7:.7;
    this.onGround = this.camera.position.y <= floor;
    if (this.onGround) {
      this.velocity.x *= Math.max(0, 1 - 4 * delta);
      this.velocity.z *= Math.max(0, 1 - 4 * delta);
    }
    for (const [axis, min, max] of [[0,-68,68], [1,floor,this.training?65:48], [2,-68,68]]) {
      const before = this.camera.position.getComponent(axis);
      const bounded = THREE.MathUtils.clamp(before, min, max);
      if (bounded !== before) this.velocity.setComponent(axis, 0);
      this.camera.position.setComponent(axis, bounded);
    }
    this.camera.rotation.y = this.heading;
    const overview = THREE.MathUtils.clamp((this.camera.position.y - 12) / 20, 0, 1);
    this.camera.rotation.x = mainControl ? this.mainFlight.shown.pitch * D2R : this.training?-.08-input.pitch*.05:-.35 - overview * .3 - input.pitch * 0.09;
    this.camera.rotation.z = mainControl ? -this.mainFlight.shown.roll * D2R : -input.roll * 0.10;
    this.camera.updateMatrixWorld(true);
    this.renderViews(delta, input);
    return resolved.blocked;
  }

  project(position) {
    const point = new THREE.Vector3(...position);
    const distance = point.distanceTo(this.camera.position);
    const projected = point.clone().project(this.camera);
    const forward = new THREE.Vector3(); this.camera.getWorldDirection(forward);
    const visible = point.clone().sub(this.camera.position).dot(forward) > 0 && Math.abs(projected.x) < 1.05 && Math.abs(projected.y) < 1.05;
    const view = this.fpvContainer || this.container;
    const width = THREE.MathUtils.clamp(380 / distance, 18, 70);
    return { visible, x: (projected.x*.5+.5)*view.clientWidth, y: (-projected.y*.5+.5)*view.clientHeight, width, height: width*1.22, distance };
  }

  renderViews(delta, input = {roll:0,pitch:0}) {
    // Headless physics tests do not construct presentation cameras.
    if (!this.chaseCamera) { this.renderer.render(this.scene,this.camera); return; }
    this.drone.position.copy(this.camera.position);
    const mainControl = ['imu','imu-stale','ai'].includes(input.source);
    this.drone.rotation.set(mainControl ? this.mainFlight.shown.pitch * D2R : -input.pitch*.22,
      this.heading, mainControl ? -this.mainFlight.shown.roll * D2R : -input.roll*.28, 'YXZ');
    this.rotors.forEach(rotor => {
      const index = (rotor.position.z < 0 ? 0 : 2) + (rotor.position.x < 0 ? 0 : 1);
      const speed = mainControl ? this.mainFlight.motorSpeeds[index] : .5;
      rotor.rotation.y += delta * 60 * speed * rotor.userData.dir;
      for (const prop of rotor.userData.blades) prop.material.opacity = 1 - .75 * speed;
      rotor.userData.disc.material.opacity = .04 + .26 * speed;
    });
    updateStatusLeds(this.leds || []);
    this.heartBeam?.update(delta, this.drone, this.velocity, this.flashHeld);
    // Rigid follow offset: altitude, turning and acceleration never change the distance.
    const followDistance = 4;
    const offset=new THREE.Vector3(Math.sin(this.heading)*followDistance,1.8,Math.cos(this.heading)*followDistance);
    const desired=this.camera.position.clone().add(offset);
    this.chaseCamera.position.copy(desired);
    this.chaseReady=true;
    this.chaseCamera.lookAt(this.camera.position.clone().add(new THREE.Vector3(-Math.sin(this.heading)*3,0,-Math.cos(this.heading)*3)));
    const w=this.container.clientWidth,h=this.container.clientHeight;
    this.renderer.setScissorTest(false);
    this.renderer.setViewport(0,0,w,h);
    this.drone.visible=true;
    this.renderer.render(this.scene,this.chaseCamera);
    if(this.fpvContainer) {
      const base=this.container.getBoundingClientRect(), pip=this.fpvContainer.getBoundingClientRect();
      const x=pip.left-base.left,y=base.bottom-pip.bottom;
      this.renderer.setScissor(x,y,pip.width,pip.height);
      this.renderer.setViewport(x,y,pip.width,pip.height);
      this.renderer.setScissorTest(true);
      this.renderer.clearDepth();
      this.drone.visible=false;
      this.renderer.render(this.scene,this.camera);
      this.drone.visible=true;
      this.renderer.setScissorTest(false);
      this.renderer.setViewport(0,0,w,h);
    }
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
    const parts=[];
    this.environment.traverse(object=>{if(object.isMesh && object.name.startsWith('Person_T03')) parts.push(object);});
    for (const person of PEOPLE) {
      const group=person.route?createWalker(THREE,person.color):new THREE.Group();
      group.name=`Person_${person.id}`;
      group.position.set(...person.feet);
      for (const source of person.route?[]:parts) {
        const mesh=source.clone();
        mesh.position.sub(new THREE.Vector3(2,0,-22));
        group.add(mesh);
      }
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
    this.camera.aspect = this.fpvContainer ? (this.fpvContainer.clientWidth / (this.fpvContainer.clientHeight || 1)) : width / height;
    this.camera.updateProjectionMatrix();
  }

  telemetry() {
    return { altitude: this.camera.position.y, speed: Math.hypot(this.velocity.x, this.velocity.z),
      climb: this.velocity.y, throttle: this.mainFlight?.throttle ?? .5, autoHover: this.mainFlight?.autoHover,
      motors: this.mainFlight?.motorSpeeds || [0,0,0,0], onGround: this.onGround,
      heading: ((-THREE.MathUtils.radToDeg(this.heading)%360)+360)%360, position: this.camera.position };
  }
}

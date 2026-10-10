import { resolveMovement, sphereIntersectsAabb } from "./collision.js";

const THREE = window.THREE;

const URBAN = { sky: 0x768384, fog: 0x65706b, start: [0, 12, 54], heading: 0 };
// Same thrust, gravity, tilt deadband and drag as tools/visualizer on main.
const G = 9.81, MAX_THRUST = 2 * G, DRAG_H = .7, DRAG_V = 1.2;
const deadband = (degrees) => Math.abs(degrees) < 3 ? 0 : degrees - Math.sign(degrees) * 3;

export class DroneWorld {
  constructor(container, onAssetStatus = () => {}) {
    this.container = container;
    this.onAssetStatus = onAssetStatus;
    this.renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(Math.min(window.devicePixelRatio, 1.65));
    this.renderer.shadowMap.enabled = true;
    this.renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    this.renderer.outputEncoding = THREE.sRGBEncoding;
    container.appendChild(this.renderer.domElement);

    this.scene = new THREE.Scene();
    this.camera = new THREE.PerspectiveCamera(68, 1, 0.1, 320);
    this.camera.rotation.order = "YXZ";
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
      if (this.colliders.some(box => sphereIntersectsAabb(URBAN.start, .7, box))) {
        throw new Error("Urban asset contains an occupied spawn point");
      }
      this.scene.add(this.environment);
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
    this.camera.position.set(...URBAN.start);
    this.heading = URBAN.heading;
    this.velocity.set(0, 0, 0);
    this.hoverAltitude = URBAN.start[1];
    this.lastSource = null;
    this.camera.rotation.set(-.35, this.heading, 0, "YXZ");
    this.camera.updateMatrixWorld(true);
  }

  update(input, delta) {
    if (this.loading) {
      this.renderer.render(this.scene, this.camera);
      return false;
    }
    const forward = new THREE.Vector3(-Math.sin(this.heading), 0, -Math.cos(this.heading));
    const right = new THREE.Vector3(Math.cos(this.heading), 0, -Math.sin(this.heading));
    if (input.source !== this.lastSource) this.hoverAltitude = this.camera.position.y;
    this.lastSource = input.source;
    if (input.source === "imu-stale" || input.brake) {
      this.velocity.set(0, 0, 0);
    } else if (input.source === "imu") {
      const tilt = new THREE.Euler(
        THREE.MathUtils.degToRad(deadband(-input.pitch * 30)), this.heading,
        THREE.MathUtils.degToRad(-deadband(input.roll * 30)), "YXZ"
      );
      const up = new THREE.Vector3(0, 1, 0).applyEuler(tilt);
      const throttle = input.flexReady
        ? (Math.abs(input.throttle - .5) < .04 ? .5 : input.throttle)
        : THREE.MathUtils.clamp(.5 / Math.max(up.y, .5) + .04 * (this.hoverAltitude - this.camera.position.y) - .1 * this.velocity.y, 0, 1);
      const thrust = throttle * MAX_THRUST;
      this.velocity.x += (up.x * thrust - DRAG_H * this.velocity.x) * delta;
      this.velocity.y += (up.y * thrust - G - DRAG_V * this.velocity.y) * delta;
      this.velocity.z += (up.z * thrust - DRAG_H * this.velocity.z) * delta;
    } else {
      const acceleration = forward.multiplyScalar(input.pitch * 13 * DRAG_H).add(right.multiplyScalar(input.roll * 10 * DRAG_H));
      this.velocity.x += (acceleration.x - DRAG_H * this.velocity.x) * delta;
      this.velocity.y += (input.throttle * 8 * DRAG_V - DRAG_V * this.velocity.y) * delta;
      this.velocity.z += (acceleration.z - DRAG_H * this.velocity.z) * delta;
    }
    const movement = this.velocity.clone().multiplyScalar(delta);
    const resolved = resolveMovement(this.camera.position.toArray(), movement.toArray(), 0.7, this.colliders);
    this.camera.position.fromArray(resolved.position);
    if (resolved.blocked) {
      for (let axis = 0; axis < 3; axis++) {
        if (resolved.blockedAxes?.includes(axis)) this.velocity.setComponent(axis, 0);
      }
    }
    this.heading += input.yaw * delta * 1.45;
    for (const [axis, min, max] of [[0,-68,68], [1,3.5,48], [2,-68,68]]) {
      const before = this.camera.position.getComponent(axis);
      const bounded = THREE.MathUtils.clamp(before, min, max);
      if (bounded !== before) this.velocity.setComponent(axis, 0);
      this.camera.position.setComponent(axis, bounded);
    }
    this.camera.rotation.y = this.heading;
    this.camera.rotation.x = -.35 - input.pitch * 0.09;
    this.camera.rotation.z = -input.roll * 0.10;
    this.camera.updateMatrixWorld(true);
    this.renderer.render(this.scene, this.camera);
    return resolved.blocked;
  }

  project(position) {
    const point = new THREE.Vector3(...position);
    const distance = point.distanceTo(this.camera.position);
    const projected = point.clone().project(this.camera);
    const forward = new THREE.Vector3(); this.camera.getWorldDirection(forward);
    const visible = point.clone().sub(this.camera.position).dot(forward) > 0 && Math.abs(projected.x) < 1.05 && Math.abs(projected.y) < 1.05;
    const width = THREE.MathUtils.clamp(380 / distance, 34, 105);
    return { visible, x: (projected.x*.5+.5)*this.container.clientWidth, y: (-projected.y*.5+.5)*this.container.clientHeight, width, height: width*1.22, distance };
  }

  syncTargets(tracks) {
    for (const track of tracks) {
      for (const { object, initial, origin } of this.targetMeshes.get(track.id) || []) {
        object.position.x = initial.x + track.position[0] - origin[0];
        object.position.z = initial.z + track.position[2] - origin[1];
        object.updateMatrixWorld(true);
      }
    }
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
    this.camera.aspect = width / height;
    this.camera.updateProjectionMatrix();
  }

  telemetry() {
    return { altitude: this.camera.position.y, speed: this.velocity.length(), heading: ((THREE.MathUtils.radToDeg(this.heading)%360)+360)%360, position: this.camera.position };
  }
}

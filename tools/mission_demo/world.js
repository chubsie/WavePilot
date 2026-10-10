import { resolveMovement } from "./collision.js";

const THREE = window.THREE;

const SCENE_SETTINGS = {
  urban: { sky: 0x768384, fog: 0x65706b, start: [0, 18, 54], heading: 0 },
  mountain: { sky: 0x75818a, fog: 0x5f6a70, start: [-10, 16, 58], heading: -0.2 },
};

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
    this.scenario = "urban";
    this.clock = new THREE.Clock();
    this.quality = "cinematic";
    this.colliders = [];

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

  async setScenario(name) {
    this.scenario = name;
    this.colliders = [];
    if (this.environment) this.scene.remove(this.environment);
    const settings = SCENE_SETTINGS[name];
    this.scene.background = new THREE.Color(settings.sky);
    this.scene.fog = new THREE.Fog(settings.fog, name === "mountain" ? 52 : 70, name === "mountain" ? 185 : 210);
    this.reset();
    this.onAssetStatus("BLENDER ASSET · LOADING");
    try {
      this.environment = await this.loadGlb(`assets/${name}.glb`);
      this.environment.updateMatrixWorld(true);
      this.environment.traverse((object) => {
        if (object.isMesh) { object.castShadow = true; object.receiveShadow = true; }
        const isProxy = object.name.startsWith("COLLIDER_");
        const isUrbanWall = name === "urban" && object.name.startsWith("Ruin_");
        if (isProxy || isUrbanWall) {
          const bounds = new THREE.Box3().setFromObject(object);
          this.colliders.push({ min: bounds.min.toArray(), max: bounds.max.toArray() });
          if (isProxy) object.visible = false;
        }
      });
      this.scene.add(this.environment);
      this.setQuality(this.quality);
      this.onAssetStatus(`BLENDER ASSET · ${name.toUpperCase()} / READY`);
    } catch (error) {
      console.warn("GLB unavailable, using procedural fallback", error);
      this.environment = this.createFallback(name);
      this.scene.add(this.environment);
      this.onAssetStatus("PROCEDURAL FALLBACK · ACTIVE");
    }
  }

  loadGlb(url) {
    return new Promise((resolve, reject) => new THREE.GLTFLoader().load(url, (gltf) => resolve(gltf.scene), undefined, reject));
  }

  createFallback(name) {
    const group = new THREE.Group();
    const material = (color) => new THREE.MeshStandardMaterial({ color, roughness: 0.85 });
    const ground = new THREE.Mesh(new THREE.PlaneGeometry(180, 180), material(name === "mountain" ? 0x454940 : 0x55574e));
    ground.rotation.x = -Math.PI / 2; ground.receiveShadow = true; group.add(ground);
    if (name === "urban") {
      [[-28,-30,16,24],[-28,28,20,31],[29,-29,17,22],[31,30,23,35],[-12,-37,10,16],[13,38,12,19]].forEach(([x,z,s,h],i) => {
        const mesh = new THREE.Mesh(new THREE.BoxGeometry(s,s,h), material(i%2 ? 0x4b504a : 0x5e5a50));
        mesh.position.set(x,h/2,z); mesh.castShadow=true; group.add(mesh);
      });
    } else {
      const rock = material(0x343936);
      for (let i=0;i<28;i++) {
        const a=i*2.399, r=18+(i%8)*6, x=Math.cos(a)*r, z=Math.sin(a)*r;
        const cliff=new THREE.Mesh(new THREE.DodecahedronGeometry(5+(i%4)*2),rock); cliff.position.set(x,4+(i%3)*3,z); cliff.scale.y=1.8; cliff.castShadow=true; group.add(cliff);
      }
    }
    return group;
  }

  reset() {
    const settings = SCENE_SETTINGS[this.scenario];
    this.camera.position.set(...settings.start);
    this.heading = settings.heading;
    this.velocity.set(0, 0, 0);
  }

  update(input, delta) {
    const forward = new THREE.Vector3(-Math.sin(this.heading), 0, -Math.cos(this.heading));
    const right = new THREE.Vector3(Math.cos(this.heading), 0, -Math.sin(this.heading));
    const desired = forward.multiplyScalar(input.pitch * 13).add(right.multiplyScalar(input.roll * 10));
    desired.y = input.throttle * 8;
    this.velocity.lerp(desired, 1 - Math.exp(-delta * 2.6));
    const movement = this.velocity.clone().multiplyScalar(delta);
    const resolved = resolveMovement(this.camera.position.toArray(), movement.toArray(), 0.7, this.colliders);
    this.camera.position.fromArray(resolved.position);
    if (resolved.blocked) this.velocity.multiplyScalar(0.2);
    this.heading += input.yaw * delta * 1.45;
    this.camera.position.x = THREE.MathUtils.clamp(this.camera.position.x, -68, 68);
    this.camera.position.z = THREE.MathUtils.clamp(this.camera.position.z, -68, 68);
    this.camera.position.y = THREE.MathUtils.clamp(this.camera.position.y, 3.5, 48);
    this.camera.rotation.y = this.heading;
    this.camera.rotation.x = -input.pitch * 0.09;
    this.camera.rotation.z = -input.roll * 0.10;
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

// Procedural humanoid with real body proportions so a COCO detector reads it as a person.
// Origin = soles of the feet, model faces +z. userData.limbs keeps the {pivot, phase} walk contract.

const REFERENCE_HEIGHT = 1.75;
// Width multiplier for torso/limbs (coats, loose trousers). Thinner bodies drop below detector range at 20 m+.
const DEFAULT_BUILD = 1.2;

export const SKIN_TONES = [0xf1c8a8, 0xe0ac87, 0xc68863, 0xa66b45, 0x7b4a2d, 0x5a3622];
export const HAIR_COLORS = [0x15110e, 0x2b1c12, 0x4a2e1a, 0x7a5532, 0x9a8f86];
// Mid/dark saturated tops only: white or yellow tops on light concrete fall under the detector threshold at 20 m.
export const SHIRT_COLORS = [0xc83c32, 0x286bd1, 0x2f8f4e, 0xb5562a, 0x7a3f9a, 0x1c2a44, 0xd86a8c, 0x1f7a7a, 0x3a3a3a];
export const PANTS_COLORS = [0x242a30, 0x2c3e66, 0x4a4036, 0x1a1a1a, 0x6b6253, 0x3b4a5c];

function createRandom(seed) {
  let state = (seed >>> 0) || 1;
  return () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 4294967296;
  };
}

function pick(list, random) {
  return list[Math.floor(random() * list.length) % list.length];
}

// r128 has no CapsuleGeometry: lathe a half-circle / straight / half-circle profile instead.
// The capsule hangs down from y=0 to y=-length (top and bottom radii can differ for tapered limbs).
function createCapsuleGeometry(THREE, topRadius, bottomRadius, length) {
  const points = [];
  const arcSteps = 5;
  for (let i = 0; i <= arcSteps; i++) {
    const a = (i / arcSteps) * Math.PI / 2;
    points.push(new THREE.Vector2(Math.sin(a) * bottomRadius, -length + bottomRadius - Math.cos(a) * bottomRadius));
  }
  for (let i = 0; i <= arcSteps; i++) {
    const a = (i / arcSteps) * Math.PI / 2;
    points.push(new THREE.Vector2(Math.cos(a) * topRadius, -topRadius + Math.sin(a) * topRadius));
  }
  return new THREE.LatheGeometry(points, 14);
}

// Torso profile (radius, height) for a clothed 1.75 m body; lathed then flattened front-to-back.
// Clothing bulk matters: a bare-anatomy silhouette is too thin for a detector at 20 m+.
const TORSO_PROFILE = [
  [0.0, 0.85], [0.15, 0.86], [0.2, 0.93], [0.195, 1.00], [0.18, 1.09],
  [0.195, 1.20], [0.215, 1.31], [0.23, 1.40], [0.19, 1.46], [0.065, 1.495],
];

function createTorso(THREE, scale, width, shirt, pants) {
  const torso = new THREE.Group();
  const toPoints = profile => profile.map(([r, y]) => new THREE.Vector2(r * width, y * scale));
  const upper = new THREE.Mesh(new THREE.LatheGeometry(toPoints(TORSO_PROFILE.slice(3)), 18), shirt);
  const lower = new THREE.Mesh(new THREE.LatheGeometry(toPoints(TORSO_PROFILE.slice(0, 4)), 18), pants);
  for (const part of [upper, lower]) {
    part.scale.z = 0.66;
    torso.add(part);
  }
  return torso;
}

function createHead(THREE, scale, skin, hairMaterial, random) {
  const head = new THREE.Group();
  const skull = new THREE.Mesh(new THREE.SphereGeometry(0.1 * scale, 16, 12), skin);
  skull.scale.set(0.82, 1.15, 0.98);
  head.add(skull);

  const jaw = new THREE.Mesh(new THREE.SphereGeometry(0.068 * scale, 12, 8), skin);
  jaw.position.set(0, -0.055 * scale, 0.018 * scale);
  jaw.scale.set(0.95, 0.9, 1);
  head.add(jaw);

  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.013 * scale, 0.035 * scale, 6), skin);
  nose.position.set(0, -0.005 * scale, 0.1 * scale);
  nose.rotation.x = Math.PI / 2;
  head.add(nose);

  const longHair = random() < 0.35;
  const hair = new THREE.Mesh(new THREE.SphereGeometry(0.106 * scale, 16, 10, 0, Math.PI * 2, 0, longHair ? Math.PI * 0.7 : Math.PI * 0.52), hairMaterial);
  hair.scale.set(0.86, 1.15, 1.02);
  hair.position.set(0, 0.008 * scale, -0.01 * scale);
  hair.rotation.x = -0.35;
  head.add(hair);
  return head;
}

function createArm(THREE, scale, width, side, sleeve, skin) {
  const shoulder = new THREE.Group();
  shoulder.position.set(side * 0.235 * width, 1.415 * scale, 0);
  shoulder.rotation.z = side * 0.14;

  const deltoid = new THREE.Mesh(new THREE.SphereGeometry(0.072 * width, 10, 8), sleeve);
  shoulder.add(deltoid);
  const upperArm = new THREE.Mesh(createCapsuleGeometry(THREE, 0.068 * width, 0.056 * width, 0.31 * scale), sleeve);
  shoulder.add(upperArm);

  const elbow = new THREE.Group();
  elbow.position.y = -0.29 * scale;
  elbow.rotation.x = -0.18;
  shoulder.add(elbow);
  const forearm = new THREE.Mesh(createCapsuleGeometry(THREE, 0.05 * width, 0.04 * width, 0.27 * scale), skin);
  elbow.add(forearm);
  const hand = new THREE.Mesh(new THREE.SphereGeometry(0.05 * scale, 10, 8), skin);
  hand.scale.set(0.55, 1.35, 0.95);
  hand.position.y = -0.3 * scale;
  elbow.add(hand);
  return { shoulder, elbow };
}

function createLeg(THREE, scale, width, side, pants, shoeMaterial) {
  const hip = new THREE.Group();
  hip.position.set(side * 0.115 * width, 0.93 * scale, 0);

  const thigh = new THREE.Mesh(createCapsuleGeometry(THREE, 0.092 * width, 0.068 * width, 0.47 * scale), pants);
  hip.add(thigh);

  const knee = new THREE.Group();
  knee.position.y = -0.44 * scale;
  hip.add(knee);
  const shin = new THREE.Mesh(createCapsuleGeometry(THREE, 0.066 * width, 0.052 * width, 0.42 * scale), pants);
  knee.add(shin);

  const shoe = new THREE.Mesh(new THREE.BoxGeometry(0.115 * scale, 0.075 * scale, 0.28 * scale), shoeMaterial);
  shoe.position.set(0, -0.452 * scale, 0.045 * scale);
  knee.add(shoe);
  return { hip, knee };
}

export function createHumanoid(THREE, { shirt = 0x286bd1, pants = 0x242a30, skin, hair, height = REFERENCE_HEIGHT, build, seed = 1 } = {}) {
  const random = createRandom(seed);
  const scale = height / REFERENCE_HEIGHT;
  const width = scale * (build ?? DEFAULT_BUILD + (random() - 0.5) * 0.12);
  // Palette values are sRGB; the renderer outputs sRGB, so convert to linear to keep colours saturated.
  const material = (hex, roughness) => new THREE.MeshStandardMaterial({ color: new THREE.Color(hex).convertSRGBToLinear(), roughness });
  const shirtMaterial = material(shirt, 0.85);
  const pantsMaterial = material(pants, 0.95);
  const skinMaterial = material(skin ?? pick(SKIN_TONES, random), 0.7);
  const hairMaterial = material(hair ?? pick(HAIR_COLORS, random), 0.9);
  const shoeMaterial = material(random() < 0.5 ? 0x1b1b1b : 0xe8e8e8, 0.8);

  const group = new THREE.Group();
  group.add(createTorso(THREE, scale, width, shirtMaterial, pantsMaterial));

  const neck = new THREE.Group();
  neck.position.y = 1.47 * scale;
  group.add(neck);
  const neckMesh = new THREE.Mesh(new THREE.CylinderGeometry(0.047 * scale, 0.055 * scale, 0.12 * scale, 10), skinMaterial);
  neckMesh.position.y = 0.05 * scale;
  neck.add(neckMesh);
  const head = createHead(THREE, scale, skinMaterial, hairMaterial, random);
  head.position.y = (REFERENCE_HEIGHT - 0.115 - 1.47) * scale;
  neck.add(head);

  const limbs = [];
  const elbows = [];
  const knees = [];
  for (const side of [-1, 1]) {
    const leg = createLeg(THREE, scale, width, side, pantsMaterial, shoeMaterial);
    group.add(leg.hip);
    limbs.push({ pivot: leg.hip, phase: side });
    knees.push(leg.knee);

    const arm = createArm(THREE, scale, width, side, shirtMaterial, skinMaterial);
    group.add(arm.shoulder);
    limbs.push({ pivot: arm.shoulder, phase: -side });
    elbows.push(arm.elbow);
  }

  group.userData.limbs = limbs;
  group.userData.joints = { neck, elbows, knees };
  group.userData.height = height;
  group.traverse(object => { if (object.isMesh) object.castShadow = true; });
  return group;
}

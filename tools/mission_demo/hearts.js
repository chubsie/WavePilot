// Heart beam and always-blinking LEDs from chubsie/main 5e5c255.
const THREE = window.THREE;
export const HEART_RATE = 30, HEART_SPEED = 30, HEART_LIFE = 1.8;
// Python fire_heart 조준 사격 — 기수(수평)가 아니라 짐벌 시선(표적이 화면 중앙)으로, 눈에 남게 느리고 크게
export const AIMED_HEART_SPEED = 14, AIMED_HEART_SCALE = 1.6;
const HEART_SPREAD = 2.4;   // main 의 발사 퍼짐(m/s) — 조준축에 수직으로 ±절반

export function createHeartTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = canvas.getContext('2d');
  ctx.translate(64, 60); ctx.beginPath(); ctx.moveTo(0, 46);
  ctx.bezierCurveTo(-70, -2, -42, -52, 0, -22);
  ctx.bezierCurveTo(42, -52, 70, -2, 0, 46); ctx.closePath();
  const gradient = ctx.createRadialGradient(-14, -18, 4, 0, 0, 62);
  gradient.addColorStop(0, '#ff9ec4'); gradient.addColorStop(1, '#e8174f');
  ctx.fillStyle = gradient; ctx.fill();
  ctx.lineWidth = 4; ctx.strokeStyle = '#fff'; ctx.stroke();
  const texture = new THREE.CanvasTexture(canvas);
  // The renderer outputs sRGB; without this the main-branch pinks wash out.
  texture.encoding = THREE.sRGBEncoding;
  return texture;
}

export class HeartBeam {
  constructor(scene, { texture = createHeartTexture(), random = Math.random } = {}) {
    this.random = random;
    this.accumulator = 0;
    this.noseDir = new THREE.Vector3();
    this.noseUp = new THREE.Vector3();
    this.noseSide = new THREE.Vector3();
    // 스프라이트를 한 그룹에 모아 FPV(YOLO 입력) 렌더 때 통째로 숨길 수 있게 한다
    this.group = new THREE.Group();
    scene.add(this.group);
    this.hearts = Array.from({ length: 90 }, () => {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, fog: false }));
      sprite.visible = false; this.group.add(sprite);
      return { sprite, vel: new THREE.Vector3(), age: HEART_LIFE, life: HEART_LIFE, spin: 0, scale: 1 };
    });
  }

  // aim = { direction, speed?, scale?, life?, spread? } 이면 그 방향으로 쏜다(기본은 AIMED_HEART_*). 없으면 main 그대로(기수 방향)
  update(dt, drone, velocity, held, aim = null) {
    this.noseDir.set(0, 0, -1).applyQuaternion(drone.quaternion);
    this.noseUp.set(0, 1, 0).applyQuaternion(drone.quaternion);
    this.noseSide.crossVectors(this.noseDir, this.noseUp);
    if (aim) this.alignToAim(aim.direction);
    const speed = aim ? aim.speed ?? AIMED_HEART_SPEED : HEART_SPEED;
    const scale = aim ? aim.scale ?? AIMED_HEART_SCALE : 1;
    const life = aim?.life ?? HEART_LIFE;
    const spread = aim?.spread ?? HEART_SPREAD;

    if (held) {
      this.accumulator += dt * HEART_RATE;
      while (this.accumulator >= 1) {
        this.accumulator -= 1;
        const heart = this.hearts.find(item => item.age >= item.life);
        if (!heart) break;
        heart.age = 0; heart.life = life; heart.spin = (this.random() - .5) * 3; heart.scale = scale;
        heart.sprite.visible = true;
        heart.sprite.position.copy(drone.position).addScaledVector(this.noseDir, 1.2);
        heart.vel.copy(this.noseDir).multiplyScalar(speed).add(velocity)
          .addScaledVector(this.noseUp, (this.random() - .5) * spread)
          .addScaledVector(this.noseSide, (this.random() - .5) * spread);
      }
    } else this.accumulator = 0;
    for (const heart of this.hearts) {
      if (heart.age >= heart.life) { heart.sprite.visible = false; continue; }
      heart.age += dt;
      heart.sprite.position.addScaledVector(heart.vel, dt);
      const t = heart.age / heart.life;
      heart.sprite.scale.setScalar((.7 + 1.1 * t) * heart.scale);
      heart.sprite.material.opacity = t < .6 ? 1 : 1 - (t - .6) / .4;
      heart.sprite.material.rotation += heart.spin * dt;
    }
  }

  // 발사 축을 조준 방향으로 바꾼다 — 퍼짐(up/side)도 그 축에 수직으로 다시 잡는다
  alignToAim(direction) {
    this.noseDir.copy(direction).normalize();
    this.noseSide.set(0, 1, 0).cross(this.noseDir);
    if (this.noseSide.lengthSq() < 1e-6) this.noseSide.set(1, 0, 0);   // 수직으로 쏠 때
    this.noseSide.normalize();
    this.noseUp.crossVectors(this.noseDir, this.noseSide).normalize();
  }
}

export function updateStatusLeds(leds, now = performance.now()) {
  const blinkOn = Math.floor(now / 250) % 2 === 0;
  for (const led of leds) led.material.color.setHex(blinkOn ? led.userData.on : 0x2a2d35);
}

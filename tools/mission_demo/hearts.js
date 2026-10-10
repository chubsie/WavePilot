// Heart beam and always-blinking LEDs from chubsie/main 5e5c255.
const THREE = window.THREE;
export const HEART_RATE = 30, HEART_SPEED = 30, HEART_LIFE = 1.8;

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
  return new THREE.CanvasTexture(canvas);
}

export class HeartBeam {
  constructor(scene, { texture = createHeartTexture(), random = Math.random } = {}) {
    this.random = random;
    this.accumulator = 0;
    this.noseDir = new THREE.Vector3();
    this.noseUp = new THREE.Vector3();
    this.noseSide = new THREE.Vector3();
    this.hearts = Array.from({ length: 90 }, () => {
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: texture, transparent: true, depthWrite: false, fog: false }));
      sprite.visible = false; scene.add(sprite);
      return { sprite, vel: new THREE.Vector3(), age: HEART_LIFE, spin: 0 };
    });
  }

  update(dt, drone, velocity, held) {
    this.noseDir.set(0, 0, -1).applyQuaternion(drone.quaternion);
    this.noseUp.set(0, 1, 0).applyQuaternion(drone.quaternion);
    this.noseSide.crossVectors(this.noseDir, this.noseUp);
    if (held) {
      this.accumulator += dt * HEART_RATE;
      while (this.accumulator >= 1) {
        this.accumulator -= 1;
        const heart = this.hearts.find(item => item.age >= HEART_LIFE);
        if (!heart) break;
        heart.age = 0; heart.spin = (this.random() - .5) * 3;
        heart.sprite.visible = true;
        heart.sprite.position.copy(drone.position).addScaledVector(this.noseDir, 1.2);
        heart.vel.copy(this.noseDir).multiplyScalar(HEART_SPEED).add(velocity)
          .addScaledVector(this.noseUp, (this.random() - .5) * 2.4)
          .addScaledVector(this.noseSide, (this.random() - .5) * 2.4);
      }
    } else this.accumulator = 0;
    for (const heart of this.hearts) {
      if (heart.age >= HEART_LIFE) { heart.sprite.visible = false; continue; }
      heart.age += dt;
      heart.sprite.position.addScaledVector(heart.vel, dt);
      const t = heart.age / HEART_LIFE;
      heart.sprite.scale.setScalar(.7 + 1.1 * t);
      heart.sprite.material.opacity = t < .6 ? 1 : 1 - (t - .6) / .4;
      heart.sprite.material.rotation += heart.spin * dt;
    }
  }
}

export function updateStatusLeds(leds, now = performance.now()) {
  const blinkOn = Math.floor(now / 250) % 2 === 0;
  for (const led of leds) led.material.color.setHex(blinkOn ? led.userData.on : 0x2a2d35);
}

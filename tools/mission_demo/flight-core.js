// Behavioral port of tools/visualizer/index.html at chubsie/main 56d963d.
// Rendering, city collision and observation AUTO are handled by world.js.
const THREE = window.THREE;
export const D2R = Math.PI / 180;
export const G = 9.81, HOVER_AGL = 9, MAX_THRUST = 2 * G;
export const DRAG_H = .7, DRAG_V = 1.2, AI_RADIUS = 20, AI_SPEED = 6;
export const deadband = degrees => Math.abs(degrees) < 3 ? 0 : degrees - Math.sign(degrees) * 3;
const clamp = value => Math.max(0, Math.min(1, value));

export class MainFlightCore {
  constructor() {
    this.shown = { roll: 0, pitch: 0, yaw: 0 };
    this.throttle = .5;
    this.autoHover = true;
    this.motorSpeeds = [0, 0, 0, 0]; // FL, FR, RL, RR
    this.aiYaw = 0;
    this.aiAgl = HOVER_AGL;
    this.ai = false;
  }

  enterAi(altitude, ground, yaw = this.shown.yaw) {
    this.aiYaw = yaw;
    this.aiAgl = Math.max(2, altitude - ground);
    this.ai = true;
  }

  step(input, delta, position, velocity, ground = 0) {
    const m = {
      roll: input.rollDegrees ?? input.roll * 30,
      pitch: input.pitchDegrees ?? -input.pitch * 30,
      yaw: input.yawDegrees ?? 0,
    };
    const ai = input.source === 'ai', fly = Boolean(input.fly);
    if (ai && !this.ai) this.enterAi(position.y, ground);
    this.ai = ai;
    for (const key of ['roll', 'pitch', 'yaw']) this.shown[key] += (m[key] - this.shown[key]) * .35;
    if (ai) {
      this.aiYaw += (AI_SPEED / AI_RADIUS) / D2R * delta;
      this.shown.yaw = this.aiYaw;
      this.shown.roll = Math.atan(AI_SPEED * AI_SPEED / (AI_RADIUS * G)) / D2R;
      this.shown.pitch = -5;
    }

    // Main updates the motors before calculating this frame's new throttle.
    const pitchCmd = Math.max(-1, Math.min(1, deadband(m.pitch) / 30));
    const rollCmd = Math.max(-1, Math.min(1, deadband(m.roll) / 30));
    for (let i = 0; i < 4; i++) {
      const front = i < 2, left = i % 2 === 0;
      let target = this.throttle;
      if (fly || ai) target += .15 * (front ? pitchCmd : -pitchCmd) + .15 * (left ? rollCmd : -rollCmd);
      this.motorSpeeds[i] += (clamp(target) - this.motorSpeeds[i]) * Math.min(1, delta * 6);
    }

    const psi = this.shown.yaw * D2R;
    if (ai) {
      velocity.set(Math.sin(psi) * AI_SPEED, (ground + this.aiAgl - position.y) * 1.5, -Math.cos(psi) * AI_SPEED);
      this.autoHover = true;
      this.throttle = .5;
    } else if (fly) {
      const tilt = new THREE.Euler(deadband(this.shown.pitch) * D2R, -psi, -deadband(this.shown.roll) * D2R, 'YXZ');
      const up = new THREE.Vector3(0, 1, 0).applyEuler(tilt);
      this.autoHover = !(input.flexReady && Number.isFinite(input.flex0));
      this.throttle = this.autoHover
        ? clamp(.5 / Math.max(up.y, .5) + .04 * (HOVER_AGL - (position.y - ground)) - .1 * velocity.y)
        : (Math.abs(input.throttle - .5) < .04 ? .5 : input.throttle);
      const thrust = this.throttle * MAX_THRUST;
      velocity.x += (up.x * thrust - DRAG_H * velocity.x) * delta;
      velocity.y += (up.y * thrust - G - DRAG_V * velocity.y) * delta;
      velocity.z += (up.z * thrust - DRAG_H * velocity.z) * delta;
    } else {
      velocity.multiplyScalar(Math.max(0, 1 - 3 * delta));
      this.throttle = .5;
    }
    return { heading: -psi, moveAltitude: fly || ai };
  }
}

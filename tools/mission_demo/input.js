const clamp = (value, min = -1, max = 1) => Math.max(min, Math.min(max, value));

export class KeyboardInput {
  constructor(onTakeover = () => {}) {
    this.keys = new Set();
    this.onTakeover = onTakeover;
    window.addEventListener("keydown", (event) => {
      if (["w", "a", "s", "d", "q", "e", "r", "f"].includes(event.key.toLowerCase())) {
        this.keys.add(event.key.toLowerCase());
        this.onTakeover();
      }
    });
    window.addEventListener("keyup", (event) => this.keys.delete(event.key.toLowerCase()));
    window.addEventListener("blur", () => this.keys.clear());
  }

  sample() {
    const down = (key) => Number(this.keys.has(key));
    return {
      roll: down("d") - down("a"),
      pitch: down("w") - down("s"),
      yaw: down("e") - down("q"),
      throttle: down("r") - down("f"),
      source: "keyboard",
      timestamp: performance.now(),
    };
  }
}

export class SerialImuInput {
  constructor(onStatus = () => {}) {
    this.port = null;
    this.reader = null;
    this.last = null;
    this.buffer = "";
    this.onStatus = onStatus;
  }

  async connect() {
    if (!("serial" in navigator)) throw new Error("Web Serial은 Chrome/Edge에서만 지원됩니다.");
    this.port = await navigator.serial.requestPort();
    await this.port.open({ baudRate: 115200 });
    this.onStatus("connected");
    this.readLoop();
  }

  async readLoop() {
    const decoder = new TextDecoder();
    this.reader = this.port.readable.getReader();
    try {
      while (true) {
        const { value, done } = await this.reader.read();
        if (done) break;
        this.buffer += decoder.decode(value, { stream: true });
        const lines = this.buffer.split(/\r?\n/);
        this.buffer = lines.pop() || "";
        lines.forEach((line) => this.parse(line));
      }
    } finally {
      this.reader.releaseLock();
      this.onStatus("disconnected");
    }
  }

  parse(line) {
    const fields = line.trim().split(",");
    if (fields[0] !== "G" || fields.length < 5) return null;
    const roll = Number(fields[2]);
    const pitch = Number(fields[3]);
    const yawRate = Number(fields[4]);
    if (![roll, pitch, yawRate].every(Number.isFinite)) return null;
    this.last = {
      roll: clamp(roll / 30), pitch: clamp(-pitch / 30), yaw: clamp(yawRate / 90), throttle: 0,
      source: "imu", timestamp: performance.now(),
    };
    return this.last;
  }

  sample() {
    if (!this.last || performance.now() - this.last.timestamp > 500) {
      return { roll: 0, pitch: 0, yaw: 0, throttle: 0, source: "imu-stale", timestamp: performance.now() };
    }
    return this.last;
  }
}

const ROUTES = {
  urban: [[-18, 16, 18], [-7, 28, 17], [8, 29, 15], [19, 15, 19], [12, -5, 21], [-10, -8, 18]],
  forest: [[-42, -52, 10], [-28, -24, 9], [-14, 4, 11], [6, 23, 12], [30, 40, 13], [48, 56, 14]],
};

export class AutoPilotInput {
  constructor() { this.route = ROUTES.urban; this.index = 0; }
  setScenario(name) { this.route = ROUTES[name]; this.index = 0; }
  sample(position, heading) {
    const target = this.route[this.index];
    const dx = target[0] - position.x;
    const dz = target[1] - position.z;
    const dy = target[2] - position.y;
    const distance = Math.hypot(dx, dz);
    if (distance < 4 && Math.abs(dy) < 3) this.index = (this.index + 1) % this.route.length;
    const desired = Math.atan2(-dx, -dz);
    let delta = desired - heading;
    while (delta > Math.PI) delta -= Math.PI * 2;
    while (delta < -Math.PI) delta += Math.PI * 2;
    return {
      roll: 0, pitch: clamp(distance / 12, 0, 0.8), yaw: clamp(delta * 1.4), throttle: clamp(dy / 8),
      source: "auto", timestamp: performance.now(),
    };
  }
}

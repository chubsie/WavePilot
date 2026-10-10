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

const NUS = {
  service: "6e400001-b5a3-f393-e0a9-e50e24dcca9e",
  rx: "6e400002-b5a3-f393-e0a9-e50e24dcca9e",
  tx: "6e400003-b5a3-f393-e0a9-e50e24dcca9e",
};

const FLEX_KEY = "wavepilot-mission-flex.v1";

export class GloveInput {
  constructor({ onStatus = () => {}, onModeToggle = () => {}, onAuxAction = () => {} } = {}) {
    this.link = null;
    this.last = null;
    this.buffer = "";
    this.onStatus = onStatus;
    this.onModeToggle = onModeToggle;
    this.onAuxAction = onAuxAction;
    this.lastSeq = null;
    this.buttons = 0;
    this.flex0 = { raw: null, recent: [], flat: null, bent: null };
    try { Object.assign(this.flex0, JSON.parse(localStorage.getItem(FLEX_KEY) || "{}")); } catch {}
  }

  async connect(kind = "usb") {
    await this.disconnect();
    if (kind === "ble") return this.connectBle();
    return this.connectUsb();
  }

  async connectUsb() {
    if (!("serial" in navigator)) throw new Error("Web Serial은 Chrome/Edge에서만 지원됩니다.");
    const port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });
    const reader = port.readable.getReader();
    const writer = port.writable.getWriter();
    this.link = {
      kind: "usb", port, reader,
      send: (text) => writer.write(new TextEncoder().encode(text)),
      async close() {
        try { await reader.cancel(); } catch {}
        try { reader.releaseLock(); } catch {}
        try { writer.releaseLock(); } catch {}
        try { await port.close(); } catch {}
      },
    };
    this.onStatus({ connected: true, kind: "usb" });
    this.readUsb(reader);
  }

  async readUsb(reader) {
    const decoder = new TextDecoder();
    try {
      while (true) {
        const { value, done } = await reader.read();
        if (done) break;
        this.feed(decoder.decode(value, { stream: true }));
      }
    } finally {
      if (this.link?.reader === reader) await this.disconnect();
    }
  }

  async connectBle() {
    if (!navigator.bluetooth) throw new Error("Web Bluetooth는 Chrome/Edge에서만 지원됩니다.");
    const device = await navigator.bluetooth.requestDevice({
      filters: [{ namePrefix: "WavePilot" }, { services: [NUS.service] }],
      optionalServices: [NUS.service],
    });
    const server = await device.gatt.connect();
    const service = await server.getPrimaryService(NUS.service);
    const tx = await service.getCharacteristic(NUS.tx);
    const rx = await service.getCharacteristic(NUS.rx);
    const decoder = new TextDecoder();
    tx.addEventListener("characteristicvaluechanged", (event) => this.feed(decoder.decode(event.target.value, { stream: true })));
    await tx.startNotifications();
    this.link = {
      kind: "ble", device,
      send: (text) => {
        const data = new TextEncoder().encode(text);
        return rx.writeValueWithoutResponse ? rx.writeValueWithoutResponse(data) : rx.writeValue(data);
      },
      async close() { try { device.gatt.disconnect(); } catch {} },
    };
    device.addEventListener("gattserverdisconnected", () => { if (this.link?.device === device) this.disconnect(); });
    this.onStatus({ connected: true, kind: "ble" });
  }

  feed(text) {
    this.buffer += text;
    let newline;
    while ((newline = this.buffer.indexOf("\n")) >= 0) {
      this.parse(this.buffer.slice(0, newline).trim());
      this.buffer = this.buffer.slice(newline + 1);
    }
  }

  parse(line) {
    const fields = line.trim().split(",");
    if (fields[0] !== "G" || fields.length < 5) return null;
    const seq = Number(fields[1]);
    const roll = Number(fields[2]);
    const pitch = Number(fields[3]);
    const yawRate = Number(fields[4]);
    if (![seq, roll, pitch, yawRate].every(Number.isFinite)) return null;
    const steps = this.lastSeq === null ? 0 : Math.min(Math.max(seq - this.lastSeq, 0), 50);
    this.lastSeq = seq;
    const flex0 = fields.length >= 6 ? Number(fields[5]) : NaN;
    const buttons = fields.length >= 8 ? Number(fields[7]) | 0 : 0;
    if (Number.isFinite(flex0)) {
      this.flex0.raw = flex0;
      this.flex0.recent.push(flex0);
      if (this.flex0.recent.length > 25) this.flex0.recent.shift();
    }
    const pressed = buttons & ~this.buttons;
    if (pressed & 2) this.onModeToggle();
    if (pressed & 1) this.onAuxAction(true);
    if (!(buttons & 1) && (this.buttons & 1)) this.onAuxAction(false);
    this.buttons = buttons;
    const bend = this.flexBend();
    this.last = {
      roll: clamp(roll / 30), pitch: clamp(-pitch / 30), yaw: clamp(yawRate / 90),
      throttle: this.flexReady() ? clamp((bend - 0.5) * 2) : 0,
      source: "imu", timestamp: performance.now(), seq, steps, flex0, buttons,
    };
    return this.last;
  }

  flexReady() {
    return Number.isFinite(this.flex0.flat) && Number.isFinite(this.flex0.bent) && Math.abs(this.flex0.bent - this.flex0.flat) >= 30;
  }

  flexBend() {
    if (!this.flexReady() || !Number.isFinite(this.flex0.raw)) return 0.5;
    return clamp((this.flex0.raw - this.flex0.flat) / (this.flex0.bent - this.flex0.flat), 0, 1);
  }

  calibrateFlex(which) {
    if (!["flat", "bent"].includes(which) || this.flex0.recent.length < 10) return false;
    this.flex0[which] = Math.round(this.flex0.recent.reduce((sum, value) => sum + value, 0) / this.flex0.recent.length);
    try { localStorage.setItem(FLEX_KEY, JSON.stringify({ flat: this.flex0.flat, bent: this.flex0.bent })); } catch {}
    return true;
  }

  async send(command) { if (this.link) await this.link.send(command); }

  async disconnect() {
    const link = this.link;
    this.link = null;
    try { await link?.close(); } catch {}
    this.onStatus({ connected: false, kind: null });
  }

  sample() {
    if (!this.last || performance.now() - this.last.timestamp > 500) {
      return { roll: 0, pitch: 0, yaw: 0, throttle: 0, source: "imu-stale", timestamp: performance.now() };
    }
    return this.last;
  }
}

export { NUS };

const ROUTES = {
  urban: [[-18, 16, 18], [-7, 28, 17], [8, 29, 15], [19, 15, 19], [12, -5, 21], [-10, -8, 18]],
  mountain: [[-34, -50, 18], [-24, -27, 22], [-10, -3, 28], [8, 20, 30], [28, 39, 34], [48, 55, 30]],
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

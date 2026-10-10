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
      yaw: down("q") - down("e"),
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

const FLEX_KEY = "gloveVizFlex.v1";

export class GloveInput {
  constructor({ onStatus = () => {}, onModeToggle = () => {}, onAuxAction = () => {}, onLog = () => {} } = {}) {
    this.link = null;
    this.last = null;
    this.buffer = "";
    this.onStatus = onStatus;
    this.onModeToggle = onModeToggle;
    this.onAuxAction = onAuxAction;
    this.onLog = onLog;
    this.yawDegrees = 0;
    this.packetCount = 0;
    this.axes = {invRoll:false,invPitch:false,invYaw:false,swapRP:false,fly:false};
    try { Object.assign(this.axes,JSON.parse(localStorage.getItem('gloveVizOpts.v2') || '{}')); } catch {}
    // The mission UI always enables flight; stale-input handling still stops motion.
    this.axes.fly = true;
    this.lastSeq = null;
    this.buttons = 0;
    this.flex0 = { raw: null, recent: [], flat: null, bent: null, bend: 0 };
    this.history = [];
    this.hz = 0;
    try {
      const saved = localStorage.getItem(FLEX_KEY);
      const legacy = localStorage.getItem('wavepilot-mission-flex.v1');
      Object.assign(this.flex0, JSON.parse(saved || legacy || '{}'));
      if (!saved && legacy) this.saveFlex();
    } catch {}
    if (typeof navigator !== 'undefined') navigator.serial?.addEventListener?.('disconnect', event => {
      if (this.link?.port === event.target) { this.onLog('USB 장갑 연결이 끊어졌습니다.'); this.disconnect(); }
    });
  }

  async connect(kind = "usb") {
    if (this.link) throw new Error('현재 장갑 연결을 해제한 후 다른 연결 방식을 선택하세요.');
    this.last = null;
    this.lastSeq = null;
    this.buffer = '';
    this.buttons = 0;
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
    this.readUsb(reader).catch(error => this.onStatus({connected:false,kind:null,error:error.message}));
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
    try {
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
    } catch (error) {
      try { device.gatt.disconnect(); } catch {}
      this.link = null;
      this.onStatus({ connected: false, kind: null });
      throw error;
    }
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
    if (fields[0] !== "G") { if(line) this.onLog(line); return null; }
    if (fields.length < 5) return null;
    const seq = Number(fields[1]);
    const roll = Number(fields[2]);
    const pitch = Number(fields[3]);
    const yawRate = Number(fields[4]);
    if (![seq, roll, pitch, yawRate].every(Number.isFinite)) return null;
    const steps = this.lastSeq === null ? 0 : Math.min(Math.max(seq - this.lastSeq, 0), 50);
    this.lastSeq = seq;
    this.packetCount++;
    if(Math.abs(yawRate)>1) this.yawDegrees += yawRate*steps*.02;
    const flex0 = fields.length >= 6 ? Number(fields[5]) : NaN;
    const buttons = fields.length >= 8 && Number.isFinite(Number(fields[7])) ? Number(fields[7]) | 0 : this.buttons;
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
    this.last = {
      roll: roll / 30, pitch: -pitch / 30, yaw: -yawRate / 90,
      throttle: this.flex0.bend, flexReady: this.flexReady(),
      rollDegrees:roll,pitchDegrees:pitch,yawRate,
      source: "imu", timestamp: performance.now(), seq, steps, flex0, buttons,
    };
    const mapped = this.mapped();
    this.history.push([mapped.rollDegrees, mapped.pitchDegrees, mapped.yawRate]);
    if (this.history.length > 500) this.history.shift();
    return this.last;
  }

  flexReady() {
    return Number.isFinite(this.flex0.flat) && Number.isFinite(this.flex0.bent) && Math.abs(this.flex0.bent - this.flex0.flat) >= 30;
  }

  flexBend() {
    return this.flex0.bend;
  }

  advanceFrame() {
    if (this.flexReady() && Number.isFinite(this.flex0.raw)) {
      const target = clamp((this.flex0.raw - this.flex0.flat) / (this.flex0.bent - this.flex0.flat), 0, 1);
      this.flex0.bend += (target - this.flex0.bend) * .3;
    }
  }

  saveFlex() {
    try { localStorage.setItem(FLEX_KEY, JSON.stringify({ flat: this.flex0.flat, bent: this.flex0.bent })); } catch {}
  }

  calibrateFlex(which) {
    if (!["flat", "bent"].includes(which) || this.flex0.recent.length < 10) return false;
    this.flex0[which] = Math.round(this.flex0.recent.reduce((sum, value) => sum + value, 0) / this.flex0.recent.length);
    this.saveFlex();
    this.onLog(this.flexReady() ? `Flex 보정: flat=${this.flex0.flat}, bent=${this.flex0.bent}` : 'Flex 두 기준값의 간격이 30 이상이어야 합니다.');
    return true;
  }

  async send(command) {
    if(command==='c') this.yawDegrees=0;
    try { if (this.link) await this.link.send(command); }
    catch (error) { this.onLog(`Send failed: ${error.message}`); }
    if (command === 'g') this.onLog('자이로 보정 중 · 장갑을 움직이지 마세요');
  }

  async disconnect() {
    const link = this.link;
    this.link = null;
    try { await link?.close(); } catch {}
    this.onStatus({ connected: false, kind: null });
  }

  mapped() {
    let roll = this.last?.rollDegrees || 0, pitch = this.last?.pitchDegrees || 0;
    if (this.axes.swapRP) [roll, pitch] = [pitch, roll];
    if (this.axes.invRoll) roll = -roll;
    if (this.axes.invPitch) pitch = -pitch;
    const yaw = this.axes.invYaw ? -this.yawDegrees : this.yawDegrees;
    const rate = (this.last?.yawRate || 0) * (this.axes.invYaw ? -1 : 1);
    return { ...this.last, rollDegrees: roll, pitchDegrees: pitch, yawDegrees: yaw, yawRate: rate,
      roll: roll / 30, pitch: -pitch / 30, yaw: -rate / 90, fly: this.axes.fly,
      flex0: this.flex0.raw, flexReady: this.flexReady(), throttle: this.flex0.bend };
  }

  sample() {
    const mapped = this.mapped();
    if (!this.last || performance.now() - this.last.timestamp > 500) {
      return { ...mapped, source: "imu-stale", timestamp: performance.now() };
    }
    return mapped;
  }
}

export { NUS };

const URBAN_ROUTE = [[0, 42, 12], [0, 18, 12], [0, -8, 12], [0, -34, 12], [0, -58, 12], [0, -30, 12], [0, 0, 12], [0, 28, 12]];

export class AutoPilotInput {
  constructor() { this.route = URBAN_ROUTE; this.index = 0; this.resetObservation(); }
  resetObservation() {
    this.targetId = null;
    this.candidates = new Map();
    this.lastSeen = 0;
    this.obstructed = false;
    this.status = 'PATROL';
  }
  reset() { this.index = 0; this.resetObservation(); }
  notifyBlocked() { if (this.targetId) this.obstructed = true; }
  sample(position, heading, tracks = [], now = performance.now()) {
    const valid = tracks.filter(t => t.confidence >= 70 && t.position?.length === 3 && t.position.every(Number.isFinite));
    const ids = new Set(valid.map(t => t.id));
    for (const id of this.candidates.keys()) if (!ids.has(id)) this.candidates.delete(id);
    for (const t of valid) if (!this.candidates.has(t.id)) this.candidates.set(t.id, now);
    if (!this.targetId) {
      const eligible = valid.filter(t => now - this.candidates.get(t.id) >= 1000);
      eligible.sort((a,b) => Math.hypot(a.position[0]-position.x,a.position[2]-position.z) - Math.hypot(b.position[0]-position.x,b.position[2]-position.z));
      if (eligible.length) { this.targetId = eligible[0].id; this.obstructed = false; }
    }
    const observed = valid.find(t => t.id === this.targetId);
    if (observed) this.lastSeen = now;
    if (this.targetId && !observed) {
      if (now - this.lastSeen < 2000) {
        this.status = 'LOST';
        return {roll:0,pitch:0,yaw:0,throttle:0,brake:true,source:'auto',timestamp:now};
      }
      this.targetId = null;
      this.obstructed = false;
    }
    const target = observed ? [observed.position[0], observed.position[2], position.y] : this.route[this.index];
    const dx = target[0] - position.x;
    const dz = target[1] - position.z;
    const dy = target[2] - position.y;
    const distance = Math.hypot(dx, dz);
    if (!observed && distance < 4 && Math.abs(dy) < 3) this.index = (this.index + 1) % this.route.length;
    const desired = Math.atan2(-dx, -dz);
    let delta = desired - heading;
    while (delta > Math.PI) delta -= Math.PI * 2;
    while (delta < -Math.PI) delta += Math.PI * 2;
    this.status = observed ? (this.obstructed ? 'BLOCKED' : distance <= 14 ? 'OBSERVE' : 'APPROACH') : 'PATROL';
    const forward = observed ? (this.obstructed ? 0 : clamp((distance - 14) / 18, 0, .35)) : clamp(distance / 12, 0, .8);
    return {
      roll: 0, pitch: forward * Math.max(0, Math.cos(delta)), yaw: clamp(delta * 1.4), throttle: clamp(dy / 8),
      brake: this.obstructed || (observed && distance <= 14) || Math.abs(delta) > .5,
      source: "auto", timestamp: now,
    };
  }
}

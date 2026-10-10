const URBAN_TRACKS = [
    { id: "T-03", kind: "PERSON", confidence: 91, position: [2, 1.7, -22], movement: "북쪽 이동", zone: "중앙 광장" },
    { id: "T-07", kind: "VEHICLE", confidence: 87, position: [-1, 1.2, 5], movement: "저속 접근", zone: "주도로" },
];

export class AnalysisEngine {
  constructor(onState) {
    this.onState = onState;
    this.lastReportAt = 0;
    this.pending = false;
    this.timer = null;
  }

  tracks(elapsedSeconds) {
    return URBAN_TRACKS.map((track, index) => {
      const phase = elapsedSeconds * (0.15 + index * 0.03);
      const position = [...track.position];
      if (track.movement !== "정차") {
        position[0] += Math.sin(phase) * 1.4;
        position[2] += Math.cos(phase * 0.7) * 1.4;
      }
      return { ...track, position, confidence: Math.max(65, track.confidence + Math.round(Math.sin(phase * 2) * 3)) };
    });
  }

  async requestReport(tracks, force = false) {
    const remaining = 8000 - (performance.now() - this.lastReportAt);
    if (this.pending || (!force && remaining > 0)) return false;
    this.pending = true;
    this.onState({ status: "loading" });
    const captured = tracks.map((track) => ({ ...track }));
    this.timer = setTimeout(() => {
      const lines = this.buildReport(captured);
      this.lastReportAt = performance.now();
      this.pending = false;
      this.onState({ status: "ready", lines, generatedAt: new Date(), stateTime: performance.now() });
    }, 1250);
    return true;
  }

  buildReport(tracks) {
    return [
      { ids: ["T-03"], text: "중앙 광장에서 보행자 한 명이 북쪽으로 이동 중입니다." },
      { ids: ["T-07"], text: "차량 한 대가 주도로를 따라 저속 접근하고 있습니다." },
      { ids: ["T-03", "T-07"], text: "두 대상의 예상 이동 경로가 가까워 광장과 주도로의 추가 관측이 필요합니다." },
    ];
  }
}

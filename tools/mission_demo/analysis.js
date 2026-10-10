const SCENARIO_TRACKS = {
  urban: [
    { id: "T-03", kind: "PERSON", confidence: 91, position: [7, 1.7, -24], movement: "북쪽 이동", zone: "동측 골목" },
    { id: "T-07", kind: "VEHICLE", confidence: 86, position: [-4, 1.2, -16], movement: "저속 접근", zone: "중앙 교차로" },
    { id: "T-09", kind: "VEHICLE", confidence: 82, position: [22, 1.2, 4], movement: "정차", zone: "남측 도로" },
  ],
  mountain: [
    { id: "T-11", kind: "PERSON", confidence: 82, position: [15, 10, -35], movement: "능선 이동", zone: "벙커 남측 사각" },
    { id: "T-12", kind: "VEHICLE", confidence: 89, position: [8, 7, -23], movement: "벙커 접근", zone: "중앙 군용도로" },
  ],
};

export class AnalysisEngine {
  constructor(onState) {
    this.onState = onState;
    this.scenario = "urban";
    this.lastReportAt = 0;
    this.pending = false;
    this.timer = null;
  }

  setScenario(name) {
    this.scenario = name;
    this.lastReportAt = 0;
    clearTimeout(this.timer);
    this.pending = false;
  }

  tracks(elapsedSeconds) {
    return SCENARIO_TRACKS[this.scenario].map((track, index) => {
      const phase = elapsedSeconds * (0.15 + index * 0.03);
      const position = [...track.position];
      if (track.movement !== "정차") {
        position[0] += Math.sin(phase) * (this.scenario === "mountain" ? 1.8 : 3.2);
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
    if (this.scenario === "urban") {
      return [
        { ids: ["T-03"], text: "동측 골목에서 보행자 한 명이 북쪽으로 이동 중입니다." },
        { ids: ["T-07"], text: "차량 한 대가 중앙 교차로로 저속 접근하고 있습니다." },
        { ids: ["T-03", "T-07"], text: "두 대상의 예상 이동 경로가 가까워 동측 골목의 추가 관측이 필요합니다." },
      ];
    }
    return [
      { ids: ["T-11"], text: "벙커 남측 능선에서 인원 한 명이 감시 사각 방향으로 이동 중입니다." },
      { ids: ["T-12"], text: "차량 한 대가 중앙 군용도로를 따라 벙커 방향으로 접근 중입니다." },
      { ids: ["T-11", "T-12"], text: "차량 접근과 인원 이동이 동시에 발생해 능선과 벙커 입구의 추가 관측이 필요합니다." },
    ];
  }
}

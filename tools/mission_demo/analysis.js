const SCENARIO_TRACKS = {
  urban: [
    { id: "T-03", kind: "PERSON", confidence: 91, position: [7, 1.7, -24], movement: "북쪽 이동", zone: "동측 골목" },
    { id: "T-07", kind: "VEHICLE", confidence: 86, position: [-4, 1.2, -16], movement: "저속 접근", zone: "중앙 교차로" },
    { id: "T-09", kind: "VEHICLE", confidence: 82, position: [22, 1.2, 4], movement: "정차", zone: "남측 도로" },
  ],
  forest: [
    { id: "T-11", kind: "PERSON", confidence: 78, position: [-10, 1.7, -11], movement: "간헐적 이동", zone: "임도 서측 수풀" },
    { id: "T-12", kind: "VEHICLE", confidence: 88, position: [10, 1.2, -26], movement: "북동 이동", zone: "중앙 임도" },
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
        position[0] += Math.sin(phase) * (this.scenario === "forest" ? 1.8 : 3.2);
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
      { ids: ["T-11"], text: "임도 서측 수풀에서 인원 한 명이 간헐적으로 관측됩니다." },
      { ids: ["T-12"], text: "차량 한 대가 중앙 임도를 따라 북동쪽으로 이동 중입니다." },
      { ids: ["T-11", "T-12"], text: "수목에 의해 인원 관측이 반복적으로 차단되어 동일 구역 재관측이 필요합니다." },
    ];
  }
}

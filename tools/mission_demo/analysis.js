import { PEOPLE, walkingPose } from './population.js';
const URBAN_TRACKS = [
    { id: "T-03", kind: "PERSON", confidence: 91, position: [2, 1.7, -22], movement: "Moving", zone: "Central street" },
    { id: "T-07", kind: "VEHICLE", confidence: 87, position: [-1, 1.2, 5], movement: "Moving", zone: "Main road" },
    ...PEOPLE.map(person=>({id:person.id,kind:'PERSON',confidence:90,position:[person.feet[0],person.feet[1]+1.7,person.feet[2]],movement:person.route?'Walking':'Stationary',zone:person.zone,clothing:person.clothing})),
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
      const walker=PEOPLE.find(p=>p.id===track.id&&p.route);
      if(walker)return {...track,...walkingPose(walker,elapsedSeconds)};
      const phase = elapsedSeconds * (0.15 + index * 0.03);
      const position = [...track.position];
      if (track.movement !== "Stationary") {
        position[0] += Math.sin(phase) * 1.4;
        position[2] += Math.cos(phase * 0.7) * 1.4;
      }
      return { ...track, position, confidence: Math.max(65, track.confidence + Math.round(Math.sin(phase * 2) * 3)) };
    });
  }

  async requestReport(tracks, force = false, context = {}) {
    const remaining = 8000 - (performance.now() - this.lastReportAt);
    if (this.pending || (!force && remaining > 0)) return false;
    this.pending = true;
    this.onState({ status: "loading" });
    const captured = tracks.map((track) => ({ ...track }));
    this.timer = setTimeout(() => {
      const lines = this.buildReport(captured, context);
      this.lastReportAt = performance.now();
      this.pending = false;
      this.onState({ status: "ready", lines, generatedAt: new Date(), stateTime: performance.now() });
    }, 1250);
    return true;
  }

  buildReport(tracks, context = {}) {
    return [
      { ids: ['VIEW'], text: tracks.length ? `Visible: ${tracks.filter(t=>t.kind==='PERSON').length} people, ${tracks.filter(t=>t.kind==='VEHICLE').length} vehicles.` : 'No targets visible in the current camera view.' },
      ...tracks.map(track=>({ids:[track.id], text:`${track.kind==='PERSON'?'Person':'Vehicle'}${track.clothing?` in ${track.clothing.toLowerCase()} · Walking`:''} · ${track.screenSide || 'Center'} of frame${Number.isFinite(track.distance)?` · ${track.distance.toFixed(1)}m away`:''}${track.zone?` · ${track.zone}`:''}.`})),
      ...(Number.isFinite(context.altitude) ? [{ids:['FLIGHT'],text:`Altitude ${context.altitude.toFixed(1)}m · ${context.speed < .2 ? 'Hovering' : `Speed ${context.speed.toFixed(1)}m/s`} · ${context.mode || 'MANUAL'}.`}] : []),
    ];
  }
}

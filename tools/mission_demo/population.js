import { createHumanoid, SHIRT_COLORS, PANTS_COLORS } from './humanoid.js';

// Feet positions; interior elevations match the exposed GLB floor slabs.
export const PEOPLE = [
  {id:'T-11', feet:[-14.2,20.66,35], zone:'West building interior'},
  {id:'T-12', feet:[14.2,13.66,35], zone:'East building interior'},
  {id:'T-13', feet:[-14.2,15.66,10.4], zone:'West upper floor'},
  {id:'T-14', feet:[14.2,19.66,10.4], zone:'East upper floor'},
  {id:'T-15', feet:[-10.5,0,27], zone:'West street corner'},
  {id:'T-16', feet:[10.5,0,-3], zone:'East street corner'},
  {id:'T-17', feet:[-18,0,-2], zone:'West alley'},
  {id:'T-18', feet:[18,0,27], zone:'East alley'},
  {id:'T-21',feet:[-7,0,12],zone:'West sidewalk',clothing:'Red',color:0xc83c32,speed:1.15,route:[[-7,12],[-7,45],[-4,45],[-4,12]]},
  {id:'T-22',feet:[6,0,46],zone:'East sidewalk',clothing:'Blue',color:0x286bd1,speed:1.3,route:[[6,46],[6,16],[3,16],[3,46]]},
  // 도로 북쪽 절반에도 빨강·파랑 보행자 — search 구역(north -45..45, east ±6) 안을 걷는다
  {id:'T-23',feet:[6,0,-12],zone:'East sidewalk north',clothing:'Red',color:0xc83c32,speed:1.0,route:[[6,-12],[6,-42],[3,-42],[3,-12]]},
  {id:'T-24',feet:[-6,0,-40],zone:'West sidewalk north',clothing:'Blue',color:0x286bd1,speed:1.2,route:[[-6,-40],[-6,-10],[-3,-10],[-3,-40]]},
];

export function walkingPose(person,time){
  const lengths=person.route.map((p,i)=>{const q=person.route[(i+1)%person.route.length];return Math.hypot(q[0]-p[0],q[1]-p[1]);});
  let distance=time*person.speed%lengths.reduce((a,b)=>a+b,0);
  for(let i=0;i<lengths.length;i++){
    if(distance>lengths[i]){distance-=lengths[i];continue;}
    const p=person.route[i],q=person.route[(i+1)%person.route.length],t=distance/lengths[i];
    return {position:[p[0]+(q[0]-p[0])*t,1.7,p[1]+(q[1]-p[1])*t],heading:Math.atan2(q[0]-p[0],q[1]-p[1]),stride:time*person.speed*5};
  }
}

export function createWalker(THREE,color){
  return createHumanoid(THREE,{shirt:color,pants:0x242a30,seed:color});
}

function hashId(id){
  let hash=2166136261;
  for(const ch of String(id))hash=Math.imul(hash^ch.charCodeAt(0),16777619)>>>0;
  return hash;
}

// Stationary person: clothing colours are derived from the id so every load looks the same.
export function createStandingPerson(THREE,person){
  const seed=hashId(person.id);
  const shirt=person.color ?? SHIRT_COLORS[seed%SHIRT_COLORS.length];
  const pants=PANTS_COLORS[(seed>>>8)%PANTS_COLORS.length];
  const group=createHumanoid(THREE,{shirt,pants,seed,height:1.62+((seed>>>16)%20)/100});
  group.name=`Person_${person.id}`;
  return group;
}

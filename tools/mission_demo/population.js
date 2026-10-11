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
  const group=new THREE.Group(),shirt=new THREE.MeshStandardMaterial({color,roughness:.9}),pants=new THREE.MeshStandardMaterial({color:0x242a30,roughness:1}),skin=new THREE.MeshStandardMaterial({color:0xbb9274,roughness:1});
  const torso=new THREE.Mesh(new THREE.BoxGeometry(.52,.65,.29),shirt);torso.position.y=1.23;group.add(torso);
  const head=new THREE.Mesh(new THREE.SphereGeometry(.18,12,10),skin);head.position.y=1.78;group.add(head);
  group.userData.limbs=[];
  for(const side of [-1,1])for(const arm of [false,true]){
    const pivot=new THREE.Group();pivot.position.set(side*(arm?.34:.15),arm?1.49:.93,0);
    const limb=new THREE.Mesh(new THREE.BoxGeometry(arm?.15:.19,arm?.62:.83,.18),arm?shirt:pants);limb.position.y=arm?-.28:-.4;pivot.add(limb);
    group.add(pivot);group.userData.limbs.push({pivot,phase:side*(arm?-1:1)});
  }
  group.traverse(o=>{if(o.isMesh)o.castShadow=true;});return group;
}

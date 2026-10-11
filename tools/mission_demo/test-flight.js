export const TEST_START=[-40,.7,52];
// Straight course with a 15% climb: 1.8m rise for each 12m forward.
export const COURSE=Array.from({length:10},(_,index)=>[TEST_START[0],8+index*12*.15,TEST_START[2]-39-index*12]);
export const NORMALS=COURSE.map((p,i)=>{
  const before=i?COURSE[i-1]:p,after=COURSE[i+1]||p;
  const dx=after[0]-before[0],dy=after[1]-before[1],dz=after[2]-before[2],length=Math.hypot(dx,dy,dz);
  return [dx/length,dy/length,dz/length];
});
// Scores user movement only; never produces flight commands.
export class TestFlight {
  constructor(){this.start();}
  start(position=TEST_START){this.state='READY';this.index=0;this.passed=new Set();this.previous=[...position];this.startedAt=null;this.elapsed=0;}
  update(position,now){
    const previous=this.previous;this.previous=[...position];
    if(this.state==='COMPLETE')return;
    const c=COURSE[this.index],n=NORMALS[this.index];
    const dot=p=>(p[0]-c[0])*n[0]+(p[1]-c[1])*n[1]+(p[2]-c[2])*n[2];
    const a=dot(previous),b=dot(position);
    if((a<0&&b>=0)||(a>0&&b<=0)){
      const t=a/(a-b),hit=previous.map((v,i)=>v+(position[i]-v)*t-c[i]);
      if(Math.hypot(...hit)<3.15){
        this.startedAt??=now;this.state='FLYING';this.passed.add(this.index++);
        if(this.index===COURSE.length)this.state='COMPLETE';
      }
    }
    if(this.startedAt!==null)this.elapsed=(now-this.startedAt)/1000;
  }
}
export function createCourse(THREE){
  const group=new THREE.Group();group.visible=false;
  const geometry=new THREE.TorusGeometry(4,.15,10,64);
  COURSE.forEach((center,index)=>{
    const ring=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({color:0xe7b94d,emissive:0xe7b94d,emissiveIntensity:.35,roughness:.5}));
    ring.position.set(...center);ring.quaternion.setFromUnitVectors(new THREE.Vector3(0,0,1),new THREE.Vector3(...NORMALS[index]));
    ring.userData.index=index;group.add(ring);
  });
  return group;
}

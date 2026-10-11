export const TEST_START=[-40,.7,52];
export const COURSE=[[-40,8,39],[-34,11,25],[-22,14,14],[-6,17,9],[12,20,3],[29,18,-9],[36,15,-25],[28,12,-42],[10,10,-49],[-9,14,-42]];
export const NORMALS=COURSE.map((p,i)=>{
  const before=i?COURSE[i-1]:TEST_START,after=COURSE[i+1]||p;
  const dx=after[0]-before[0],dz=after[2]-before[2],length=Math.hypot(dx,dz);
  return [dx/length,0,dz/length];
});
// Scores user movement only; never produces flight commands.
export class TestFlight {
  constructor(){this.start();}
  start(position=TEST_START){this.state='READY';this.index=0;this.passed=new Set();this.previous=[...position];this.startedAt=null;this.elapsed=0;}
  update(position,now){
    const previous=this.previous;this.previous=[...position];
    if(this.state==='COMPLETE')return;
    const c=COURSE[this.index],n=NORMALS[this.index];
    const dot=p=>(p[0]-c[0])*n[0]+(p[2]-c[2])*n[2];
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

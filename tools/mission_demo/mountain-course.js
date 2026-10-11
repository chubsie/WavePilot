import {COURSE,TEST_START} from './test-flight.js';
const path=[TEST_START,...COURSE];
export function valleyDistance(x,z){
  let distance=Infinity;
  for(let i=1;i<path.length;i++){
    const a=path[i-1],b=path[i],dx=b[0]-a[0],dz=b[2]-a[2];
    const t=Math.max(0,Math.min(1,((x-a[0])*dx+(z-a[2])*dz)/(dx*dx+dz*dz)));
    distance=Math.min(distance,Math.hypot(x-a[0]-t*dx,z-a[2]-t*dz));
  }
  return distance;
}
export function terrainHeight(x,z){
  const d=Math.max(0,valleyDistance(x,z)-9);
  return d*(.8+.26*Math.sin(x*.075)*Math.cos(z*.09)) + Math.min(d/5,1)*(Math.sin(x*.41)*Math.cos(z*.32)*1.2);
}
export function createMountain(THREE){
  const group=new THREE.Group();group.name='Mountain training valley';
  const geometry=new THREE.PlaneGeometry(360,360,180,180);geometry.rotateX(-Math.PI/2);
  const points=geometry.attributes.position,colors=[];
  for(let i=0;i<points.count;i++){
    const x=points.getX(i),z=points.getZ(i),h=terrainHeight(x,z);points.setY(i,h);
    const d=valleyDistance(x,z),color=new THREE.Color(d<6?0x82765a:h>45?0x9a9d94:h>20?0x626952:0x526343);
    color.multiplyScalar(.88+.12*Math.sin(x*2.7+z*1.9));colors.push(color.r,color.g,color.b);
  }
  geometry.setAttribute('color',new THREE.Float32BufferAttribute(colors,3));geometry.computeVertexNormals();
  const terrain=new THREE.Mesh(geometry,new THREE.MeshStandardMaterial({vertexColors:true,roughness:1}));terrain.receiveShadow=true;group.add(terrain);
  const rocks=new THREE.InstancedMesh(new THREE.DodecahedronGeometry(1,0),new THREE.MeshStandardMaterial({color:0x767970,roughness:1}),200);
  const dummy=new THREE.Object3D();let count=0;
  for(let i=0;i<450&&count<200;i++){
    const x=Math.sin(i*127.1)*115,z=Math.sin(i*311.7)*115;
    if(valleyDistance(x,z)<15)continue;
    const scale=1+Math.abs(Math.sin(i*7.3))*3;
    dummy.position.set(x,terrainHeight(x,z)+scale*.2,z);dummy.scale.set(scale,scale*.7,scale*.8);dummy.rotation.set(i,0,i*.3);dummy.updateMatrix();rocks.setMatrixAt(count++,dummy.matrix);
  }
  rocks.count=count;rocks.castShadow=true;group.add(rocks);
  const pad=new THREE.Mesh(new THREE.CylinderGeometry(3,3,.12,48),new THREE.MeshStandardMaterial({color:0x333b32,roughness:1}));
  pad.position.set(TEST_START[0],.06,TEST_START[2]);group.add(pad);
  return group;
}

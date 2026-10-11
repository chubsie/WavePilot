const THREE = window.THREE;

export function createDrone() {
  const drone = new THREE.Group();
  const shell = new THREE.MeshStandardMaterial({color:0x353c3d, metalness:.6, roughness:.4});
  const trim = new THREE.MeshStandardMaterial({color:0xc9ef58, roughness:.5});
  const blade = new THREE.MeshStandardMaterial({color:0x171b1c, metalness:.3, roughness:.6});
  const rotors = [];
  const leds = [];
  const add = (geometry, material, x,y,z) => {
    const mesh=new THREE.Mesh(geometry,material);
    mesh.position.set(x,y,z); mesh.castShadow=true; drone.add(mesh); return mesh;
  };
  add(new THREE.BoxGeometry(.65,.24,.9),shell,0,0,0);
  add(new THREE.BoxGeometry(.4,.06,.45),trim,0,.15,0);
  for (const x of [-.75,.75]) for (const z of [-.65,.65]) {
    const arm=add(new THREE.BoxGeometry(Math.hypot(x,z),.09,.12),shell,x/2,0,z/2);
    arm.rotation.y=-Math.atan2(z,x);
    add(new THREE.CylinderGeometry(.12,.12,.17,12),shell,x,.09,z);
    const rotor=new THREE.Group(); rotor.position.set(x,.2,z);
    rotor.userData.blades = [];
    rotor.userData.dir = x * z > 0 ? 1 : -1;
    for (const angle of [0,Math.PI/2]) {
      const material = blade.clone(); material.transparent = true;
      const prop=new THREE.Mesh(new THREE.BoxGeometry(.9,.025,.075),material);
      prop.rotation.y=angle; rotor.add(prop);
      rotor.userData.blades.push(prop);
    }
    const disc = new THREE.Mesh(new THREE.CircleGeometry(.45,24),new THREE.MeshBasicMaterial({color:0x6b7580,transparent:true,opacity:.04,side:THREE.DoubleSide,depthWrite:false}));
    disc.rotation.x = -Math.PI/2; rotor.add(disc); rotor.userData.disc = disc;
    drone.add(rotor); rotors.push(rotor);
    const led = add(new THREE.SphereGeometry(.045,8,6),new THREE.MeshBasicMaterial({color:0x2a2d35}),x,.07,z);
    led.userData.on=z<0?0xff3b3b:0x3bff6a;
    leds.push(led);
  }
  add(new THREE.SphereGeometry(.15,12,8),shell,0,-.23,-.33);
  const lens=add(new THREE.CylinderGeometry(.075,.075,.08,12),new THREE.MeshStandardMaterial({color:0x183f49,metalness:.7,roughness:.12}),0,-.23,-.47);
  lens.rotation.x=Math.PI/2;
  for (const x of [-.3,.3]) add(new THREE.BoxGeometry(.045,.28,.045),shell,x,-.22,.2);
  return {drone,rotors,leds};
}

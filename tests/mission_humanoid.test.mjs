import test from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { createHumanoid } from '../tools/mission_demo/humanoid.js';
import { PEOPLE, createWalker, createStandingPerson } from '../tools/mission_demo/population.js';

const require = createRequire(import.meta.url);
const THREE = require('../tools/visualizer/three.min.js');
globalThis.window = { THREE };

function getBounds(object) {
  object.updateMatrixWorld(true);
  return new THREE.Box3().setFromObject(object);
}

test('humanoid stands on its soles and is as tall as requested', () => {
  for (const height of [1.6, 1.75, 1.9]) {
    const bounds = getBounds(createHumanoid(THREE, { height, seed: 7 }));
    assert.ok(Math.abs(bounds.min.y) < 0.02, `feet at y=${bounds.min.y}`);
    assert.ok(Math.abs(bounds.max.y - height) < height * 0.03, `top ${bounds.max.y} vs ${height}`);
  }
});

test('humanoid has a human silhouette: narrower than tall, head about 1/7.5 of height', () => {
  const human = createHumanoid(THREE, { height: 1.75, seed: 3 });
  const size = getBounds(human).getSize(new THREE.Vector3());
  const skull = human.userData.joints.neck.children[1].children[0];
  const headBounds = getBounds(skull);
  const headHeight = headBounds.max.y - headBounds.min.y;
  assert.ok(size.x > 0.45 && size.x < 0.9, `width ${size.x}`);
  assert.ok(size.z < size.x, `depth ${size.z} should be under width ${size.x}`);
  assert.ok(headHeight > 1.75 / 9 && headHeight < 1.75 / 6.5, `head ${headHeight}`);
});

test('humanoid keeps the walk contract: limbs with pivot and opposing phase', () => {
  const human = createHumanoid(THREE, { seed: 1 });
  const { limbs, joints } = human.userData;
  assert.ok(limbs.length >= 4);
  for (const { pivot, phase } of limbs) {
    assert.ok(pivot.isObject3D);
    assert.equal(Math.abs(phase), 1);
  }
  assert.equal(limbs.reduce((sum, limb) => sum + limb.phase, 0), 0);
  assert.equal(joints.elbows.length, 2);
  assert.equal(joints.knees.length, 2);

  for (const { pivot, phase } of limbs) pivot.rotation.x = Math.sin(1) * 0.45 * phase;
  assert.ok(Math.abs(getBounds(human).min.y) < 0.15, 'mid-stride pose stays near the ground');
});

test('every humanoid mesh casts a shadow and uses a standard material', () => {
  const human = createHumanoid(THREE, { seed: 5 });
  let meshCount = 0;
  human.traverse(object => {
    if (!object.isMesh) return;
    meshCount++;
    assert.ok(object.castShadow);
    assert.ok(object.material.isMeshStandardMaterial);
  });
  assert.ok(meshCount > 15);
});

test('createWalker and createStandingPerson return humanoid groups', () => {
  const walker = createWalker(THREE, 0xc83c32);
  assert.ok(walker.isGroup);
  assert.ok(walker.userData.limbs.length >= 4);

  const standing = PEOPLE.filter(person => !person.route).map(person => createStandingPerson(THREE, person));
  assert.ok(standing.length > 0);
  for (const group of standing) {
    assert.ok(group.isGroup);
    assert.ok(group.name.startsWith('Person_T-'));
    assert.ok(Math.abs(getBounds(group).min.y) < 0.02);
  }
  const again = createStandingPerson(THREE, PEOPLE[0]);
  const shirtOf = group => group.children[0].children[0].material.color.getHex();
  assert.equal(shirtOf(again), shirtOf(standing[0]), 'clothing is deterministic per id');
});

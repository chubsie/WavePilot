const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export function sphereIntersectsAabb(position, radius, box) {
  let squaredDistance = 0;
  for (let axis = 0; axis < 3; axis += 1) {
    const closest = clamp(position[axis], box.min[axis], box.max[axis]);
    const delta = position[axis] - closest;
    squaredDistance += delta * delta;
  }
  return squaredDistance < radius * radius;
}

export function resolveMovement(position, movement, radius, boxes) {
  const collides = (candidate) => boxes.some((box) => sphereIntersectsAabb(candidate, radius, box));
  const full = position.map((value, axis) => value + movement[axis]);
  if (!collides(full)) return { position: full, blocked: false };

  const resolved = [...position];
  for (const axis of [2, 0, 1]) {
    if (!movement[axis]) continue;
    const candidate = [...resolved];
    candidate[axis] += movement[axis];
    if (!collides(candidate)) resolved[axis] = candidate[axis];
  }
  return { position: resolved, blocked: true };
}

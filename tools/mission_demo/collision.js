const clamp = (value, min, max) => Math.max(min, Math.min(max, value));

export function sphereIntersectsAabb(position, radius, box, verticalRadius = radius) {
  let squaredDistance = 0;
  for (let axis = 0; axis < 3; axis += 1) {
    const closest = clamp(position[axis], box.min[axis], box.max[axis]);
    const delta = position[axis] - closest;
    const axisRadius = axis === 1 ? verticalRadius : radius;
    squaredDistance += (delta * delta) / (axisRadius * axisRadius);
  }
  return squaredDistance < 1;
}

export function resolveMovement(position, movement, radius, boxes, horizontalClearance = radius) {
  const collides = (candidate) => boxes.some((box) => sphereIntersectsAabb(candidate, horizontalClearance, box, radius));
  const resolved = [...position];
  const blockedAxes = new Set();
  const steps = Math.max(1, Math.ceil(Math.hypot(...movement) / (radius * 0.5)));
  for (let step = 0; step < steps; step++) {
    for (const axis of [2, 0, 1]) {
      if (!movement[axis]) continue;
      const candidate = [...resolved];
      candidate[axis] += movement[axis] / steps;
      if (!collides(candidate)) resolved[axis] = candidate[axis];
      else blockedAxes.add(axis);
    }
  }
  return { position: resolved, blocked: blockedAxes.size > 0, blockedAxes: [...blockedAxes] };
}

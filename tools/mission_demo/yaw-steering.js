// Hand yaw is a steering deflection, not the drone's absolute heading.
export function gloveTurnRate(degrees) {
  if (!Number.isFinite(degrees)) return 0;
  const angle=((degrees+180)%360+360)%360-180;
  const strength=Math.min(1,Math.max(0,(Math.abs(angle)-8)/(30-8)));
  return Math.sign(angle)*strength*90;
}

export interface LoopBackGeometry {
  path: string;
  labelX: number;
  labelY: number;
  depth: number;
}

interface Point {
  x: number;
  y: number;
}

function cubicAt(a: number, b: number, c: number, d: number, t: number): number {
  const mt = 1 - t;
  return mt ** 3 * a + 3 * mt ** 2 * t * b + 3 * mt * t ** 2 * c + t ** 3 * d;
}

/** Return all in-range extrema for one cubic axis, including endpoints. */
function cubicExtrema(a: number, b: number, c: number, d: number): number[] {
  const qa = -a + 3 * b - 3 * c + d;
  const qb = 2 * (a - 2 * b + c);
  const qc = b - a;
  const roots = [0, 1];
  if (Math.abs(qa) < 1e-9) {
    if (Math.abs(qb) >= 1e-9) roots.push(-qc / qb);
  } else {
    const discriminant = qb ** 2 - 4 * qa * qc;
    if (discriminant >= 0) {
      const root = Math.sqrt(discriminant);
      roots.push((-qb + root) / (2 * qa), (-qb - root) / (2 * qa));
    }
  }
  return roots.filter((t) => t >= 0 && t <= 1);
}

/**
 * Build a loop-back cubic below both handles. The label is evaluated at the
 * actual lowest point of the curve rather than assuming t=.5. A coincident
 * source/target gets opposing horizontal controls so a self-loop stays visible.
 */
export function getLoopBackGeometry(
  sourceX: number,
  sourceY: number,
  targetX: number,
  targetY: number,
): LoopBackGeometry {
  const horizontalDistance = Math.abs(targetX - sourceX);
  const verticalDistance = Math.abs(targetY - sourceY);
  const selfLoop = horizontalDistance < 1 && verticalDistance < 1;
  const depth = Math.min(Math.max(horizontalDistance * 0.35 + verticalDistance * 0.2, selfLoop ? 92 : 70), 180);
  const floorY = Math.max(sourceY, targetY) + depth;
  const spread = selfLoop ? 72 : 0;
  const p0: Point = { x: sourceX, y: sourceY };
  const p1: Point = { x: sourceX + spread, y: floorY };
  const p2: Point = { x: targetX - spread, y: floorY };
  const p3: Point = { x: targetX, y: targetY };
  const extrema = cubicExtrema(p0.y, p1.y, p2.y, p3.y);
  const lowestT = extrema.reduce((best, candidate) => (
    cubicAt(p0.y, p1.y, p2.y, p3.y, candidate)
      > cubicAt(p0.y, p1.y, p2.y, p3.y, best) ? candidate : best
  ), 0);

  return {
    path: `M ${p0.x},${p0.y} C ${p1.x},${p1.y} ${p2.x},${p2.y} ${p3.x},${p3.y}`,
    labelX: cubicAt(p0.x, p1.x, p2.x, p3.x, lowestT),
    labelY: cubicAt(p0.y, p1.y, p2.y, p3.y, lowestT),
    depth,
  };
}

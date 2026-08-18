import test from 'node:test';
import assert from 'node:assert/strict';
import { getLoopBackGeometry } from './loopBackGeometry.ts';

test('loop-back geometry puts an equal-height label at the actual lower extreme', () => {
  const geometry = getLoopBackGeometry(300, 100, 80, 100);
  assert.match(geometry.path, /^M 300,100 C /);
  assert.equal(geometry.labelY, 100 + geometry.depth * 0.75);
  assert.equal(geometry.labelX, 190);
});

test('loop-back geometry handles different handle heights without assuming t=.5', () => {
  const geometry = getLoopBackGeometry(320, 60, 100, 180);
  const midpointY = (60 + 3 * (180 + geometry.depth) + 3 * (180 + geometry.depth) + 180) / 8;
  assert.ok(geometry.labelY >= midpointY);
  assert.ok(geometry.labelY > 180);
  assert.ok(Number.isFinite(geometry.labelX));
});

test('self-loop geometry remains visible and labels its lowest point', () => {
  const geometry = getLoopBackGeometry(120, 80, 120, 80);
  assert.match(geometry.path, /C 192,/);
  assert.match(geometry.path, /48,/);
  assert.ok(geometry.labelY > 80);
  assert.equal(geometry.labelX, 120);
});

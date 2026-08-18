import test from 'node:test';
import assert from 'node:assert/strict';
import {
  DOCK_DEFAULTS,
  DOCK_LIMITS,
  clampDockLayout,
  getDockPressure,
  resizeDock,
} from './useDockLayout.ts';

test('clampDockLayout rejects invalid persisted values and preserves collapse state', () => {
  const result = clampDockLayout({
    left: Number.NaN,
    right: 9999,
    bottom: -50,
    collapsed: { left: true, right: false, bottom: true },
  }, { width: 1600, height: 900 });
  assert.equal(result.left, DOCK_DEFAULTS.left);
  assert.equal(result.right, DOCK_LIMITS.right.max);
  assert.equal(result.bottom, DOCK_LIMITS.bottom.min);
  assert.deepEqual(result.collapsed, { left: true, right: false, bottom: true });
});

test('clampDockLayout protects the center canvas when the viewport is constrained', () => {
  const viewport = { width: 1000, height: 600 };
  const result = clampDockLayout({ left: 420, right: 680, bottom: 320 }, viewport);
  assert.equal(result.left + result.right, viewport.width - DOCK_LIMITS.minCenterWidth);
  assert.ok(result.left >= DOCK_LIMITS.left.min);
  assert.ok(result.right >= DOCK_LIMITS.right.min);
  assert.equal(result.bottom, 320);
});

test('resizeDock applies screen-direction deltas for every dock', () => {
  const viewport = { width: 1600, height: 900 };
  const base = clampDockLayout(DOCK_DEFAULTS, viewport);
  assert.equal(resizeDock(base, 'left', 20, viewport).left, base.left + 20);
  assert.equal(resizeDock(base, 'right', -20, viewport).right, base.right + 20);
  assert.equal(resizeDock(base, 'bottom', -20, viewport).bottom, base.bottom + 20);
});

test('pressure collapse and overlays are derived without mutating preferences', () => {
  assert.deepEqual(getDockPressure({ width: 700, height: 500 }), {
    narrow: true,
    short: true,
    forceCollapseLeft: true,
    forceCollapseBottom: true,
    rightOverlay: true,
  });
  assert.equal(getDockPressure({ width: 1200, height: 800 }).narrow, false);
});

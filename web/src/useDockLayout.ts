import { useCallback, useEffect, useMemo, useState } from 'react';

export type DockSide = 'left' | 'right' | 'bottom';

export interface DockLayoutState {
  left: number;
  right: number;
  bottom: number;
  collapsed: Record<DockSide, boolean>;
}

export interface DockViewport {
  width: number;
  height: number;
}

export const DOCK_STORAGE_KEY = 'atlas-dock-layout-v1';
export const DOCK_DEFAULTS: DockLayoutState = {
  left: 248,
  right: 500,
  bottom: 112,
  collapsed: { left: false, right: false, bottom: false },
};

export const DOCK_LIMITS = {
  left: { min: 190, max: 420 },
  right: { min: 320, max: 680 },
  bottom: { min: 84, max: 320 },
  minCenterWidth: 360,
  minCenterHeight: 220,
  narrowWidth: 870,
  shortHeight: 560,
} as const;

export function clampNumber(value: unknown, min: number, max: number, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value)
    ? Math.min(max, Math.max(min, value))
    : fallback;
}

/** Clamp persisted or user-provided values against both panel limits and the current viewport. */
export function clampDockLayout(
  value: Partial<DockLayoutState> | null | undefined,
  viewport: DockViewport,
): DockLayoutState {
  const widthBudget = Math.max(0, viewport.width - DOCK_LIMITS.minCenterWidth);
  const heightBudget = Math.max(0, viewport.height - DOCK_LIMITS.minCenterHeight);
  let left = clampNumber(value?.left, DOCK_LIMITS.left.min, DOCK_LIMITS.left.max, DOCK_DEFAULTS.left);
  let right = clampNumber(value?.right, DOCK_LIMITS.right.min, DOCK_LIMITS.right.max, DOCK_DEFAULTS.right);
  const bottom = clampNumber(
    value?.bottom,
    DOCK_LIMITS.bottom.min,
    Math.min(DOCK_LIMITS.bottom.max, Math.max(DOCK_LIMITS.bottom.min, heightBudget)),
    DOCK_DEFAULTS.bottom,
  );
  const total = left + right;
  if (total > widthBudget) {
    const usable = Math.max(0, widthBudget);
    const minTotal = DOCK_LIMITS.left.min + DOCK_LIMITS.right.min;
    if (usable >= minTotal) {
      const extra = usable - minTotal;
      const desiredExtra = Math.max(1, total - minTotal);
      left = DOCK_LIMITS.left.min + Math.floor(extra * (left - DOCK_LIMITS.left.min) / desiredExtra);
      right = usable - left;
    }
  }
  const collapsed = {
    left: Boolean(value?.collapsed?.left),
    right: Boolean(value?.collapsed?.right),
    bottom: Boolean(value?.collapsed?.bottom),
  };
  return { left, right, bottom, collapsed };
}

export interface DockPressure {
  narrow: boolean;
  short: boolean;
  forceCollapseLeft: boolean;
  forceCollapseBottom: boolean;
  rightOverlay: boolean;
}

/** Responsive pressure is derived, never persisted, so a narrow window cannot destroy desktop preferences. */
export function getDockPressure(viewport: DockViewport): DockPressure {
  const narrow = viewport.width < DOCK_LIMITS.narrowWidth;
  const short = viewport.height < DOCK_LIMITS.shortHeight;
  return {
    narrow,
    short,
    forceCollapseLeft: narrow,
    forceCollapseBottom: short,
    rightOverlay: narrow,
  };
}

export function resizeDock(
  layout: DockLayoutState,
  side: DockSide,
  delta: number,
  viewport: DockViewport,
): DockLayoutState {
  const signedDelta = side === 'left' ? delta : -delta;
  return clampDockLayout({
    ...layout,
    [side]: layout[side] + signedDelta,
  }, viewport);
}

function currentViewport(): DockViewport {
  if (typeof window === 'undefined') return { width: 1440, height: 900 };
  return { width: window.innerWidth, height: window.innerHeight };
}

function readStoredLayout(viewport: DockViewport): DockLayoutState {
  if (typeof localStorage === 'undefined') return clampDockLayout(DOCK_DEFAULTS, viewport);
  try {
    const raw = localStorage.getItem(DOCK_STORAGE_KEY);
    return clampDockLayout(raw ? JSON.parse(raw) as Partial<DockLayoutState> : DOCK_DEFAULTS, viewport);
  } catch {
    return clampDockLayout(DOCK_DEFAULTS, viewport);
  }
}

export function useDockLayout() {
  const [viewport, setViewport] = useState<DockViewport>(currentViewport);
  const [layout, setLayout] = useState<DockLayoutState>(() => readStoredLayout(currentViewport()));

  useEffect(() => {
    const onResize = () => setViewport(currentViewport());
    window.addEventListener('resize', onResize);
    return () => window.removeEventListener('resize', onResize);
  }, []);

  useEffect(() => {
    setLayout((current) => clampDockLayout(current, viewport));
  }, [viewport]);

  useEffect(() => {
    try { localStorage.setItem(DOCK_STORAGE_KEY, JSON.stringify(layout)); } catch { /* storage may be disabled */ }
  }, [layout]);

  const setSize = useCallback((side: DockSide, size: number) => {
    setLayout((current) => clampDockLayout({ ...current, [side]: size }, viewport));
  }, [viewport]);

  const adjust = useCallback((side: DockSide, delta: number) => {
    setLayout((current) => resizeDock(current, side, delta, viewport));
  }, [viewport]);

  const toggle = useCallback((side: DockSide) => {
    setLayout((current) => ({
      ...current,
      collapsed: { ...current.collapsed, [side]: !current.collapsed[side] },
    }));
  }, []);

  const reset = useCallback((side?: DockSide) => {
    setLayout((current) => side
      ? clampDockLayout({ ...current, [side]: DOCK_DEFAULTS[side] }, viewport)
      : clampDockLayout(DOCK_DEFAULTS, viewport));
  }, [viewport]);

  return useMemo(() => ({
    layout,
    viewport,
    pressure: getDockPressure(viewport),
    setSize,
    adjust,
    toggle,
    reset,
  }), [layout, viewport, setSize, adjust, toggle, reset]);
}

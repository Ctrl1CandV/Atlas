import {
  useCallback,
  useEffect,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from 'react';
import { useDockLayout, type DockSide } from './useDockLayout';

const LABELS: Record<DockSide, string> = {
  left: '导航栏',
  right: '节点详情',
  bottom: '运行栏',
};

function CollapseButton({ side, collapsed, onToggle }: {
  side: DockSide;
  collapsed: boolean;
  onToggle: () => void;
}) {
  const glyph = side === 'left'
    ? (collapsed ? '›' : '‹')
    : side === 'right'
      ? (collapsed ? '‹' : '›')
      : (collapsed ? '⌃' : '⌄');
  return (
    <button
      type="button"
      className={`dock-toggle dock-toggle-${side}`}
      aria-label={`${collapsed ? '展开' : '折叠'}${LABELS[side]}`}
      aria-expanded={!collapsed}
      onClick={onToggle}
    >
      {glyph}
    </button>
  );
}

function DockSeparator({ side, onAdjust, onReset }: {
  side: DockSide;
  onAdjust: (delta: number) => void;
  onReset: () => void;
}) {
  const vertical = side !== 'bottom';
  const onPointerDown = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const element = event.currentTarget;
    element.setPointerCapture(event.pointerId);
    let previous = vertical ? event.clientX : event.clientY;
    const onMove = (moveEvent: PointerEvent) => {
      const current = vertical ? moveEvent.clientX : moveEvent.clientY;
      onAdjust(current - previous);
      previous = current;
    };
    const onEnd = () => {
      element.removeEventListener('pointermove', onMove);
      element.removeEventListener('pointerup', onEnd);
      element.removeEventListener('pointercancel', onEnd);
    };
    element.addEventListener('pointermove', onMove);
    element.addEventListener('pointerup', onEnd);
    element.addEventListener('pointercancel', onEnd);
  }, [onAdjust, vertical]);

  return (
    <div
      className={`dock-separator dock-separator-${side}`}
      role="separator"
      tabIndex={0}
      aria-label={`调整${LABELS[side]}大小`}
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      title="拖拽调整；方向键微调；Shift 加速；双击复位"
      onPointerDown={onPointerDown}
      onDoubleClick={onReset}
      onKeyDown={(event) => {
        const step = event.shiftKey ? 32 : 8;
        let delta = 0;
        if (vertical && event.key === 'ArrowLeft') delta = -step;
        if (vertical && event.key === 'ArrowRight') delta = step;
        if (!vertical && event.key === 'ArrowUp') delta = -step;
        if (!vertical && event.key === 'ArrowDown') delta = step;
        if (event.key === 'Home') {
          event.preventDefault();
          onReset();
          return;
        }
        if (delta !== 0) {
          event.preventDefault();
          onAdjust(delta);
        }
      }}
    />
  );
}

/** Fixed workbench shell. Its children stay mounted while docks collapse. */
export function DockWorkspace({ children }: { children: ReactNode }) {
  const dock = useDockLayout();
  const [pressureOpen, setPressureOpen] = useState({ left: false, bottom: false });
  useEffect(() => {
    if (!dock.pressure.forceCollapseLeft && !dock.pressure.forceCollapseBottom) {
      setPressureOpen({ left: false, bottom: false });
    }
  }, [dock.pressure.forceCollapseLeft, dock.pressure.forceCollapseBottom]);
  const leftCollapsed = dock.layout.collapsed.left
    || (dock.pressure.forceCollapseLeft && !pressureOpen.left);
  const rightCollapsed = dock.layout.collapsed.right;
  const bottomCollapsed = dock.layout.collapsed.bottom
    || (dock.pressure.forceCollapseBottom && !pressureOpen.bottom);
  const style = {
    '--dock-left': `${dock.layout.left}px`,
    '--dock-right': `${dock.layout.right}px`,
    '--dock-bottom': `${dock.layout.bottom}px`,
  } as CSSProperties;
  const classes = [
    'dock-workspace',
    leftCollapsed ? 'left-collapsed' : '',
    rightCollapsed ? 'right-collapsed' : '',
    bottomCollapsed ? 'bottom-collapsed' : '',
    dock.pressure.narrow ? 'is-narrow' : '',
    dock.pressure.short ? 'is-short' : '',
  ].filter(Boolean).join(' ');

  return (
    <div className={classes} style={style}>
      {children}
      {!leftCollapsed && (
        <DockSeparator side="left" onAdjust={(delta) => dock.adjust('left', delta)} onReset={() => dock.reset('left')} />
      )}
      {!rightCollapsed && (
        <DockSeparator side="right" onAdjust={(delta) => dock.adjust('right', delta)} onReset={() => dock.reset('right')} />
      )}
      {!bottomCollapsed && (
        <DockSeparator side="bottom" onAdjust={(delta) => dock.adjust('bottom', delta)} onReset={() => dock.reset('bottom')} />
      )}
      <CollapseButton
        side="left"
        collapsed={leftCollapsed}
        onToggle={() => dock.pressure.forceCollapseLeft
          ? setPressureOpen((current) => ({ ...current, left: !current.left }))
          : dock.toggle('left')}
      />
      <CollapseButton side="right" collapsed={rightCollapsed} onToggle={() => dock.toggle('right')} />
      <CollapseButton
        side="bottom"
        collapsed={bottomCollapsed}
        onToggle={() => dock.pressure.forceCollapseBottom
          ? setPressureOpen((current) => ({ ...current, bottom: !current.bottom }))
          : dock.toggle('bottom')}
      />
    </div>
  );
}

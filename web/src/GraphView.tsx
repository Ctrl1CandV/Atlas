import { useEffect, useMemo, useRef } from 'react';
import dagre from '@dagrejs/dagre';
import {
  Background,
  BaseEdge,
  Controls,
  EdgeLabelRenderer,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  useNodesInitialized,
  useReactFlow,
  type Edge,
  type EdgeProps,
  type Node,
  type NodeProps,
} from '@xyflow/react';
import {
  CheckCircle,
  CircleNotch,
  PauseCircle,
  WarningCircle,
} from '@phosphor-icons/react';
import { getLoopBackGeometry } from './loopBackGeometry';
import type { RunNode, WFEdge, WFNode } from './types';

const END_ID = '__end__';
const NODE_W = 220;
const NODE_H = 76;

type AtlasNodeData = {
  id: string;
  model: string;
  nodeType?: string;
  thinking?: string | null;
  isEnd: boolean;
  run?: RunNode;
  runInterrupted?: boolean;
};

type AtlasFlowNode = Node<AtlasNodeData, 'atlas'>;

function StatusIcon({ status }: { status: string }) {
  if (status === 'running')
    return <CircleNotch size={12} weight="bold" className="spin" />;
  if (status === 'done')
    return <CheckCircle size={12} weight="fill" />;
  if (status === 'failed')
    return <WarningCircle size={12} weight="fill" />;
  if (status === 'interrupted')
    return <PauseCircle size={12} weight="fill" />;
  return null;
}

function AtlasNode({ data }: NodeProps<AtlasFlowNode>) {
  const { run, model, nodeType, thinking, isEnd, id, runInterrupted } = data;
  const status = isEnd
    ? 'end'
    : (runInterrupted && run?.status === 'running' ? 'interrupted' : (run?.status ?? 'pending'));
  const degraded = run?.degraded;
  const truncated = run?.output_truncated;
  const attempts = run?.attempts?.length ?? 0;
  // search 节点没有模型字段,不能因为 model 为空就被当成"待配置"
  const isSearch = nodeType === 'search';
  const unconfigured = !isEnd && !run && !model && !isSearch;
  const cls = [
    'atlas-node',
    `status-${status}`,
    degraded ? 'degraded' : '',
    unconfigured ? 'unconfigured' : '',
  ].filter(Boolean).join(' ');

  // 思考徽章(M6-D):显式档位 / 供应商默认但有思考证据,两种都值得显示;
  // 指定了档位却无痕迹的用警告样式(可能未生效)
  const tier = run?.thinking?.requested_tier ?? thinking ?? null;
  const evidence = run?.thinking?.evidence;
  const hasEvidence = evidence?.kind === 'reasoning_tokens'
    || evidence?.kind === 'thinking_block';
  const showThink = !isEnd && (tier || hasEvidence);
  const ineffective = tier && tier !== 'provider_default' && !hasEvidence;

  return (
    <div className={cls} title={model}>
      {/* 句柄全部显式给 id 并被边显式引用——React Flow 对未指定句柄的边
          取"第一个匹配类型的句柄",曾因句柄声明顺序让全部正向边从底部
          出发弯成弧线(第五轮审查抓出的回归,主人反馈确认)。
          正向边:左入右出;回边:底部出、底部入(供 U 弧从下方回勾)。 */}
      <Handle type="target" position={Position.Left} id="in-left" />
      {!isEnd && <Handle type="source" position={Position.Right} id="out-right" />}
      {!isEnd && (
        <Handle type="source" position={Position.Bottom} id="back-src" className="handle-back" />
      )}
      {!isEnd && (
        <Handle type="target" position={Position.Bottom} id="back-tgt" className="handle-back" />
      )}
      <div className="atlas-node-title">
        {!isEnd && status !== 'pending' && (
          <span className={`st st-${status}`}><StatusIcon status={status} /></span>
        )}
        {id}
        {degraded && <span className="badge badge-warn" title={`实际应答:${run?.model_used}`}>降级</span>}
        {truncated && <span className="badge badge-warn" title="输出打满 max_tokens,可能句中截断">截断</span>}
        {attempts > 0 && (
          <span className="badge badge-fail" title={run?.attempts.map((a) => a.reason).join('\n')}>
            {attempts} 次失败
          </span>
        )}
        {showThink && (
          <span
            className={`badge ${ineffective ? 'badge-warn' : 'badge-think'}`}
            title={
              tier && tier !== 'provider_default'
                ? (hasEvidence
                  ? `思考档位 ${tier}${evidence?.kind === 'reasoning_tokens' && evidence.value != null ? `,实际 ${evidence.value.toLocaleString()} reasoning tokens` : ',检测到 thinking block'}`
                  : `思考档位 ${tier},但响应中无思考痕迹(可能未生效)`)
                : '未指定档位(供应商默认),响应含思考痕迹'
            }
          >
            ⚡{tier && tier !== 'provider_default' ? tier : '默认'}
          </span>
        )}
      </div>
      {!isEnd && (
        <div className="atlas-node-model" title={model || '模型未配置'}>
          {isSearch
            ? (run?.model_used ?? '🔎 检索')
            : (run?.model_used ?? (model || '模型未配置'))}
        </div>
      )}
      {!isEnd && (
        <div className="atlas-node-meta num">
          {status === 'running' && '执行中…'}
          {status === 'interrupted' && '控制器已中断'}
          {status === 'done' && run?.duration_s !== undefined && `${run.duration_s.toFixed(1)}s`}
          {status === 'done' && run?.output_tokens != null && ` · ${run.output_tokens.toLocaleString()} tok`}
          {status === 'failed' && '失败'}
        </div>
      )}
    </div>
  );
}

const nodeTypes = { atlas: AtlasNode };

/** 回边:从源节点底部下探、在节点行下方扫过、回勾进目标节点底部的
 *  半椭圆 U 弧(mermaid / node-red 的循环回边画法)。
 *  标签放在弧的最低点——在节点下方,而不是两节点之间。
 *  弧只经过节点行以下的空白区,不会从节点底下穿过被遮盖。
 *  路径手写而不用 getBezierPath:它的控制点偏移对同一行句柄
 *  恒为 0(calculateControlOffset 的 distance>=0 分支),画不出下探。 */
function LoopBackEdge({
  id, sourceX, sourceY, targetX, targetY, markerEnd, style, label,
}: EdgeProps) {
  const { path, labelX, labelY } = getLoopBackGeometry(
    sourceX, sourceY, targetX, targetY,
  );
  return (
    <>
      <BaseEdge id={id} path={path} markerEnd={markerEnd} style={style} />
      {typeof label === 'string' && label && (
        <EdgeLabelRenderer>
          <div
            className="edge-back-label"
            style={{ transform: `translate(-50%, -50%) translate(${labelX}px, ${labelY}px)` }}
          >
            {label}
          </div>
        </EdgeLabelRenderer>
      )}
    </>
  );
}

const edgeTypes = { loopback: LoopBackEdge };

/** 找出回边:回到已执行节点的**条件**边。
 *  判定 1 的 UI 义务:回边画虚线并标注轮数上限。
 *  环上的无条件边不算回边——author→reviewer 虽在环里,但它是正常
 *  前向数据流(循环必须由 when 条件边驱动,否则第一次迭代就是死环);
 *  把它也画成绕行回边会让主数据流失去直连边(第五轮 P5 验收时实测)。 */
function findBackEdges(edges: WFEdge[]): Set<number> {
  const adj = new Map<string, string[]>();
  for (const e of edges) {
    if (e.to !== 'END') adj.set(e.from, [...(adj.get(e.from) ?? []), e.to]);
  }
  const reaches = (from: string, to: string): boolean => {
    const seen = new Set<string>();
    const stack = [...(adj.get(from) ?? [])];
    while (stack.length) {
      const cur = stack.pop()!;
      if (cur === to) return true;
      if (seen.has(cur)) continue;
      seen.add(cur);
      stack.push(...(adj.get(cur) ?? []));
    }
    return false;
  };
  const back = new Set<number>();
  edges.forEach((e, i) => {
    if (e.when && e.to !== 'END' && reaches(e.to, e.from)) back.add(i);
  });
  return back;
}

/** MiniMap 的节点颜色按运行状态映射;显式传入,不赌库的默认变量
 *  (M6-C:主题与对比度必须在两种配色下都成立)。 */
function minimapNodeColor(n: Node, theme: 'dark' | 'light'): string {
  const d = n.data as AtlasNodeData | undefined;
  if (!d || d.isEnd) return theme === 'dark' ? '#3f3f46' : '#d4d4d8';
  const status = d.run?.status ?? 'pending';
  switch (status) {
    case 'running': return '#5b8def';
    case 'done': return '#3fb27f';
    case 'failed': return '#d9534f';
    default: return theme === 'dark' ? '#52525b' : '#a1a1aa';
  }
}

export function GraphView({
  nodes, edges, runNodes, runStatus, selected, onSelect, maxIterations, theme,
}: {
  nodes: WFNode[];
  edges: WFEdge[];
  runNodes: Record<string, RunNode>;
  runStatus?: string;
  selected: string | null;
  onSelect: (id: string) => void;
  maxIterations?: number | null;
  theme: 'dark' | 'light';
}) {
  const backEdges = useMemo(() => findBackEdges(edges), [edges]);
  const nodesInitialized = useNodesInitialized();
  const { fitView } = useReactFlow();
  const fitted = useRef(false);

  // 只在节点首次完成测量时适配一次；Dock 拖动只改变 viewport，不逐帧重置用户视角。
  useEffect(() => {
    if (!nodesInitialized || nodes.length === 0 || fitted.current) return;
    fitted.current = true;
    requestAnimationFrame(() => { fitView({ padding: 0.15 }); });
  }, [nodesInitialized, fitView, nodes.length]);

  const { flowNodes, flowEdges } = useMemo(() => {
    const hasEnd = edges.some((e) => e.to === 'END');
    const ids = [...nodes.map((n) => n.id), ...(hasEnd ? [END_ID] : [])];

    const g = new dagre.graphlib.Graph();
    // marginy 留出回边 U 弧的下探空间:fitView 只按节点包围盒适配,
    // 没有这份余量,弧的最低点(与标签)会被裁出视野
    g.setGraph({ rankdir: 'LR', nodesep: 46, ranksep: 96, marginx: 24, marginy: 110 });
    g.setDefaultEdgeLabel(() => ({}));
    ids.forEach((id) =>
      g.setNode(id, { width: NODE_W, height: id === END_ID ? NODE_H - 24 : NODE_H }));
    edges.forEach((e) => g.setEdge(e.from, e.to === 'END' ? END_ID : e.to));
    dagre.layout(g);

    const flowNodes: AtlasFlowNode[] = ids.map((id) => {
      const pos = g.node(id);
      const isEnd = id === END_ID;
      const height = isEnd ? NODE_H - 24 : NODE_H;
      return {
        id,
        type: 'atlas' as const,
        width: NODE_W,
        height,
        position: { x: pos.x - NODE_W / 2, y: pos.y - height / 2 },
        data: {
          id, model: isEnd ? '' : (nodes.find((n) => n.id === id)?.model ?? ''),
          nodeType: isEnd ? '' : (nodes.find((n) => n.id === id)?.type ?? ''),
          thinking: nodes.find((n) => n.id === id)?.thinking,
          isEnd, run: runNodes[id], runInterrupted: runStatus === 'interrupted',
        },
        selected: selected === id,
      };
    });

    const flowEdges: Edge[] = edges.map((e, i) => {
      const target = e.to === 'END' ? END_ID : e.to;
      // 数据正在这条边上流动:源节点在执行、目标还没完成
      const flowing = runNodes[e.from]?.status === 'running'
        || (runNodes[e.from]?.status === 'done'
            && runNodes[target]?.status === 'pending');
      const isBack = backEdges.has(i);
      const consumedOk = runNodes[target]?.consumed?.some(
        (c) => c.name === `${e.from}.output`);   // 数据完整到达过(红线③的边级展示)
      const label = isBack
        ? [e.when ?? '', maxIterations ? `≤${maxIterations} 轮` : '']
            .filter(Boolean).join(' ')
        : e.when ?? undefined;
      const classes = [
        flowing ? 'edge-live' : '',
        isBack ? 'edge-back' : '',
      ].filter(Boolean).join(' ') || undefined;
      const style = {
        strokeWidth: flowing ? 2 : 1.4,
        stroke: consumedOk ? 'var(--color-done)' : undefined,
      };
      if (isBack) {
        // 回边:底部出、底部入的半椭圆 U 弧,从节点行下方回勾;
        // "≤N 轮"标签挂在弧的最低点,不与正向边争夺节点间的位置
        return {
          id: `e${i}`,
          source: e.from,
          target,
          type: 'loopback',
          sourceHandle: 'back-src',
          targetHandle: 'back-tgt',
          label,
          className: classes,
          animated: false,
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          style,
        };
      }
      // 正向边:直线(链路的自然形态),显式句柄——不显式指定时
      // React Flow 取第一个匹配句柄,句柄声明顺序一变就全错
      return {
        id: `e${i}`,
        source: e.from,
        target,
        type: 'straight',
        sourceHandle: 'out-right',
        targetHandle: 'in-left',
        label,
        labelShowBg: true,
        className: classes,
        animated: false,   // 流动感由 edge-live 的 CSS 虚线动画表达
        markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
        style,
      };
    });
    return { flowNodes, flowEdges };
  }, [nodes, edges, runNodes, selected, backEdges, maxIterations, runStatus]);

  return (
    <ReactFlow
      nodes={flowNodes}
      edges={flowEdges}
      nodeTypes={nodeTypes}
      edgeTypes={edgeTypes}
      onNodeClick={(_, n) => n.id !== END_ID && onSelect(n.id)}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable
      fitView
      minZoom={0.3}
      colorMode={theme}
      proOptions={{ hideAttribution: true }}
    >
      <Background gap={22} size={1.4} />
      <Controls showInteractive={false} />
      <MiniMap
        pannable
        zoomable
        nodeColor={(node) => minimapNodeColor(node, theme)}
        nodeStrokeColor={(node) => (node.id === selected
          ? '#5b8def'
          : minimapNodeColor(node, theme))}
        nodeStrokeWidth={selected ? 2 : 1}
        maskColor={theme === 'dark' ? 'rgba(11,13,18,0.22)' : 'rgba(245,246,249,0.28)'}
        bgColor={theme === 'dark' ? '#10131a' : '#ffffff'}
        style={{ width: 180, height: 120, border: `1px solid ${theme === 'dark' ? '#262c39' : '#e2e6ee'}` }}
      />
      {nodes.length === 0 && <div className="graph-empty-state">当前工作流没有可显示的节点</div>}
    </ReactFlow>
  );
}

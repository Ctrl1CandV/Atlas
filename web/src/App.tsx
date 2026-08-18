import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { motion, useReducedMotion } from 'motion/react';
import { ReactFlowProvider } from '@xyflow/react';
import {
  approveRun,
  deleteRun,
  getRun,
  getWorkflow,
  listProviders,
  listRuns,
  listWorkflows,
  previewWorkflow,
  startRun,
  subscribeRun,
} from './api';
import { ArtifactWorkSpace, type ArtifactViewTarget } from './ArtifactWorkSpace';
import { DockWorkspace } from './DockWorkspace';
import { GraphView } from './GraphView';
import { GuidePage } from './GuidePage';
import { NodeDetail } from './NodeDetail';
import { deriveModelOptions } from './modelOptions';
import { SettingsPage } from './SettingsPage';
import { hrefFor, useRoute } from './router';
import type {
  AtlasEvent,
  NodeOverride,
  NodeOverrides,
  ParamDefaults,
  RunListItem,
  RunNode,
  RunSummary,
  WorkflowListItem,
  WorkflowSpec,
} from './types';

/** 数字滚动:token/成本的累积感(钱在花)。reduced-motion 直接跳。 */
function CountUp({ value }: { value: number }) {
  const [shown, setShown] = useState(value);
  const reduce = useReducedMotion();
  const ref = useRef({ from: value, raf: 0 });
  useEffect(() => {
    if (reduce) { setShown(value); return; }
    const from = ref.current.from;
    const start = performance.now();
    const dur = 420;
    const tick = (now: number) => {
      const t = Math.min(1, (now - start) / dur);
      const eased = 1 - (1 - t) ** 3;
      setShown(Math.round(from + (value - from) * eased));
      if (t < 1) ref.current.raf = requestAnimationFrame(tick);
      else ref.current.from = value;
    };
    const raf = requestAnimationFrame(tick);
    ref.current.raf = raf;
    return () => cancelAnimationFrame(raf);
  }, [value, reduce]);
  return <>{shown.toLocaleString()}</>;
}

/** 顶栏 sparkline:token 随时间的累积曲线(数据全在事件流里,有出处)。 */
function Sparkline({ points }: { points: number[] }) {
  if (points.length < 2) return null;
  const w = 96, h = 22;
  const max = Math.max(...points, 1);
  const step = w / (points.length - 1);
  const coords = points.map((p, i) => `${(i * step).toFixed(1)},${(h - (p / max) * (h - 2) - 1).toFixed(1)}`);
  return (
    <span className="spark" title="token 累积曲线">
      <svg width={w} height={h}>
        <polygon className="fill" points={`0,${h} ${coords.join(' ')} ${w},${h}`} />
        <polyline points={coords.join(' ')} />
      </svg>
    </span>
  );
}

const STATUS_LABEL: Record<string, string> = {
  pending: '等待中',
  paused: '等待批准',
  starting: '启动中',
  running: '运行中',
  done: '已完成',
  failed: '失败',
};

const KIND_BADGE: Record<string, { label: string; cls: string }> = {
  example: { label: '示例', cls: 'badge-done' },
  template: { label: '模板', cls: 'badge-run' },
  custom: { label: '自定义', cls: 'badge-warn' },
};

/** 工作流发现(PLAN-v3 §2.5):示例是起点不是上限——卡片讲清楚
 *  用途、结构、调用量与要求,自定义图与示例同引擎。 */
function WorkflowCard({
  w, active, onOpen,
}: {
  w: WorkflowListItem;
  active: boolean;
  onOpen: () => void;
}) {
  const meta = w.meta;
  const kind = KIND_BADGE[meta?.kind ?? 'custom'] ?? KIND_BADGE.custom;
  return (
    <div
      className={`wf-card ${active ? 'active' : ''} ${w.valid ? '' : 'invalid'}`}
      role="button" tabIndex={0}
      onClick={onOpen}
      onKeyDown={(ev) => (ev.key === 'Enter' || ev.key === ' ') && onOpen()}
      title={w.valid ? (meta?.description || w.description || w.id) : (w.error ?? '')}
    >
      <div className="wf-card-head">
        <span className="li-title">{meta?.title || w.name}</span>
        <span className={`badge ${kind.cls}`}>{kind.label}</span>
      </div>
      <div className="li-sub">
        {w.valid
          ? (meta?.description || w.description || w.id)
          : `校验不过:${w.error?.slice(0, 60)}…`}
      </div>
      {w.valid && (meta?.tags?.length || w.node_count != null || meta?.estimated_calls != null) && (
        <div className="wf-card-meta num">
          {w.node_count != null && <span>{w.node_count} 节点</span>}
          {meta?.estimated_calls != null && <span>约 {meta.estimated_calls} 次调用</span>}
          {meta?.requires?.human_approval && <span className="accent">需人工批准</span>}
          {meta?.requires?.workdir && <span className="accent">需工作目录</span>}
          {meta?.tags?.slice(0, 3).map((t) => <span key={t} className="chip">{t}</span>)}
        </div>
      )}
    </div>
  );
}

export default function App() {
  const [workflows, setWorkflows] = useState<WorkflowListItem[]>([]);
  const [spec, setSpec] = useState<WorkflowSpec | null>(null);
  const [runs, setRuns] = useState<RunListItem[]>([]);
  const [runId, setRunId] = useState<string | null>(null);
  const [summary, setSummary] = useState<RunSummary | null>(null);
  const [events, setEvents] = useState<AtlasEvent[]>([]);
  const [task, setTask] = useState('');
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [deletingRun, setDeletingRun] = useState<string | null>(null);
  const [approvalComment, setApprovalComment] = useState('');
  const [workspaceTarget, setWorkspaceTarget] = useState<ArtifactViewTarget | null>(null);
  const [wfSearch, setWfSearch] = useState('');
  const [wfCategory, setWfCategory] = useState<string>('全部');
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [agentModelOptions, setAgentModelOptions] = useState<string[]>([]);
  const [unconfiguredNodes, setUnconfiguredNodes] = useState<string[] | null>(null);
  const [paramDefaults, setParamDefaults] = useState<Record<string, ParamDefaults> | null>(null);
  const [previewExecutionSha256, setPreviewExecutionSha256] = useState<string | null>(null);
  const [inheritedSpec, setInheritedSpec] = useState<WorkflowSpec | null>(null);
  const [overridesByWorkflow, setOverridesByWorkflow] = useState<Record<string, NodeOverrides>>({});
  const previewSeq = useRef(0);
  // hash 直达(#settings):可收藏、可分享;也方便自动化验证
  const [theme, setTheme] = useState<'dark' | 'light'>(
    () => (localStorage.getItem('atlas-theme') as 'dark' | 'light')
      ?? (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark'));
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  const reduceMotion = useReducedMotion();
  const enter = (delay: number) =>
    reduceMotion
      ? {}
      : {
          initial: { opacity: 0, y: 10 },
          animate: { opacity: 1, y: 0 },
          transition: { type: 'spring' as const, stiffness: 120, damping: 18, delay },
        };
  // hash 路由(PLAN-v3 M6-E):视图/工作流/运行/节点/指南章都有可分享 URL
  const [route, navigateHash] = useRoute();
  const view = route.view;
  const eventsRef = useRef<HTMLDivElement>(null);

  const refreshRuns = useCallback(() => {
    listRuns().then(setRuns).catch((e: Error) => setError(e.message));
  }, []);

  const refreshModelOptions = useCallback(() => {
    return listProviders()
      .then((providers) => {
        const options = deriveModelOptions(providers);
        setModelOptions(options.llm);
        setAgentModelOptions(options.agent);
      })
      .catch(() => {
        setModelOptions([]);
        setAgentModelOptions([]);
      });
  }, []);

  useEffect(() => {
    listWorkflows().then(setWorkflows).catch((e: Error) => setError(e.message));
    refreshModelOptions();
    refreshRuns();
  }, [refreshModelOptions, refreshRuns]);

  const openWorkflow = useCallback((wid: string) => {
    setSelectedNode(null);
    if (route.workflowId === wid && !route.runId) return;
    setRunId(null);
    setSummary(null);
    setEvents([]);
    setSpec(null);
    setInheritedSpec(null);
    setUnconfiguredNodes(null);
    setPreviewExecutionSha256(null);
    navigateHash(hrefFor({ view: 'observe', workflowId: wid, runId: null, nodeId: null }));
  }, [route.workflowId, route.runId, navigateHash]);

  // 历史运行的图只由下方 run 订阅从有效快照恢复；这里不并发重复请求。
  const openRun = useCallback((rid: string, _graph: string | null) => {
    setSelectedNode(null);
    if (route.runId === rid) return;
    setRunId(rid);
    setSummary(null);
    setEvents([]);
    setSpec(null);
    setInheritedSpec(null);
    setUnconfiguredNodes([]);
    setParamDefaults(null);
    navigateHash(hrefFor({ runId: rid, nodeId: null }));
  }, [route.runId, navigateHash]);

  const selectNode = useCallback((id: string | null) => {
    setSelectedNode(id);
    navigateHash(hrefFor({ nodeId: id }));
  }, [navigateHash]);

  // 工作流路由恢复和参数覆盖共用同一预览链：先算 YAML 继承规格，
  // 再用同一份覆盖算最终显示规格。序号确保旧请求不能覆盖新选择。
  useEffect(() => {
    const wid = route.workflowId;
    if (!wid || route.runId) return;
    let alive = true;
    const request = ++previewSeq.current;
    setInheritedSpec(null);
    setUnconfiguredNodes(null);
    const overrides = overridesByWorkflow[wid] ?? {};
    const inheritedPreviewRequest = previewWorkflow(wid);
    const finalPreviewRequest = Object.keys(overrides).length > 0
      ? previewWorkflow(wid, overrides)
      : inheritedPreviewRequest;
    Promise.all([
      getWorkflow(wid),
      inheritedPreviewRequest,
      finalPreviewRequest,
    ]).then(([baseSpec, inheritedPreview, finalPreview]) => {
      if (!alive || request !== previewSeq.current) return;
      setInheritedSpec(inheritedPreview.effective_workflow);
      setSpec(finalPreview.effective_workflow ?? baseSpec);
      setUnconfiguredNodes(finalPreview.unconfigured_nodes ?? []);
      setParamDefaults(finalPreview.param_defaults ?? null);
      setPreviewExecutionSha256(finalPreview.execution_sha256 ?? null);
      setError(null);
    }).catch((e: Error) => {
      if (!alive || request !== previewSeq.current) return;
      setInheritedSpec(null);
      setParamDefaults(null);
      setPreviewExecutionSha256(null);
      getWorkflow(wid).then((baseSpec) => {
        if (!alive || request !== previewSeq.current) return;
        setSpec(baseSpec);
        setUnconfiguredNodes(
          baseSpec.nodes.filter((node) => node.type === 'llm' && !node.model).map((node) => node.id),
        );
      }).catch(() => undefined);
      setError(e.message);
    });
    return () => { alive = false; };
  }, [route.workflowId, route.runId, overridesByWorkflow]);

  useEffect(() => {
    if (route.runId && runId !== route.runId) {
      setRunId(route.runId);
      setSpec(null);
      setInheritedSpec(null);
      setUnconfiguredNodes([]);
    }
  }, [route.runId, runId]);

  useEffect(() => {
    setSelectedNode(route.nodeId);
  }, [route.nodeId]);

  // 切换运行时关闭产物工作区:旧运行的产物路径对新 run 是 404(审查 M6-minor11)
  useEffect(() => { setWorkspaceTarget(null); }, [runId]);

  // 订阅选中 run 的事件流;事件到达时刷新摘要(本地回环,量级无压力)
  useEffect(() => {
    if (!runId) return;
    let alive = true;
    let seq = 0;                 // 请求序号:旧响应直接丢弃,防乱序覆盖
    let timer: ReturnType<typeof setTimeout> | null = null;
    setEvents([]);
    setSummary(null);
    const pull = () => {
      const my = ++seq;
      getRun(runId).then((s) => {
        if (!alive || my !== seq) return;
        setSummary(s);
        if (s.effective_workflow) setSpec(s.effective_workflow);
      }).catch(() => undefined);
    };
    pull();
    const cancel = subscribeRun(
      runId,
      (e) => {
        if (!alive) return;
        setEvents((prev) => [...prev.slice(-199), e]);   // 状态截断,有环长跑不无限涨
        if (['node_started', 'node_done', 'model_failed', 'output_truncated',
          'run_done', 'run_failed', 'run_resumed'].includes(e.type)) {
          if (timer) clearTimeout(timer);   // 150ms 合并:历史重放不打接口风暴
          timer = setTimeout(pull, 150);
        }
      },
      () => refreshRuns(),
    );
    return () => {
      alive = false;
      if (timer) clearTimeout(timer);
      cancel();
    };
  }, [runId, refreshRuns]);

  useEffect(() => {
    const el = eventsRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
    if (nearBottom) el.scrollTo({ top: el.scrollHeight });
  }, [events]);

  const tokenCurve = useMemo(() => {
    const pts: number[] = [0];
    let acc = 0;
    for (const e of events) {
      if (e.type === 'node_done') {
        acc += (Number(e.input_tokens) || 0) + (Number(e.output_tokens) || 0);
        pts.push(acc);
      }
    }
    return pts.slice(-40);
  }, [events]);

  const runNodes: Record<string, RunNode> = useMemo(() => {
    const map: Record<string, RunNode> = {};
    summary?.nodes.forEach((n) => (map[n.id] = n));
    return map;
  }, [summary]);

  const handleRun = async () => {
    if (!spec || !task.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const rid = await startRun(
        spec.id,
        task.trim(),
        overridesByWorkflow[spec.id] ?? {},
        previewExecutionSha256 ?? undefined,
      );
      refreshRuns();
      setRunId(rid);
      setSelectedNode(null);
      navigateHash(hrefFor({ runId: rid, nodeId: null }));   // 深链跟上新运行
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const selectedSpecNode = spec?.nodes.find((n) => n.id === selectedNode) ?? null;
  const workflowOverrides = spec ? (overridesByWorkflow[spec.id] ?? {}) : {};
  const selectedOverride = selectedSpecNode ? (workflowOverrides[selectedSpecNode.id] ?? {}) : {};
  const updateSelectedOverride = useCallback((override: NodeOverride) => {
    if (!spec || !selectedNode || runId) return;
    setOverridesByWorkflow((current) => {
      const workflow = { ...(current[spec.id] ?? {}) };
      if (Object.keys(override).length === 0) delete workflow[selectedNode];
      else workflow[selectedNode] = override;
      return { ...current, [spec.id]: workflow };
    });
  }, [spec, selectedNode, runId]);
  const status = summary?.status ?? (runId ? 'starting' : 'idle');

  const categories = useMemo(() => {
    const cats = new Set<string>(['全部']);
    workflows.forEach((w) => cats.add(w.meta?.category || '其他'));
    return [...cats];
  }, [workflows]);

  const shownWorkflows = useMemo(() => {
    const q = wfSearch.trim().toLowerCase();
    return workflows.filter((w) => {
      if (wfCategory !== '全部' && (w.meta?.category || '其他') !== wfCategory) return false;
      if (!q) return true;
      const hay = [w.name, w.id, w.description,
        w.meta?.title, w.meta?.description, ...(w.meta?.tags ?? [])]
        .filter(Boolean).join(' ').toLowerCase();
      return hay.includes(q);
    });
  }, [workflows, wfSearch, wfCategory]);

  const clearDeletedSelection = useCallback((deletedIds: string[]) => {
    if (!runId || !deletedIds.includes(runId)) return;
    // runId 置空会触发订阅 effect cleanup，立即关闭当前 EventSource。
    setRunId(null);
    setSummary(null);
    setEvents([]);
    setSpec(null);
    setInheritedSpec(null);
    setSelectedNode(null);
    setWorkspaceTarget(null);
    setUnconfiguredNodes(null);
    setParamDefaults(null);
    navigateHash('#/observe');
  }, [runId, navigateHash]);

  const handleDeleteRun = async (run: RunListItem) => {
    if (!window.confirm(`删除运行 ${run.run_id}? 其产物、检查点和工作区副本都会永久删除。`)) {
      return;
    }
    setDeletingRun(run.run_id);
    setError(null);
    try {
      await deleteRun(run.run_id);
      setRuns((current) => current.filter((item) => item.run_id !== run.run_id));
      clearDeletedSelection([run.run_id]);
    } catch (e) {
      setError((e as Error).message);
      refreshRuns();
    } finally {
      setDeletingRun(null);
    }
  };

  const handleApproval = async (decision: 'approve' | 'reject') => {
    if (!runId) return;
    try {
      await approveRun(runId, decision, approvalComment);
      setApprovalComment('');
    } catch (e) {
      setError((e as Error).message);
    }
  };

  return (
    <div className={`app${theme === 'dark' ? ' is-grain' : ''}`}>
      <header className="topbar">
        <div className="brand"><span className="logo" />Atlas</div>
        <div className="metrics" style={{ gap: 10 }}>
          <button
            className="tv-icon-btn"
            title="切换主题"
            onClick={() => {
              const next = theme === 'dark' ? 'light' : 'dark';
              setTheme(next);
              localStorage.setItem('atlas-theme', next);
            }}
          >
            <span style={{ fontSize: 13 }}>{theme === 'light' ? '☀' : '☾'}</span>
          </button>
        </div>
        <div className="view-switch">
          <button className={view === 'observe' ? 'active' : ''}
                  onClick={() => navigateHash('#/observe', view !== 'observe')}>观测台</button>
          <button className={view === 'guide' ? 'active' : ''}
                  onClick={() => navigateHash('#/guide', view !== 'guide')}>使用指南</button>
          <button className={view === 'settings' ? 'active' : ''}
                  onClick={() => navigateHash('#/settings', view !== 'settings')}>设置</button>
        </div>
        {view === 'observe' && (
          <>
            <div className="title">
              {spec ? spec.name : '选择一个工作流'}
              {summary && <span className={`status status-${status}`}>
                {STATUS_LABEL[status] ?? status}
              </span>}
            </div>
            <div className="metrics">
              {summary && (
                <>
                  <span className="m"><span className="v"><CountUp value={summary.nodes_done.length} />/{spec?.nodes.length ?? '—'}</span><span className="k">节点</span></span>
                  <Sparkline points={tokenCurve} />
                  <span className="m"><span className="v"><CountUp value={summary.totals.input_tokens} /></span><span className="k">tokens in</span></span>
                  <span className="m"><span className="v"><CountUp value={summary.totals.output_tokens} /></span><span className="k">out</span></span>
                  <span className="m"><span className="v">
                    {summary.totals.actual_cost_unknown_count > 0
                      ? `$${summary.totals.accounted_cost_usd.toFixed(3)}*`
                      : `$${summary.totals.known_actual_cost_usd.toFixed(3)}`}
                  </span><span className="k">
                    {summary.totals.actual_cost_unknown_count > 0 ? '守卫计入成本' : '实际成本'}
                  </span></span>
                </>
              )}
            </div>
          </>
        )}
      </header>

      {error && <div className="error-bar" onClick={() => setError(null)}>{error}</div>}
      {!runId && view === 'observe' && unconfiguredNodes && unconfiguredNodes.length > 0 && (
        <div className="binding-warning" role="status">
          待选择模型：{unconfiguredNodes.join('、')}。点开节点设置 model 与 thinking 后才能运行。
        </div>
      )}

      {view === 'settings' ? (
        <motion.div className="settings-wrap" {...enter(0.06)}>
          <SettingsPage
            onProvidersChanged={refreshModelOptions}
            onRunsDeleted={(ids) => {
              clearDeletedSelection(ids);
              refreshRuns();
            }}
          />
        </motion.div>
      ) : view === 'guide' ? (
        <motion.div className="settings-wrap" {...enter(0.06)}>
          <GuidePage chapter={route.chapter} onNavigate={(h) => navigateHash(h, true)} />
        </motion.div>
      ) : (
      <motion.div className="body" {...enter(0.06)}>
        <DockWorkspace>
        <aside className="sidebar dock-left">
          <h4>工作流</h4>
          <div className="wf-discover">
            <input
              className="wf-search"
              value={wfSearch}
              onChange={(e) => setWfSearch(e.target.value)}
              placeholder="搜索名称 / 标签…"
              aria-label="搜索工作流"
            />
            <div className="wf-cats">
              {categories.map((c) => (
                <button
                  key={c}
                  className={`chip-btn ${wfCategory === c ? 'active' : ''}`}
                  onClick={() => setWfCategory(c)}
                >{c}</button>
              ))}
            </div>
          </div>
          <div className="wf-list">
            {shownWorkflows.map((w) => (
              <WorkflowCard
                key={w.id}
                w={w}
                active={spec?.id === w.id}
                onOpen={() => openWorkflow(w.id)}
              />
            ))}
            {shownWorkflows.length === 0 && (
              <div className="li-sub" style={{ padding: '8px 2px' }}>
                没有匹配的工作流。这些只是起点——对装了 Atlas skill 的 AI
                描述你的目标,它能生成任何结构合法的新图。
              </div>
            )}
          </div>
          <h4>运行记录</h4>
          <button className="refresh" onClick={refreshRuns}>刷新</button>
          {runs.map((r) => (
            <div
              key={r.run_id}
              className={`list-item ${runId === r.run_id ? 'active' : ''}`}
              role="button" tabIndex={0}
              onClick={() => openRun(r.run_id, r.graph)}
              onKeyDown={(ev) => (ev.key === 'Enter' || ev.key === ' ') && openRun(r.run_id, r.graph)}
            >
              <div className="run-item-head">
                <div className="li-title mono">{r.run_id}</div>
                {(r.status === 'done' || r.status === 'failed') && (
                  <button
                    className="run-delete"
                    disabled={deletingRun === r.run_id}
                    aria-label={`删除运行 ${r.run_id}`}
                    title="删除运行记录"
                    onClick={(ev) => {
                      ev.stopPropagation();
                      void handleDeleteRun(r);
                    }}
                    onKeyDown={(ev) => ev.stopPropagation()}
                  >
                    {deletingRun === r.run_id ? '…' : '删除'}
                  </button>
                )}
              </div>
              <div className="li-sub">
                <span className={`status status-${r.status}`}>{STATUS_LABEL[r.status] ?? r.status}</span>
                {' '}{r.graph ?? ''} · {r.nodes_done} 节点
              </div>
            </div>
          ))}
        </aside>

        <main className="canvas dock-center">
          {spec ? (
            // GraphView 用了 useNodesInitialized/useReactFlow(M6-C 的 MiniMap 修复引入),
            // 这两个 hook 必须在 ReactFlowProvider 内部,否则渲染时直接抛错(白屏)
            <ReactFlowProvider key={spec.id}>
              <GraphView
                nodes={spec.nodes}
                edges={spec.edges}
                runNodes={runNodes}
                selected={selectedNode}
                onSelect={selectNode}
                maxIterations={spec.guards.max_iterations}
                theme={theme}
              />
            </ReactFlowProvider>
          ) : (
            <div className="placeholder">从左侧选择一个工作流(YAML 文件是图的真相,这里只渲染)</div>
          )}
          {summary?.failed_error && (
            <div className="run-error" title={summary.failed_error}>
              失败:{summary.failed_error.slice(0, 200)}
            </div>
          )}
          {summary?.status === 'paused' && (
            <div className="approval-bar">
              <span>⏸ 等待人工批准——点画布上的节点看要审的材料</span>
              <input
                value={approvalComment}
                onChange={(e) => setApprovalComment(e.target.value)}
                placeholder="批复说明(驳回时必填理由)"
              />
              <button className="approve" onClick={() => handleApproval('approve')}>
                批准
              </button>
              <button className="reject" onClick={() => handleApproval('reject')}>
                驳回
              </button>
            </div>
          )}
        </main>

        <aside className="drawer dock-right">
          <div className="drawer-inner">
            {selectedSpecNode ? (
              <>
                <button className="close" onClick={() => selectNode(null)}>×</button>
                <NodeDetail
                  spec={selectedSpecNode}
                  inheritedSpec={inheritedSpec?.nodes.find((n) => n.id === selectedSpecNode.id)}
                  run={runNodes[selectedSpecNode.id]}
                  runId={runId}
                  onOpenArtifact={setWorkspaceTarget}
                  override={selectedOverride}
                  onOverrideChange={updateSelectedOverride}
                  editable={!runId}
                  modelOptions={selectedSpecNode.type === 'research'
                    || selectedSpecNode.type === 'coding_agent'
                    ? agentModelOptions : modelOptions}
                  paramDefaults={paramDefaults?.[selectedSpecNode.id]}
                />
              </>
            ) : (
              <div className="placeholder small">点节点看它的完整输入与输出</div>
            )}
          </div>
        </aside>

      <div className="runbar dock-bottom">
        <textarea
          value={task}
          onChange={(e) => setTask(e.target.value)}
          placeholder={spec ? `要交给「${spec.name}」的任务描述…` : '先选工作流'}
          disabled={!spec || !!runId}
          rows={2}
        />
        <button
          className="run"
          disabled={!spec || !!runId || busy || !task.trim()
            || unconfiguredNodes === null || unconfiguredNodes.length > 0}
          title={unconfiguredNodes && unconfiguredNodes.length > 0
            ? `先为这些节点选择模型：${unconfiguredNodes.join('、')}` : undefined}
          onClick={handleRun}
        >
          {busy ? '启动中…' : '运行'}
        </button>
        <div className="events" ref={eventsRef}>
          {events.length === 0 && <span className="li-sub">事件流会实时出现在这里</span>}
          {events.slice(-120).map((e) => (
            <motion.div key={e.seq} className={`ev ev-${e.type}`}
              initial={reduceMotion ? false : { opacity: 0, y: -8 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}>
              <span className="ev-seq">#{e.seq}</span>
              <span className="ev-type">{e.type}</span>
              {e.node && <span className="ev-node">{e.node}</span>}
              {e.type === 'model_failed' && (
                <span className="ev-reason">{String(e.reason).slice(0, 120)}</span>
              )}
            </motion.div>
          ))}
        </div>
      </div>
      </DockWorkspace>
      </motion.div>
      )}

      {workspaceTarget && (
        <ArtifactWorkSpace
          target={workspaceTarget}
          onClose={() => setWorkspaceTarget(null)}
        />
      )}
    </div>
  );
}

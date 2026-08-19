import { useEffect, useState } from 'react';
import {
  ArrowsOutSimple,
  CheckCircle,
  DownloadSimple,
  GitDiff,
  Warning,
} from '@phosphor-icons/react';
import type { ArtifactViewTarget } from './ArtifactWorkSpace';
import { fetchText, runArtifactUrl, runProjectionUrl } from './api';
import { formatAgentExecution } from './nodeDetailPresentation';
import type { NodeOverride, ParamDefaults, RunNode, WFNode } from './types';
import { TextViewer } from './TextViewer';

function useRemoteText(url: string | null) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    let alive = true;
    setText(null);
    setError(null);
    if (!url) return;
    fetchText(url)
      .then((t) => alive && setText(t))
      .catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, [url]);
  return { text, error };
}

/** 完整性标记:红线③的第一等公民展示。遍历的是 spec 声明的 consumes,
 *  尚未执行/缺失的显示"待消费"而不是整块消失。 */
function IntegrityChip({ run, name }: { run?: RunNode; name: string }) {
  const consumed = run?.consumed?.find((c) => c.name === name);
  if (!consumed) {
    return <span className="integrity-chip pending">{name} · 待消费</span>;
  }
  return (
    <span className="integrity-chip" title={`sha256 ${consumed.sha256.slice(0, 24)}… 读取时已校验`}>
      <CheckCircle size={11} weight="bold" />
      {name} · 已验证
    </span>
  );
}

const CAPABILITY_LABELS: Record<string, string> = {
  effort: 'reasoning_effort 档位',
  budget: '思考 token 预算',
  none: '无思考控制',
  unknown: '未探测',
  unprobed: '未探测',
};

/** 三层思考语义(PLAN-v3 M6-D):能力 / 请求 / 证据,分开展示不混同。 */
function ThinkingRows({ run }: { run?: RunNode }) {
  const th = run?.thinking;
  if (!th && !run?.thinking_tier && !(run?.reasoning_tokens ?? 0)) return null;
  const tierLabel = th
    ? (th.requested_tier === 'provider_default' ? '供应商默认' : th.requested_tier)
    : (run?.thinking_tier ?? '供应商默认');
  let evidence = '供应商未返回思考用量';
  if (th?.evidence.kind === 'reasoning_tokens' && th.evidence.value != null) {
    evidence = `${th.evidence.value.toLocaleString()} reasoning tokens`;
  } else if (th?.evidence.kind === 'thinking_block') {
    evidence = '检测到 thinking block(该协议不给 token 数)';
  } else if (!th && (run?.reasoning_tokens ?? 0) > 1) {
    evidence = `${run?.reasoning_tokens?.toLocaleString()} reasoning tokens`;
  }
  const capLabel = th ? CAPABILITY_LABELS[th.capability] ?? th.capability : null;
  return (
    <>
      {capLabel && <><span className="k">模型思考能力</span><span>{capLabel}</span></>}
      <span className="k">请求档位</span><span>{tierLabel}</span>
      <span className="k">响应证据</span><span className="num">{evidence}</span>
    </>
  );
}

type Tab = 'report' | 'diff' | 'input';

type NumericOverrideKey = 'max_output_tokens' | 'temperature' | 'seed' | 'timeout_s' | 'retry' | 'max_turns';

function parseOptionalNumber(value: string, integer: boolean): number | undefined {
  if (value.trim() === '') return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || (integer && !Number.isInteger(parsed))) return undefined;
  return parsed;
}

function cleanOverride(spec: WFNode, next: NodeOverride): NodeOverride {
  const clean: NodeOverride = {};
  const sameList = (left: string[], right: string[]) =>
    left.length === right.length && left.every((value, index) => value === right[index]);
  if (spec.type === 'llm') {
    if (next.model !== undefined && next.model !== spec.model) clean.model = next.model;
    if (next.fallback !== undefined && !sameList(next.fallback, spec.fallback)) clean.fallback = next.fallback;
    if (next.thinking !== undefined && next.thinking !== spec.thinking) clean.thinking = next.thinking;
    if (next.max_output_tokens !== undefined && next.max_output_tokens !== spec.max_output_tokens) clean.max_output_tokens = next.max_output_tokens;
    if (next.temperature !== undefined && next.temperature !== spec.temperature) clean.temperature = next.temperature;
    if (next.seed !== undefined && next.seed !== spec.seed) clean.seed = next.seed;
  }
  if (spec.type === 'research' || spec.type === 'coding_agent') {
    if (next.model !== undefined && next.model !== spec.model) clean.model = next.model;
    if (next.max_turns !== undefined && next.max_turns !== spec.max_turns) clean.max_turns = next.max_turns;
  }
  if (spec.type === 'coding_agent') {
    if (next.workdir !== undefined && next.workdir !== spec.workdir) clean.workdir = next.workdir;
  }
  if (spec.type === 'llm' || spec.type === 'research' || spec.type === 'coding_agent') {
    if (next.timeout_s !== undefined && next.timeout_s !== spec.timeout_s) clean.timeout_s = next.timeout_s;
    if (next.retry !== undefined && next.retry !== spec.retry) clean.retry = next.retry;
  }
  // 职责文本:所有类型都可覆盖,完整替换(不是追加)
  if (next.prompt !== undefined && next.prompt !== spec.prompt) clean.prompt = next.prompt;
  return clean;
}

function NumericOverrideInput({
  id, value, inherited, integer, nullable, min, max, step, disabled,
  placeholder, hint, onCommit,
}: {
  id: string;
  value: number | null | undefined;
  inherited: number | null | undefined;
  integer: boolean;
  nullable: boolean;
  min?: number;
  max?: number;
  step?: number;
  disabled: boolean;
  /** 空值时灰显的真实生效默认值(后端 preview 提供,前端不自算)。 */
  placeholder?: string;
  /** 一句人话说明,hover 可见。 */
  hint?: string;
  onCommit: (value: number | null | undefined) => void;
}) {
  const shown = value == null ? '' : String(value);
  const [draft, setDraft] = useState(shown);
  useEffect(() => { setDraft(shown); }, [shown, id]);

  const commit = () => {
    if (draft.trim() === '') {
      onCommit(nullable && inherited != null ? null : undefined);
      return;
    }
    const parsed = parseOptionalNumber(draft, integer);
    if (parsed === undefined
        || (min !== undefined && parsed < min)
        || (max !== undefined && parsed > max)) {
      setDraft(shown);
      return;
    }
    onCommit(parsed === inherited ? undefined : parsed);
  };

  return (
    <input
      id={id}
      type="text"
      inputMode={integer ? 'numeric' : 'decimal'}
      value={draft}
      disabled={disabled}
      data-step={step}
      placeholder={placeholder}
      title={hint}
      onChange={(event) => setDraft(event.target.value)}
      onBlur={commit}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') {
          setDraft(shown);
          event.currentTarget.blur();
        }
      }}
    />
  );
}

/** fallback 是有序尝试链:顺序即优先级。chips 让"不重复、不含主模型"
 *  在结构上不可能违反,而不是靠后端报错纠正。 */
function FallbackChips({
  id, value, primary, candidates, disabled, onChange,
}: {
  id: string;
  value: string[];
  primary: string;
  candidates: string[];
  disabled: boolean;
  onChange: (next: string[]) => void;
}) {
  const selectable = candidates.filter((model) => model !== primary && !value.includes(model));
  const byVendor = new Map<string, string[]>();
  for (const model of selectable) {
    const vendor = model.split(':', 1)[0];
    byVendor.set(vendor, [...(byVendor.get(vendor) ?? []), model]);
  }
  const move = (index: number, delta: number) => {
    const next = [...value];
    const target = index + delta;
    if (target < 0 || target >= next.length) return;
    [next[index], next[target]] = [next[target], next[index]];
    onChange(next);
  };
  return (
    <div className="fallback-chips" id={id}>
      {value.length === 0 && (
        <span className="dim">无备选:主模型失败即节点失败</span>
      )}
      {value.map((model, index) => (
        <span key={model} className="chip">
          <span className="chip-order">{index + 1}</span>
          <span className="mono">{model}</span>
          {!disabled && (
            <>
              <button type="button" title="提高优先级" aria-label={`提高 ${model} 优先级`}
                disabled={index === 0} onClick={() => move(index, -1)}>↑</button>
              <button type="button" title="降低优先级" aria-label={`降低 ${model} 优先级`}
                disabled={index === value.length - 1} onClick={() => move(index, 1)}>↓</button>
              <button type="button" title="移除" aria-label={`移除 ${model}`}
                onClick={() => onChange(value.filter((m) => m !== model))}>×</button>
            </>
          )}
        </span>
      ))}
      {!disabled && selectable.length > 0 && (
        <select
          className="chip-add"
          value=""
          aria-label="添加备选模型"
          onChange={(event) => {
            if (event.target.value) onChange([...value, event.target.value]);
          }}
        >
          <option value="">+ 添加备选…</option>
          {[...byVendor.entries()].map(([vendor, models]) => (
            <optgroup key={vendor} label={vendor}>
              {models.map((model) => <option key={model} value={model}>{model}</option>)}
            </optgroup>
          ))}
        </select>
      )}
      {!disabled && selectable.length === 0 && value.length > 0 && (
        <span className="dim">候选已用尽</span>
      )}
    </div>
  );
}

/** 职责文本覆盖:完整替换本次运行的 prompt。并排显示 YAML 继承原文,
 *  审计时能一眼看出模型真正收到了什么。 */
function PromptOverrideEditor({
  id, inherited, effective, disabled, onCommit,
}: {
  id: string;
  inherited: string;
  effective: string;
  disabled: boolean;
  onCommit: (text: string | undefined) => void;
}) {
  const [draft, setDraft] = useState(effective);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => { setDraft(effective); }, [effective, id]);
  const overridden = effective !== inherited;

  const commit = () => {
    if (!draft.trim()) {
      // 空职责文本非法:回到当前生效值,不提交
      setDraft(effective);
      return;
    }
    onCommit(draft === inherited ? undefined : draft);
  };

  return (
    <div className="prompt-override">
      <textarea
        id={id}
        rows={4}
        value={draft}
        disabled={disabled}
        placeholder={inherited}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Escape') {
            setDraft(effective);
            event.currentTarget.blur();
          }
        }}
      />
      <div className="prompt-override-tools">
        <button type="button" className="ghost" disabled={disabled || !overridden}
          onClick={() => { setDraft(inherited); onCommit(undefined); }}>
          恢复继承
        </button>
        <button type="button" className="ghost" onClick={() => setExpanded((v) => !v)}>
          {expanded ? '收起原文' : '对照 YAML 原文'}
        </button>
        <span className="dim">{overridden ? '已覆盖 · 仅本次运行' : '继承 YAML'}</span>
      </div>
      {expanded && <pre className="detail-pre">{inherited}</pre>}
    </div>
  );
}

/** workdir 覆盖:只对 coding_agent 开放。校验与 YAML 同一条路径
 *  (目录必须存在),错误由后端 preview 返回。 */
function WorkdirOverrideInput({
  id, inherited, value, disabled, onCommit,
}: {
  id: string;
  inherited: string;
  value: string;
  disabled: boolean;
  onCommit: (dir: string | undefined) => void;
}) {
  const [draft, setDraft] = useState(value);
  useEffect(() => { setDraft(value); }, [value, id]);
  return (
    <input
      id={id}
      type="text"
      className="mono"
      value={draft}
      disabled={disabled}
      placeholder={inherited}
      title="本次运行改跑的目标项目目录;改动仍只发生在它的隔离副本里"
      onChange={(event) => setDraft(event.target.value)}
      onBlur={() => {
        const next = draft.trim();
        onCommit(next === inherited || next === '' ? undefined : next);
      }}
      onKeyDown={(event) => {
        if (event.key === 'Enter') event.currentTarget.blur();
        if (event.key === 'Escape') {
          setDraft(value);
          event.currentTarget.blur();
        }
      }}
    />
  );
}

/** 节点详情:页签化产物导航(报告/代码改动/完整输入),可下载原文。 */
export function NodeDetail({
  spec, inheritedSpec, run, runId, onOpenArtifact, override,
  onOverrideChange, editable, modelOptions, paramDefaults,
}: {
  spec: WFNode;
  inheritedSpec?: WFNode;
  run?: RunNode;
  runId: string | null;
  onOpenArtifact: (target: ArtifactViewTarget) => void;
  override: NodeOverride;
  onOverrideChange: (override: NodeOverride) => void;
  editable: boolean;
  modelOptions: string[];
  /** 后端 preview 算出的空输入框生效默认值(如供应商 token 上限)。 */
  paramDefaults?: ParamDefaults;
}) {
  const artifacts = run?.artifacts ?? [];
  const diffArt = artifacts.find((a) => a.role === 'diff');
  const reportArt = artifacts.find((a) => a.role === 'report')
    ?? artifacts.find((a) => a.role === 'output');
  // 旧运行可能只有 output_path；优先使用账本中的类型化产物，再兼容历史路径。
  const reportPath = reportArt?.path ?? run?.output_path;

  const [tab, setTab] = useState<Tab>('report');
  useEffect(() => { setTab('report'); }, [spec.id]);

  const projUrl = runId && run?.projection_path
    ? runProjectionUrl(runId, run.projection_path) : null;
  const reportUrl = runId && reportPath
    ? runArtifactUrl(runId, reportPath) : null;
  const proj = useRemoteText(tab === 'input' ? projUrl : null);
  const report = useRemoteText(tab === 'report' ? reportUrl : null);

  const tabs: { key: Tab; label: string; enabled: boolean }[] = [
    { key: 'report', label: '执行报告', enabled: true },
    { key: 'diff', label: '代码改动', enabled: !!diffArt },
    { key: 'input', label: '完整输入', enabled: true },
  ];
  const inherited = inheritedSpec ?? spec;
  const isLlm = inherited.type === 'llm';
  const isAgent = inherited.type === 'research' || inherited.type === 'coding_agent';
  const agentExecution = isAgent ? formatAgentExecution(run?.runner, spec.allow_web) : null;
  // human 节点也能覆盖职责文本(审批时问什么),但没有模型参数
  const canOverride = isLlm || isAgent || inherited.type === 'human';
  const hasOverride = (key: keyof NodeOverride) =>
    Object.prototype.hasOwnProperty.call(override, key);
  const updateOverride = (patch: Partial<NodeOverride>) => {
    onOverrideChange(cleanOverride(inherited, { ...override, ...patch }));
  };
  const overrideValue = (key: NumericOverrideKey) =>
    hasOverride(key) ? override[key] : inherited[key];

  return (
    <div className={`node-detail${runId ? ' is-history' : ''}`}>
      <header>
        <h3>
          {spec.id}
          {run?.status === 'done' && <span className="badge badge-done">完成</span>}
          {run?.status === 'running' && <span className="badge badge-run">执行中</span>}
          {run?.status === 'failed' && <span className="badge badge-fail">失败</span>}
          {run?.degraded && <span className="badge badge-warn"><Warning size={10} /> 降级</span>}
          {run?.output_truncated && <span className="badge badge-warn"><Warning size={10} /> 截断</span>}
        </h3>
        <div className="kv">
          <span className="k">类型</span><span>{spec.type}</span>
          <span className="k">请求模型</span><span className="mono">{run?.model_requested ?? (spec.model || '未配置(待选择)')}</span>
          <span className="k">实际应答</span>
          <span className="mono">{run?.model_used ?? '—'}</span>
          {agentExecution && (
            <>
              <span className="k">执行后端</span>
              <span>{agentExecution.runnerLabel}</span>
              {agentExecution.boundaryNote && (
                <><span className="k">安全边界</span><span>{agentExecution.boundaryNote}</span></>
              )}
              <span className="k">allow_web</span>
              <span>{agentExecution.allowWebLabel}</span>
              <span className="k">联网边界</span>
              <span>{agentExecution.allowWebNote}</span>
            </>
          )}
          {(run?.input_tokens != null || run?.output_tokens != null) && (
            <>
              <span className="k">tokens</span>
              <span className="num">in {run?.input_tokens ?? '—'} / out {run?.output_tokens ?? '—'}</span>
            </>
          )}
          <ThinkingRows run={run} />
          {run?.duration_s !== undefined && (
            <>
              <span className="k">耗时</span>
              <span className="num">{run.duration_s.toFixed(1)}s</span>
            </>
          )}
          {run?.iteration !== undefined && run.iteration > 1 && (
            <>
              <span className="k">执行轮次</span><span>第 {run.iteration} 轮</span>
            </>
          )}
          <span className="k">consumes</span><span className="mono">{spec.consumes.join(', ')}</span>
          {spec.required_fields.length > 0 && (
            <>
              <span className="k">必填字段</span><span className="mono">{spec.required_fields.join(', ')}</span>
            </>
          )}
        </div>
      </header>

      {canOverride && (
        <details className="detail-section node-overrides" open={!runId}>
          <summary className="detail-section-head">
            <h4 className="eyebrow">下次运行参数</h4>
            <span className={`override-mode ${editable ? 'editable' : ''}`}>
              {editable ? '可编辑 · 仅本工作流' : '历史运行 · 只读'}
            </span>
          </summary>
          <div className="override-grid">
            {isLlm && (
              <>
                <label htmlFor={`${spec.id}-model`}>model</label>
                <select
                  id={`${spec.id}-model`}
                  value={hasOverride('model') ? override.model : spec.model}
                  disabled={!editable
                    || (modelOptions.length === 0 && !spec.model && !hasOverride('model'))}
                  title={modelOptions.length === 0 && !spec.model
                    ? '没有已配置密钥的供应商:先到「设置」配置' : undefined}
                  onChange={(event) => updateOverride({
                    model: event.target.value === inherited.model ? undefined : event.target.value,
                  })}
                >
                  <option value="">待选择…</option>
                  {Array.from(new Set([inherited.model, spec.model, ...modelOptions]))
                    .filter(Boolean)
                    .map((model) => (
                      <option key={model} value={model}>{model}</option>
                    ))}
                </select>
                <label htmlFor={`${spec.id}-fallback`} title="失败时的有序尝试链:主模型失败后按顺序尝试,内容不合格同样触发降级">fallback</label>
                <FallbackChips
                  id={`${spec.id}-fallback`}
                  value={hasOverride('fallback') ? override.fallback ?? [] : spec.fallback}
                  primary={hasOverride('model') ? override.model ?? '' : spec.model}
                  candidates={modelOptions}
                  disabled={!editable}
                  onChange={(values) => updateOverride({ fallback: values })}
                />
                <label htmlFor={`${spec.id}-thinking`} title="思考深度档位;不支持的候选会被跳过并记账,不会静默丢掉意图">thinking</label>
                <select
                  id={`${spec.id}-thinking`}
                  value={hasOverride('thinking') ? (override.thinking ?? '') : (inherited.thinking ?? '')}
                  disabled={!editable}
                  onChange={(event) => {
                    const value = event.target.value || null;
                    updateOverride({ thinking: value === (inherited.thinking ?? null) ? undefined : value });
                  }}
                >
                  <option value="">供应商默认</option>
                  {['low', 'medium', 'high', 'xhigh'].map((tier) => <option key={tier}>{tier}</option>)}
                </select>
                <label htmlFor={`${spec.id}-max-output`} title="本次调用最多生成多少 token;打满会触发截断检测并显式失败,不会静默截断">max_output_tokens</label>
                <NumericOverrideInput
                  id={`${spec.id}-max-output`}
                  value={overrideValue('max_output_tokens') as number | null | undefined}
                  inherited={inherited.max_output_tokens}
                  integer nullable min={1} disabled={!editable}
                  placeholder={paramDefaults?.max_output_tokens != null
                    ? `默认 ${paramDefaults.max_output_tokens}(供应商上限)`
                    : '选模型后按供应商上限'}
                  hint="本次调用最多生成多少 token;打满会触发截断检测并显式失败,不会静默截断"
                  onCommit={(value) => updateOverride({ max_output_tokens: value })}
                />
                <label htmlFor={`${spec.id}-temperature`} title="随机性;留空用供应商默认。要可复现就配合 seed">temperature</label>
                <NumericOverrideInput
                  id={`${spec.id}-temperature`}
                  value={overrideValue('temperature') as number | null | undefined}
                  inherited={inherited.temperature}
                  integer={false} nullable min={0} max={2} step={0.1} disabled={!editable}
                  placeholder="留空 = 供应商默认"
                  hint="随机性;留空用供应商默认。要可复现就配合 seed"
                  onCommit={(value) => updateOverride({ temperature: value })}
                />
                <label htmlFor={`${spec.id}-seed`} title="同样输入尽量得到同样输出;多数供应商只是尽力而为,不是保证">seed</label>
                <NumericOverrideInput
                  id={`${spec.id}-seed`}
                  value={overrideValue('seed') as number | null | undefined}
                  inherited={inherited.seed}
                  integer nullable disabled={!editable}
                  placeholder="留空 = 不固定"
                  hint="同样输入尽量得到同样输出;多数供应商只是尽力而为,不是保证"
                  onCommit={(value) => updateOverride({ seed: value })}
                />
              </>
            )}
            {isAgent && (
              <>
                <label htmlFor={`${spec.id}-model`}>model</label>
                <select
                  id={`${spec.id}-model`}
                  value={hasOverride('model') ? override.model : spec.model}
                  disabled={!editable
                    || (modelOptions.length === 0 && !spec.model && !hasOverride('model'))}
                  title={modelOptions.length === 0 && !spec.model
                    ? '没有已配置密钥且提供 Anthropic 兼容端点的供应商' : undefined}
                  onChange={(event) => updateOverride({
                    model: event.target.value === inherited.model ? undefined : event.target.value,
                  })}
                >
                  <option value="">待选择…</option>
                  {Array.from(new Set([inherited.model, spec.model, ...modelOptions]))
                    .filter(Boolean)
                    .map((model) => <option key={model} value={model}>{model}</option>)}
                </select>
                <label htmlFor={`${spec.id}-turns`} title="当前 Claude CLI 无最大轮数参数;该字段保留为规格元数据,硬边界由 timeout_s 与预算承担">max_turns</label>
                <NumericOverrideInput
                  id={`${spec.id}-turns`}
                  value={overrideValue('max_turns') as number | null | undefined}
                  inherited={inherited.max_turns}
                  integer nullable={false} min={1} max={64} disabled={!editable}
                  placeholder={`默认 ${paramDefaults?.max_turns ?? 12}`}
                  hint="agent 的最大对话轮数;上限靠 timeout_s 墙钟兜底"
                  onCommit={(value) => updateOverride({ max_turns: value ?? undefined })}
                />
              </>
            )}
            {inherited.type === 'coding_agent' && (
              <>
                <label htmlFor={`${spec.id}-workdir`} title="本次运行改跑的目标项目目录;改动仍只发生在隔离副本里,原目录不碰">workdir</label>
                <WorkdirOverrideInput
                  id={`${spec.id}-workdir`}
                  inherited={inherited.workdir ?? ''}
                  value={hasOverride('workdir') ? override.workdir ?? '' : inherited.workdir ?? ''}
                  disabled={!editable}
                  onCommit={(dir) => updateOverride({ workdir: dir })}
                />
              </>
            )}
            {(isLlm || isAgent) && (
              <>
                <label htmlFor={`${spec.id}-timeout`} title={isAgent ? 'CLI 子进程超时' : '单次模型调用的超时;与 guards.timeout_s(整个运行的墙钟)不同'}>timeout_s</label>
                <NumericOverrideInput
                  id={`${spec.id}-timeout`}
                  value={overrideValue('timeout_s') as number | null | undefined}
                  inherited={inherited.timeout_s}
                  integer={false} nullable min={0.1} step={0.1} disabled={!editable}
                  placeholder={`默认 ${paramDefaults?.timeout_s ?? (isAgent ? 1800 : 300)}s`}
                  hint={isAgent
                    ? 'CLI 子进程超时;与 guards.timeout_s(整个运行的墙钟)不同'
                    : '单次模型调用的超时;与 guards.timeout_s(整个运行的墙钟)不同'}
                  onCommit={(value) => updateOverride({ timeout_s: value })}
                />
                <label htmlFor={`${spec.id}-retry`} title="传输类错误重试次数;内容不合格不走 retry,走 fallback 链">retry</label>
                <NumericOverrideInput
                  id={`${spec.id}-retry`}
                  value={overrideValue('retry') as number | null | undefined}
                  inherited={inherited.retry}
                  integer nullable={false} min={0} max={10} disabled={!editable}
                  placeholder={`默认 ${paramDefaults?.retry ?? 0}`}
                  hint="传输类错误重试次数;内容不合格不走 retry,走 fallback 链"
                  onCommit={(value) => updateOverride({ retry: value ?? undefined })}
                />
              </>
            )}
            <label htmlFor={`${spec.id}-prompt`} title="完整替换本次运行该节点的职责文本(不是追加);仅本次运行,YAML 不变">prompt</label>
            <PromptOverrideEditor
              id={`${spec.id}-prompt`}
              inherited={inherited.prompt}
              effective={hasOverride('prompt') ? override.prompt ?? inherited.prompt : inherited.prompt}
              disabled={!editable}
              onCommit={(text) => updateOverride({ prompt: text })}
            />
          </div>
          {isLlm && modelOptions.length === 0 && !spec.model && (
            <p className="override-note">
              还没有已配置密钥的供应商:先在「设置」里配置供应商与密钥,再回来选择模型。
            </p>
          )}
          {Object.keys(override).length > 0 && editable && (
            <button className="ghost override-reset" onClick={() => onOverrideChange({})}>恢复工作流默认值</button>
          )}
          <p className="override-note">
            运行参数与职责文本仅覆盖本次运行;不修改节点权限、consumes 或图拓扑——要长期生效就让
            AI 通过 MCP 写入 YAML。
          </p>
        </details>
      )}

      {/* 完整性:这个节点消费的每份上游产物,哈希都验证过 */}
      <section className="detail-section">
        <div className="detail-section-head"><h4 className="eyebrow">完整性(读取时哈希校验)</h4></div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
          {spec.consumes.map((name) => <IntegrityChip key={name} run={run} name={name} />)}
        </div>
      </section>

      {run && run.attempts.length > 0 && (
        <section className="detail-section">
          <div className="detail-section-head"><h4 className="eyebrow">失败尝试({run.attempts.length})</h4></div>
          {run.attempts.map((a, i) => (
            <div key={i} className="attempt">
              <div className="attempt-model mono">{a.model}</div>
              <div className="attempt-reason">{a.reason}</div>
            </div>
          ))}
        </section>
      )}

      <section className="detail-section">
        <div className="detail-section-head"><h4 className="eyebrow">prompt(本次生效,含覆盖)</h4></div>
        <pre className="detail-pre">{spec.prompt}</pre>
      </section>

      <section className="detail-section detail-tabs-section">
        <div
          className="detail-tabs"
          role="tablist"
          aria-label="节点产物"
          onKeyDown={(event) => {
            const enabled = tabs.filter((item) => item.enabled);
            const current = enabled.findIndex((item) => item.key === tab);
            let next = current;
            if (event.key === 'ArrowRight' || event.key === 'ArrowDown') next = (current + 1) % enabled.length;
            else if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') next = (current - 1 + enabled.length) % enabled.length;
            else if (event.key === 'Home') next = 0;
            else if (event.key === 'End') next = enabled.length - 1;
            else return;
            event.preventDefault();
            setTab(enabled[next].key);
            document.getElementById(`${spec.id}-tab-${enabled[next].key}`)?.focus();
          }}
        >
          {tabs.filter((t) => t.enabled).map((t) => (
            <button
              key={t.key}
              id={`${spec.id}-tab-${t.key}`}
              type="button"
              role="tab"
              aria-selected={tab === t.key}
              aria-controls={`${spec.id}-panel-${t.key}`}
              tabIndex={tab === t.key ? 0 : -1}
              className={`detail-tab ${tab === t.key ? 'active' : ''}`}
              onClick={() => setTab(t.key)}
            >
              {t.key === 'diff' && <GitDiff size={12} />} {t.label}
              {t.key === 'diff' && diffArt?.metadata?.files_changed != null && (
                <span className="num dim"> {diffArt.metadata.files_changed}</span>
              )}
            </button>
          ))}
        </div>

        {tab === 'report' && (
          <div
            className="detail-tab-body"
            id={`${spec.id}-panel-report`}
            role="tabpanel"
            aria-labelledby={`${spec.id}-tab-report`}
          >
            <div className="detail-section-head slim">
              <span className="dim">{reportArt ? `${reportArt.media_type ?? '旧产物'} · 按媒体类型查看` : ''}</span>
              <span className="head-actions">
                {reportUrl && reportArt && (
                  <button className="ghost" onClick={() => onOpenArtifact({
                    kind: 'text',
                    title: `${spec.id} 执行报告`,
                    rawUrl: reportUrl,
                    artifact: reportArt,
                  })}>
                    <ArrowsOutSimple size={12} /> 放大查看
                  </button>
                )}
                {reportUrl && <a className="dl-link" href={reportUrl} target="_blank" rel="noreferrer"><DownloadSimple size={11} /> 下载原文</a>}
              </span>
            </div>
            {report.text !== null
              ? <TextViewer text={report.text} artifact={reportArt} />
              : <div className="detail-error">{report.error ?? '尚未完成,没有产物'}</div>}
          </div>
        )}

        {tab === 'diff' && diffArt && runId && (
          <div
            className="detail-tab-body"
            id={`${spec.id}-panel-diff`}
            role="tabpanel"
            aria-labelledby={`${spec.id}-tab-diff`}
          >
            <div className="detail-section-head slim">
              <span className="num dim">
                {diffArt.metadata?.files_changed != null &&
                  `${diffArt.metadata.files_changed} 文件`}
                {diffArt.metadata?.additions != null && (
                  <> <b className="df-add">+{diffArt.metadata.additions}</b>
                    <b className="df-del"> −{diffArt.metadata.deletions}</b></>
                )}
                {diffArt.complete === false && <b className="badge-warn"> 不完整</b>}
              </span>
              <span className="head-actions">
                <button className="ghost" onClick={() => onOpenArtifact({
                  kind: 'diff',
                  title: `代码改动 ${diffArt.name}`,
                  rawUrl: runArtifactUrl(runId, diffArt.path),
                  artifact: diffArt,
                })}>
                  <GitDiff size={12} /> 在工作区审阅
                </button>
              </span>
            </div>
            <div className="detail-error" style={{ borderStyle: 'dashed' }}>
              完整的逐行审阅(文件树 / 分栏 / 行号 / 跳转)在专用工作区打开——这里放摘要。
            </div>
          </div>
        )}

        {tab === 'input' && (
          <div
            className="detail-tab-body"
            id={`${spec.id}-panel-input`}
            role="tabpanel"
            aria-labelledby={`${spec.id}-tab-input`}
          >
            <div className="detail-section-head slim">
              <span className="dim">投影原文 = 模型实际收到的全部文字</span>
              <span className="head-actions">
                {projUrl && (
                  <button className="ghost" onClick={() => onOpenArtifact({
                    kind: 'text',
                    title: `${spec.id} 完整输入(投影)`,
                    rawUrl: projUrl,
                    sha256: run?.projection_sha256,
                  })}>
                    <ArrowsOutSimple size={12} /> 放大查看
                  </button>
                )}
                {projUrl && <a className="dl-link" href={projUrl} target="_blank" rel="noreferrer"><DownloadSimple size={11} /> 下载原文</a>}
              </span>
            </div>
            {proj.text !== null
              ? <TextViewer text={proj.text} />
              : <div className="detail-error">{proj.error ?? '尚未执行,没有投影'}</div>}
          </div>
        )}
      </section>
    </div>
  );
}

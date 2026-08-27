// 与 atlas/web.py 的 API 形状一一对应

/** 类型化产物(PLAN-v3 M6-B):patch 不再靠扩展名猜,角色是封闭枚举。 */
export interface Artifact {
  name: string;            // 逻辑名(consumes 引用它)
  role: 'report' | 'output' | 'diff' | 'projection' | 'raw';
  title: string;
  path: string;
  sha256: string;
  bytes: number;           // -1 = 旧事件未知
  complete?: boolean;
  media_type?: string;       // 旧事件可能没有,由 role 兼容推断
  metadata?: DiffMetadata & Record<string, unknown>;
}

/** diff 产物的 numstat 元数据(后端 _parse_numstat)。 */
export interface DiffMetadata {
  files?: { path: string; additions: number; deletions: number; binary: boolean }[];
  files_changed?: number;
  additions?: number;
  deletions?: number;
  binary_files?: number;
  complete?: boolean;
  note?: string;
}

/** 三层思考语义(PLAN-v3 M6-D):能力 / 请求档位 / 响应证据。 */
export interface ThinkingSummary {
  capability: 'effort' | 'budget' | 'none' | 'unprobed';
  requested_tier: string;  // 'provider_default' | low/medium/high/xhigh
  evidence: {
    kind: 'reasoning_tokens' | 'thinking_block' | 'unknown' | 'none';
    value: number | null;
  };
}

export type CapabilityKind = 'effort' | 'budget' | 'none' | 'unprobed';

export interface WorkflowListItem {
  id: string;
  name: string;
  description: string;
  valid: boolean;
  error: string | null;
  meta?: WorkflowMeta;
  node_count?: number;
  structure_tags?: string[];
}

/** 工作流元数据(YAML meta 块;只影响展示,不参与执行)。 */
export interface WorkflowMeta {
  title: string;
  description: string;
  kind: 'example' | 'template' | 'custom';
  category: string;
  tags: string[];
  estimated_calls?: number | string;
  requires?: { workdir?: boolean; human_approval?: boolean };
  example_task?: string;
}

export interface WFNode {
  id: string;
  type: string;
  model: string;
  fallback: string[];
  prompt: string;
  consumes: string[];
  required_fields: string[];
  route_field: string;
  thinking?: string | null;
  max_output_tokens?: number | null;
  temperature?: number | null;
  seed?: number | null;
  timeout_s?: number | null;
  retry?: number;
  writable?: boolean;
  allow_web?: boolean | null;
  workdir?: string;
  max_turns?: number;
}

export interface NodeOverride {
  model?: string;
  fallback?: string[];
  thinking?: string | null;
  max_output_tokens?: number | null;
  temperature?: number | null;
  seed?: number | null;
  timeout_s?: number | null;
  retry?: number;
  max_turns?: number;
  /** 完整替换本次运行该节点的职责文本(不是追加);仅本次运行,YAML 不变。 */
  prompt?: string;
  /** 仅 coding_agent:本次运行改跑这个目标目录(隔离副本仍从这里复制)。 */
  workdir?: string;
}

export type NodeOverrides = Record<string, NodeOverride>;

/** 账本/预览里的覆盖摘要:prompt 被脱敏成长度+哈希元数据,不是全文。 */
export interface OverrideSummaryEntry {
  node: string;
  fields: Record<string, unknown>;
}

export interface ModelBinding {
  node: string;
  source: 'override' | 'yaml';
  base_model: string;
  model: string;
  fallback: string[];
}

export interface WorkflowPreview {
  effective_workflow: WorkflowSpec;
  base_spec_sha256: string;
  effective_spec_sha256: string;
  /** 完整执行身份；配置不完整时后端返回 null。 */
  execution_sha256?: string | null;
  bindings: ModelBinding[];
  overrides: OverrideSummaryEntry[];
  /** 模型仍为空(待选择)的 llm 节点;预览可见,运行被拒绝。 */
  unconfigured_nodes?: string[];
  /** 本次运行职责文本被完整替换的节点。 */
  prompt_overridden?: string[];
  /** 每个节点空输入框背后的真实生效默认值(后端算,前端只显示)。 */
  param_defaults?: Record<string, ParamDefaults>;
}

/** 参数默认值:llm 节点含模型参数;agent 节点只有 turns/timeout/retry。 */
export interface ParamDefaults {
  max_output_tokens?: number | null;
  temperature?: number | null;
  seed?: number | null;
  timeout_s?: number;
  retry?: number;
  max_turns?: number;
}

export interface WFEdge {
  from: string;
  to: string;
  when: string | null;
}

export interface WorkflowSpec {
  id: string;
  name: string;
  description: string;
  entry: string;
  entries?: string[];
  nodes: WFNode[];
  edges: WFEdge[];
  guards: {
    max_iterations: number | null;
    max_iterations_effective?: number;
    max_cost_usd: number | null;
    timeout_s: number | null;
  };
  meta?: WorkflowMeta;
}

export type RunStatus =
  | 'pending'
  | 'starting'
  | 'running'
  | 'interrupted'
  | 'paused'
  | 'done'
  | 'cancelled'
  | 'failed';

export interface RunListItem {
  run_id: string;
  graph: string | null;
  status: RunStatus;
  nodes_done: number;
  started: string | null;
}

export interface ModelAttempt {
  model: string;
  reason: string;
}

export interface RunNode {
  id: string;
  status: 'pending' | 'running' | 'done' | 'failed' | 'failed_soft';
  attempts: ModelAttempt[];
  model_requested?: string;
  model_used?: string;
  degraded?: boolean;
  output_truncated?: boolean;
  output_path?: string;
  output_sha256?: string;
  artifacts?: Artifact[];
  input_tokens?: number | null;
  output_tokens?: number | null;
  reasoning_tokens?: number;
  thinking_tier?: string | null;
  thinking?: ThinkingSummary;
  duration_s?: number;
  projection_path?: string;
  projection_sha256?: string;
  consumed?: { name: string; path: string; sha256: string }[];
  iteration?: number;
  cost_usd?: number | null;
  runner?: 'local_cli' | 'injected';
  error_class?: string;
  on_error?: string;
  /** 体验债 2b:发送过的 temperature/seed 与供应商回显的比对结论。 */
  param_audit?: Record<string, string> | null;
}

export interface FinaleNodeRecap {
  node: string;
  model_used: string | null;
  duration_s: number | null;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  ts: string | null;
  recap: string;
}

export interface FinaleLlmSummary {
  model: string | null;
  sha256: string | null;
  path: string | null;
  text: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cost_usd: number | null;
  note: string;
}

export interface Finale {
  status: string;
  started_ts: string | null;
  finished_ts: string | null;
  nodes: FinaleNodeRecap[];
  llm_summary: FinaleLlmSummary | null;
  llm_summary_error: { error_type: string | null; error: string | null } | null;
}

export interface RunSummary {
  run_id: string;
  graph: string | null;
  status: RunStatus;
  nodes_done: string[];
  artifacts: Record<string, { name: string; path: string; sha256: string }>;
  nodes: RunNode[];
  totals: {
    input_tokens: number;
    output_tokens: number;
    /** 仅供应商明确返回的实际费用；存在 unknown 时不是完整总额。 */
    known_actual_cost_usd: number;
    accounted_cost_usd: number;
    actual_cost_unknown_count: number;
    outstanding_reserved_usd: number;
    cost_usd: number | null;
  };
  failed_error: string | null;
  finale?: Finale | null;
  effective_workflow?: WorkflowSpec;
  effective_spec?: WorkflowSpec;
  model_bindings?: Record<string, string>;
  binding_warnings?: string[];
}

export interface AtlasEvent {
  seq: number;
  ts: string;
  type: string;
  node?: string;
  [key: string]: unknown;
}

export interface CredentialState {
  configured: boolean;
  source: string;
  writable: boolean;
}

export interface Provider {
  id: string;
  openaiBaseUrl: string | null;
  anthropicBaseUrl: string | null;
  preferTransport: string | null;
  maxOutputTokens: number | null;
  models: string[];
  apiKeyEnv: string;
  credential: CredentialState;
}

export interface DiscoveryResponse {
  ok: boolean;
  models: string[];
  errorKind?: string;
  message?: string;
}

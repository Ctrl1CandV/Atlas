import type {
  AtlasEvent,
  CapabilityKind,
  CredentialState,
  DiscoveryResponse,
  Provider,
  NodeOverrides,
  RunListItem,
  RunSummary,
  WorkflowListItem,
  WorkflowPreview,
  WorkflowSpec,
} from './types';

async function get<T>(url: string): Promise<T> {
  const resp = await fetch(url);
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch { /* keep statusText */ }
    throw new Error(`${resp.status}: ${detail}`);
  }
  return resp.json();
}

export const listWorkflows = () => get<WorkflowListItem[]>('/api/workflows');
export const getWorkflow = (id: string) => get<WorkflowSpec>(`/api/workflows/${id}`);
export const deleteWorkflow = (id: string, allowExample = false) =>
  req<{ deleted: boolean; workflow_id: string; next: string }>(
    `/api/workflows/${encodeURIComponent(id)}${allowExample ? '?allow_example=1' : ''}`, 'DELETE');
export const previewWorkflow = (id: string, nodeOverrides: NodeOverrides = {}) =>
  req<WorkflowPreview>(`/api/workflows/${id}/preview`, 'POST', {
    node_overrides: nodeOverrides,
  });
export const listRuns = () => get<RunListItem[]>('/api/runs');
export const getRun = (rid: string) => get<RunSummary>(`/api/runs/${rid}`);
export const resumeRun = (rid: string) =>
  req<{ run_id: string; status: string }>(
    `/api/runs/${encodeURIComponent(rid)}/resume`, 'POST');
export const deleteRun = (rid: string) =>
  req<{ deleted: string }>(`/api/runs/${encodeURIComponent(rid)}`, 'DELETE');

export interface RunCleanupResult {
  eligible: number;
  deleted: string[];
  failed: { runId: string; error: string }[];
}

/** 串行套用单条删除 API，避免清理操作制造无界并发。 */
export async function deleteCompletedRuns(runs: RunListItem[]): Promise<RunCleanupResult> {
  const completed = runs.filter((run) => run.status === 'done' || run.status === 'failed');
  const result: RunCleanupResult = { eligible: completed.length, deleted: [], failed: [] };
  for (const run of completed) {
    try {
      await deleteRun(run.run_id);
      result.deleted.push(run.run_id);
    } catch (error) {
      result.failed.push({ runId: run.run_id, error: (error as Error).message });
    }
  }
  return result;
}

export async function startRun(
  wid: string,
  task: string,
  nodeOverrides: NodeOverrides,
  expectedExecutionSha256?: string,
): Promise<string> {
  const resp = await fetch(`/api/workflows/${wid}/run`, {
    method: 'POST',
    // X-Atlas-Request:服务端要求的自定义头——浏览器里恶意网页的 no-cors
    // 简单请求带不了它,预检也过不了(防跨站驱动本机花钱)
    headers: { 'Content-Type': 'application/json', 'X-Atlas-Request': '1' },
    body: JSON.stringify({
      task,
      node_overrides: nodeOverrides,
      expected_execution_sha256: expectedExecutionSha256,
    }),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
  return (await resp.json()).run_id as string;
}

export async function fetchText(url: string): Promise<string> {
  const resp = await fetch(url);
  if (!resp.ok) throw new Error(`${resp.status}: ${resp.statusText}`);
  return resp.text();
}

export async function approveRun(
  rid: string,
  decision: 'approve' | 'reject',
  comment: string,
): Promise<void> {
  const resp = await fetch(`/api/runs/${rid}/approve`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Atlas-Request': '1' },
    body: JSON.stringify({ decision, comment }),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
}

export async function cancelRun(rid: string, reason = ''): Promise<void> {
  const resp = await fetch(`/api/runs/${rid}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-Atlas-Request': '1' },
    body: JSON.stringify({ reason }),
  });
  if (!resp.ok) {
    let detail = resp.statusText;
    try {
      detail = (await resp.json()).detail ?? detail;
    } catch { /* keep statusText */ }
    throw new Error(detail);
  }
}

// ── 配置面(供应商/密钥/模型白名单)──────────────────────────

async function req<T>(url: string, method: string, body?: unknown): Promise<T> {
  const resp = await fetch(url, {
    method,
    headers: { 'Content-Type': 'application/json', 'X-Atlas-Request': '1' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let data: unknown = {};
  if (text) {
    try { data = JSON.parse(text); } catch { /* 非 JSON 错误页:保留状态码信息 */ }
  }
  const obj = (typeof data === 'object' && data !== null ? data : {}) as Record<string, unknown>;
  if (!resp.ok) {
    const detail = typeof obj.detail === 'string' ? obj.detail : `${resp.status}: ${resp.statusText}`;
    throw new Error(detail);
  }
  return data as T;
}

export interface InitializationNotice {
  event_id: string;
  created: string[];
}

export const getInitializationNotice = () =>
  req<InitializationNotice | null>('/api/config/initialization', 'GET');

export const acknowledgeInitialization = (eventId: string) =>
  req<{ acknowledged: string }>('/api/config/initialization/ack', 'POST', {
    event_id: eventId,
  });

export const listProviders = () => req<Provider[]>('/api/providers', 'GET');

export const listThinkingCapabilities = () =>
  req<Record<string, { kind: CapabilityKind; evidence?: string }>>(
    '/api/thinking-capabilities', 'GET');

export const createProvider = (body: {
  id: string;
  openaiBaseUrl?: string;
  anthropicBaseUrl?: string;
  apiKey?: string;
}) => req<{ provider: Provider; warnings: string[] }>('/api/providers', 'POST', body);

export const updateProvider = (pid: string, body: {
  openaiBaseUrl?: string;
  anthropicBaseUrl?: string;
  preferTransport?: string | null;
  maxOutputTokens?: number | null;
}) => req<Provider>(`/api/providers/${pid}`, 'PUT', body);

export const deleteProvider = (pid: string) =>
  req<{ deleted: string; note: string }>(`/api/providers/${pid}`, 'DELETE');

export const setProviderKey = (pid: string, value: string) =>
  req<{ credential: CredentialState; warnings: string[] }>(
    `/api/providers/${pid}/key`, 'POST', { value });

export const discoverProviderModels = (pid: string, overrides: {
  openaiBaseUrl?: string;
  anthropicBaseUrl?: string;
  apiKey?: string;
}) => req<DiscoveryResponse>(`/api/providers/${pid}/discover`, 'POST', overrides);

export const setProviderModels = (pid: string, models: string[]) =>
  req<Provider>(`/api/providers/${pid}/models`, 'PUT', { models });

/** 订阅运行事件流;断线自动用 ?after=<seq> 续听(架构 7.5:先拉全量再续听)。
 * 事件空窗可能掐断连接,所以 onerror 不放弃:重连直到收到终态事件。
 * run_interrupted 是非持久控制通知:保留账本游标并明确停止当前订阅。 */
export function subscribeRun(
  rid: string,
  onEvent: (e: AtlasEvent) => void,
  onEnd: () => void,
): () => void {
  let last = 0;
  let stopped = false;
  let es: EventSource | null = null;
  let retries = 0;
  const MAX_RETRIES = 15;   // 无终态事件的 run(进程被杀):别永远重连下去

  const finish = () => {
    stopped = true;
    es?.close();
    onEnd();
  };

  const handleInterrupted = (source: EventSource) => {
    if (stopped || es !== source) return;
    stopped = true;
    source.close();
    // 用当前持久游标构造 UI 信号，但绝不把控制通知写回 last。
    onEvent({ seq: last, ts: '', type: 'run_interrupted' });
  };

  const open = () => {
    if (stopped) return;
    const source = new EventSource(`/api/runs/${rid}/events?after=${last}`);
    es = source;
    source.addEventListener('run_interrupted', () => handleInterrupted(source));
    source.onmessage = (ev) => {
      if (stopped || es !== source) return;
      const e = JSON.parse(ev.data) as AtlasEvent;
      // 兼容短暂的新旧服务端混用；同样不能让旧版 seq:-1 污染游标。
      if (e.type === 'run_interrupted') {
        handleInterrupted(source);
        return;
      }
      if (e.type === 'stream_closed') {
        // 服务端安全阀关流(慢节点可能长时间无事件):不是终态,重连续听
        source.close();
        if (es === source) es = null;
        if (!stopped) setTimeout(open, 1000);
        return;
      }
      if (!Number.isSafeInteger(e.seq) || e.seq <= last) return;
      last = e.seq;
      retries = 0;   // 收到事件说明链路活着,重连预算重置
      onEvent(e);
      if (e.type === 'run_done' || e.type === 'run_failed') finish();
    };
    source.onerror = () => {
      if (stopped || es !== source) return;
      source.close();
      es = null;
      if (++retries <= MAX_RETRIES) {
        setTimeout(open, Math.min(1000 * 2 ** (retries - 1), 15000));  // 指数退避
      } else {
        onEvent({ seq: -1, ts: '', type: 'stream_lost' } as AtlasEvent);
      }
    };
  };

  open();
  return () => {
    stopped = true;
    es?.close();
  };
}

export const basename = (p: string) => p.split(/[\\/]/).pop() ?? p;
export const runArtifactUrl = (rid: string, path: string) =>
  `/api/runs/${rid}/artifacts/${encodeURIComponent(basename(path))}`;
export const runProjectionUrl = (rid: string, path: string) =>
  `/api/runs/${rid}/projections/${encodeURIComponent(basename(path))}`;

import { useCallback, useEffect, useState } from 'react';
import { summarizeRunCleanup } from './runCleanup';
import {
  acknowledgeInitialization,
  createProvider,
  deleteCompletedRuns,
  deleteProvider,
  discoverProviderModels,
  getInitializationNotice,
  listProviders,
  listRuns,
  listThinkingCapabilities,
  setProviderKey,
  setProviderModels,
  updateProvider,
  type InitializationNotice,
} from './api';
import type { CapabilityKind, DiscoveryResponse, Provider } from './types';

/** 思考能力徽章(M6-D):拉取到模型 ≠ 能力已探测。
 *  unprobed 是合法且常见的状态,不得显示成"已配置"。 */
function CapabilityBadge({ kind }: { kind: CapabilityKind | undefined }) {
  if (!kind) return null;
  const map: Record<CapabilityKind, { label: string; cls: string; title: string }> = {
    effort: { label: '思考档位', cls: 'cap-effort', title: '支持 reasoning_effort 档位控制' },
    budget: { label: '思考预算', cls: 'cap-budget', title: '支持思考 token 预算控制' },
    none: { label: '无思考控制', cls: 'cap-none', title: '已探测:该模型没有真实的思考控制' },
    unprobed: { label: '未探测', cls: 'cap-unprobed', title: '尚未探测思考能力——模型可见不等于档位已验证' },
  };
  const m = map[kind];
  return <span className={`cap-badge ${m.cls}`} title={m.title}>{m.label}</span>;
}

function CredentialDot({ p }: { p: Provider }) {
  const c = p.credential;
  const cls = c.configured ? 'ok' : 'missing';
  const title = c.configured
    ? `密钥已配置(${c.source === 'file' ? 'config/.env' : '进程环境'})`
    : `密钥未配置(${p.apiKeyEnv})`;
  return <span className={`cred-dot ${cls}`} title={title} />;
}

function ProviderCard({ p, caps, onChanged }: {
  p: Provider;
  caps: Record<string, { kind: CapabilityKind; evidence?: string }>;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [openai, setOpenai] = useState(p.openaiBaseUrl ?? '');
  const [anthropic, setAnthropic] = useState(p.anthropicBaseUrl ?? '');
  const [keyInput, setKeyInput] = useState('');
  const [busy, setBusy] = useState('');
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err' | 'info'; text: string } | null>(null);
  const [candidates, setCandidates] = useState<string[] | null>(null);
  const [checked, setChecked] = useState<Set<string>>(new Set(p.models));
  const [manual, setManual] = useState('');

  const overrides = useCallback(
    () => ({
      ...(openai.trim() && openai.trim() !== (p.openaiBaseUrl ?? '')
          ? { openaiBaseUrl: openai.trim() } : {}),
      ...(anthropic.trim() && anthropic.trim() !== (p.anthropicBaseUrl ?? '')
          ? { anthropicBaseUrl: anthropic.trim() } : {}),
      ...(keyInput.trim() ? { apiKey: keyInput.trim() } : {}),
    }),
    [openai, anthropic, keyInput, p]);

  async function discover() {
    setBusy('discover');
    setMsg(null);
    try {
      const r: DiscoveryResponse = await discoverProviderModels(p.id, overrides());
      if (r.ok) {
        // 勾选状态:已配置的默认保持勾选;用户之前勾过的赢(不在清单里也保留)
        setChecked((prev) => {
          const next = new Set(p.models);
          for (const m of prev) next.add(m);
          return next;
        });
        setCandidates(r.models);
        setMsg({ kind: 'ok', text: `拉到 ${r.models.length} 个可见模型,勾选要用的` });
      } else {
        setMsg({ kind: 'err', text: r.message || '拉取失败' });
      }
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    } finally {
      setBusy('');
    }
  }

  async function saveModels(list: string[]) {
    setBusy('models');
    try {
      await setProviderModels(p.id, list);
      setMsg({ kind: 'ok', text: `白名单已保存(${list.length} 个模型)` });
      onChanged();
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    } finally {
      setBusy('');
    }
  }

  async function saveAll() {
    setBusy('save');
    setMsg(null);
    try {
      const urlsChanged =
        openai.trim() !== (p.openaiBaseUrl ?? '') ||
        anthropic.trim() !== (p.anthropicBaseUrl ?? '');
      if (urlsChanged) {
        await updateProvider(p.id, {
          openaiBaseUrl: openai.trim(),
          anthropicBaseUrl: anthropic.trim(),
        });
      }
      if (keyInput.trim()) {
        await setProviderKey(p.id, keyInput.trim());
        setKeyInput('');   // 写完即忘:不留任何密钥痕迹在前端状态里
      }
      if (candidates) {
        const list = [...checked];
        await setProviderModels(p.id, list);
      }
      setMsg({ kind: 'ok', text: '已保存' });
      onChanged();
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    } finally {
      setBusy('');
    }
  }

  async function remove() {
    if (!window.confirm(
        `删除供应商 ${p.id}?${p.credential.configured ? '它派生的密钥也会从 .env 里删除。' : ''}`)) {
      return;
    }
    try {
      await deleteProvider(p.id);
      onChanged();
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    }
  }

  const modelsList = candidates ?? p.models;
  const ordered = [
    ...modelsList.filter((m) => checked.has(m)),
    ...modelsList.filter((m) => !checked.has(m)),
  ];

  return (
    <div className={`prov-card ${open ? 'open' : ''}`}>
      <div className="prov-head" role="button" tabIndex={0}
           onClick={() => setOpen(!open)}
           onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setOpen(!open)}>
        <CredentialDot p={p} />
        <span className="prov-name">{p.id}</span>
        <span className="prov-meta">
          {p.models.length} 个模型
          {p.openaiBaseUrl ? ' · openai' : ''}
          {p.anthropicBaseUrl ? ' · anthropic' : ''}
        </span>
        <span className="prov-chevron">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="prov-body">
          <div className="form-grid">
            <label>OpenAI 兼容端点</label>
            <input value={openai} onChange={(e) => setOpenai(e.target.value)}
                   placeholder="https://…/v1(留空表示该供应商没有)" />
            <label>Anthropic 兼容端点</label>
            <input value={anthropic} onChange={(e) => setAnthropic(e.target.value)}
                   placeholder="https://…(留空表示没有)" />
            <label>API 密钥</label>
            <input type="password" value={keyInput}
                   onChange={(e) => setKeyInput(e.target.value)}
                   placeholder={p.credential.configured
                     ? '已配置——输入新值可替换'
                     : `未配置(${p.apiKeyEnv})`} />
          </div>
          <div className="prov-actions">
            <button disabled={!!busy} onClick={discover}>
              {busy === 'discover' ? '拉取中…' : '测试连接并拉取模型'}
            </button>
            <button disabled={!!busy} onClick={saveAll}>
              {busy === 'save' ? '保存中…' : '保存修改'}
            </button>
            <button className="danger" disabled={!!busy} onClick={remove}>删除</button>
          </div>
          {msg && <div className={`prov-msg ${msg.kind}`}>{msg.text}</div>}
          {candidates && (
            <div className="model-picker">
              <div className="picker-head">
                勾选要用的模型(已选 {checked.size}/{candidates.length};
                拉取结果只是候选,不等于都要配)
              </div>
              {ordered.map((m) => (
                <label key={m} className="model-row">
                  <input
                    type="checkbox"
                    checked={checked.has(m)}
                    onChange={(e) => {
                      const next = new Set(checked);
                      if (e.target.checked) next.add(m);
                      else next.delete(m);
                      setChecked(next);
                    }}
                  />
                  <span className="mono">{m}</span>
                  <CapabilityBadge kind={caps[`${p.id}:${m}`]?.kind} />
                  {p.models.includes(m) && <span className="badge badge-ok">已配置</span>}
                </label>
              ))}
              <div className="manual-add">
                <input value={manual} onChange={(e) => setManual(e.target.value)}
                       placeholder="手动添加模型 id(供应商没有列表端点时的退路)" />
                <button disabled={!manual.trim() || !!busy}
                        onClick={() => {
                          const id = manual.trim();
                          const next = new Set(checked);
                          next.add(id);
                          setChecked(next);
                          setManual('');
                        }}>添加</button>
              </div>
              <button className="primary" disabled={!!busy}
                      onClick={() => saveModels([...checked])}>
                保存白名单({checked.size})
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function NewProviderCard({ onChanged }: { onChanged: () => void }) {
  const [open, setOpen] = useState(false);
  const [id, setId] = useState('');
  const [openai, setOpenai] = useState('');
  const [anthropic, setAnthropic] = useState('');
  const [apiKey, setApiKey] = useState('');
  const [msg, setMsg] = useState<{ kind: 'ok' } | { kind: 'err'; text: string } | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      await createProvider({
        id: id.trim(),
        openaiBaseUrl: openai.trim() || undefined,
        anthropicBaseUrl: anthropic.trim() || undefined,
        apiKey: apiKey.trim() || undefined,
      });
      setMsg({ kind: 'ok' });
      setId(''); setOpenai(''); setAnthropic(''); setApiKey('');
      setOpen(false);
      onChanged();
    } catch (e) {
      setMsg({ kind: 'err', text: (e as Error).message });
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={`prov-card new ${open ? 'open' : ''}`}>
      <div className="prov-head" role="button" tabIndex={0}
           onClick={() => setOpen(!open)}
           onKeyDown={(e) => (e.key === 'Enter' || e.key === ' ') && setOpen(!open)}>
        <span className="prov-name dim">+ 新建供应商</span>
        <span className="prov-chevron">{open ? '▾' : '▸'}</span>
      </div>
      {open && (
        <div className="prov-body">
          <div className="form-grid">
            <label>供应商 id</label>
            <input value={id} onChange={(e) => setId(e.target.value)}
                   placeholder="字母开头,如 Siliconflow" />
            <label>OpenAI 兼容端点</label>
            <input value={openai} onChange={(e) => setOpenai(e.target.value)}
                   placeholder="https://…/v1(与下一条至少填一个)" />
            <label>Anthropic 兼容端点</label>
            <input value={anthropic} onChange={(e) => setAnthropic(e.target.value)}
                   placeholder="https://…" />
            <label>API 密钥</label>
            <input type="password" value={apiKey}
                   onChange={(e) => setApiKey(e.target.value)}
                   placeholder="存入 config/.env(保存后不再回显)" />
          </div>
          <div className="prov-actions">
            <button className="primary" disabled={busy || !id.trim()}
                    onClick={create}>{busy ? '创建中…' : '创建'}</button>
          </div>
          {msg?.kind === 'err' && <div className="prov-msg err">{msg.text}</div>}
        </div>
      )}
    </div>
  );
}

export function SettingsPage({ onProvidersChanged, onRunsDeleted }: {
  onProvidersChanged?: () => void;
  onRunsDeleted?: (deletedIds: string[]) => void;
}) {
  const [providers, setProviders] = useState<Provider[]>([]);
  const [caps, setCaps] = useState<Record<string, { kind: CapabilityKind; evidence?: string }>>({});
  const [error, setError] = useState<string | null>(null);
  const [initialization, setInitialization] = useState<InitializationNotice | null>(null);
  const [initializationBusy, setInitializationBusy] = useState(false);
  const [cleanupBusy, setCleanupBusy] = useState(false);
  const [cleanupMessage, setCleanupMessage] = useState<{
    kind: 'ok' | 'err' | 'info'; text: string;
  } | null>(null);

  const refresh = useCallback(() => {
    listProviders().then((rows) => {
      setProviders(rows);
      onProvidersChanged?.();
    }).catch((e: Error) => setError(e.message));
    listThinkingCapabilities().then(setCaps).catch(() => undefined);
  }, [onProvidersChanged]);
  useEffect(refresh, [refresh]);
  useEffect(() => {
    getInitializationNotice()
      .then(setInitialization)
      .catch((e: Error) => setError(e.message));
  }, []);

  async function acknowledgeInitializationNotice() {
    if (!initialization) return;
    setInitializationBusy(true);
    try {
      await acknowledgeInitialization(initialization.event_id);
      setInitialization(null);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setInitializationBusy(false);
    }
  }

  async function cleanupCompletedRuns() {
    setCleanupBusy(true);
    setCleanupMessage(null);
    try {
      const runs = await listRuns();
      const eligible = runs.filter((run) => run.status === 'done' || run.status === 'failed');
      if (eligible.length === 0) {
        setCleanupMessage({ kind: 'info', text: '没有可清理的已完成运行。' });
        return;
      }
      if (!window.confirm(
        `清理 ${eligible.length} 条已完成或失败的运行记录？其产物、检查点和工作区副本都会永久删除。`,
      )) return;

      const result = await deleteCompletedRuns(runs);
      onRunsDeleted?.(result.deleted);
      setCleanupMessage(summarizeRunCleanup(result));
    } catch (e) {
      setCleanupMessage({ kind: 'err', text: (e as Error).message });
    } finally {
      setCleanupBusy(false);
    }
  }

  return (
    <div className="settings-page">
      <h3>模型与供应商</h3>
      <p className="dim">
        界面改的是环境(供应商、模型白名单、密钥),图的定义仍在 YAML 文件里。
        API 里可见的模型不等于要全配——白名单是你勾选的结果;
        「未探测」表示思考能力还没验证,不代表模型不好。
      </p>
      {initialization && (
        <div className="prov-msg info" role="status">
          <strong>已从通用模板初始化缺失的本机配置。</strong>
          <span className="mono">{initialization.created.join('、')}</span>
          <button type="button" className="ghost" disabled={initializationBusy}
            onClick={acknowledgeInitializationNotice}>
            {initializationBusy ? '确认中…' : '知道了'}
          </button>
        </div>
      )}
      {error && <div className="prov-msg err">{error}</div>}
      {providers.map((p) => (
        <ProviderCard key={p.id} p={p} caps={caps} onChanged={refresh} />
      ))}
      <NewProviderCard onChanged={refresh} />

      <section className="run-cleanup">
        <h3>运行记录</h3>
        <p className="dim">
          清理所有已完成或失败的运行目录。暂停中和运行中的记录不会删除；
          每条记录仍由同一删除接口复核状态与锁。
        </p>
        <div className="prov-actions">
          <button className="danger" disabled={cleanupBusy} onClick={cleanupCompletedRuns}>
            {cleanupBusy ? '清理中…' : '清理全部已完成'}
          </button>
        </div>
        {cleanupMessage && (
          <div className={`prov-msg ${cleanupMessage.kind}`}>{cleanupMessage.text}</div>
        )}
      </section>
    </div>
  );
}

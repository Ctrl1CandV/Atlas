import { SafeMarkdown } from './SafeMarkdown';
import type { Finale } from './types';

const STATUS_LABEL: Record<string, string> = {
  done: '完成',
  failed: '失败',
  cancelled: '已取消',
};

function fmtDuration(s: number | null | undefined): string {
  if (s == null) return '—';
  return s >= 100 ? `${Math.round(s)}s` : `${s.toFixed(1)}s`;
}

function fmtTokens(n: number | null | undefined): string {
  return n == null ? '—' : n.toLocaleString();
}

function fmtCost(c: number | null | undefined): string {
  return c == null ? '成本未知' : `$${c < 0.0001 ? c.toExponential(1) : c.toFixed(4)}`;
}

/**
 * S1 终局卡片:run 到终态后运行页顶部的零成本总结视图。
 * 数据纯由事件账本派生(后端 build_finale),无 LLM 也能渲染;
 * llm_summary 是 opt-in 总结调用的产物,始终带着
 * 「LLM 叙述,事实以账本为准」的标注。
 */
export function FinaleCard({ finale }: { finale: Finale }) {
  const totalDuration = finale.nodes.reduce(
    (acc, n) => acc + (n.duration_s ?? 0), 0);
  const maxDuration = finale.nodes.reduce(
    (acc, n) => Math.max(acc, n.duration_s ?? 0), 0);
  return (
    <section className="finale-card" aria-label="终局总结">
      <header className="finale-head">
        <span className={`finale-status finale-status-${finale.status}`}>
          {STATUS_LABEL[finale.status] ?? finale.status}
        </span>
        <span className="finale-title">终局总结</span>
        <span className="finale-meta">
          {finale.nodes.length} 次节点完成 · 节点累计 {fmtDuration(totalDuration)}
          {finale.finished_ts
            ? ` · 收官 ${new Date(finale.finished_ts).toLocaleTimeString()}`
            : ''}
        </span>
      </header>
      <ol className="finale-nodes">
        {finale.nodes.map((n, i) => (
          <li key={`${n.node}-${i}`} className="finale-node">
            <div className="finale-node-main">
              <span className="finale-node-name">{n.node}</span>
              <span className="finale-node-model">{n.model_used ?? '—'}</span>
              <span className="finale-node-facts">
                {fmtDuration(n.duration_s)}
                <span className="dot">·</span>
                {fmtTokens(n.input_tokens)}/{fmtTokens(n.output_tokens)} tok
                <span className="dot">·</span>
                {fmtCost(n.cost_usd)}
              </span>
              <span
                className="finale-node-bar"
                style={{
                  width: `${
                    maxDuration > 0 && n.duration_s
                      ? Math.max(2, (n.duration_s / maxDuration) * 100)
                      : 2}%`,
                }}
                aria-hidden="true"
              />
            </div>
            <p className="finale-node-recap">{n.recap}</p>
          </li>
        ))}
      </ol>
      {finale.llm_summary && (
        <div className="finale-llm">
          <div className="finale-llm-head">
            <span className="finale-badge">{finale.llm_summary.note}</span>
            <span className="finale-llm-model">{finale.llm_summary.model}</span>
          </div>
          <SafeMarkdown>{finale.llm_summary.text}</SafeMarkdown>
        </div>
      )}
      {finale.llm_summary_error && (
        <div className="finale-llm-error" role="status">
          总结调用失败({finale.llm_summary_error.error_type}):
          {finale.llm_summary_error.error}
        </div>
      )}
    </section>
  );
}

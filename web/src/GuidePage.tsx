import { useMemo } from 'react';
import { SafeMarkdown } from './SafeMarkdown';
import overview from './guide/overview.md?raw';
import quickstart from './guide/quickstart.md?raw';
import concepts from './guide/concepts.md?raw';
import examples from './guide/examples.md?raw';
import models from './guide/models.md?raw';
import results from './guide/results.md?raw';
import safety from './guide/safety.md?raw';
import mcpHuman from './guide/mcp-human.md?raw';
import troubleshooting from './guide/troubleshooting.md?raw';
import development from './guide/development.md?raw';

// 固定 manifest:章节在构建期锁定,不接受运行时指定任意文件(PLAN-v3 §6.5)
const CHAPTERS: { id: string; title: string; body: string }[] = [
  { id: 'overview', title: '概览', body: overview },
  { id: 'quickstart', title: '五分钟开始', body: quickstart },
  { id: 'concepts', title: '工作流概念', body: concepts },
  { id: 'examples', title: '示例导览', body: examples },
  { id: 'models', title: '模型与思考', body: models },
  { id: 'results', title: '运行结果与 Diff', body: results },
  { id: 'safety', title: '沙箱与隐私', body: safety },
  { id: 'mcp-human', title: 'MCP 与人工审批', body: mcpHuman },
  { id: 'troubleshooting', title: '常见问题', body: troubleshooting },
  { id: 'development', title: '开发者构建', body: development },
];

export function GuidePage({
  chapter, onNavigate,
}: {
  chapter: string;
  onNavigate: (hash: string) => void;
}) {
  const current = useMemo(
    () => CHAPTERS.find((c) => c.id === chapter) ?? CHAPTERS[0],
    [chapter]);
  const idx = CHAPTERS.indexOf(current);
  const prev = idx > 0 ? CHAPTERS[idx - 1] : null;
  const next = idx < CHAPTERS.length - 1 ? CHAPTERS[idx + 1] : null;

  return (
    <div className="guide-page">
      <aside className="guide-nav">
        <h4 className="eyebrow">使用指南</h4>
        {CHAPTERS.map((c) => (
          <button
            key={c.id}
            className={`guide-nav-item ${c.id === current.id ? 'active' : ''}`}
            onClick={() => onNavigate(`#/guide/${c.id}`)}  // 同章重复点击由路由去重
          >{c.title}</button>
        ))}
      </aside>
      <div className="guide-body">
        <article className="guide-article">
          {/* 构建期指南与运行产物共用同一安全边界。 */}
          <SafeMarkdown onNavigate={onNavigate}>{current.body}</SafeMarkdown>
          <div className="guide-pager">
            {prev
              ? <button className="ghost" onClick={() => onNavigate(`#/guide/${prev.id}`)}>← {prev.title}</button>
              : <span />}
            {next
              ? <button className="ghost" onClick={() => onNavigate(`#/guide/${next.id}`)}>{next.title} →</button>
              : <span />}
          </div>
        </article>
      </div>
    </div>
  );
}

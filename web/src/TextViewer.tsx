import { useEffect, useMemo, useState } from 'react';
import { Virtuoso } from 'react-virtuoso';
import { SafeMarkdown } from './SafeMarkdown';
import type { Artifact } from './types';
import {
  ArrowsInLineHorizontal,
  ArrowsOutLineHorizontal,
  Article,
  Code,
  MagnifyingGlass,
  X,
} from '@phosphor-icons/react';

type ViewerMode = 'text' | 'markdown' | 'json';

/** media_type is authoritative; role only preserves old artifacts without it. */
function artifactViewerMode(artifact?: Artifact): ViewerMode {
  const mediaType = artifact?.media_type?.split(';', 1)[0].trim().toLowerCase();
  if (mediaType === 'text/markdown') return 'markdown';
  if (mediaType === 'application/json') return 'json';
  if (mediaType === 'text/plain') return 'text';
  if (!mediaType && (artifact?.role === 'report' || artifact?.role === 'output')) {
    return 'markdown';
  }
  return 'text';
}

/** Long text viewer with an explicit rendered/raw state and virtualized raw mode. */
export function TextViewer({
  text,
  mode,
  artifact,
  fill = false,
}: {
  text: string;
  mode?: ViewerMode;
  artifact?: Artifact;
  /** 大窗口模式:虚拟列表与渲染视图填满容器高度(内嵌预览固定 300px)。 */
  fill?: boolean;
}) {
  const resolvedMode = mode ?? artifactViewerMode(artifact);
  const displayText = useMemo(() => {
    if (resolvedMode !== 'json') return text;
    try {
      return JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      return text;
    }
  }, [text, resolvedMode]);
  const [wrap, setWrap] = useState(true);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(false);
  const [rendered, setRendered] = useState(resolvedMode === 'markdown');

  useEffect(() => {
    setRendered(resolvedMode === 'markdown');
    setQuery('');
    setActive(false);
  }, [resolvedMode, artifact?.path]);

  const lines = useMemo(
    () => displayText.replace(/\r/g, '').split('\n'), [displayText]);
  const q = query.trim().toLowerCase();

  const decorated = useMemo(() => {
    if (!q) return null;
    return lines.map((line) => {
      const row: { text: string; hit: boolean }[] = [];
      let rest = line;
      while (rest) {
        const index = rest.toLowerCase().indexOf(q);
        if (index < 0) { row.push({ text: rest, hit: false }); break; }
        if (index > 0) row.push({ text: rest.slice(0, index), hit: false });
        row.push({ text: rest.slice(index, index + q.length), hit: true });
        rest = rest.slice(index + q.length);
      }
      if (row.length === 0) row.push({ text: line, hit: false });
      return row;
    });
  }, [lines, q]);

  const hitCount = useMemo(
    () => (q ? lines.filter((line) => line.toLowerCase().includes(q)).length : 0),
    [lines, q]);
  const canRender = resolvedMode === 'markdown';

  return (
    <div className={`textviewer${fill ? ' fill' : ''}`}>
      <div className="tv-toolbar">
        <span className="num tv-count">
          {displayText.length.toLocaleString()} 字符 · {lines.length.toLocaleString()} 行
          {resolvedMode === 'json' && ' · JSON 格式化原文'}
        </span>
        {canRender && (
          <div className="tv-toggle" role="group" aria-label="Markdown 查看方式">
            <button
              className={rendered ? 'on' : ''}
              aria-pressed={rendered}
              onClick={() => setRendered(true)}
            ><Article size={12} /> 渲染</button>
            <button
              className={!rendered ? 'on' : ''}
              aria-pressed={!rendered}
              onClick={() => setRendered(false)}
            ><Code size={12} /> 原文</button>
          </div>
        )}
        {!rendered && (active ? (
          <span className="tv-search">
            <MagnifyingGlass size={12} weight="bold" />
            <input
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              placeholder="搜索…"
              autoFocus
            />
            {q && <span className="num tv-hits">{hitCount} 行命中</span>}
            <button className="tv-icon-btn" aria-label="关闭搜索" onClick={() => { setActive(false); setQuery(''); }}>
              <X size={12} />
            </button>
          </span>
        ) : (
          <button className="tv-icon-btn" title="搜索" onClick={() => setActive(true)}>
            <MagnifyingGlass size={13} />
          </button>
        ))}
        {!rendered && (
          <button
            className="tv-icon-btn"
            title={wrap ? '关闭折行' : '开启折行'}
            onClick={() => setWrap(!wrap)}
          >
            {wrap ? <ArrowsInLineHorizontal size={13} /> : <ArrowsOutLineHorizontal size={13} />}
          </button>
        )}
      </div>
      {rendered ? (
        <div className="tv-md"><SafeMarkdown>{text}</SafeMarkdown></div>
      ) : (
        <Virtuoso
          style={fill ? { height: '100%', minHeight: 0 } : { height: 300 }}
          className="tv-body"
          totalCount={lines.length}
          itemContent={(index) =>
            decorated ? (
              <div className={`tv-line${decorated[index]?.some((part) => part.hit) ? ' tv-hit' : ''} ${wrap ? '' : 'tv-nowrap'}`}>
                {decorated[index]?.map((part, partIndex) =>
                  part.hit ? <mark key={partIndex}>{part.text}</mark> : <span key={partIndex}>{part.text}</span>)}
              </div>
            ) : (
              <div className={`tv-line ${wrap ? '' : 'tv-nowrap'}`}>{lines[index]}</div>
            )
          }
        />
      )}
    </div>
  );
}

import { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import {
  ArrowsOutSimple,
  ArrowDown,
  ArrowUp,
  CheckCircle,
  CopySimple,
  DownloadSimple,
  File,
  FilePlus,
  FileMinus,
  FileArrowUp,
  FileDashed,
  MagnifyingGlass,
  TextAa,
  Warning,
  X,
} from '@phosphor-icons/react';
import { fetchText } from './api';
import {
  deriveSplitRows,
  parseUnifiedDiff,
  type DiffFile,
  type DiffLine,
  type SplitDiffRow,
} from './diffParse';
import type { Artifact } from './types';

const BIG_FILE_LINES = 4000;   // 超过则默认折叠内容,点开再渲染(大 patch 不卡界面)

function StatusGlyph({ status }: { status: DiffFile['status'] }) {
  const props = { size: 13, weight: 'bold' as const };
  if (status === 'added') return <FilePlus {...props} className="df-add" />;
  if (status === 'deleted') return <FileMinus {...props} className="df-del" />;
  if (status === 'renamed') return <FileArrowUp {...props} className="df-ren" />;
  if (status === 'binary') return <FileDashed {...props} className="df-bin" />;
  return <File {...props} className="df-mod" />;
}

const lineNumber = (number: number | null) => (number === null ? '' : String(number));
const lineMarker = (line: DiffLine | null) => {
  if (!line || line.type === 'meta') return '';
  if (line.type === 'add') return '+';
  if (line.type === 'del') return '-';
  return ' ';
};

function UnifiedLineRow({ line }: { line: DiffLine }) {
  if (line.type === 'meta') {
    return (
      <div className="dl dl-meta dl-meta-row">
        <span className="ln" /><span className="ln" /><span className="dl-sign" />
        <code>{line.text}</code>
      </div>
    );
  }
  return (
    <div className={`dl dl-${line.type}`}>
      <span className="ln num">{lineNumber(line.oldNumber)}</span>
      <span className="ln num">{lineNumber(line.newNumber)}</span>
      <span className="dl-sign" aria-hidden>{lineMarker(line)}</span>
      <code>{line.text || ' '}</code>
    </div>
  );
}

function SplitCell({ line, side }: { line: DiffLine | null; side: 'old' | 'new' }) {
  const number = side === 'old' ? line?.oldNumber ?? null : line?.newNumber ?? null;
  return (
    <div className={`dl-split-cell ${line ? `dl-${line.type}` : 'dl-empty'}`}>
      <span className="ln num">{lineNumber(number)}</span>
      <span className="dl-sign" aria-hidden>{lineMarker(line)}</span>
      <code>{line?.text || ' '}</code>
    </div>
  );
}

function SplitLineRow({ row }: { row: SplitDiffRow }) {
  if (row.type === 'meta') {
    return <div className="dl-split-meta mono">{row.line.text}</div>;
  }
  return (
    <div className="dl-split-row">
      <SplitCell line={row.oldLine} side="old" />
      <SplitCell line={row.newLine} side="new" />
    </div>
  );
}

/** 代码改动专用工作区(PLAN-v3 M6-B §3.6):解析 unified patch,
 *  按 file → hunk → line 渲染;不重新计算 diff,不做 HTML 注入。 */
export function DiffWorkSpace({
  artifact, rawUrl, onClose,
}: {
  artifact: Artifact;
  rawUrl: string;
  onClose: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [split, setSplit] = useState(false);
  const [wrap, setWrap] = useState(false);
  const [filter, setFilter] = useState('');
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [activeFile, setActiveFile] = useState<number>(0);
  const [copied, setCopied] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);
  const fileRefs = useRef<(HTMLDivElement | null)[]>([]);

  useEffect(() => {
    let alive = true;
    fetchText(rawUrl)
      .then((t) => alive && setText(t))
      .catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, [rawUrl]);

  const parsed = useMemo(() => (text === null ? null : parseUnifiedDiff(text)), [text]);
  const meta = useMemo(() => artifact.metadata ?? {}, [artifact.metadata]);

  // 元数据(后端 numstat)与解析结果互补:解析失败时摘要仍可用
  const stats = useMemo(() => {
    if (parsed && !parsed.parseError) {
      return {
        files: parsed.files.length,
        additions: parsed.additions,
        deletions: parsed.deletions,
        binary: parsed.files.filter((f) => f.status === 'binary').length,
      };
    }
    return {
      files: meta.files_changed ?? meta.files?.length ?? 0,
      additions: meta.additions ?? 0,
      deletions: meta.deletions ?? 0,
      binary: meta.binary_files ?? 0,
    };
  }, [parsed, meta]);

  const shownFiles = useMemo(() => {
    if (!parsed || parsed.parseError) return [];
    if (!filter.trim()) return parsed.files;
    const q = filter.trim().toLowerCase();
    return parsed.files.filter(
      (f) => f.oldPath.toLowerCase().includes(q) || f.newPath.toLowerCase().includes(q));
  }, [parsed, filter]);

  // O(1) 取文件序号:indexOf 对 N 文件是 O(N²)(审查 M6-suggest1)
  const fileIndex = useMemo(
    () => new Map((parsed?.files ?? []).map((f, i) => [f, i])),
    [parsed]);

  useEffect(() => {
    // 文件很多时默认折叠内容,只留文件头(大 patch 渲染预算)
    if (parsed && parsed.files.length > 6) {
      setCollapsed(new Set(parsed.files.map((_, i) => i)));
    }
  }, [parsed]);

  const jumpToFile = (i: number) => {
    setActiveFile(i);
    setCollapsed((s) => { const n = new Set(s); n.delete(i); return n; });
    requestAnimationFrame(() => {
      fileRefs.current[i]?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  };

  const navChange = (dir: 1 | -1) => {
    const el = scrollRef.current;
    if (!el) return;
    const anchors = Array.from(el.querySelectorAll<HTMLElement>('[data-change-anchor]'));
    if (anchors.length === 0) return;
    const mid = el.scrollTop + el.clientHeight / 3;
    let target: HTMLElement | undefined;
    if (dir === 1) target = anchors.find((a) => a.offsetTop >= mid + 8);
    else target = [...anchors].reverse().find((a) => a.offsetTop < mid - 8);
    (target ?? anchors[dir === 1 ? 0 : anchors.length - 1])
      .scrollIntoView({ behavior: 'smooth', block: 'center' });
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      // 输入控件里的按键不拦截:过滤框打字不能触发跳转/关闭(审查 M6-minor10)
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                 || el.isContentEditable)) return;
      if (e.key === 'Escape') onClose();
      if (e.key === 'j' || e.key === 'n') navChange(1);
      if (e.key === 'k' || e.key === 'p') navChange(-1);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const copyPatch = async () => {
    if (text === null) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* 剪贴板权限被拒:静默,不阻塞 */ }
  };

  return (
    <AnimatePresence>
      <motion.div
        className="diff-ws-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
      >
        <motion.div
          className="diff-ws"
          initial={{ y: 26, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 18, opacity: 0 }}
          transition={{ type: 'spring', stiffness: 320, damping: 34 }}
          role="dialog"
          aria-label={`代码改动 ${artifact.name}`}
        >
          <header className="diff-ws-head">
            <div className="diff-ws-title">
              <h3>代码改动</h3>
              <span className="mono dim">{artifact.name}</span>
              <span className={`badge ${artifact.complete === false ? 'badge-warn' : 'badge-done'}`}>
                {artifact.complete === false ? '不完整(超限摘要)' : '完整'}
              </span>
              <span className="num dim" title="sha256 已校验">
                <CheckCircle size={11} weight="fill" /> {artifact.sha256.slice(0, 12)}…
              </span>
            </div>
            <div className="diff-ws-actions">
              <span className="df-stats num">
                {stats.files} 文件
                <b className="df-add"> +{stats.additions.toLocaleString()}</b>
                <b className="df-del"> −{stats.deletions.toLocaleString()}</b>
                {stats.binary > 0 && <b className="df-bin"> · {stats.binary} 二进制</b>}
              </span>
              <div className="diff-view-toggle" role="group" aria-label="Diff 查看方式">
                <button
                  className={`ghost ${!split ? 'active' : ''}`}
                  aria-pressed={!split}
                  onClick={() => setSplit(false)}
                >统一</button>
                <button
                  className={`ghost ${split ? 'active' : ''}`}
                  aria-pressed={split}
                  onClick={() => setSplit(true)}
                ><ArrowsOutSimple size={12} /> 分栏</button>
              </div>
              <button className="ghost" onClick={() => setWrap((v) => !v)} title="折行开关">
                <TextAa size={12} /> {wrap ? '不折行' : '折行'}
              </button>
              <button className="ghost" onClick={copyPatch} title="复制完整 patch">
                {copied ? <CheckCircle size={12} className="ok" /> : <CopySimple size={12} />} 复制
              </button>
              <a className="ghost" href={rawUrl} download={`${artifact.name}.patch`}>
                <DownloadSimple size={12} /> 下载
              </a>
              <button className="ghost" onClick={() => navChange(-1)} title="上一处改动 (k)"><ArrowUp size={12} /></button>
              <button className="ghost" onClick={() => navChange(1)} title="下一处改动 (j)"><ArrowDown size={12} /></button>
              <button className="ghost close" onClick={onClose} title="关闭 (Esc)"><X size={14} /></button>
            </div>
          </header>

          {artifact.complete === false && meta.note && (
            <div className="diff-ws-note"><Warning size={12} weight="fill" /> {String(meta.note)}</div>
          )}

          <div className="diff-ws-body">
            <aside className="diff-ws-tree">
              <div className="diff-ws-search">
                <MagnifyingGlass size={12} />
                <input
                  value={filter}
                  onChange={(e) => setFilter(e.target.value)}
                  placeholder="过滤文件…"
                  aria-label="过滤文件"
                />
              </div>
              {shownFiles.map((f) => {
                const idx = fileIndex.get(f)!;
                return (
                  <button
                    key={`${f.oldPath}|${idx}`}
                    className={`df-tree-item ${activeFile === idx ? 'active' : ''}`}
                    onClick={() => jumpToFile(idx)}
                  >
                    <StatusGlyph status={f.status} />
                    <span className="df-tree-path" title={f.newPath || f.oldPath}>
                      {f.newPath || f.oldPath}
                    </span>
                    {f.status !== 'binary' && (f.additions > 0 || f.deletions > 0) && (
                      <span className="num df-tree-pm">
                        <b className="df-add">+{f.additions}</b>
                        <b className="df-del">−{f.deletions}</b>
                      </span>
                    )}
                  </button>
                );
              })}
              {parsed?.parseError && (
                <div className="df-tree-empty dim">结构化解析失败,请看原文</div>
              )}
            </aside>

            <div className={`diff-ws-main ${wrap ? 'wrap' : ''}`} ref={scrollRef}>
              {text === null && !error && <div className="df-loading">读取 patch…</div>}
              {error && <div className="detail-error">{error}</div>}
              {parsed?.parseError && text !== null && (
                <section className="df-raw">
                  <div className="df-raw-head">
                    <Warning size={12} weight="fill" /> {parsed.parseError}
                    <a className="dl-link" href={rawUrl} target="_blank" rel="noreferrer">
                      <DownloadSimple size={11} /> 下载原文
                    </a>
                  </div>
                  <pre>{text}</pre>
                </section>
              )}
              {parsed && !parsed.parseError && shownFiles.map((f) => {
                const idx = fileIndex.get(f)!;
                const isCollapsed = collapsed.has(idx);
                const lineCount = f.hunks.reduce((s, h) => s + h.lines.length, 0);
                return (
                  <div
                    key={`${f.newPath}|${idx}`}
                    className="df-file"
                    ref={(el) => { fileRefs.current[idx] = el; }}
                  >
                    <div
                      className="df-file-head"
                      data-change-anchor
                      onClick={() => setCollapsed((s) => {
                        const n = new Set(s);
                        if (n.has(idx)) n.delete(idx); else n.add(idx);
                        return n;
                      })}
                      role="button"
                      tabIndex={0}
                      onKeyDown={(e) => e.key === 'Enter' && setCollapsed((s) => {
                        const n = new Set(s);
                        if (n.has(idx)) n.delete(idx); else n.add(idx);
                        return n;
                      })}
                    >
                      <StatusGlyph status={f.status} />
                      <span className="df-file-paths" title={`${f.oldPath} → ${f.newPath}`}>
                        <span className="mono"><b>Before</b> {f.status === 'added' ? '/dev/null' : f.oldPath}</span>
                        <span className="mono"><b>After</b> {f.status === 'deleted' ? '/dev/null' : f.newPath}</span>
                      </span>
                      <span className="num df-file-pm">
                        {f.status !== 'binary' && <><b className="df-add">+{f.additions}</b><b className="df-del">−{f.deletions}</b></>}
                        {f.status === 'binary' && <b className="df-bin">二进制</b>}
                      </span>
                      <span className="dim">{isCollapsed ? '展开' : '折叠'}</span>
                    </div>
                    {!isCollapsed && f.status === 'binary' && (
                      <div className="df-binary-note dim">二进制文件,内容不进文本 patch(Git 语义)</div>
                    )}
                    {!isCollapsed && f.status !== 'binary' && (
                      lineCount > BIG_FILE_LINES ? (
                        <div className="df-binary-note">
                          此文件 {lineCount.toLocaleString()} 行,渲染会卡顿;
                          <a href={rawUrl} target="_blank" rel="noreferrer">下载完整 patch</a> 查看
                        </div>
                      ) : f.hunks.map((h, hi) => (
                        <div className="df-hunk" key={hi}>
                          <div className="df-hunk-head mono" data-change-anchor>{h.header}</div>
                          {split ? (
                            <>
                              <div className="df-split-head" aria-hidden>
                                <span>Before · {f.status === 'added' ? '/dev/null' : f.oldPath}</span>
                                <span>After · {f.status === 'deleted' ? '/dev/null' : f.newPath}</span>
                              </div>
                              {deriveSplitRows(h.lines).map((row, rowIndex) => (
                                <SplitLineRow key={rowIndex} row={row} />
                              ))}
                            </>
                          ) : h.lines.map((line, lineIndex) => (
                            <UnifiedLineRow key={lineIndex} line={line} />
                          ))}
                        </div>
                      ))
                    )}
                    {f.status !== 'binary' && f.hunks.length === 0 && (
                      <div className="df-binary-note dim">此文件只有元数据变化(重命名/模式),没有行级改动</div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

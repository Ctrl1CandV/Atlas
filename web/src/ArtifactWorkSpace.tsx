import { useEffect, useState } from 'react';
import { AnimatePresence, motion } from 'motion/react';
import { CheckCircle, CopySimple, DownloadSimple, X } from '@phosphor-icons/react';
import { fetchText } from './api';
import { DiffWorkSpace } from './DiffWorkSpace';
import { TextViewer } from './TextViewer';
import type { Artifact } from './types';

/** 大窗口查看的目标:diff 走专用工作区,其余按 media_type 渲染。 */
export interface ArtifactViewTarget {
  kind: 'diff' | 'text';
  title: string;
  rawUrl: string;
  /** diff 必有;报告类产物有;投影没有(只有路径与哈希)。 */
  artifact?: Artifact;
  sha256?: string;
}

/** 通用产物工作台(第五轮 P3):节点详情里 320px 的小框只是预览,
 *  这里是大尺寸主路径。渲染器按 media_type 选择,与 TextViewer 的
 *  判定一致;下载与哈希显示保留——审计可见性不因容器变大而丢。 */
export function ArtifactWorkSpace({
  target, onClose,
}: {
  target: ArtifactViewTarget;
  onClose: () => void;
}) {
  if (target.kind === 'diff') {
    if (!target.artifact) {
      // 防御:diff 没有产物对象就无法进入专用工作区,显式失败而非空白
      return (
        <div className="diff-ws-backdrop" role="dialog" aria-label={target.title}>
          <div className="tv-ws">
            <header className="diff-ws-head">
              <div className="diff-ws-title">
                <h3>{target.title}</h3>
                <span className="dim">缺少产物元数据,无法打开工作区</span>
              </div>
              <button className="ghost close" onClick={onClose} title="关闭 (Esc)"><X size={14} /></button>
            </header>
          </div>
        </div>
      );
    }
    return <DiffWorkSpace artifact={target.artifact} rawUrl={target.rawUrl} onClose={onClose} />;
  }
  return <TextViewerPanel target={target} onClose={onClose} />;
}

function TextViewerPanel({
  target, onClose,
}: {
  target: ArtifactViewTarget;
  onClose: () => void;
}) {
  const [text, setText] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let alive = true;
    setText(null);
    setError(null);
    fetchText(target.rawUrl)
      .then((t) => alive && setText(t))
      .catch((e: Error) => alive && setError(e.message));
    return () => { alive = false; };
  }, [target.rawUrl]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null;
      if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA'
                 || el.isContentEditable)) return;
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);

  const copyText = async () => {
    if (text === null) return;
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch { /* 剪贴板权限被拒:静默,不阻塞 */ }
  };

  const sha = target.artifact?.sha256 ?? target.sha256;

  return (
    <AnimatePresence>
      <motion.div
        className="diff-ws-backdrop"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
      >
        <motion.div
          className="tv-ws"
          initial={{ y: 26, opacity: 0 }}
          animate={{ y: 0, opacity: 1 }}
          exit={{ y: 18, opacity: 0 }}
          transition={{ type: 'spring', stiffness: 320, damping: 34 }}
          role="dialog"
          aria-label={target.title}
        >
          <header className="diff-ws-head">
            <div className="diff-ws-title">
              <h3>{target.title}</h3>
              <span className="mono dim">{target.artifact?.media_type ?? 'text/plain'}</span>
              {sha && (
                <span className="num dim" title="sha256 已校验">
                  <CheckCircle size={11} weight="fill" /> {sha.slice(0, 12)}…
                </span>
              )}
            </div>
            <div className="diff-ws-actions">
              <button className="ghost" onClick={copyText} title="复制全文">
                {copied ? <CheckCircle size={12} className="ok" /> : <CopySimple size={12} />} 复制
              </button>
              <a className="ghost" href={target.rawUrl} download>
                <DownloadSimple size={12} /> 下载
              </a>
              <button className="ghost close" onClick={onClose} title="关闭 (Esc)"><X size={14} /></button>
            </div>
          </header>
          <div className="tv-ws-body">
            {text !== null && (
              <TextViewer text={text} artifact={target.artifact} fill />
            )}
            {error && <div className="detail-error">{error}</div>}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// unified diff 解析器(PLAN-v3 M6-B §3.5)。
// 原则:已生成的 Git patch 不做二次文本比较,直接解析其语义
// (file → hunk → line,保留新旧行号)。解析失败不掩盖:
// 返回 parseError,由调用方退回原文查看,产物字节真相不动。

export type DiffLineType = 'context' | 'add' | 'del' | 'meta';

export interface DiffLine {
  type: DiffLineType;
  oldNumber: number | null;
  newNumber: number | null;
  text: string;
}

export interface DiffHunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  header: string;
  lines: DiffLine[];
}

export type DiffFileStatus = 'modified' | 'added' | 'deleted' | 'renamed' | 'binary';

export interface DiffFile {
  oldPath: string;
  newPath: string;
  status: DiffFileStatus;
  additions: number;
  deletions: number;
  hunks: DiffHunk[];
}

export interface ParsedDiff {
  files: DiffFile[];
  additions: number;
  deletions: number;
  parseError: string | null;
  truncated: boolean;    // 内容是摘要而非完整 patch(后端超限时)
}

export type SplitDiffRow =
  | { type: 'pair'; oldLine: DiffLine | null; newLine: DiffLine | null }
  | { type: 'meta'; line: DiffLine };

/**
 * Derive side-by-side rows from the hunk's existing semantic lines.
 * Context is mirrored; an immediately adjacent deletion/addition run is zipped;
 * unmatched lines stay on one side and metadata spans both columns.
 */
export function deriveSplitRows(lines: DiffLine[]): SplitDiffRow[] {
  const rows: SplitDiffRow[] = [];
  let index = 0;
  while (index < lines.length) {
    const line = lines[index];
    if (line.type === 'meta') {
      rows.push({ type: 'meta', line });
      index += 1;
      continue;
    }
    if (line.type === 'context') {
      rows.push({ type: 'pair', oldLine: line, newLine: line });
      index += 1;
      continue;
    }
    if (line.type === 'del') {
      const deleted: DiffLine[] = [];
      while (index < lines.length && lines[index].type === 'del') {
        deleted.push(lines[index]);
        index += 1;
      }
      // Git 把 “No newline at end of file” 放在对应删/增行之后。
      // 这条元数据不能打断 replacement 的左右配对。
      const betweenMeta: DiffLine[] = [];
      while (index < lines.length && lines[index].type === 'meta') {
        betweenMeta.push(lines[index]);
        index += 1;
      }
      const added: DiffLine[] = [];
      while (index < lines.length && lines[index].type === 'add') {
        added.push(lines[index]);
        index += 1;
      }
      const length = Math.max(deleted.length, added.length);
      for (let i = 0; i < length; i += 1) {
        rows.push({ type: 'pair', oldLine: deleted[i] ?? null, newLine: added[i] ?? null });
      }
      rows.push(...betweenMeta.map((meta) => ({ type: 'meta' as const, line: meta })));
      continue;
    }
    rows.push({ type: 'pair', oldLine: null, newLine: line });
    index += 1;
  }
  return rows;
}

const FILE_HEADER = /^diff --git (?:"?a\/(.+?)"?) (?:"?b\/(.+?)"?)$/;
const HUNK_HEADER = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(.*)$/;
// OLD/NEW 行捕获完整路径(含引号与前缀):去引号后恰好剥一次 a/ b/,
// 避免正则消费与后置剥离叠加造成二次剥(顶层 a/ b/ 目录会错位)
const OLD_PATH = /^--- (.+)$/;
const NEW_PATH = /^\+\+\+ (.+)$/;

/** git C 风格转义路径还原:去首尾引号 + \NNN 八进制转义(按 UTF-8 字节解码)。
 *  core.quotePath=false 已避免非 ASCII 被转义,这里兜控制字符与空格引号。 */
function unquotePath(p: string): string {
  let s = p;
  if (s.length >= 2 && s.startsWith('"') && s.endsWith('"')) s = s.slice(1, -1);
  if (!s.includes('\\')) return s;
  const bytes: number[] = [];
  const parts = s.split('\\');
  for (const c of parts[0]) bytes.push(c.charCodeAt(0));
  for (let i = 1; i < parts.length; i++) {
    const m = parts[i].match(/^(\d{1,3})(.*)$/);
    if (m) {
      bytes.push(parseInt(m[1], 8));
      for (const c of m[2]) bytes.push(c.charCodeAt(0));
    } else {
      for (const c of parts[i]) bytes.push(c.charCodeAt(0));
    }
  }
  return new TextDecoder('utf-8', { fatal: false }).decode(new Uint8Array(bytes));
}

/** 解析 unified diff 文本。宽松但诚实:结构完全认不出时给 parseError。 */
export function parseUnifiedDiff(text: string): ParsedDiff {
  // 末尾换行产生的空串不是真实行——留着会被计成 context 并虚增行号
  const lines = (text.endsWith('\n') ? text.slice(0, -1) : text).split('\n');
  const empty: ParsedDiff = { files: [], additions: 0, deletions: 0, parseError: null, truncated: false };
  if (!text.trim()) return { ...empty, parseError: 'patch 为空' };

  // 后端超限时的摘要形态:不是 unified diff,明确标注而不是硬解
  if (!lines.some((l) => l.startsWith('diff --git')) &&
      !lines.some((l) => l.startsWith('@@'))) {
    const looksNumstat = lines.some((l) => /^\d+\t\d+\t\S+/.test(l) || /^-\t-\t\S+/.test(l));
    return {
      ...empty,
      truncated: true,
      parseError: looksNumstat
        ? '完整 diff 超过上限,这里只有 numstat 摘要;可下载原文或到隔离副本查看完整改动'
        : '内容不是 unified diff 格式',
    };
  }

  const files: DiffFile[] = [];
  let cur: DiffFile | null = null;
  let hunk: DiffHunk | null = null;
  let oldNo = 0;
  let newNo = 0;

  for (const raw of lines) {
    const m = raw.match(FILE_HEADER);
    if (m) {
      cur = {
        oldPath: unquotePath(m[1]),
        newPath: unquotePath(m[2]),
        status: 'modified',
        additions: 0,
        deletions: 0,
        hunks: [],
      };
      files.push(cur);
      hunk = null;
      continue;
    }
    if (!cur) continue;   // diff 头之前的杂行(stat 前缀等)忽略

    if (raw.startsWith('new file mode')) { cur.status = 'added'; continue; }
    if (raw.startsWith('deleted file mode')) { cur.status = 'deleted'; continue; }
    if (raw.startsWith('rename from ')) { cur.status = 'renamed'; continue; }
    if (raw.startsWith('rename to ')) { continue; }
    if (raw.startsWith('Binary files') || raw.startsWith('GIT binary patch')) {
      cur.status = 'binary';
      hunk = null;
      continue;
    }
    if (raw.startsWith('index ') || raw.startsWith('similarity index') ||
        raw.startsWith('dissimilarity index') || raw.startsWith('old mode') ||
        raw.startsWith('new mode')) continue;

    const om = raw.match(OLD_PATH);
    if (om && !hunk) {
      const pth = unquotePath(om[1]);
      if (pth === '/dev/null') cur.status = 'added';
      else cur.oldPath = pth.replace(/^a\//, '');
      continue;
    }
    const nm = raw.match(NEW_PATH);
    if (nm && !hunk) {
      const pth = unquotePath(nm[1]);
      if (pth === '/dev/null') cur.status = 'deleted';
      else cur.newPath = pth.replace(/^b\//, '');
      continue;
    }

    const hm = raw.match(HUNK_HEADER);
    if (hm) {
      hunk = {
        oldStart: parseInt(hm[1], 10),
        oldLines: hm[2] === undefined ? 1 : parseInt(hm[2], 10),
        newStart: parseInt(hm[3], 10),
        newLines: hm[4] === undefined ? 1 : parseInt(hm[4], 10),
        header: raw,
        lines: [],
      };
      cur.hunks.push(hunk);
      oldNo = hunk.oldStart;
      newNo = hunk.newStart;
      continue;
    }
    if (!hunk) continue;

    if (raw.startsWith('\\')) {   // "\ No newline at end of file"
      hunk.lines.push({ type: 'meta', oldNumber: null, newNumber: null, text: raw });
      continue;
    }
    if (raw.startsWith('+')) {
      hunk.lines.push({ type: 'add', oldNumber: null, newNumber: newNo++, text: raw.slice(1) });
      cur.additions += 1;
    } else if (raw.startsWith('-')) {
      hunk.lines.push({ type: 'del', oldNumber: oldNo++, newNumber: null, text: raw.slice(1) });
      cur.deletions += 1;
    } else if (raw.startsWith(' ') || raw === '') {
      hunk.lines.push({ type: 'context', oldNumber: oldNo++, newNumber: newNo++, text: raw.slice(1) });
    }
    // 其他行(异常内容):忽略,不伪造结构
  }

  const additions = files.reduce((s, f) => s + f.additions, 0);
  const deletions = files.reduce((s, f) => s + f.deletions, 0);
  return { files, additions, deletions, parseError: null, truncated: false };
}

export function fileStatusIcon(status: DiffFileStatus): string {
  switch (status) {
    case 'added': return 'A';
    case 'deleted': return 'D';
    case 'renamed': return 'R';
    case 'binary': return 'B';
    default: return 'M';
  }
}

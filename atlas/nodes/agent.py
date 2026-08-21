# -*- coding: utf-8 -*-
"""research / coding_agent 节点的投影、冻结副本与产物契约。

测试可通过 ``execute_graph(..., agent_runner=...)`` 显式注入替身；生产 runner
负责约束 CLI 进程树。coding_agent 只在 run 目录内由同一冻结 baseline 派生的
副本上工作，成功后输出报告与基于普通文件字节清单的可审阅 diff。

字节比较不调用 Git，也不读取 agent 可写的 Git attributes/config，因此 commit、
clean filter、hook、textconv 和 external diff 都不能改变审批对象。目录副本仍不是
OS 安全边界；同用户恶意进程攻击控制器本身超出 v1 承诺。
"""
import difflib
import hashlib
import os
import re
import secrets
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from atlas.artifacts import artifact_entry
from atlas.integrity import build_projection, store_artifact
from atlas.spec import NodeSpec, WorkflowSpec

# agent attempt 之间的固定等待。engine 的 guarded_agent_runner 依赖这个值
# 判断"剩余 deadline 是否还够一次 retry sleep",两处必须同源。
AGENT_RETRY_SLEEP_S = 2.0

# 隔离副本和最终结果均受此上限约束。
WORKTREE_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB
DIFF_MAX_BYTES = 4 * 1024 * 1024              # 4 MiB
# difflib 需要把单个变更文件的两版行表放入内存；超限明确失败而非 OOM。
DIFF_FILE_MAX_BYTES = 64 * 1024 * 1024         # 64 MiB per version


class AgentCliError(Exception):
    """Agent 执行、产物或 diff 契约失败。"""


_CACHE_DIRS = {"__pycache__", ".pytest_cache", "node_modules", ".venv", "dist"}
_CACHE_FILES = {".DS_Store", "Thumbs.db"}
_RESERVED_WINDOWS_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_HEX_OID = re.compile(r"[0-9a-fA-F]{40}(?:[0-9a-fA-F]{24})?\Z")


@dataclass(frozen=True)
class SourceBaselineToken:
    """生产预检接受的不可变 source 状态，供冻结阶段无 Git 地复核。"""
    node_id: str
    source_path: str
    tree_digest: str
    head: str
    index_path: str
    index_digest: str


def _canonical_path(path: Path, *, label: str) -> str:
    try:
        resolved = path.resolve(strict=True)
    except OSError as e:
        raise AgentCliError(f"无法解析 {label} 路径 {path}:{e}") from e
    return os.path.normcase(str(resolved))


def _file_digest(path: Path, *, label: str) -> str:
    try:
        before = path.stat(follow_symlinks=False)
    except OSError as e:
        raise AgentCliError(f"无法检查 {label} {path}:{e}") from e
    try:
        digest, _ = _hash_regular_file(path, before)
    except AgentCliError as e:
        raise AgentCliError(f"无法安全读取 {label} {path}:{e}") from e
    return digest


def _is_reparse(info: os.stat_result) -> bool:
    return bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _reject_windows_streams(path: Path) -> None:
    """拒绝 NTFS alternate data streams；它们不会出现在 ``scandir`` 中。"""
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    class _StreamData(ctypes.Structure):
        _fields_ = [
            ("stream_size", ctypes.c_longlong),
            ("stream_name", wintypes.WCHAR * 296),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    find_first = kernel32.FindFirstStreamW
    find_first.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                           ctypes.POINTER(_StreamData), wintypes.DWORD]
    find_first.restype = wintypes.HANDLE
    find_next = kernel32.FindNextStreamW
    find_next.argtypes = [wintypes.HANDLE, ctypes.POINTER(_StreamData)]
    find_next.restype = wintypes.BOOL
    find_close = kernel32.FindClose
    find_close.argtypes = [wintypes.HANDLE]
    find_close.restype = wintypes.BOOL

    data = _StreamData()
    handle = find_first(str(path), 0, ctypes.byref(data), 0)
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        error = ctypes.get_last_error()
        # ERROR_HANDLE_EOF:该文件系统明确报告没有可枚举流。
        if error == 38:
            return
        raise AgentCliError(f"无法枚举 Windows 文件流 {path}:WinError {error}")
    try:
        while True:
            if data.stream_name != "::$DATA":
                raise AgentCliError(
                    f"workdir 含 Windows alternate data stream:{path}{data.stream_name}")
            if not find_next(handle, ctypes.byref(data)):
                error = ctypes.get_last_error()
                if error == 38:
                    break
                raise AgentCliError(f"无法枚举 Windows 文件流 {path}:WinError {error}")
    finally:
        find_close(handle)


def _validate_component(name: str, display: str) -> None:
    if not name or name in {".", ".."}:
        raise AgentCliError(f"workdir 含非法路径:{display}")
    if "\\" in name or ":" in name:
        raise AgentCliError(f"workdir 含 Windows 危险路径:{display}")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
        raise AgentCliError(f"workdir 路径含控制字符:{display}")
    if name.endswith((" ", ".")):
        raise AgentCliError(f"workdir 路径以空格或点结尾:{display}")
    stem = name.split(".", 1)[0].upper()
    if stem in _RESERVED_WINDOWS_NAMES:
        raise AgentCliError(f"workdir 含 Windows 设备名:{display}")


def _is_cache_path(parts: tuple[str, ...]) -> bool:
    if any(part in _CACHE_DIRS for part in parts[:-1]):
        return True
    name = parts[-1]
    return (name in _CACHE_DIRS or name in _CACHE_FILES
            or name.endswith((".pyc", ".pyo")))


def _hash_regular_file(path: Path, before: os.stat_result) -> tuple[str, int]:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise AgentCliError(f"无法安全读取 workdir 文件 {path}:{e}") from e
    digest = hashlib.sha256()
    try:
        after = os.fstat(fd)
        if (not stat.S_ISREG(after.st_mode) or _is_reparse(after)
                or getattr(after, "st_nlink", 1) != 1):
            raise AgentCliError(f"workdir 含链接或非普通文件:{path}")
        # Windows 的 DirEntry.stat() 在部分临时/虚拟卷会返回占位的
        # st_dev/st_ino；已打开句柄的类型、reparse/link/size 校验才可信。
        # POSIX 则额外绑定枚举与打开前后的设备/inode，拒绝路径替换。
        identity_changed = (
            os.name != "nt"
            and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        )
        if identity_changed or before.st_size != after.st_size:
            raise AgentCliError(f"workdir 文件在采集期间发生变化:{path}")
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            digest.update(block)
    finally:
        os.close(fd)
    return digest.hexdigest(), after.st_size


def _scan_tree(root: Path, *, require_git: bool = False) -> tuple[dict[str, dict], str, int]:
    """安全枚举普通文件，返回清单、tree digest 和含排除项的总字节数。

    根目录精确名 ``.git`` 只作为供 agent 使用的元数据复制，不进入权威清单；
    任意大小写变体或嵌套 ``.git`` 都拒绝。缓存目录仍会遍历并检查链接/路径，
    但其普通文件字节不进入清单。
    """
    try:
        root_info = root.stat(follow_symlinks=False)
    except OSError as e:
        raise AgentCliError(f"无法检查 workdir {root}:{e}") from e
    if not stat.S_ISDIR(root_info.st_mode) or root.is_symlink() or _is_reparse(root_info):
        raise AgentCliError(f"workdir 不是安全的普通目录:{root}")
    _reject_windows_streams(root)

    manifest: dict[str, dict] = {}
    total = 0
    found_root_git = False
    stack: list[tuple[Path, tuple[str, ...], bool]] = [(root, (), False)]
    while stack:
        current, parent_parts, parent_excluded = stack.pop()
        try:
            entries = sorted(os.scandir(current), key=lambda item: item.name.casefold())
        except OSError as e:
            raise AgentCliError(f"无法枚举 workdir 目录 {current}:{e}") from e
        folded: set[str] = set()
        for entry in entries:
            display = str(Path(entry.path))
            _validate_component(entry.name, display)
            folded_name = entry.name.casefold()
            if folded_name in folded:
                raise AgentCliError(f"workdir 含大小写冲突路径:{display}")
            folded.add(folded_name)

            parts = (*parent_parts, entry.name)
            rel = "/".join(parts)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as e:
                raise AgentCliError(f"无法检查 workdir 路径 {display}:{e}") from e
            if entry.is_symlink() or _is_reparse(info):
                raise AgentCliError(
                    f"workdir 含符号链接/junction/reparse point:{display}")

            git_metadata = False
            if folded_name == ".git":
                if len(parts) != 1 or entry.name != ".git" or not stat.S_ISDIR(info.st_mode):
                    raise AgentCliError(f"workdir 含大小写变体或嵌套 .git:{display}")
                found_root_git = True
                git_metadata = True

            excluded = parent_excluded or git_metadata or _is_cache_path(parts)
            if stat.S_ISDIR(info.st_mode):
                _reject_windows_streams(Path(entry.path))
                stack.append((Path(entry.path), parts, excluded))
                continue
            if not stat.S_ISREG(info.st_mode):
                raise AgentCliError(f"workdir 含非普通文件:{display}")
            total += info.st_size
            if total > WORKTREE_MAX_BYTES:
                raise AgentCliError(
                    f"workdir {root} 体积超过隔离副本上限 "
                    f"{WORKTREE_MAX_BYTES / 1e9:.0f}GB。排除大目录后重试")
            # hardlink 与文件身份必须在已打开句柄上判断。Windows 的
            # DirEntry.stat() 可能短暂返回陈旧链接数，fstat() 同时关闭 TOCTOU。
            path = Path(entry.path)
            _reject_windows_streams(path)
            file_digest, size = _hash_regular_file(path, info)
            if excluded:
                continue
            manifest[rel] = {"sha256": file_digest, "size": size}

    if require_git and not found_root_git:
        raise AgentCliError(
            "目标项目不是 git 仓库，无法冻结 coding_agent 基线；请先 git init")
    tree_hash = hashlib.sha256()
    for rel, item in sorted(manifest.items()):
        encoded = rel.encode("utf-8")
        tree_hash.update(len(encoded).to_bytes(4, "big"))
        tree_hash.update(encoded)
        tree_hash.update(int(item["size"]).to_bytes(8, "big"))
        tree_hash.update(bytes.fromhex(item["sha256"]))
    return manifest, tree_hash.hexdigest(), total


def _git_check_env() -> dict[str, str]:
    """Git 清洁性检查的最小环境；不继承凭据或任意宿主变量。"""
    allowed = ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP")
    env = {key: os.environ[key] for key in allowed if os.environ.get(key)}
    env.update({
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _verify_clean_source(root: Path) -> str:
    """在冻结边界复核 HEAD 与工作树，避免 preflight→copy 竞态。"""
    flags = [
        "-c", "core.fsmonitor=false",
        "-c", "core.untrackedCache=false",
        "-c", f"core.hooksPath={os.devnull}",
    ]
    try:
        head = subprocess.run(
            ["git", "-C", str(root), *flags, "rev-parse", "--verify", "HEAD"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_check_env(), shell=False)
        status = subprocess.run(
            ["git", "-C", str(root), *flags, "status", "--porcelain=v1",
             "--untracked-files=all"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=30, env=_git_check_env(), shell=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AgentCliError(f"无法在冻结边界检查 Git 基线:{type(e).__name__}") from e
    if head.returncode != 0:
        raise AgentCliError("源仓库没有可审计的 HEAD 提交")
    if status.returncode != 0:
        raise AgentCliError(f"源仓库 git status 失败(退出码 {status.returncode})")
    if status.stdout.strip():
        raise AgentCliError("源 workdir 在冻结边界存在未提交改动;拒绝归因给 agent")
    oid = head.stdout.strip().lower()
    if not _HEX_OID.fullmatch(oid):
        raise AgentCliError("源仓库 HEAD 不是可识别对象 id")
    return oid


def _read_source_head(root: Path) -> str:
    """不启动 Git，仅读取已安全枚举的 HEAD/ref 作为审计信息。"""
    git_dir = root / ".git"
    head_path = git_dir / "HEAD"
    try:
        raw = head_path.read_bytes()
    except OSError as e:
        raise AgentCliError(f"无法读取源仓库 HEAD:{e}") from e
    if len(raw) > 4096:
        raise AgentCliError("源仓库 HEAD 异常过大")
    value = raw.decode("ascii", errors="strict").strip()
    if value.startswith("ref: "):
        ref = value[5:]
        parts = tuple(ref.split("/"))
        if not parts or parts[0] != "refs" or any(
                not p or p in {".", ".."} or "\\" in p or ":" in p for p in parts):
            raise AgentCliError("源仓库 HEAD 引用了非法 ref")
        ref_path = git_dir.joinpath(*parts)
        if ref_path.is_file():
            try:
                value = ref_path.read_text(encoding="ascii").strip()
            except OSError as e:
                raise AgentCliError(f"无法读取源仓库 HEAD ref:{e}") from e
        else:
            try:
                packed = (git_dir / "packed-refs").read_text(
                    encoding="ascii", errors="strict")
            except OSError as e:
                raise AgentCliError("源仓库 HEAD ref 不存在") from e
            if len(packed) > 16 * 1024 * 1024:
                raise AgentCliError("源仓库 packed-refs 异常过大")
            matches = [line.split(" ", 1)[0] for line in packed.splitlines()
                       if not line.startswith(("#", "^")) and line.endswith(f" {ref}")]
            if len(matches) != 1:
                raise AgentCliError("源仓库 HEAD ref 无法唯一解析")
            value = matches[0]
    if not _HEX_OID.fullmatch(value):
        raise AgentCliError("源仓库没有可审计的 HEAD 提交")
    return value.lower()


def _token_index_relative(token: SourceBaselineToken) -> Path:
    source = Path(token.source_path)
    index = Path(token.index_path)
    try:
        return index.relative_to(source)
    except ValueError as e:
        raise AgentCliError("SourceBaselineToken 的 index 路径不在 source 内") from e


def _validate_token_state(root: Path, token: SourceBaselineToken, *,
                          bind_source_path: bool) -> tuple[dict[str, dict], str]:
    if bind_source_path and _canonical_path(root, label="source") != token.source_path:
        raise AgentCliError("SourceBaselineToken 与 coding_agent workdir 不匹配")
    manifest, digest, _ = _scan_tree(root, require_git=True)
    if digest != token.tree_digest:
        raise AgentCliError("source 字节树与预检 SourceBaselineToken 不符")
    if _read_source_head(root) != token.head:
        raise AgentCliError("source HEAD 与预检 SourceBaselineToken 不符")
    index_path = root / _token_index_relative(token)
    if bind_source_path and _canonical_path(index_path, label="Git index") != token.index_path:
        raise AgentCliError("source Git index 路径绑定与预检 SourceBaselineToken 不符")
    if _file_digest(index_path, label="Git index") != token.index_digest:
        raise AgentCliError("source Git index 与预检 SourceBaselineToken 不符")
    return manifest, digest


def _freeze_baseline(run_dir: Path, node: NodeSpec, iteration: int,
                     source: Path, *, require_head: bool = True,
                     token: SourceBaselineToken | None = None,
                     ) -> tuple[Path, dict[str, dict], str, str | None]:
    if token is not None:
        if token.node_id != node.id:
            raise AgentCliError("SourceBaselineToken 与 coding_agent 节点不匹配")
        source_manifest, source_digest = _validate_token_state(
            source, token, bind_source_path=True)
        source_head = token.head
    else:
        # 显式注入 runner 是测试/嵌入扩展点，不经过生产 preflight token。
        # 这里只要求可审计 HEAD 并冻结普通文件字节；不能用另一套 Git 配置
        # 重解释 clean 状态，否则 Windows core.autocrlf 会把干净仓库误报为脏。
        source_manifest, source_digest, _ = _scan_tree(
            source, require_git=require_head)
        source_head = _read_source_head(source) if require_head else None
    baseline_root = run_dir / "baselines"
    baseline_root.mkdir(parents=True, exist_ok=True)
    baseline = baseline_root / f"{node.id}.{iteration}.{secrets.token_hex(8)}"
    if baseline.exists():
        raise AgentCliError(f"随机冻结 baseline 路径碰撞，拒绝覆盖:{baseline}")
    try:
        shutil.copytree(source, baseline, symlinks=True)
    except OSError as e:
        raise AgentCliError(f"无法冻结 workdir baseline:{e}") from e
    baseline_manifest, baseline_digest, _ = _scan_tree(
        baseline, require_git=require_head)
    if baseline_digest != source_digest or baseline_manifest != source_manifest:
        raise AgentCliError("源 workdir 在冻结期间发生变化，baseline 不完整")
    if token is not None:
        _validate_token_state(baseline, token, bind_source_path=False)
        final_manifest, final_digest = _validate_token_state(
            source, token, bind_source_path=True)
        if final_digest != source_digest or final_manifest != source_manifest:
            raise AgentCliError("源 workdir 在 baseline 冻结期间发生变化")
    else:
        final_manifest, final_digest, _ = _scan_tree(
            source, require_git=require_head)
        final_head = _read_source_head(source) if require_head else None
        if (final_digest != source_digest or final_manifest != source_manifest
                or final_head != source_head):
            raise AgentCliError("源 workdir 在 baseline 冻结期间发生变化")
    return baseline, baseline_manifest, baseline_digest, source_head


def _verify_baseline(baseline: Path, expected_manifest: dict[str, dict],
                     expected_digest: str, *, require_head: bool = True) -> None:
    manifest, digest, _ = _scan_tree(baseline, require_git=require_head)
    if digest != expected_digest or manifest != expected_manifest:
        raise AgentCliError("冻结 baseline 在 agent 执行期间被篡改")


def _prepare_worktree(run_dir: Path, node: NodeSpec, iteration: int,
                      baseline: Path, expected_manifest: dict[str, dict],
                      expected_digest: str, attempt: int = 1, *,
                      require_head: bool = True) -> Path:
    """每次 attempt 只从同一冻结 baseline 派生，不再读取可变源目录。"""
    _verify_baseline(
        baseline, expected_manifest, expected_digest, require_head=require_head)
    base = run_dir / "worktrees" / f"{node.id}.{iteration}"
    wt = base if attempt == 1 else base.with_name(f"{base.name}.r{attempt}")
    n = attempt
    while wt.exists():
        n += 1
        wt = base.with_name(f"{base.name}.r{n}")
    try:
        shutil.copytree(baseline, wt, symlinks=True)
    except OSError as e:
        raise AgentCliError(f"无法派生 agent worktree:{e}") from e
    copied_manifest, copied_digest, _ = _scan_tree(
        wt, require_git=require_head)
    if copied_digest != expected_digest or copied_manifest != expected_manifest:
        raise AgentCliError("从冻结 baseline 派生的 worktree 不完整")
    return wt


def _read_changed_text(path: Path, item: dict | None) -> list[str]:
    if item is None:
        return []
    size = int(item["size"])
    if size > DIFF_FILE_MAX_BYTES:
        raise AgentCliError(
            f"变更文件 {path} 为 {size} 字节，超过安全文本 diff 输入上限 "
            f"{DIFF_FILE_MAX_BYTES}")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as e:
        raise AgentCliError(f"无法安全读取变更文件 {path}:{e}") from e
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or _is_reparse(info)
                or getattr(info, "st_nlink", 1) != 1 or info.st_size != size):
            raise AgentCliError(f"变更文件在 diff 生成期间发生变化:{path}")
        blocks = []
        remaining = size
        while remaining:
            block = os.read(fd, min(1024 * 1024, remaining))
            if not block:
                raise AgentCliError(f"变更文件在 diff 生成期间被截断:{path}")
            blocks.append(block)
            remaining -= len(block)
        if os.read(fd, 1):
            raise AgentCliError(f"变更文件在 diff 生成期间增长:{path}")
    finally:
        os.close(fd)
    data = b"".join(blocks)
    if hashlib.sha256(data).hexdigest() != item["sha256"]:
        raise AgentCliError(f"变更文件在 diff 生成期间发生变化:{path}")
    if b"\0" in data:
        raise UnicodeError
    return data.decode("utf-8", errors="strict").splitlines(keepends=True)


def _collect_diff(baseline: Path, result: Path,
                  expected_manifest: dict[str, dict],
                  expected_digest: str) -> tuple[bytes, dict]:
    """按 baseline/result 普通文件字节生成 unified diff，全程不执行 Git。"""
    _verify_baseline(baseline, expected_manifest, expected_digest)
    result_manifest, result_digest, _ = _scan_tree(result, require_git=True)
    changed = [path for path in sorted(set(expected_manifest) | set(result_manifest))
               if expected_manifest.get(path) != result_manifest.get(path)]

    chunks: list[bytes] = []
    files: list[dict] = []
    total_size = 0
    additions = deletions = 0
    for rel in changed:
        before = expected_manifest.get(rel)
        after = result_manifest.get(rel)
        try:
            old_lines = _read_changed_text(baseline / Path(*rel.split("/")), before)
            new_lines = _read_changed_text(result / Path(*rel.split("/")), after)
        except (UnicodeError, UnicodeDecodeError):
            before_hash = before["sha256"] if before else None
            after_hash = after["sha256"] if after else None
            raise AgentCliError(
                f"二进制文件变更无法生成可审计文本 patch:{rel};"
                f"before_sha256={before_hash};after_sha256={after_hash}")

        old_name = f"a/{rel}" if before else "/dev/null"
        new_name = f"b/{rel}" if after else "/dev/null"
        # 前端 diffParse 以 `diff --git` 行切分文件;没有它整个补丁会被
        # 当成空补丁。这里补上 git 惯例头,正文仍由 difflib 生成。
        header = [f"diff --git a/{rel} b/{rel}\n"]
        if before is None:
            header.append("new file mode 100644\n")
        elif after is None:
            header.append("deleted file mode 100644\n")
        rendered: list[str] = []
        file_add = file_del = 0
        for line in difflib.unified_diff(
                old_lines, new_lines, fromfile=old_name, tofile=new_name, lineterm="\n"):
            if line.startswith("+") and not line.startswith("+++"):
                file_add += 1
            elif line.startswith("-") and not line.startswith("---"):
                file_del += 1
            if line.endswith(("\n", "\r")):
                rendered.append(line)
            else:
                rendered.append(line + "\n\\ No newline at end of file\n")
        # difflib 对空文件的新增/删除返回空序列；仍必须把路径级变更放进
        # patch，避免 UI 展示空补丁而 metadata 单独声称有改动。
        if not rendered and (before is None or after is None):
            rendered = [f"--- {old_name}\n", f"+++ {new_name}\n"]
        encoded = "".join(header).encode("utf-8") + "".join(rendered).encode("utf-8")
        total_size += len(encoded)
        if total_size > DIFF_MAX_BYTES:
            raise AgentCliError(
                f"完整 diff {total_size} 字节超过 diff 上限 {DIFF_MAX_BYTES};"
                "拒绝用摘要冒充可审阅的完整补丁")
        chunks.append(encoded)
        additions += file_add
        deletions += file_del
        files.append({
            "path": rel,
            "change": "added" if before is None else "deleted" if after is None else "modified",
            "additions": file_add,
            "deletions": file_del,
            "binary": False,
            "before_sha256": before["sha256"] if before else None,
            "after_sha256": after["sha256"] if after else None,
            "before_bytes": before["size"] if before else None,
            "after_bytes": after["size"] if after else None,
        })

    patch = b"".join(chunks)
    patch_digest = hashlib.sha256(patch).hexdigest()
    metadata = {
        "files": files,
        "files_changed": len(files),
        "additions": additions,
        "deletions": deletions,
        "binary_files": 0,
        "complete": True,
        "baseline_digest": expected_digest,
        "result_digest": result_digest,
        "patch_digest": patch_digest,
    }
    return patch, metadata


def make_agent_node_fn(node: NodeSpec, spec: WorkflowSpec, ctx):
    """engine 的节点工厂:research / coding_agent 共用。"""

    def run(state) -> dict:
        started = time.monotonic()
        iteration = state.get("iterations", {}).get(node.id, 0) + 1
        if iteration > spec.guards.effective_max_iterations:
            from atlas.engine import GuardViolation
            raise GuardViolation(
                f"节点 {node.id} 将第 {iteration} 次执行,超过 max_iterations")

        projection, proj_ref, consumed = build_projection(
            ctx.run_dir, node_id=node.id, iteration=iteration,
            prompt=node.prompt, consumes=node.consumes,
            artifacts=state["artifacts"],
        )
        ctx.log.emit(
            "node_input", node=node.id, iteration=iteration,
            projection_path=str(proj_ref.path), projection_sha256=proj_ref.sha256,
            consumed=[r.as_dict() for r in consumed],
        )
        requested_model = node.model or f"agent:{node.type}"
        runner_name = getattr(ctx._agent_runner_raw, "runner_name", "injected")

        def _stage(stage: str, **extra) -> None:
            """冻结/派生副本的阶段性进度。大 workdir 的全树扫描+拷贝
            可能耗时分钟级,没有事件时界面在 node_input 与 node_started
            之间看起来像卡死。复用 node_progress 事件类型,不破坏旧 fold。"""
            ctx.log.emit("node_progress", node=node.id, iteration=iteration,
                         phase=stage, model=requested_model, runner=runner_name,
                         **extra)

        baseline = None
        baseline_manifest = None
        baseline_digest = None
        source_head = None
        if node.type == "coding_agent":
            baseline_token = ctx.source_baseline_tokens.get(node.id)
            if (getattr(ctx._agent_runner_raw, "production_runner", False)
                    and node.writable and baseline_token is None):
                raise AgentCliError(
                    f"生产 coding_agent 节点 {node.id} 缺少 SourceBaselineToken")
            _stage("baseline_freeze")
            baseline, baseline_manifest, baseline_digest, source_head = _freeze_baseline(
                ctx.run_dir, node, iteration, Path(node.workdir),
                require_head=node.writable, token=baseline_token)

        # retry 使用同一冻结 baseline 的全新派生副本与独立预算预留。
        last_err = None
        text = None
        result = None
        worktree = None
        started_emitted = False
        total_attempts = 1 + node.retry
        for attempt in range(1, total_attempts + 1):
            ctx.check_timeout(spec.guards.timeout_s, node.id)
            cwd = None
            if node.type == "coding_agent":
                _stage("worktree_derive", attempt=attempt)
                worktree = _prepare_worktree(
                    ctx.run_dir, node, iteration, baseline, baseline_manifest,
                    baseline_digest, attempt=attempt, require_head=node.writable)
                cwd = worktree

            reservation = None
            settled = False
            try:
                if spec.guards.max_cost_usd is not None:
                    from atlas.costs import CostLimitError
                    from atlas.engine import CostExceeded
                    try:
                        reservation = ctx.cost_ledger.reserve_remaining(
                            description=f"节点 {node.id} agent 第 {attempt} 次派发")
                    except CostLimitError as e:
                        raise CostExceeded(str(e)) from e
                    ctx.log.emit(
                        "cost_reserved", node=node.id, iteration=iteration,
                        attempt=attempt, model=requested_model,
                        reservation_id=reservation.reservation_id,
                        reserved_usd=reservation.amount,
                    )
                if not started_emitted:
                    ctx.log.emit(
                        "node_started", node=node.id, iteration=iteration,
                        model_requested=requested_model, runner=runner_name)
                    started_emitted = True

                runner_kwargs = {
                    "node_type": node.type,
                    "max_turns": node.max_turns,
                    "cwd": cwd,
                    "writable": node.writable,
                    "allow_web": bool(node.allow_web),
                    "allowed_paths": list(node.allowed_paths),
                    "timeout_s": node.timeout_s,
                    "model_ref": node.model,
                }
                if getattr(ctx._agent_runner_raw, "production_runner", False):
                    runner_kwargs.update({
                        "node_id": node.id,
                        "max_budget_usd": (
                            reservation.amount if reservation else None),
                    })
                raw_result = ctx.agent_runner(proj_ref.path, **runner_kwargs)
                if isinstance(raw_result, str):
                    text = raw_result
                    result = None
                else:
                    text = getattr(raw_result, "text", None)
                    result = raw_result
                if not isinstance(text, str) or not text.strip():
                    raise AgentCliError("CLI 返回空输出(假成功形态)")

                actual_cost = getattr(result, "cost_usd", None)
                unknown = actual_cost is None
                exceeded = None
                try:
                    accounted_cost = ctx.cost_ledger.settle(
                        reservation, actual_cost,
                        description=f"节点 {node.id} agent 第 {attempt} 次结算",
                        unknown_as_reserved=unknown)
                    settled = True
                except CostLimitError as e:
                    settled = True
                    accounted_cost = actual_cost
                    exceeded = e
                if unknown:
                    ctx.log.emit(
                        "cost_unknown", run_id=ctx.run_dir.name,
                        models=[requested_model], attempt=attempt,
                        reservation_id=(reservation.reservation_id
                                        if reservation else None),
                        reason="Claude CLI 未返回可信 total_cost_usd；有成本帽时按本次"
                               "预留全额计入 guarded/accounted 成本，不再释放重用。",
                    )
                ctx.log.emit(
                    "cost_settled", node=node.id, iteration=iteration,
                    attempt=attempt, model=requested_model,
                    reservation_id=(reservation.reservation_id
                                    if reservation else None),
                    actual_cost_usd=actual_cost,
                    accounted_cost_usd=accounted_cost,
                    cost_unknown=unknown,
                    cost_usd=actual_cost,
                    input_tokens=(getattr(getattr(result, "usage", None),
                                          "input_tokens", None)),
                    output_tokens=(getattr(getattr(result, "usage", None),
                                           "output_tokens", None)),
                )
                if exceeded is not None:
                    raise CostExceeded(str(exceeded)) from exceeded
                last_err = None
                break
            except AgentCliError as e:
                if not settled:
                    try:
                        accounted_cost = ctx.cost_ledger.settle(
                            reservation, None,
                            description=f"节点 {node.id} agent 失败保守结算",
                            unknown_as_reserved=True)
                    except CostLimitError:
                        accounted_cost = (reservation.amount
                                          if reservation else None)
                    settled = True
                    ctx.log.emit(
                        "cost_unknown", run_id=ctx.run_dir.name,
                        models=[requested_model], attempt=attempt,
                        reservation_id=(reservation.reservation_id
                                        if reservation else None),
                        reason="Agent CLI 已派发但未返回可信费用；有成本帽时按预留"
                               "全额计入 guarded/accounted 成本。",
                    )
                    ctx.log.emit(
                        "cost_settled", node=node.id, iteration=iteration,
                        attempt=attempt, model=requested_model,
                        reservation_id=(reservation.reservation_id
                                        if reservation else None),
                        actual_cost_usd=None,
                        accounted_cost_usd=accounted_cost,
                        cost_unknown=True, cost_usd=None,
                        input_tokens=None, output_tokens=None,
                    )
                last_err = e
                worktree = None
                ctx.log.emit("model_failed", node=node.id, iteration=iteration,
                             model_requested=requested_model,
                             reason=f"AgentCliError(第 {attempt} 次):{e}")
                if attempt < total_attempts:
                    time.sleep(AGENT_RETRY_SLEEP_S)
        if last_err is not None:
            raise last_err

        extra_outputs = []
        diff_meta: dict | None = None
        diff_complete = True
        if worktree is not None and node.writable:
            diff, diff_meta = _collect_diff(
                baseline, worktree, baseline_manifest, baseline_digest)
            diff_meta["source_head"] = source_head
            diff_complete = bool(diff_meta.get("complete", True))
            diff_ref = store_artifact(
                ctx.run_dir, name=f"{node.id}.diff",
                filename=f"{node.id}.diff.{iteration}.patch", content=diff)
            extra_outputs = [diff_ref]

        ref = store_artifact(
            ctx.run_dir, name=f"{node.id}.output",
            filename=f"{node.id}.output.{iteration}.txt",
            content=text.encode("utf-8"),
        )
        artifacts = [artifact_entry(
            name=ref.name, role="report", path=ref.path, sha256=ref.sha256,
            size_bytes=len(text.encode("utf-8")), media_type="text/markdown")]
        if extra_outputs:
            artifacts.append(artifact_entry(
                name=extra_outputs[0].name, role="diff",
                path=extra_outputs[0].path, sha256=extra_outputs[0].sha256,
                size_bytes=(extra_outputs[0].path.stat().st_size
                            if extra_outputs[0].path.exists() else -1),
                media_type="text/x-diff", complete=diff_complete,
                metadata=diff_meta))
        usage = getattr(result, "usage", None)
        actual_cost = getattr(result, "cost_usd", None)
        events = {
            "node": node.id, "iteration": iteration,
            "model_requested": requested_model,
            "model_used": requested_model,
            "runner": runner_name,
            "degraded": False, "output_truncated": False,
            "output_path": str(ref.path), "output_sha256": ref.sha256,
            "artifacts": artifacts,
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
            "cost_usd": actual_cost,
            "duration_s": round(time.monotonic() - started, 3),
        }
        if extra_outputs:
            events.update({
                "diff_path": str(extra_outputs[0].path),
                "diff_sha256": extra_outputs[0].sha256,
                "source_head": source_head,
                "baseline_digest": diff_meta["baseline_digest"],
                "result_digest": diff_meta["result_digest"],
                "patch_digest": diff_meta["patch_digest"],
            })
        ctx.log.emit("node_done", **events)

        # state 里的产物与事件里的类型化条目同构(A6:重放 == 运行时状态)
        updates = {"artifacts": {e["name"]: e for e in artifacts},
                   "iterations": {node.id: 1}}
        return updates

    return run

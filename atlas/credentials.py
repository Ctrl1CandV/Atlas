# -*- coding: utf-8 -*-
"""凭据读写:config/.env 是唯一存储,界面是写入方之一。

安全设计(PLAN-v2 决定 2 + dsh 调研的采纳项):

- **读视图在类型上没有 value 字段**。不是"记得别返回",是 CredentialView
  这个 dataclass 里根本没有放值的位置(A8 的被测对象)。值只在两个方向过线:
  界面写入(upsert)与引擎调用(取值给 HTTP 客户端)。
- **原子写**:先写同目录临时文件再 os.replace,写一半崩溃不留半份配置。
- **尽力收紧 ACL**:Windows 上用 icacls 移除继承、只留当前用户——这是
  Windows 上最接近 0600 的做法;失败不阻塞、返回 warning。
- 保留文件里已有的注释与键顺序,只动要改的行。
- 空/纯空格的密钥值显式报错,不静默存一个空串(空串会伪装成"已配置")。

诚实边界(PLAN 第 7 节):明文挡得住其他 Windows 用户,挡不住同用户身份的
进程(agent 与你同 UID,能读任何你能读的文件)。这是自律,不是边界。
"""
import dataclasses
import os
import subprocess
import sys
import tempfile
import threading
import time as _time
from pathlib import Path

from atlas.config import CONFIG_DIR

ENV_PATH = CONFIG_DIR / ".env"

# 读-改-写整体加锁:Web 线程池并发写时后写覆盖先写会静默丢一笔更新(M3 审查🟠5)
_WRITE_LOCK = threading.Lock()


@dataclasses.dataclass(frozen=True)
class CredentialView:
    """凭据状态视图。字段集合是契约:A8 断言这里没有 value 类字段。"""
    configured: bool
    source: str        # "file"(.env) | "env"(进程环境) | "unset"
    writable: bool


class CredentialError(Exception):
    """密钥值的写入不合法(空/纯空格),或文件不可写。"""


def _parse_env_file(text: str) -> dict[str, str]:
    """解析 KEY=VALUE;忽略注释与空行;不评价格式。"""
    values: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


class EnvStore:
    """config/.env 的结构保留式读写。"""

    def __init__(self, path: Path = ENV_PATH) -> None:
        self.path = Path(path)

    # ── 读 ──────────────────────────────────────

    def read_value(self, key: str) -> str | None:
        """只在引擎调用方向使用;不进任何 API 响应。"""
        if self.path.exists():
            file_vals = _parse_env_file(self.path.read_text(encoding="utf-8-sig"))
            if file_vals.get(key):
                return file_vals[key]
        env_val = os.environ.get(key)
        return env_val or None

    def view(self, key: str) -> CredentialView:
        """凭据状态三态,无值。"""
        in_file = bool(
            self.path.exists()
            and _parse_env_file(self.path.read_text(encoding="utf-8-sig")).get(key)
        )
        in_env = bool(os.environ.get(key))
        writable = (not self.path.exists()) or os.access(self.path, os.W_OK)
        return CredentialView(
            configured=in_file or in_env,
            source="file" if in_file else ("env" if in_env else "unset"),
            writable=writable,
        )

    # ── 写 ──────────────────────────────────────

    def remove(self, key: str) -> bool:
        """删除一个键(保留其余行)。返回是否真的删了行。

        无匹配行时不重写文件——空跑一遍会白白变更 mtime/ACL(🟡9)。
        """
        with _WRITE_LOCK:
            if not self.path.exists():
                return False
            lines = self.path.read_text(encoding="utf-8-sig").splitlines()
            out = []
            removed = False
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    if stripped.partition("=")[0].strip() == key:
                        removed = True
                        continue
                out.append(line)
            if not removed:
                return False
            self._save("\n".join(out) + "\n")
            return True

    def upsert(self, key: str, value: str) -> list[str]:
        """写入或更新一个键。返回执行过程中的 warning 列表(非致命)。

        保留所有注释、空行与其他键的顺序;目标键已存在就原地替换,
        不存在就追加到末尾。
        """
        if not value or not value.strip():
            # 空输入的语义是"保留原值",由调用方处理;走到这里的是纯空格——
            # 静默存空串会让"已配置"伪装成立
            raise CredentialError(
                f"密钥 {key} 的值是空的。留空表示保留原值,纯空格不是合法密钥"
            )
        with _WRITE_LOCK:
            if self.path.exists():
                lines = self.path.read_text(encoding="utf-8-sig").splitlines()
            else:
                lines = []
            replaced = False
            out: list[str] = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#") and "=" in stripped:
                    k = stripped.partition("=")[0].strip()
                    if k == key:
                        out.append(f"{key}={value}")
                        replaced = True
                        continue
                out.append(line)
            if not replaced:
                if out and out[-1].strip():
                    out.append("")
                out.append(f"{key}={value}")
            return self._save("\n".join(out) + "\n")

    def _save(self, content: str) -> list[str]:
        """原子写 + 尽力 ACL。返回 warnings。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".env-", suffix=".tmp")
        warnings: list[str] = []
        try:
            # 先收紧 ACL 再写内容:临时文件默认继承目录 ACL,密钥若先写,
            # 「写入→replace→icacls」窗口内对其他用户可读(🟡8)
            tmp_path = Path(tmp_name)
            warnings.extend(self._harden_acl(tmp_path))
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            self._replace_with_retry(tmp_path, self.path)
        except Exception:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        return warnings

    def _replace_with_retry(self, tmp: Path, target: Path) -> None:
        """os.replace 在 Windows 上可能因他线程正持有打开句柄而 PermissionError
        (读取方没有 FILE_SHARE_DELETE;AV 扫描也会命中)。短退避重试(🟡7)。"""
        for attempt in range(4):
            try:
                os.replace(tmp, target)
                return
            except PermissionError:
                if attempt == 3:
                    raise
                _time.sleep(0.05 * (attempt + 1))

    def _harden_acl(self, path: Path | None = None) -> list[str]:
        """Windows:icacls 移除继承、只授权当前用户。失败不阻塞。"""
        if sys.platform != "win32":
            return []
        target = path or self.path
        if not target.exists():
            return []
        try:
            user = os.environ.get("USERNAME", "")
            if not user:
                return ["无法确定当前用户名,跳过 ACL 收紧"]
            proc = subprocess.run(
                ["icacls", str(target), "/inheritance:r",
                 f"/grant:r", f"{user}:F"],
                capture_output=True, timeout=10,
            )
            if proc.returncode != 0:
                stderr = (proc.stderr or b"").decode(errors="replace").strip()
                return [f"icacls 收紧失败(不阻塞):{stderr[:120]}"]
            return []
        except Exception as e:  # noqa: BLE001 - 加固尽力而为
            return [f"ACL 收紧异常(不阻塞):{e}"]

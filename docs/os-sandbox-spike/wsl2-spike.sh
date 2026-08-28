#!/usr/bin/env bash
# WSL2 spike 一键复现脚本（E-5 调研；在 Windows 侧 Git Bash 运行）。
# 前置：WSL2 + 一个发行版（本机 Ubuntu-24.04）；管理员权限不需要。
# 用法：bash docs/os-sandbox-spike/wsl2-spike.sh > docs/os-sandbox-spike/results-wsl2.md
# 产物是 markdown 原始记录；判读与 GO/NO-GO 判定写在 RESEARCH-os-sandbox.md §6。
set -u
DISTRO="Ubuntu-24.04"
HDR=0
section() { HDR=$((HDR+1)); echo; echo "## $HDR. $*"; }

echo "# WSL2 spike 实测原始记录"
echo "> 运行时间：$(date '+%Y-%m-%d %H:%M:%S%z')；脚本：os-sandbox-spike/wsl2-spike.sh"

section "版本锁定（发行版/内核/WSL，复现前提）"
echo '```text'
# wsl.exe 管理命令在管道里输出 UTF-16LE,剥 NUL 会把中文毁掉,须按 UTF-16 转;
# powershell 中文输出是 GBK。两者都经 iconv -c 容错。
wsl.exe --version 2>&1 | iconv -f UTF-16LE -t UTF-8 -c
wsl.exe -l -v 2>&1 | iconv -f UTF-16LE -t UTF-8 -c
wsl.exe -d "$DISTRO" -e sh -c 'uname -a; head -2 /etc/os-release'
powershell -NoProfile -Command "\$os=Get-CimInstance Win32_OperatingSystem; '{0} {1}' -f \$os.Caption, \$os.Version" | iconv -f GBK -t UTF-8 -c
echo '```'

section "冷启动延迟（wsl --shutdown 后第一条命令；G1 上界 10s）"
wsl.exe --shutdown >/dev/null 2>&1
sleep 2
echo '```text'
{ time wsl.exe -d "$DISTRO" -e sh -c 'true' ; } 2>&1
echo '```'

section "热命令往返 ×3（G1 上界 2s/次）"
echo '```text'
for i in 1 2 3; do
  { time wsl.exe -d "$DISTRO" -e sh -c 'true' ; } 2>&1 | grep real
done
echo '```'

section "drvfs 跨界文件 IO：Windows 卷上写/读 64MiB（G2：相对 ext4 劣化 ≤10x）"
WIN_TMP="$(cygpath -m "${LOCALAPPDATA:-$TEMP}")/atlas-wsl-spike-$$"
WSL_TMP="/mnt/$(echo "$WIN_TMP" | cut -c1 | tr 'A-Z' 'a-z')${WIN_TMP#?:}"
mkdir -p "$WIN_TMP"
echo '```text'
wsl.exe -d "$DISTRO" -e sh -c "sync && dd if=/dev/zero of='$WSL_TMP/t.bin' bs=1M count=64 conv=fdatasync 2>&1 | tail -1; dd if='$WSL_TMP/t.bin' of=/dev/null bs=1M 2>&1 | tail -1; rm -f '$WSL_TMP/t.bin'"
echo '```'

section "WSL 原生 ext4 文件 IO：写/读 64MiB（G2 基准）"
echo '```text'
wsl.exe -d "$DISTRO" -e sh -c "dd if=/dev/zero of=\$HOME/atlas-spike.bin bs=1M count=64 conv=fdatasync 2>&1 | tail -1; dd if=\$HOME/atlas-spike.bin of=/dev/null bs=1M 2>&1 | tail -1; rm -f \$HOME/atlas-spike.bin"
echo '```'
rmdir "$WIN_TMP" 2>/dev/null

section "env 白名单传递（G3：默认不继承；WSLENV 显式放行才可见）"
echo '```text'
echo "Windows 侧环境变量条数: $(env | wc -l)"
printf '默认继承 ATLAS_SPIKE 的条数(期望 0): '
wsl.exe -d "$DISTRO" -e sh -c 'env | grep -c ATLAS_SPIKE' || echo 0
printf 'WSLENV=ATLAS_SPIKE 放行后的值: '
ATLAS_SPIKE=marker-xyz WSLENV=ATLAS_SPIKE wsl.exe -d "$DISTRO" -e sh -c 'echo "$ATLAS_SPIKE"'
echo "放行后 WSL 可见 env 总条数(对比 Windows 侧,证明默认是白名单而非全量): "
wsl.exe -d "$DISTRO" -e sh -c 'env | wc -l'
echo '```'

section "跨 OS 取消信号级联（G3：杀 Windows 侧 wsl.exe，Linux 侧进程树是否存活）"
echo '```text'
# 方法论(三次迭代后定稿,详见 results-wsl2.md 的勘误记):
#  - 检测必须用"独立 wsl 客户端 + 不自匹配的 pgrep 模式"(8640[1]);
#  - 必须区分"客户端干净退出"(会话清理会杀子进程,测量会空洞化)与
#    "taskkill 硬杀"(孤儿存活——这才是 Atlas 取消路径真正会发生的事)。
( wsl.exe -d "$DISTRO" -e sh -c 'setsid sh -c "sleep 86401 & sleep 86401" >/dev/null 2>&1 & sleep 40' & )
sleep 4
printf 't+4 客户端存活,独立客户端查进程数(期望 4:外层 sh 的 -c 串自含模式串 + setsid sh + 两 sleep): '
wsl.exe -d "$DISTRO" -e sh -c 'pgrep -fc "sleep 8640[1]"'
echo "-- taskkill 硬杀 wsl.exe 客户端(Atlas 取消路径等价物) --"
for pid in $(powershell -NoProfile -Command "Get-Process wsl -ErrorAction SilentlyContinue | ForEach-Object {\$_.Id}"); do
  taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
done
sleep 3
printf 't+7 硬杀后独立客户端查孤儿数(>0 = 取消不级联,G3 红): '
wsl.exe -d "$DISTRO" -e sh -c 'pgrep -fc "sleep 8640[1]"'
echo "-- 对照组:wsl --terminate(VM 级清理) --"
wsl.exe --terminate "$DISTRO" >/dev/null 2>&1
sleep 2
printf 'terminate 后孤儿数(期望 0): '
wsl.exe -d "$DISTRO" -e sh -c 'pgrep -fc "sleep 8640[1]"'
echo '```'

section "stdout/stderr 流式回传（G3：逐行到达而非结束后一次性吐出）"
echo '```text'
wsl.exe -d "$DISTRO" -e sh -c 'for i in 1 2 3; do echo "line-$i out"; echo "line-$i err" >&2; sleep 1; done' 2>&1 |
  while IFS= read -r line; do echo "$(date +%H:%M:%S.%3N 2>/dev/null || date +%H:%M:%S) $line"; done
echo '```'
echo "判读方法:三行时间戳应各差约 1s(真流式);全部同秒=缓冲后一次吐出(G3 红)。"

section "退出码传播（G3：Linux exit 42 必须原样到达 Windows 侧）"
wsl.exe -d "$DISTRO" -e sh -c 'exit 42'
echo "Linux exit 42 到达 Windows 侧的退出码: $?（期望 42）"
wsl.exe -d "$DISTRO" -e sh -c 'sh -c "exit 7"'
echo "嵌套 `exit 7` 退出码: $?（期望 7）"

section ".wslconfig 内存限制机制"
echo '```text'
if [ -f "$USERPROFILE/.wslconfig" ]; then cat "$USERPROFILE/.wslconfig"; else
  echo ".wslconfig 不存在(默认=主机内存的 50% 或 Windows 11 的动态分配——官方文档口径,未实测压测)"
fi
echo '```'

section "localhost 转发（agent 在 WSL 内绑回环时 Windows 侧可达性；含取消路径对照）"
echo '```text'
wsl.exe -d "$DISTRO" -e sh -c 'command -v python3 || echo NO_PYTHON3'
# 客户端保持存活(Atlas 正常运行形态):Windows 侧应可达。
( wsl.exe -d "$DISTRO" -e sh -c 'cd /tmp && setsid python3 -m http.server 8931 >/dev/null 2>&1 & sleep 30' & )
sleep 4
printf 't+4 客户端存活期 Windows 侧 curl(期望 200): '
curl -s -o /dev/null -w '%{http_code}\n' --max-time 4 http://127.0.0.1:8931/ || echo unreachable
echo "-- 硬杀客户端(取消路径):孤儿服务器继续服务(与 §7 级联红一致) --"
for pid in $(powershell -NoProfile -Command "Get-Process wsl -ErrorAction SilentlyContinue | ForEach-Object {\$_.Id}"); do
  taskkill //PID "$pid" //T //F >/dev/null 2>&1 || true
done
sleep 3
printf 't+7 硬杀后 curl(实测仍 200 = 孤儿仍在服务): '
curl -s -o /dev/null -w '%{http_code}\n' --max-time 4 http://127.0.0.1:8931/ || echo unreachable
wsl.exe --terminate "$DISTRO" >/dev/null 2>&1
sleep 2
printf 'terminate 后 curl(期望 000/unreachable): '
curl -s -o /dev/null -w '%{http_code}\n' --max-time 4 http://127.0.0.1:8931/ || echo unreachable
echo '```'

section "收尾"
wsl.exe --terminate "$DISTRO" >/dev/null 2>&1
echo "spike 完成。把本文件内容粘进 results-wsl2.md 并在 RESEARCH-os-sandbox.md §2.2/§6 做判读。"

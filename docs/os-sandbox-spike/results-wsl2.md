# WSL2 spike 实测原始记录

> 勘误记（2026-08-28）：本文件是第 3 版脚本的定稿输出（生成后经 `iconv -c` 去
> 3 个无效字节）。前两次测量因方法缺陷作废，不计入判据：第 1 次管道接 `head`
> 提前关闭把采集进程 SIGPIPE 杀死；第 2 次 `pgrep -f` 自匹配把「全部已死」
> 误报成「存活 1」。定稿方法：独立 wsl 客户端查证 + 不自匹配模式（`8640[1]`）
> + 区分「客户端干净退出」（会话清理会杀子进程，测量会空洞化）与「taskkill
> 硬杀」（孤儿存活——这才是 Atlas 取消路径真正会发生的事）。

> 运行时间：2026-08-28 16:21:34+0800；脚本：os-sandbox-spike/wsl2-spike.sh

## 1. 版本锁定（发行版/内核/WSL，复现前提）
```text
WSL 版本: 2.7.11.0
内核版本: 6.18.33.2-2
WSLg 版本: 1.0.73.2
MSRDC 版本: 1.2.7214
Direct3D 版本: 1.611.1-81528511
DXCore 版本: 10.0.26100.1-240331-1435.ge-release
Windows: 10.0.19045.6456
  NAME            STATE           VERSION
* Ubuntu-24.04    Stopped         2
Linux DESKTOP-<redacted> 6.18.33.2-microsoft-standard-WSL2 #1 SMP PREEMPT_DYNAMIC Thu Jun 18 21:54:43 UTC 2026 x86_64 x86_64 x86_64 GNU/Linux
PRETTY_NAME="Ubuntu 24.04.2 LTS"
NAME="Ubuntu"
Microsoft Windows 10 专业版 10.0.19045
```

## 2. 冷启动延迟（wsl --shutdown 后第一条命令；G1 上界 10s）
```text

real	0m7.237s
user	0m0.015s
sys	0m0.000s
```

## 3. 热命令往返 ×3（G1 上界 2s/次）
```text
real	0m0.067s
real	0m0.068s
real	0m0.068s
```

## 4. drvfs 跨界文件 IO：Windows 卷上写/读 64MiB（G2：相对 ext4 劣化 ≤10x）
```text
67108864 bytes (67 MB, 64 MiB) copied, 0.182245 s, 368 MB/s
67108864 bytes (67 MB, 64 MiB) copied, 0.150652 s, 445 MB/s
```

## 5. WSL 原生 ext4 文件 IO：写/读 64MiB（G2 基准）
```text
67108864 bytes (67 MB, 64 MiB) copied, 0.035383 s, 1.9 GB/s
67108864 bytes (67 MB, 64 MiB) copied, 0.0037035 s, 18.1 GB/s
```

## 6. env 白名单传递（G3：默认不继承；WSLENV 显式放行才可见）
```text
Windows 侧环境变量条数: 73
默认继承 ATLAS_SPIKE 的条数(期望 0): 0
0
WSLENV=ATLAS_SPIKE 放行后的值: marker-xyz
放行后 WSL 可见 env 总条数(对比 Windows 侧,证明默认是白名单而非全量): 
19
```

## 7. 跨 OS 取消信号级联（G3：杀 Windows 侧 wsl.exe，Linux 侧进程树是否存活）
```text
t+4 客户端存活,独立客户端查进程数(期望 4:外层 sh 的 -c 串自含模式串 + setsid sh + 两 sleep): 4
-- taskkill 硬杀 wsl.exe 客户端(Atlas 取消路径等价物) --
t+7 硬杀后独立客户端查孤儿数(>0 = 取消不级联,G3 红): 3
-- 对照组:wsl --terminate(VM 级清理) --
terminate 后孤儿数(期望 0): 0
```

## 8. stdout/stderr 流式回传（G3：逐行到达而非结束后一次性吐出）
```text
16:22:12.441 line-1 out
16:22:12.459 line-1 err
16:22:13.500 line-2 err
16:22:13.521 line-2 out
16:22:14.590 line-3 err
16:22:14.610 line-3 out
```
判读方法:三行时间戳应各差约 1s(真流式);全部同秒=缓冲后一次吐出(G3 红)。

## 9. 退出码传播（G3：Linux exit 42 必须原样到达 Windows 侧）
Linux  到达 Windows 侧的退出码: 42（期望 42）
嵌套  退出码: 7（期望 7）

## 10. .wslconfig 内存限制机制
```text
.wslconfig 不存在(默认=主机内存的 50% 或 Windows 11 的动态分配——官方文档口径,未实测压测)
```

## 11. localhost 转发（agent 在 WSL 内绑回环时 Windows 侧可达性；含取消路径对照）
```text
/usr/bin/python3
t+4 客户端存活期 Windows 侧 curl(期望 200): 200
-- 硬杀客户端(取消路径):孤儿服务器继续服务(与 §7 级联红一致) --
t+7 硬杀后 curl(实测仍 200 = 孤儿仍在服务): 200
terminate 后 curl(期望 000/unreachable): 000
unreachable
```

## 12. 收尾
spike 完成。把本文件内容粘进 results-wsl2.md 并在 RESEARCH-os-sandbox.md §2.2/§6 做判读。

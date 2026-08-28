# OS 级沙箱调研（Stage E · E-5）

> 状态：**调研中**（本文件随实测推进填充；结论节最后写）。
> 合同：`docs/PLAN-stage-e-2026-08-27.md` E-5 章。纯调研批次——不做生产功能、
> 不改任何默认 runner 行为；调研完成前 README/skill 不得出现任何 isolated/
> secure/隔离**宣称**（`tests/test_docs_agent_contract.py` 的 grep 断言永久把守）。

## 0. 调研边界与非目标

- **非目标**：把 worktree 副本改名叫"沙箱"；在本批内落地隔离 runner；宣称 Atlas 已具备 OS 级隔离。
- **目标**：给出企业内网用户最常问的"能不能沙箱"一个**有实测依据的答案**，并在判据满足时立案真正的隔离 runner。
- 方法论：每个象限只写"现状事实"（可复现的观测/官方文档明示的行为），不写猜想；未实测的环境（无 Docker、Windows Sandbox 需管理员启用）如实标注 `未实测`。

## 1. 威胁模型四象限

### 1.1 宿主文件系统写面

- **Attack scenario**：agent 收到恶意指令（被注入的 issue 文本、被投毒的依赖 README），在 workdir 之外写文件——部署 SSH key、改 PATH、加密用户文档。
- **现状事实**：CLI 以当前用户身份运行（`skill/SKILL.md`、SECURITY.md 免责句）；`writable: false` 只是事后 diff 对照能**发现**越权写，不能**阻止**它；进程理论上可访问该用户的全部可达路径。
- **候选缓解**：WSL2 内运行 agent（写面收敛到 WSL 文件系统 + 显式映射目录）；Windows Sandbox（一次性桌面，丢弃式）；容器只读根文件系统 + 显式挂载。

### 1.2 网络出口

- **Attack scenario**：agent 把 env 里读到的密钥 POST 到第三方域名；或被指示下载执行二阶段载荷。
- **现状事实**：`allow_web: false` 只收敛 Claude CLI 自带的 WebSearch/WebFetch 工具；`Bash`（coding agent）始终可能访问网络（`skill/SKILL.md` 明示）；无任何进程级网络边界。
- **候选缓解**：WSL2/VM 层防火墙（Hyper-V `New-NetFirewallRule -VMCreatorId`，Windows 11 22H2+ 支持 per-VM 出站规则）；Windows Sandbox `Networking=Disable`；容器 network=none。

### 1.3 凭据与环境变量

- **Attack scenario**：agent 枚举进程环境，捞到 `ANTHROPIC_API_KEY` 等明文密钥外传。
- **现状事实**：agent 子进程环境是白名单（必需系统变量 + 所选供应商 endpoint 与凭据，见 `skill/SKILL.md`）；但**凭据本身必须进环境**（SDK/CLI 认证需要），子进程能读自己的 env 即能外传；这是白名单机制的固有边界。
- **候选缓解**：WSL2 跨界环境变量走 `WSLENV` 白名单机制（默认不继承全部变量，实测见 §2）；隔离层内用短期 token/代理网关换真凭据（架构性缓解，超出本批）。

### 1.4 产物回收完整性

- **Attack scenario**：隔离层吞掉/截断 stdout 尾部、丢退出码、丢 diff 原料，导致 Atlas 账本记录"成功"而产物不完整——数据不静默丢失红线被隔离层破坏。
- **现状事实**：当前同用户进程模式无隔离层，stdout/stderr 直连管道、退出码直传；Atlas 已有的保障（write-once 产物 + SHA-256、读回复验）作用在文件层。
- **候选缓解**：任何隔离 runner 必须过 §5-G3 的兼容矩阵（流式回传/退出码/取消级联），这是 GO 的硬门槛。

## 2. WSL2 spike（实测）

> 环境：见 §2.1 的版本记录。脚本：`os-sandbox-spike/wsl2-spike.sh`（一键复现）。
> 结果文件：`os-sandbox-spike/results-wsl2.md`（实测原始记录，判读在 §6）。

### 2.1 环境版本锁定

（待实测填充：Windows 版本、WSL 版本、内核、发行版）

### 2.2 实测 checklist（合同 ≥8 条）

| # | 项目 | 判据关注点 | 实测记录 |
|---|---|---|---|
| 1 | 冷启动延迟 | G1 上界 | 待实测 |
| 2 | 热命令往返 | G1 上界 | 待实测 |
| 3 | drvfs 跨界文件 IO | G2 劣化倍数 | 待实测 |
| 4 | WSL 原生 ext4 文件 IO | G2 基准 | 待实测 |
| 5 | env 白名单传递（WSLENV） | G3 | 待实测 |
| 6 | 跨 OS 取消信号级联 | G3 | 待实测 |
| 7 | stdout/stderr 流式回传 | G3 | 待实测 |
| 8 | 退出码传播 | G3 | 待实测 |
| 9 | `.wslconfig` 内存限制机制 | 运维边界 | 待实测 |
| 10 | 发行版/内核版本锁定 | 可复现性 | 待实测 |

## 3. Windows Sandbox（`.wsb` 参数矩阵）

- **许可前提**：Windows 10/11 **Pro/Enterprise/Education**；Home 不可用。功能默认未启用，需管理员 `Enable-WindowsOptionalFeature -FeatureName 'Containers-DisposableClientVM'`。
- **本机状态**：`未实测`——功能查询/启用需要管理员权限，本调研环境无管理员会话；`.wsb` 样例已生成（`os-sandbox-spike/wsb/`），启用后可一键复测。

| 参数组合 | 文件 | 预期代价（官方文档口径） | 实测 |
|---|---|---|---|
| 只读映射工作目录 | `mapped-ro.wsb` | agent 写 `writable:false` 路径直接失败 | 未实测 |
| 读写映射工作目录 | `mapped-rw.wsb` | 写面收敛到映射目录；关闭即弃 | 未实测 |
| 禁网 + 读写映射 | `mapped-rw-nonet.wsb` | 供应商 API 不可达——**与需要 LLM 的 agent 互斥**，仅适合纯本地 CLI 型任务 | 未实测 |

## 4. 容器化对照

- **本机状态**：`未实测`——调研环境无 Docker/Podman。下表为官方文档口径的候选分析（明确标注非实测），立项前须在目标环境复测。

| 维度 | Docker Desktop | Podman Machine | Windows Containers |
|---|---|---|---|
| 守护进程依赖 | 需要（WSL2 后端） | 无守护（podman machine 为轻量 VM） | 需要（Windows 服务） |
| 冷启动 | 容器秒级；Desktop 本身常驻 | machine 启动秒级 | 容器秒级 |
| 文件 IO（bind mount 跨 /mnt） | 与 WSL2 drvfs 同源，见 §2.3 | 同左 | Windows 原生卷 |
| 许可 | 企业 >250 人/$10M 营收需付费订阅 | 无 | 随 Windows Server |
| 对 Claude CLI 的兼容 | Linux 容器内 CLI 可行；凭据注入同 §1.3 | 同左 | Windows 容器镜像生态弱，候选存疑 |

## 5. GO/NO-GO 预定义判据（先于实测定义——本节随骨架提交，时序可由 git 历史验证）

对 **WSL2 候选**（`ATLAS_AGENT_SANDBOX=wsl` 方向）：

- **G1 启动开销上界**：`wsl --shutdown` 后冷启动 ≤ **10 秒**，且单命令热往返 ≤ **2 秒**（agent 节点粒度可接受的上界；超过即 NO-GO，因为每个 agent 节点都要付这个成本）。
- **G2 文件 IO 劣化上界**：工作目录放在 Windows 卷（drvfs `/mnt/...`）时，吞吐相对 WSL 原生 ext4 劣化 ≤ **10×**。Atlas 的 worktree 现实位于 Windows 侧，drvfs 是默认路径；若劣化超标，"把 worktree 迁入 WSL 原生 FS"是**另一个立项**（涉及与 Windows 侧审计/产物管线的路径契约），不计入本判据的豁免。
- **G3 关键兼容矩阵全绿**：`[流式回传、取消信号级联、env 受控传递、退出码传播]` 四项全部实测通过；**任一红 = NO-GO**（隔离层破坏产物回收完整性 = 违反数据红线，宁可不用）。

**GO = G1 ∧ G2 ∧ G3 同时满足**，才立案真正的隔离 runner（独立批次，含 feature-flag 原型 `ATLAS_AGENT_SANDBOX=wsl`）；任一不满足 = **NO-GO**，维持现状并保持全部文档的如实表述。Windows Sandbox 与容器路径参照同构判据，待各自环境可测时再评。

## 6. 结论（最后写）

（待全部实测完成后填充：逐判据判定 + 理由 + 后续动作建议）

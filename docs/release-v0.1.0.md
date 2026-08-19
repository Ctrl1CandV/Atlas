# Atlas v0.1.0 发布与构建来源记录

> 记录日期：2026-08-19。本文保存已经发布资产的 as-built truth，不尝试用后续文档或 workflow 改动重写历史。

## Release 身份

- Release：<https://github.com/Ctrl1CandV/Atlas/releases/tag/v0.1.0>
- Release ID：`373036384`
- 类型：stable（draft=false，prerelease=false），source-only；未发布 PyPI、wheel 或 installer。
- annotated tag object：`8da8d822350803ef44dd524a3afa036bc24132fe`
- peeled tag commit：`4f9b0b5fb4b14fe0523e1cc47cc5e11597d55a94`
- tag 签名：unsigned / unverified
- 发布时 `target_commitish`：`release/v0.1.0-rc.1`。验证源码身份时应以 peeled tag commit 为准，而不是可移动分支名。

## 当前三个资产

| 文件 | 大小 | SHA-256 |
|---|---:|---|
| `atlas-0.1.0.tar.gz` | 358,260 bytes | `a4a7f5fc55c80b0b0baccb8ab173fccfbf4d70b8690df88458b8169e221827ef` |
| `atlas-v0.1.0.spdx.json` | 1,349,050 bytes | `565d45fd62642273e27c64f9b4723c025e8ed7262034584ed3d204e94641a8f7` |
| `SHA256SUMS` | 176 bytes | `0d9c88a4733038430e10c10d5e1f71e369fa5e9045b9ee384ba2b6df75da0bf7` |

`SHA256SUMS` 的内容：

```text
a4a7f5fc55c80b0b0baccb8ab173fccfbf4d70b8690df88458b8169e221827ef  atlas-0.1.0.tar.gz
565d45fd62642273e27c64f9b4723c025e8ed7262034584ed3d204e94641a8f7  atlas-v0.1.0.spdx.json
```

本机 `dist/` 中后来重新构建的同名文件不是发布证据；只有从 Release 下载并匹配上述 digest 的字节才是当前公开资产。

## Provenance

当前三个资产由以下运行生成并上传：

- Workflow：`Release assets`
- Run ID：`32254337034`（run number 6，attempt 1）
- Run：<https://github.com/Ctrl1CandV/Atlas/actions/runs/32254337034>
- Job ID：`96072425494`
- Source commit：`d34d785e1f2203453e62c16fdcc612295d6e8715`
- Attestation ID：`41605837`
- Predicate：SLSA provenance v1
- Signing/Transparency：GitHub Actions OIDC、public Sigstore、Rekor integrated timestamp

只有 attestation `41605837` 同时匹配当前三个资产。更早的成功 runs/attestations 对应被替换的旧资产，不能用于验证当前下载。

## 已知 tag/build 差异

`v0.1.0` tag 指向 `4f9b0b5…`，当前资产 provenance 指向 `d34d785…`。原因是当时的手动 workflow checkout 了触发所在分支 commit，并把结果发布到硬编码 tag，而不是显式 checkout `refs/tags/v0.1.0`。

审计时确认，从 tag commit 到 asset-build commit 的 tracked 变化只有 `.github/workflows/release-assets.yml` 的加入/演进；该文件不进入 sdist。这说明已观察到的应用 payload 漂移风险较低，但**不能证明两个源码身份相等**。

处理原则：

1. 不移动 `v0.1.0`，不静默重新上传资产来掩盖差异。
2. 保留本页与 Release 正文中的准确披露。
3. 在 `v0.1.1` 或下一正式版本使用 exact-tag checkout，并断言 tag commit、workflow `head_sha`、provenance `gitCommit` 完全一致。

## 已完成与未完成的验证

已记录完成：

- 发布页为 stable，且恰好有上述三个资产。
- 下载的 `SHA256SUMS` 与 sdist/SBOM digest 匹配。
- attestation `41605837` 的 subjects 匹配三个当前资产。
- 2026-08-19 曾对下载的 sdist 执行 Windows/Python 3.12 offline smoke：100 entries、0 scan findings、version 0.1.0、三个 console scripts、六 MCP tools、spec parse 和 config init。

没有完成或不能从公开元数据证明：

- tag 的加密签名（tag 未签名）。
- v0.1.0 的公开 required Windows CI（公开仓库没有完整 CI workflow run）。
- protected GitHub environment 中的 real-provider job；阶段 D 是本机经 MCP 执行，不是该作业。
- tag commit 与 asset build commit 相同（明确不相同）。

## 用户验证命令

下载三个资产后，在 PowerShell 中：

```powershell
Get-FileHash -Algorithm SHA256 atlas-0.1.0.tar.gz
Get-FileHash -Algorithm SHA256 atlas-v0.1.0.spdx.json
Get-Content SHA256SUMS
```

若安装了 GitHub CLI，可按 GitHub 当前 attestation CLI 文档验证相应资产；无论使用何种工具，都必须检查 subject digest 和 provenance 的 `gitCommit`，不能只看“验证成功”四个字。

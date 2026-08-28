# 开发者构建与发布检查

## 公开树的检查（任何人可复现）

```powershell
uv lock --check
uv run python -m compileall -q atlas
npm --prefix web run lint
npm --prefix web run build
uv run python scripts/docs_contract_gate.py
uv build --sdist
```

本仓库是**产品交付面**：内部开发设施（Python/Web 测试套件、浏览器 e2e、
规划与审查档案）不随公开仓库分发，保留在维护者的开发树中；公开质量凭证
以 [`VERIFICATION 报告`](../../docs/VERIFICATION-2026-08-28.md) 与 CI 门为准。
真实 API 测试默认排除，只能在有超时、受保护环境和一次性凭据的手动流程中运行。

## CI 信号

Windows 是必需支持信号。Ubuntu job 只提供兼容性信息。CI 会校验/预演六个
工作流、从干净源码构建、扫描发布表面中的秘密/私有路径/占位符、执行文档
契约门，并检查 source distribution。

## 发布产物

Tag 形如 `v<包版本>`（当前 `v0.1.0`，包版本 `0.1.0`）。发布上传源码 sdist、
SBOM、bundle 包与 SHA256SUMS；不构建 wheel、不发布 PyPI。GitHub provenance
attestation 失败会阻断发布，不能把未生成的证明描述为成功。

→ [回到概览](#/guide/overview)

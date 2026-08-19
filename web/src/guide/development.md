# 开发者构建与发布检查

## 本机检查

```powershell
uv lock --check
uv run python -m compileall -q atlas
uv run pytest
npm --prefix web run lint
npm --prefix web run test:diff
npm --prefix web run build
uv build --sdist
```

不要在文档中手写测试数量；以命令输出和 CI 结果为准。真实 API 测试默认排除，只能在有超时、受保护环境和一次性凭据的手动流程中运行。

## CI 信号

Windows 是必需支持信号。Ubuntu job 只提供兼容性信息。CI 还会校验/预演六个工作流、从干净源码构建、扫描发布表面中的秘密/私有路径/占位符，并检查 source distribution。

## 发布产物

Tag 必须是 `v0.1.0`，与 Python 包版本 `0.1.0` 一致。发布仅上传源码 sdist、SHA256 与 SPDX SBOM；不构建 wheel、不发布 PyPI。GitHub provenance attestation 失败会阻断发布，不能把未生成的证明描述为成功。

→ [回到概览](#/guide/overview)

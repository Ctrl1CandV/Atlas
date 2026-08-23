## 变更内容

一句话说明这个 PR 做了什么,以及为什么。

## 用户可见影响

- [ ] 是用户可见变更(已更新 `CHANGELOG.md` 的 Unreleased 段)
- [ ] 纯内部变更(测试、重构、注释)

## 验证

在 Windows 源码根目录执行并粘贴结果摘要:

```powershell
uv lock --check
uv run python -m compileall -q atlas scripts
uv run pytest
npm --prefix web run lint
npm --prefix web test
npm --prefix web run build
```

涉及真实供应商行为的变更:默认排除的 `real_api` 测试是否需要补充?是否先 dry-run?

## 纪律确认(见 CONTRIBUTING.md)

- [ ] 失败路径 fail-closed,不吞异常、不静默降级
- [ ] 事件账本保持 append-only 且可重放;新事件有测试
- [ ] 没有把 `config/.env`、活动配置、本机绝对路径或运行产物带进提交
- [ ] 文档与实际行为一致(不写"已支持"而实际没有)

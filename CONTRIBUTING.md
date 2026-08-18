# Contributing to Atlas

Atlas uses Python version `0.1.0rc1`, product version `0.1.0-rc.1`, and release tag `v0.1.0-rc.1`. This RC is Windows-supported, source-only, and not published to PyPI.

## Source setup

Requirements: Windows 10/11 x64, Git, Python 3.12, [uv](https://docs.astral.sh/uv/), and Node.js 22.12 or newer. Claude Code 2.1.0+ is needed only for opt-in production agent execution.

```powershell
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

Copy generic config examples only when local runtime configuration is needed. Use test credentials and never commit `config/.env`, active `config/*.json`, or `runs/`.

## Change rules

1. Keep changes focused and add regression coverage for behavior changes.
2. Preserve fail-closed validation, loopback-only service behavior, append-only evidence, and artifact integrity.
3. Production agents must stay disabled unless `config/agents.json` explicitly selects `runner: local_cli`. Missing configuration and incomplete preflight must fail before run creation.
4. Do not describe worktree copying as an OS sandbox. Claude CLI runs as the current user and can theoretically access all host paths available to that user; Atlas avoids writing the original project but does not provide same-user OS isolation.
5. Preserve byte-manifest diff collection: compare the frozen baseline and agent result as ordinary-file bytes, generate the complete textual unified diff without `git add`, filters, hooks, attributes, textconv, or external diff, fail loudly on binary changes, and bind approval to `baseline_digest`, `result_digest`, and `patch_digest`.
6. Preserve the minimal agent child environment: required system variables plus the selected provider endpoint and credential. Agent models require `anthropicBaseUrl`.
7. Document `allow_web` accurately: it defaults off and only adds `WebSearch`/`WebFetch`; coding-agent `Bash` may still access the network. Keep `max_turns` documented as specification metadata, with hard limits supplied by deadlines and configured budgets.
8. Treat workflow inputs and external content as untrusted.
9. Keep real-provider tests opt-in, narrowly scoped, protected, time-limited, and absent from ordinary CI.
10. Update `CHANGELOG.md` for user-visible changes. Do not add repository or maintainer URLs unless verified.

## Validate

```powershell
uv lock --check
uv run python -m compileall -q atlas
uv run pytest
npm --prefix web run lint
npm --prefix web run test:diff
npm --prefix web run build
uv build --sdist
```

Real-provider tests can cost money. Run only the deliberately selected test with disposable credentials or use the protected manual workflow; do not upload resulting private configuration or run data.

## Submit

Describe user-facing effects, security implications, tests run, and Windows-specific behavior. Do not attach private configuration, credentials, run directories, prompts, model outputs, or proprietary source. Contributions are accepted under Apache License 2.0.

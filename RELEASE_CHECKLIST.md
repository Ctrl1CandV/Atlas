# Release Checklist

## Scope and version
- [ ] `pyproject.toml`, `uv.lock`, and `atlas/__init__.py` report Python `0.1.0rc1`.
- [ ] `web/package.json` and its lockfile report private frontend `0.1.0-rc.1`.
- [ ] Release tag is exactly `v0.1.0-rc.1`; CI converts PEP 440 `0.1.0rc1` before comparison.
- [ ] Release is marked prerelease, Windows-supported, source-only, and not published to PyPI.
- [ ] No unverified repository-owner placeholder, clone URL, advisory URL, or changelog URL is present.

## Security and privacy
- [ ] Production agents are documented as opt-in only through explicit `config/agents.json` `runner: local_cli`; missing configuration remains fail-closed.
- [ ] Claude CLI is documented as a same-user process, and worktree copying is never described as an OS sandbox.
- [ ] Documentation states that Atlas does not write the original project; compares frozen baseline/result ordinary-file byte manifests; generates a complete textual unified diff without `git add`, filters, hooks, attributes, textconv, or external diff; fails loudly on binary changes; binds approval to `baseline_digest`, `result_digest`, and `patch_digest`; and cannot prevent access to other current-user host paths.
- [ ] Agent environment allowlisting, provider `anthropicBaseUrl`, `allow_web`/`WebSearch`/`WebFetch`, coding `Bash` network exposure, and `max_turns` specification-only semantics are current.
- [ ] `config/.env`, active `config/*.json`, `runs/`, caches, logs, and nested repositories are absent from the source archive.
- [ ] Secret/private-path/placeholder scan passes on release-facing text and examples without reading active config.
- [ ] Web remains bound to `127.0.0.1`; security tests pass.

## Automated validation
- [ ] `uv lock --check` and Python `compileall` pass.
- [ ] Tests pass with real-API tests deselected, including documentation contract tests.
- [ ] Web lint, diff test, and production build pass on Node.js 22.12+.
- [ ] All six shipped workflows parse, validate, and dry-run without provider calls or run directories.
- [ ] Windows required CI passes; Ubuntu compatibility signal is reviewed but is not a support claim.
- [ ] Clean checkout/source smoke and `uv build --sdist` pass; archive contents are inspected.
- [ ] Manual real-API job uses the protected environment, one selected discovery test, disposable credentials, and a timeout.

## Manual RC gates
- [ ] Fresh Windows source follows both README build paths.
- [ ] Generic config examples can be copied without editing the examples; agent execution stays disabled until the local copy explicitly selects `local_cli`.
- [ ] Built-in guide covers install/upgrade, YAML fields, MCP, human gates, agent runner/security/network boundaries, privacy, troubleshooting, and developer build.
- [ ] A dry-run creates no run directory and makes no provider call.
- [ ] Missing/disabled agent runner, missing Claude CLI, missing `anthropicBaseUrl`, missing model, and missing credential each fail closed before run creation.
- [ ] With disposable configuration, the coding workflow leaves the original directory unchanged, compares frozen baseline/result ordinary-file bytes, returns a complete textual unified diff for review, fails loudly on binary changes, and presents matching `baseline_digest`/`result_digest`/`patch_digest` approval evidence.

## Publish
- [ ] Tag points to the validated commit and release is marked prerelease.
- [ ] Uploaded artifacts are source sdist, `.sha256`, and SPDX SBOM only; no wheel or PyPI publish step exists.
- [ ] SHA256 file matches the uploaded sdist.
- [ ] GitHub provenance attestation succeeds for every uploaded artifact; failure blocks release rather than being reported as success.
- [ ] Downloaded source archive smoke-installs on Windows.

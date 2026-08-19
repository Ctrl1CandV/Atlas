# Release Checklist

Status: phase C and the reduced P0min/P1/P6 batch closed locally on 2026-08-19. The release candidate is the commit containing this checklist and the matching evidence in `docs/PLAN-rc1-followup.md` §11. All first-review blockers are fixed, the final local gates are green, and REVIEW-004 independent review passed. Remote GitHub Actions, tagging, artifact upload, provenance, and phase D have NOT run yet.

## Scope and version
- [x] `pyproject.toml`, `uv.lock`, and `atlas/__init__.py` report Python `0.1.0rc1`.
- [x] `web/package.json` and its lockfile report private frontend `0.1.0-rc.1`.
- [ ] Release tag is exactly `v0.1.0-rc.1`; CI converts PEP 440 `0.1.0rc1` before comparison. *(tag not created yet)*
- [ ] Release is marked prerelease, Windows-supported, source-only, and not published to PyPI. *(no GitHub release yet)*
- [x] No unverified repository-owner placeholder, clone URL, advisory URL, or changelog URL is present.

## Security and privacy
- [x] Production agents are documented as opt-in only through explicit `config/agents.json` `runner: local_cli`; missing configuration remains fail-closed.
- [x] Claude CLI is documented as a same-user process, and worktree copying is never described as an OS sandbox.
- [x] Documentation states that Atlas does not write the original project; compares frozen baseline/result ordinary-file byte manifests; generates a complete textual unified diff without `git add`, filters, hooks, attributes, textconv, or external diff; fails loudly on binary changes; binds approval to `baseline_digest`, `result_digest`, and `patch_digest`; and cannot prevent access to other current-user host paths.
- [x] Agent environment allowlisting, provider `anthropicBaseUrl`, `allow_web`/`WebSearch`/`WebFetch`, coding `Bash` network exposure, the `allowed_paths` read-only-agent-only contract, and `max_turns` specification-only semantics are current.
- [x] `config/.env`, active `config/*.json`, `runs/`, caches, logs, and nested repositories are absent from the final local source archive. *(173-entry sdist scan, zero findings)*
- [x] Secret/private-path/placeholder scan passes on release-facing text and examples without reading active config.
- [x] Web remains bound to `127.0.0.1`; security tests pass.

## Automated validation
- [x] `uv lock --check` and Python `compileall` pass.
- [x] Tests pass with real-API tests deselected, including documentation contracts, malformed-cost/YAML attacks, SSE cursor races, and real subprocess-kill recovery. *(425 passed, 1 skipped, 5 deselected on Python 3.14.6 and Python 3.12.9)*
- [x] Web tests, lint, and production build pass on Node.js 22.12+. *(full `npm test` 22 passed, oxlint 0/0, build OK)*
- [x] All six shipped workflows parse, validate, and dry-run without provider calls or run directories. *(strict offline gate: 6 registry preflights, 6 runner preflights, 0 provider calls, 0 agent calls, 0 run dirs, all `execution_sha256` non-null)*
- [ ] Windows required CI passes; Ubuntu compatibility signal is reviewed but is not a support claim. *(workflow definitions validated locally; remote run pending push)*
- [x] Clean checkout/source smoke and `uv build --sdist` pass; archive contents are inspected. *(clean-init gate: two real `atlas init` runs, exact template bytes, `fail_closed` preserved, MCP stdout silent; final sdist gate: 173 entries, zero findings, offline locked install into Python 3.12, imports, spec parse, six MCP tools, config init)*
- [ ] Manual real-API job uses the protected environment, one selected discovery test, disposable credentials, and a timeout. *(deferred with stage D)*

## Manual RC gates
- [x] Fresh Windows source follows both README build paths. *(2026-08-19 walkthrough on an isolated copy of the current tree without active config: `uv sync --locked --all-groups` → `npm --prefix web ci` (0 vulnerabilities) → `npm --prefix web run build`; upgrade path re-sync idempotent; `atlas-web` served `/api/workflows` (200, 6 workflows) and auto-created all six active configs with `agents.runner=fail_closed`)*
- [x] Generic config examples can be copied without editing the examples; agent execution stays disabled until the local copy explicitly selects `local_cli`. *(clean-init gate twice, `agents.runner=fail_closed` verified)*
- [x] Built-in guide covers install/upgrade, YAML fields, MCP, human gates, agent runner/security/network boundaries, privacy, troubleshooting, and developer build. *(docs contract tests green)*
- [x] A dry-run creates no run directory and makes no provider call. *(strict gate asserts `run_directories=0`, `provider_calls=0`)*
- [x] Missing/disabled agent runner, missing Claude CLI, missing `anthropicBaseUrl`, missing model, and missing credential each fail closed before run creation. *(preflight regression suite green)*
- [x] With disposable configuration, the coding workflow leaves the original directory unchanged, compares frozen baseline/result ordinary-file bytes, returns a complete textual unified diff for review, fails loudly on binary changes, and presents matching `baseline_digest`/`result_digest`/`patch_digest` approval evidence. *(attack regression suite green; GUI acceptance verified diff workspace, digest-verified approval material, and cleanup flows with synthetic offline data)*

## Publish
- [ ] Tag points to the validated commit and release is marked prerelease.
- [ ] Uploaded artifacts are source sdist, `SHA256SUMS`, and SPDX SBOM only; no wheel or PyPI publish step exists.
- [ ] SHA256 file matches the uploaded sdist.
- [ ] GitHub provenance attestation succeeds for every uploaded artifact; failure blocks release rather than being reported as success.
- [ ] Downloaded source archive smoke-installs on Windows. *(local equivalent done; post-publish verification pending)*

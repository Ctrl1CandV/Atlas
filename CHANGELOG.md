# Changelog

All notable changes to Atlas are documented here. The format follows Keep a Changelog and versions follow Semantic Versioning.

## Unreleased

### Added
- Dry-run now returns a `warnings` list (advisory only, admission stays fail-closed): reasoning-capable candidates whose effective `max_output_tokens` (node > provider `maxOutputTokens` > 8192) is below 16384 are flagged — hidden thinking burns the output budget (the 2026-08-22 real truncation); and when `max_cost_usd` is set while any billable candidate (including fallbacks) has unknown pricing, dry-run states that the guard will only admit the first billable node (run `20260822-113908-531dab`). Only probed capability data warns; unprobed models stay silent.
- Lenient JSON extraction (B1): when strict JSON parsing fails on a node with required fields, Atlas strips markdown code fences and extracts the outermost balanced JSON object; if all required fields survive, the same response is accepted with an `output_json_recovered` event instead of burning a fallback candidate, and artifacts still store the raw bytes. Conditional routers read artifacts through the same extraction so routing graphs keep completing. Missing fields after recovery, empty output, truncation sentinels, and output caps still fail exactly as before.
- `atlas-web` now serves the same six MCP tools over streamable-http at `/mcp`, so harnesses can connect by URL instead of a stdio subprocess; the stdio entry point is unchanged.
- `atlas_run_workflow` accepts a full custom graph as `yaml` text (ad-hoc run, nothing written) and can solidify it into `workflows/` after a real run via `persist_as`, reusing the same validation and anti-overwrite contract.
- Workflows can be deleted from the Web UI; `atlas_validate_workflow` echoes `file_sha256` so agents can do a read-modify-write loop through `atlas_save_workflow`.
- Paused human gates now show a review-materials panel in the Web UI: the artifacts the gate consumes and the full approval projection, each with its SHA-256, openable in the artifact workspace.
- Rejecting a human gate now requires a reason, enforced synchronously by both the Web UI and the API (`400` before any run lock is touched); approvals may still omit a comment.

### Changed
- Rewrote the READMEs around real runs: the shipped example YAML with its actual ledgered cost, honest failure cases from the run matrix, and measured capability boundaries.
- Renamed the shipped loop example's claim from "repair loop" to bounded retry: back edges re-run a node with the same static `consumes`, so reviewer feedback does not reach the revised node. The example YAML, in-app guide, and skill now say this explicitly; a feedback-carrying loop design is recorded in `docs/BACKLOG.md`.

### Fixed
- The MCP route merge no longer swallows `GET /` (the SPA briefly returned 404 when both were mounted).
- Manual model entry stays reachable in Settings when provider discovery fails.
- Windows reserved device names (`CON`, `PRN`, `COM1`…) are rejected as node ids at validation time, before any run directory is created.
- In-app guide navigation: the `mcp-human` and `development` chapters were unreachable via URL and pager (the router whitelist missed them).
- The offline CI scan no longer reads the deleted `README.zh-CN.md`; it scans `README.en.md` instead.
- Console entry points (`atlas`, `atlas-web`, `atlas-mcp`) force standard streams to UTF-8 at startup: on Windows consoles with a non-UTF-8 code page (e.g. cp1252 on Western locales), `atlas init` crashed with `UnicodeEncodeError` before printing its Chinese summary. Stdio MCP JSON-RPC also requires UTF-8, which the code page did not guarantee.
- Web test scripts pass `--experimental-strip-types` to Node so `.test.mjs` files importing `.ts` sources run on Node 22.12 (the documented minimum), not only on Node ≥ 23.6 where type stripping is on by default; this was the Ubuntu CI failure.
- CI hardening (not shipped code): the sdist smoke now pins dependency versions with constraints exported from `uv.lock` and installs online by default (`--offline` remains opt-in) — `uv sync` never populates the registry-metadata cache layer pip-style offline resolution needs, so the previous offline install only passed on machines with historically warm caches; agent CLI stubs in tests are cross-platform (`.cmd` forwarder on Windows, `#!/bin/sh` forwarder elsewhere) so the unsupported-platform signal reflects real compatibility, not stub mechanics.

### Documentation
- Added a v0.1.0 provenance record with an honest tag-versus-artifact-build disclosure.
- The public README and screenshots above were committed after `v0.1.0`; they are not part of that tag or its published sdist.
- Refreshed `docs/STATUS.md` to the 2026-08-22 source state: new MCP capabilities, the 10-node ad-hoc real run, and the current test baseline.
- Added `docs/PLAN-post-audit-2026-08-22.md`: consolidated post-audit fixes and phasing.
- R0 governance closed out (2026-08-23): default branch is `main`, the outdated `release/v0.1.0-rc.1` remote branch is deleted, public CI is green on both branches, READMEs now carry the CI badge, and the `release` environment requires a human approver. STATUS/PLAN/ROADMAP refreshed to these facts.
- Planned a run-final visualization card plus an opt-in summarizer node (S1, after echelon 5); the offline report export item was dropped by owner decision. The in-app guide and skill now recommend cross-vendor fallback for structured-output nodes (qwen3.8-max/kimi tested stable; glm occasionally fenced/invalid JSON), and `providers.example.json` documents the vendor-level `maxOutputTokens` field for reasoning models.

## 0.1.0 - 2026-08-19

Python package version: `0.1.0`. Release tag: `v0.1.0`.
This is the first official, stable-tagged alpha source release; the candidate content shipped without a separate RC tag.

### Added
- Windows 10/11 source release with YAML workflows, six MCP tools, local Web observation/control, human approval, and six model-neutral examples.
- Dynamic interrupted-run detection and checkpoint recovery through Web and `atlas_resume_run`; paused runs remain approval-only.
- With `max_cost_usd`, persistent per-attempt LLM cost reservations use the projected amount when known and reserve the remaining budget when pricing is unknown, with conservative crash-window accounting; uncapped runs do not invent reservations or amounts.
- YAML syntax and semantic validation errors with canonical field paths and one-based line/column locations when source marks are available.
- Bounded routing and loops, explicit fallback visibility, structured-output checks, append-only run events, effective-spec snapshots, and artifact hashes.
- Local workflow validate/preview, clean-source, Python/Web, security, and opt-in real-provider validation. The tagged public repository did not contain the full CI workflow/tests, so this is not a claim of public required CI success.
- Release SHA256 checksums, SPDX SBOM, and GitHub build-provenance attestations. Current asset provenance resolves to `d34d785…`, while the tag resolves to `4f9b0b5…`; see `docs/release-v0.1.0.md` in the post-release tree.
- Opt-in production `research` and `coding_agent` execution through Claude CLI when `config/agents.json` explicitly selects `runner: local_cli`; missing configuration remains fail-closed.
- First-start configuration initialization and `atlas init`, terminal-run deletion, agent-boundary visibility, and project-level MCP harness setup.

### Fixed
- Agent CLI preflight now accepts real `claude --help` output; a double-escaped regex class previously rejected flags followed by ordinary spaces/descriptions.
- Agent child processes use an isolated empty `CLAUDE_CONFIG_DIR`, preventing user settings from overriding the selected endpoint and credential. The temporary directory is removed after each call.
- Shipped in-app guides point to the project `.mcp.json` and README rather than local development-only `docs/mcp.md`.

### Security
- Web access remains loopback-only.
- Active credentials, runtime configuration, and run evidence are excluded from source artifacts and scanned release surfaces.
- Missing agent configuration and unmet agent preflight requirements fail closed before run creation.
- Coding-agent work-directory copying is staging, not an OS sandbox: Claude CLI is a same-user process and Atlas does not write the original directory. Atlas compares frozen baseline/result ordinary-file bytes to generate the textual unified diff without Git post-processing, fails loudly on binary changes, and binds approval to baseline/result/patch digests.
- Agent environments contain required system variables plus the selected provider endpoint and credential. `allow_web` controls WebSearch/WebFetch, not OS-level network access.

### Known limitations
- Source distribution only; no PyPI package or prebuilt installer.
- Windows 10/11 x64 is the supported runtime. No Linux/macOS support promise.
- Production agent execution requires explicit `runner: local_cli`, Claude Code 2.1.0+, an agent-model provider with `anthropicBaseUrl`, and a configured credential.
- Agent execution is not OS-sandboxed; coding Bash may access the network even when `allow_web` is false.
- Claude CLI does not enforce Atlas `max_turns`; deadlines and effective budgets are the hard limits.
- Model aliases, reasoning controls, usage, prices, and seed behavior depend on local configuration and provider behavior.
- Multi-user, cloud-hosted, and network-exposed deployments are unsupported.

Repository: https://github.com/Ctrl1CandV/Atlas

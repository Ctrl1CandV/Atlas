# Changelog

All notable changes to Atlas are documented here. The format follows Keep a Changelog and versions follow Semantic Versioning, with Python's equivalent PEP 440 spelling noted explicitly.

## Unreleased

### Changed
- Synchronized the production agent documentation and shipped coding workflow with the opt-in Claude CLI runner: `config/agents.json` must explicitly set `runner` to `local_cli`, while missing configuration remains fail-closed.
- Documented same-user worktree staging, original-directory write avoidance, frozen baseline/result ordinary-file byte-manifest comparison, complete textual unified diff generation without Git add/filter/hook/attributes/textconv/external-diff execution, binary fail-loud behavior, approval binding to `baseline_digest`/`result_digest`/`patch_digest`, minimal child environment, `anthropicBaseUrl`, `allow_web`, network limitations, and the current `max_turns` specification-only behavior.

## 0.1.0-rc.1 - 2026-08-18

Python package version: `0.1.0rc1`. Release tag: `v0.1.0-rc.1`.

### Added
- Source-only Windows RC with YAML workflows, five MCP tools, local Web observation, human approval, and six model-neutral examples.
- Bounded routing and loops, explicit fallback visibility, structured output checks, append-only run events, effective-spec snapshots, and artifact hashes.
- Windows CI, an advisory Ubuntu compatibility signal, workflow validate/preview checks, clean-source smoke checks, and opt-in real-provider discovery.
- Release SHA256 checksums, SPDX SBOM generation, and GitHub build-provenance attestations.
- Opt-in production `research` and `coding_agent` execution through the Claude CLI when `config/agents.json` explicitly selects `runner: local_cli`.

### Security
- Web access remains loopback-only.
- Active credentials, runtime configuration, and run evidence are excluded from source artifacts and scanned release surfaces.
- Missing agent configuration and unmet agent preflight requirements fail closed before run creation.
- Coding-agent work-directory copying is staging rather than an OS sandbox: Claude CLI is a same-user process and Atlas does not write the original directory. Atlas compares frozen baseline/result ordinary-file bytes to generate the textual unified diff without Git post-processing, fails loudly on binary changes, and binds approval to baseline/result/patch digests.
- Agent environments contain required system variables plus the current provider endpoint and credential. `allow_web` only controls `WebSearch`/`WebFetch`, not OS-level network access.

### Known limitations
- Source distribution only; no PyPI package or prebuilt installer is published.
- Windows 10/11 x64 is the supported RC runtime. Ubuntu CI is compatibility information, not a support promise.
- Production agent execution requires explicit `runner: local_cli`, Claude Code 2.1.0+, an agent model provider with `anthropicBaseUrl`, and a configured credential.
- Agent execution is not OS-sandboxed; coding `Bash` may access the network even when `allow_web` is false.
- The current Claude CLI does not enforce `max_turns`; deadlines and configured budgets are the hard limits.
- Model aliases, reasoning controls, usage, prices, and seed behavior depend on local configuration and provider behavior.
- Multi-user, cloud, and network-exposed deployments are unsupported.

Repository comparison links are intentionally omitted until a verified repository URL is available.

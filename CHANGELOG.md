# Changelog

All notable changes to Atlas are documented here. The format follows Keep a Changelog and versions follow Semantic Versioning.

## Unreleased

### Changed
- Made the primary README Chinese-first and added a full English companion, badges, product highlights, and real run/node screenshots.
- Hardened the existing release-assets workflow to build only from a stable version tag, reserve a new draft Release atomically, and publish assets without replacing an existing Release.
- Corrected the source-distribution include set so the English README, screenshots, and generic project-level `.mcp.json` ship together; removed the deleted `README.zh-CN.md` entry.

### Documentation
- Added a v0.1.0 provenance record with an honest tag-versus-artifact-build disclosure.
- The public README and screenshots above were committed after `v0.1.0`; they are not part of that tag or its published sdist.

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

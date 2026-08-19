# Changelog

All notable changes to Atlas are documented here. The format follows Keep a Changelog and versions follow Semantic Versioning, with Python's equivalent PEP 440 spelling noted explicitly.

## 0.1.0-rc.1 - Pending

Python package version: `0.1.0rc1`. Expected release tag: `v0.1.0-rc.1`.
The tag and GitHub prerelease have not been created yet.

### Added
- Source-only Windows RC with YAML workflows, six MCP tools, local Web observation, human approval, and six model-neutral examples.
- Dynamic interrupted-run detection and checkpoint recovery through Web and `atlas_resume_run`; paused runs remain approval-only.
- With `max_cost_usd`, persistent per-attempt LLM cost reservations use the projected amount when known and reserve the remaining budget when pricing is unknown, with conservative crash-window accounting; uncapped runs do not invent reservations or amounts.
- YAML syntax and semantic validation errors with canonical field paths and one-based line/column locations when source marks are available.
- Bounded routing and loops, explicit fallback visibility, structured output checks, append-only run events, effective-spec snapshots, and artifact hashes.
- Windows CI, an advisory Ubuntu compatibility signal, workflow validate/preview checks, clean-source smoke checks, and opt-in real-provider discovery.
- Release SHA256 checksums, SPDX SBOM generation, and GitHub build-provenance attestations.
- Opt-in production `research` and `coding_agent` execution through the Claude CLI when `config/agents.json` explicitly selects `runner: local_cli`; missing configuration remains fail-closed.
- First-start configuration initialization and `atlas init`, terminal-run deletion, agent boundary visibility, and MCP harness setup documentation.

### Fixed
- Agent CLI preflight now accepts real `claude --help` output: the required-flag check previously used a double-escaped regex class that misread flags followed by spaces and descriptions as missing, rejecting every real Claude CLI before run creation.
- Agent child processes now run Claude CLI with an isolated empty `CLAUDE_CONFIG_DIR`: user settings files (`~/.claude/settings.json` env block) take precedence over process environment and previously redirected agent calls to a personal gateway and credential, bypassing the selected provider. The temporary directory is removed after each call.
- Shipped in-app guides no longer reference `docs/mcp.md` (not distributed with the repository); they point to the committed `.mcp.json` and the README harness section instead.

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

# Atlas

[简体中文](README.zh-CN.md)

Atlas is a local, auditable workflow engine. YAML defines the graph; MCP tools validate, preview, and run it; the loopback Web UI shows recorded node inputs, outputs, failures, and artifacts.

> **Release candidate:** Python package version `0.1.0rc1`, product/release version `0.1.0-rc.1`, Git tag `v0.1.0-rc.1`. This is a Windows 10/11, source-only prerelease. It is not published to PyPI and has no prebuilt installer.

## Honest RC scope

- Supported now: `llm`, `research`, and `coding_agent` workflows, bounded routing/loops, six MCP tools, YAML validation with source locations, local run inspection, interrupted-only checkpoint recovery, and human approval gates.
- Production agent execution is opt-in: Atlas enables the Claude CLI runner only when `config/agents.json` explicitly sets `runner` to `local_cli`. Missing configuration and every unmet preflight requirement fail closed before a run is created.
- Claude CLI runs as a same-user host process. Atlas does not write the original directory. For a writable `coding_agent`, it freezes a baseline and compares the ordinary-file byte manifests of that baseline and the agent result to generate a complete textual unified diff.
- Diff collection does not run `git add`, filters, hooks, attributes, textconv, or external diff. Binary changes fail loudly, and approval evidence binds `baseline_digest`, `result_digest`, and `patch_digest`.
- The worktree copy is **not an OS sandbox**. The process can theoretically access any host path available to the current user. `allowed_paths`, tool selection, and loopback binding are not security boundaries.
- Six model-neutral workflow examples are included. Names such as “multi-vendor” describe the intended topology only; opinions are independent only after the user binds genuinely distinct providers/models.

## Requirements

- Windows 10 or 11 x64 (the supported RC runtime)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22.12 or newer and npm, for the private Web build
- Git, for source development and coding-agent source HEAD/clean-state preflight (diff generation itself does not execute Git)
- Claude Code 2.1.0 or newer, only when enabling production agent execution

## Install or upgrade from source

Use a trusted source archive or an existing checkout; no repository URL is asserted here.

```powershell
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

For an upgrade, replace/update the source checkout, review `CHANGELOG.md`, then repeat the locked sync and clean Web install above. Runtime configuration remains local; compare new `config/*.example.json` files before changing active files.

On first `atlas-web` or `atlas-mcp` startup, Atlas creates only missing local configuration files from the generic examples. Existing files are never overwritten. To initialize explicitly instead, run:

```powershell
uv run atlas init
```

Edit only the ignored runtime files. Put credentials only in `config/.env`; do not commit or share active provider, agent, capability, pricing, run, prompt, or output data. Unknown prices stay `null`.

Agent execution remains disabled until `config/agents.json` explicitly contains `"runner": "local_cli"`. Each selected agent model must belong to a configured provider with `anthropicBaseUrl`, an allowlisted model id, and its current provider credential in `config/.env`. See [`config/README.md`](config/README.md).

## Run locally

```powershell
uv run python -m atlas.web   # equivalent: uv run atlas-web
```

Open <http://127.0.0.1:8321>. Keep the service on loopback; this RC has no multi-user authentication or remote-deployment security model.

### MCP in your harness

The repository ships [`.mcp.json`](.mcp.json). Open the cloned Atlas repository in Claude Code (or another harness that reads project-level MCP config) and the six Atlas MCP tools start automatically over stdio — no manual terminal required. The config runs `uv --directory . run atlas-mcp` from the repository root, so it works on any machine after `uv sync --locked --all-groups`.

For each workflow: validate, dry-run, review model bindings/guards/cost, then explicitly request a real run. Validation and dry-run make no provider calls. Real runs may cost money. With `max_cost_usd`, unknown prices conservatively consume all remaining Atlas budget, but only locally verified prices can establish that the provider's actual charge stayed within the dollar cap. Human nodes pause for a decision in the local UI.

`coding_agent.workdir` must name an existing directory. Atlas does not expand `${ATLAS_HOME}` inside YAML. The shipped example uses relative `demo-project`, which resolves only when Atlas starts from the source root; otherwise supply an existing absolute path through the supported run override.

Agent child environments contain only required system variables plus the selected provider endpoint and credential. `allow_web` defaults to `false`; setting it to `true` adds only Claude CLI's `WebSearch` and `WebFetch` tools. It is not OS-level network isolation: a writable coding agent has `Bash`, which may still reach the network. `allowed_paths` is accepted only for `research` or `coding_agent` with `writable: false`; writable coding plus `allowed_paths` fails before run creation because Claude `--add-dir` is not a read-only boundary. `max_turns` remains a validated workflow/specification field, but the current Claude CLI has no hard turn-count parameter; hard limits come from deadlines and configured budgets.

## Security and privacy

- Treat tasks, Web content, repositories, and model output as untrusted data.
- `runs/` may contain complete prompts, source, outputs, diffs, hashes, and approval records. Git ignore is not encryption or retention management.
- Never expose `config/.env`, active configuration, or run artifacts in issues or CI artifacts.
- Do not treat directory copying, path allowlists, tool lists, `allow_web`, or loopback binding as an OS security boundary.

Read [`SECURITY.md`](SECURITY.md) before using real credentials. Workflow fields and MCP usage are documented in [`skill/SKILL.md`](skill/SKILL.md) and the built-in guide.

## Developer validation

```powershell
uv lock --check
uv run python -m compileall -q atlas
uv run pytest
npm --prefix web run lint
npm --prefix web run test:diff
npm --prefix web run build
uv build --sdist
```

Real-provider tests are opt-in, billable, and excluded by default. The manual CI workflow uses a protected environment, one narrowly selected discovery test, and a job timeout; it must use disposable test credentials.

See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CHANGELOG.md`](CHANGELOG.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

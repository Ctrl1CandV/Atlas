# Atlas

[中文](README.md) · **English**

![version](https://img.shields.io/github/v/tag/Ctrl1CandV/Atlas) ![license](https://img.shields.io/badge/license-Apache--2.0-green) ![platform](https://img.shields.io/badge/platform-Windows%2010%2F11-lightgrey) ![python](https://img.shields.io/badge/python-3.12-blue)

Atlas is a **local, auditable multi-model workflow engine**. YAML defines the graph; models from different vendors collaborate — parallel research, cross-examination debates, code implementation, human approval — while the loopback Web UI records every node's full input and output.

> **Release scope:** the current stable version is `v0.1.0`, supports Windows 10/11 x64 only, and ships as a source sdist. It is not published to PyPI and has no prebuilt installer. A Git clone and the release sdist do not contain exactly the same files; see the install notes below.

![Atlas run view](assets/observe-run.png)

Open any node to inspect its full input and output, the model actually used, tokens, and duration:

![Atlas node detail](assets/observe-node.png)

## Why Atlas

- **Multi-model collaboration**: cross-vendor fallback chains, debate adjudication, parallel sharding, bounded repair loops. Six MCP tools form the control plane, so your AI assistant (Claude Code and others) can write, validate, preview, and run graphs for you.
- **Preview before you pay**: `validate` and `dry_run` are zero-cost; fake-success detection rejects empty output, truncation, and missing required fields — a model that just answers "OK" does not pass.
- **Auditable end to end**: append-only event ledger, write-once artifacts with hash assertions, and approval evidence bound to baseline/result/patch digests.
- **Crash-recoverable**: a killed controller leaves the run dynamically marked interrupted; checkpoint resume completes only unfinished nodes and never re-opens the budget.
- **Human in the loop**: `human` nodes pause the graph in the Web UI until you review the real artifacts and approve or reject.
- **Local first**: the Web UI binds to loopback only, credentials stay in local `config/.env`, and Atlas has no hosted control plane or built-in telemetry. Real runs may still call the remote model providers you configure.

## Honest scope

- Supported now: `llm`, `research`, and `coding_agent` workflows, bounded routing/loops, six MCP tools, YAML validation with source locations, local run inspection, interrupted-only checkpoint recovery, and human approval gates.
- Production agent execution is opt-in: Atlas enables the Claude CLI runner only when `config/agents.json` explicitly sets `runner` to `local_cli`. Missing configuration and every unmet preflight requirement fail closed before a run is created.
- Claude CLI runs as a same-user host process. Atlas does not write the original directory. For a writable `coding_agent`, it freezes a baseline and compares the ordinary-file byte manifests of that baseline and the agent result to generate a complete textual unified diff.
- Diff collection does not run `git add`, filters, hooks, attributes, textconv, or external diff. Binary changes fail loudly, and approval evidence binds `baseline_digest`, `result_digest`, and `patch_digest`.
- The worktree copy is **not an OS sandbox**. The process can theoretically access any host path available to the current user. `allowed_paths`, tool selection, and loopback binding are not security boundaries.
- Six model-neutral examples are included (parallel research synthesis, multi-vendor debate, map-reduce analysis, repair loop, approval pipeline, code-change review). Names such as “multi-vendor” describe the intended topology only; opinions are independent only after you bind genuinely distinct providers/models.

## Requirements

- Windows 10 or 11 x64 (the supported runtime)
- Python 3.12 and [uv](https://docs.astral.sh/uv/)
- Node.js 22.12 or newer and npm, for the private Web build
- Git, for source development and coding-agent source HEAD/clean-state preflight (diff generation itself does not execute Git)
- Claude Code 2.1.0 or newer, only when enabling production agent execution

## Install or upgrade from source

```powershell
git clone https://github.com/Ctrl1CandV/Atlas.git
cd Atlas
uv sync --locked --all-groups
npm --prefix web ci
npm --prefix web run build
```

You can also download the Atlas source sdist from the [v0.1.0 release](https://github.com/Ctrl1CandV/Atlas/releases/tag/v0.1.0), alongside `SHA256SUMS` and build provenance. GitHub's generated Source code archive, a Git clone, and this custom sdist are three different inputs. The published `v0.1.0` sdist does not contain the project-level `.mcp.json`, the English README, or the screenshots; prefer a Git clone for automatic MCP setup, or configure `atlas-mcp` manually. The current branch fixes the next release's sdist manifest without silently replacing existing `v0.1.0` assets.

For an upgrade, update the source, review `CHANGELOG.md`, then repeat the locked sync and clean Web install. Runtime configuration remains local; compare new `config/*.example.json` files before changing active files.

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

Open <http://127.0.0.1:8321>. Keep the service on loopback; there is no multi-user authentication or remote-deployment security model.

### MCP in your harness

A Git clone ships [`.mcp.json`](.mcp.json). Open the cloned Atlas repository in Claude Code (or another harness that reads project-level MCP config) and the six Atlas MCP tools start automatically over stdio — no manual terminal required. The config runs `uv --directory . run atlas-mcp` from the repository root after `uv sync --locked --all-groups`. The published `v0.1.0` sdist does not include this file; the next-release sdist manifest now does.

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
npm --prefix web test
npm --prefix web run lint
npm --prefix web run build
uv build --sdist
```

Real-provider tests are opt-in, billable, and excluded by default. See [`CONTRIBUTING.md`](CONTRIBUTING.md) and [`CHANGELOG.md`](CHANGELOG.md).

## License

Apache License 2.0. See [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

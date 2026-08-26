# Changelog

All notable changes to Atlas are documented here. The format follows Keep a Changelog and versions follow Semantic Versioning.

## Unreleased

### Added
- Controller heartbeat (P9). During every in-flight model or agent-CLI dispatch the controller writes `node_progress` events (node, iteration, attempt, candidate model, controller elapsed_ms, phase `waiting`/`retry`) at a run-level interval: default and floor 30 seconds, configurable through `ATLAS_NODE_HEARTBEAT_INTERVAL_S`; values below the floor are rejected loudly instead of clamped. The heartbeat says only that the Atlas controller is still waiting for the call to return — it never claims model-internal progress or percentages (the wording boundary is test-locked: the heartbeat field set is the entire vocabulary). Dispatch windows close at attempt end, transport-failure give-up, unexpected exceptions, cancellation, and terminal state; the close joins the heartbeat thread, so late ticks are rejected and no heartbeat can appear after `run_done`/`run_failed`/`run_cancelled` — verified with a frozen-provider run and a cancel-mid-run run, plus a unit test on the late-tick contract. `fold_events` explicitly ignores the type and a regression test asserts that stripping every heartbeat changes the folded state by nothing. Capacity is counted honestly: at the 30-second cadence one node emits ≈2880 events per day against the 16 MiB ledger cap, so long-running graphs carry a real ledger cost until P10's segmented-ledger governance lands. The same-model transport-retry wait and the agent retry backoff flip the phase to `retry`; agent heartbeats carry the runner name, and SSE/Web show the events as they flow through the ledger.
- Cooperative cancellation (P2). `atlas_cancel_run` (MCP, eighth tool) and `POST /api/runs/{id}/cancel` (Web API; the UI button ships in a later batch) write an atomic create-if-absent request file without waiting on the controller lock. A running controller consumes the request at llm/human/agent node entry, candidate switch, and transport-retry waits (interruptible sleep), then writes the single `run_cancelled` terminal event; in-flight provider calls and agent CLI executions run to completion — Atlas does not claim hard kill. Requests are not revocable: if the controller dies after a request, resume consumes it at the first node boundary. Paused/interrupted runs are cancelled directly under the run lock by the cancel entry (sole writer). `cancelled` is a replayable terminal status: resume and approval reject it, deletion accepts it, repeat cancels conflict, and pending cost reservations stay conservatively accounted. Tool surface is now eight tools. An RFC draft (`docs/rfcs/agent-retry-budget.md`) records the agent-retry budget decision path; no default behavior changed. Also removed the legacy seed-byte write on OS lock files (init and run locks): a concurrent process's write could hit the byte another process had just locked, the recurring `PermissionError [Errno 13]` CI flake; region locks work on empty files and the seed was never read.
- MCP grows to seven tools (P4). `atlas_list_runs(limit, cursor)` lists runs newest-first with stable cursor pagination and the same closed status set, sharing the run-summary builder with the Web list. `atlas_run_workflow(wait=false)` returns `run_id`/`status: starting` right after full preflight, execution-identity assertion and run-lock admission, then a registered background controller executes the graph — long tasks no longer block the MCP session (`wait=false` is mutually exclusive with `persist_as`; default synchronous `wait=true` is unchanged). Web start/resume/approval continuations now run through the same in-process launcher registry (one controller per run; the ledger remains the single source of truth for status).
- Dry-run now returns a `warnings` list (advisory only, admission stays fail-closed): reasoning-capable candidates whose effective `max_output_tokens` (node > provider `maxOutputTokens` > 8192) is below 16384 are flagged — hidden thinking burns the output budget (the 2026-08-22 real truncation); and when `max_cost_usd` is set while any billable candidate (including fallbacks) has unknown pricing, dry-run states that the guard will only admit the first billable node (run `20260822-113908-531dab`). Only probed capability data warns; unprobed models stay silent.
- Lenient JSON extraction (B1): when strict JSON parsing fails on a node with required fields, Atlas strips markdown code fences and extracts the outermost balanced JSON object; if all required fields survive, the same response is accepted with an `output_json_recovered` event instead of burning a fallback candidate, and artifacts still store the raw bytes. Conditional routers read artifacts through the same extraction so routing graphs keep completing. Missing fields after recovery, empty output, truncation sentinels, and output caps still fail exactly as before.
- `atlas-web` serves the MCP tool surface over streamable-http at `/mcp`, so harnesses can connect by URL instead of a stdio subprocess; the stdio entry point is unchanged.
- `atlas_run_workflow` accepts a full custom graph as `yaml` text (ad-hoc run, nothing written) and can solidify it into `workflows/` after a real run via `persist_as`, reusing the same validation and anti-overwrite contract.
- Workflows can be deleted from the Web UI; `atlas_validate_workflow` echoes `file_sha256` so agents can do a read-modify-write loop through `atlas_save_workflow`.
- Paused human gates now show a review-materials panel in the Web UI: the artifacts the gate consumes and the full approval projection, each with its SHA-256, openable in the artifact workspace.
- Rejecting a human gate now requires a reason, enforced synchronously by both the Web UI and the API (`400` before any run lock is touched); approvals may still omit a comment.

### Changed
- The repo-root `.mcp.json` now points to the HTTP endpoint (`http://127.0.0.1:8321/mcp`) instead of launching a stdio child via `uv --directory .`: a shipped relative directory breaks in any harness that spawns the subprocess outside the repo (field-proven with ZCode on 2026-08-23), and a stdio child never shares code with the running `atlas-web`. stdio remains an explicitly documented alternative with an absolute path (`docs/mcp.md` Option 2); README, in-app guides, and the docs contract tests were updated to match.
- Rewrote the READMEs around real runs: the shipped example YAML with its actual ledgered cost, honest failure cases from the run matrix, and measured capability boundaries.
- Renamed the shipped loop example's claim from "repair loop" to bounded retry: back edges re-run a node with the same static `consumes`, so reviewer feedback does not reach the revised node. The example YAML, in-app guide, and skill now say this explicitly; a feedback-carrying loop design is recorded in `docs/BACKLOG.md`.

### Fixed
- `atlas-web` now fails loud before printing its banner when port 8321 is already occupied (2026-08-23 field report: a stale instance kept serving old code on the port while the new process printed "started" and exited, so harnesses silently talked to stale code and debugging was misled); the error names the likely cause and the exact netstat/taskkill steps. `docs/mcp.md` gained a bilingual troubleshooting section (port conflicts, stale sessions, connectivity probe).
- The MCP route merge no longer swallows `GET /` (the SPA briefly returned 404 when both were mounted).
- Manual model entry stays reachable in Settings when provider discovery fails.
- Windows reserved device names (`CON`, `PRN`, `COM1`…) are rejected as node ids at validation time, before any run directory is created.
- In-app guide navigation: the `mcp-human` and `development` chapters were unreachable via URL and pager (the router whitelist missed them).
- The offline CI scan no longer reads the deleted `README.zh-CN.md`; it scans `README.en.md` instead.
- Console entry points (`atlas`, `atlas-web`, `atlas-mcp`) force standard streams to UTF-8 at startup: on Windows consoles with a non-UTF-8 code page (e.g. cp1252 on Western locales), `atlas init` crashed with `UnicodeEncodeError` before printing its Chinese summary. Stdio MCP JSON-RPC also requires UTF-8, which the code page did not guarantee.
- Web test scripts pass `--experimental-strip-types` to Node so `.test.mjs` files importing `.ts` sources run on Node 22.12 (the documented minimum), not only on Node ≥ 23.6 where type stripping is on by default; this was the Ubuntu CI failure.
- CI hardening (not shipped code): the sdist smoke now pins dependency versions with constraints exported from `uv.lock` and installs online by default (`--offline` remains opt-in) — `uv sync` never populates the registry-metadata cache layer pip-style offline resolution needs, so the previous offline install only passed on machines with historically warm caches; agent CLI stubs in tests are cross-platform (`.cmd` forwarder on Windows, `#!/bin/sh` forwarder elsewhere) so the unsupported-platform signal reflects real compatibility, not stub mechanics.

### Documentation
- Fact surfaces resynced to the P4/P2 delivery: STATUS gains shared-launcher and cooperative-cancel capability rows and drops the now-false "no cancel / blocking MCP" limitations; BACKLOG moves P4/P2 to a completed section with the explicitly remaining scope (CLI process-tree kill, Web cancel button, budget mapping); the skill's required sequence covers `wait=false`, `atlas_list_runs`, and cancel semantics.
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

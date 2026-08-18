# Security Policy

## Supported version

Atlas is a source-only release candidate for Windows 10/11 x64. Security fixes are provided only for the latest RC.

| Version | Product/tag | Supported |
|---|---|---|
| Python `0.1.0rc1` | `0.1.0-rc.1` / `v0.1.0-rc.1` | Yes |
| Earlier snapshots | — | No |

## Reporting a vulnerability

Do not open a public issue containing vulnerability details, credentials, prompts, run artifacts, private paths, or project data. If this source is hosted on GitHub, use that repository's private Security advisory form. Otherwise contact the maintainers privately through the channel from which you obtained the source and request a secure reporting route. No public repository or maintainer address is guessed here.

Include the affected version, Windows build, impact, and reproduction using synthetic data. Remove secrets and private paths. Maintainers aim to acknowledge a report within seven days; disclosure timing follows triage.

## RC security boundaries

- The Web service is single-user and loopback-only (`127.0.0.1`). It has no supported network-exposed or multi-user authentication model.
- Production `research` and `coding_agent` execution is opt-in. Atlas uses the Claude CLI runner only when `config/agents.json` explicitly sets `runner` to `local_cli`; missing configuration or failed preflight remains fail-closed before run creation.
- Claude CLI is a same-user host process, not an operating-system sandbox. It can theoretically access every host path available to that user. Never use Atlas as malware isolation or run untrusted repositories under a privileged account.
- Atlas does not write the original project directory. For a writable coding agent, it freezes a baseline and compares the ordinary-file byte manifests of that baseline and the agent result to generate a complete textual unified diff. It does not run `git add`, filters, hooks, attributes, textconv, or external diff during collection; binary changes fail loudly. Approval evidence binds `baseline_digest`, `result_digest`, and `patch_digest`. This staging and integrity boundary does not prevent a same-user process from accessing or attacking other host paths.
- Agent child environments are allowlisted to required system variables plus the selected provider endpoint and credential. Agent models require a provider with `anthropicBaseUrl`; do not treat environment filtering as process isolation.
- `allow_web` defaults to `false` and, when enabled, adds only `WebSearch` and `WebFetch`. It is not an OS network firewall: writable coding agents also have `Bash`, which may initiate network access independently.
- `max_turns` is retained as validated specification metadata; the current Claude CLI has no hard turn-count argument. Enforce hard bounds with deadlines and configured budgets.
- Tasks, external content, repositories, and model output are untrusted input and may contain prompt injection.
- `config/.env`, active provider/agent/capability/pricing configuration, and `runs/` may contain credentials, prompts, source, output, diffs, and approvals. Git ignore does not encrypt, redact, or delete them.
- Validation and dry-run make no provider calls. Real runs can incur charges. `max_cost_usd` is not reliable for models whose prices are unknown or stale.
- The release workflow publishes source only plus checksums, an SPDX SBOM, and GitHub provenance attestations. It does not publish to PyPI.

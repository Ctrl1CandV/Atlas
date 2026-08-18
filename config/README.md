# Atlas configuration

Runtime configuration is local and private. Source releases contain generic examples only; active files are ignored and must never be included in reports or release artifacts.

| Runtime file (ignored) | Start from | Purpose |
|---|---|---|
| `providers.json` | `providers.example.json` | Provider endpoints, credential variable names, and model allowlists |
| `agents.json` | `agents.example.json` | Explicit production agent runner selection and Claude CLI command |
| `.env` | `.env.example` | Secret credential values |
| `models.reference.json` | `models.reference.example.json` | Optional local model-selection notes |
| `capabilities.json` | `capabilities.example.json` | Locally verified thinking-control behavior |
| `pricing.json` | `pricing.example.json` | Verified per-token prices for cost accounting |

On first `atlas-web` or `atlas-mcp` startup, Atlas copies each missing runtime file from its example with an atomic create-if-absent operation. Existing files are never overwritten. To run the same initialization explicitly from the source root:

```powershell
uv run atlas init
```

Edit only the ignored runtime files. `apiKeyEnv` is an environment-variable name matching `^[A-Z][A-Z0-9_]*$`, never a credential. Model references use `Provider:model-id`, and each provider's `models` list is an explicit allowlist. Keep unknown prices `null`; do not estimate them. The generated `agents.json` remains `fail_closed` until you deliberately opt in to `local_cli`.

The Settings page may maintain local providers and credentials after initialization. Do not attach active files, `config/.env`, private endpoints, or run data to issues. Share only generic example schemas and sanitized errors.

## Production agent runner

Agent execution is disabled by default. Missing `config/agents.json` and the shipped `agents.example.json` both resolve to `runner: fail_closed`. To opt in, explicitly set the ignored local file to:

```json
{
  "runner": "local_cli",
  "cli": {
    "kind": "claude",
    "command": "claude",
    "extra_args": []
  }
}
```

The selected agent model must reference a provider whose `models` allowlist contains that model and whose `anthropicBaseUrl` is configured. Its `apiKeyEnv` value must exist in `config/.env`. Missing runner opt-in, Claude CLI, compatible endpoint, model, or credential fails closed before a run is created.

Claude CLI runs as the current user. Atlas does not write the original directory. For a writable coding agent, it freezes a baseline and compares the ordinary-file byte manifests of that baseline and the agent result to generate a complete textual unified diff. Collection does not run `git add`, filters, hooks, attributes, textconv, or external diff; binary changes fail loudly. Approval evidence binds `baseline_digest`, `result_digest`, and `patch_digest`. The worktree copy is not an OS sandbox, and the process can theoretically access any host path available to the current user.

The child environment contains only required system variables plus the selected provider endpoint and credential. `allow_web` defaults to `false`; enabling it adds only `WebSearch` and `WebFetch`. It does not create OS-level network isolation, and coding-agent `Bash` may still access the network. `max_turns` is specification metadata because the current Claude CLI exposes no hard turn-count parameter; deadlines and configured budgets provide hard limits.

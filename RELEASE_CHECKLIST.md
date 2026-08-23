# v0.1.0 Release Record

> Final factual record for the 2026-08-19 stable source release. This file was local/untracked at the time of the release and therefore is not evidence contained in the tag or published sdist. Public metadata and local validation are identified separately below.

## Scope and version

- [x] `pyproject.toml`, `uv.lock`, `atlas/__init__.py`, `web/package.json`, and the frontend lock report `0.1.0`.
- [x] Annotated tag `v0.1.0` exists and peels to `4f9b0b5fb4b14fe0523e1cc47cc5e11597d55a94`.
- [x] GitHub Release `v0.1.0` is stable (`draft=false`, `prerelease=false`) and source-only.
- [x] No wheel, PyPI publication, or prebuilt installer was produced.
- [x] Supported runtime is Windows 10/11 x64; this is an alpha-quality first release, not an RC package version.

## Security and privacy

- [x] Production agents require explicit `config/agents.json` `runner: local_cli`; missing requirements fail closed before run creation.
- [x] Documentation states that Claude CLI is a same-user process and the worktree copy is not an OS sandbox.
- [x] Atlas leaves the original coding workdir unchanged, compares frozen baseline/result ordinary-file byte manifests, generates a complete textual unified diff without Git post-processing, fails loudly on binary changes, and binds approval to baseline/result/patch digests.
- [x] Agent environment allowlisting, `anthropicBaseUrl`, `allow_web`, coding Bash network exposure, `allowed_paths`, and `max_turns` limitations are documented.
- [x] Release scan excluded active config, credentials, runs, caches, logs, nested repositories, and generated output.
- [x] Web remains loopback-only.

## Local validation recorded before/after stage D

- [x] `uv lock --check` and compileall passed.
- [x] Final recorded backend baseline after the two stage-D CLI fixes: 427 passed, 1 skipped, 5 `real_api` deselected.
- [x] Web: 22 tests passed, lint clean, production build succeeded.
- [x] Six shipped workflows validated and dry-ran with 0 provider calls, 0 agent calls, and 0 run directories.
- [x] Clean-init verified two idempotent real `atlas init` calls, exact templates, `agents.runner=fail_closed`, and silent MCP stdout.
- [x] Final release sdist gate recorded 100 entries, zero findings, Python 3.12 offline install, version 0.1.0, three console scripts, six MCP tools, spec parse, and config init.
- [x] Stage D used the real MCP stdio server and pinned every billable run to the preceding dry-run `execution_sha256`.
- [x] Stage D covered the three-provider example matrix, real fallback rescue, a custom parallel+loop+human+agent graph, budget/timeout/bad-key/resume rejection paths, workdir isolation, diff artifacts, digest-bound approval, and cost settlement.
- [x] Stage D found and fixed real `claude --help` flag parsing and user-settings endpoint/credential override via per-call empty `CLAUDE_CONFIG_DIR`.
- [x] The Kiro agent overrun is recorded honestly: first attempt self-reported about $10.508 and automatic retry was force-terminated. This proves that no pricing + no graph cap is not a safe operating mode.

## Public automation status

- [ ] Required public Windows CI passed for the tagged commit. **Not completed:** the public tree did not contain the full CI workflow/tests used locally.
- [ ] A protected GitHub environment real-provider job passed. **Not completed:** local stage D is not that job.
- [x] Release-assets workflow run `32254337034` succeeded and uploaded exactly three current assets.

## Published assets and provenance

- [x] `atlas-0.1.0.tar.gz`: `a4a7f5fc55c80b0b0baccb8ab173fccfbf4d70b8690df88458b8169e221827ef`.
- [x] `atlas-v0.1.0.spdx.json`: `565d45fd62642273e27c64f9b4723c025e8ed7262034584ed3d204e94641a8f7`.
- [x] `SHA256SUMS`: `0d9c88a4733038430e10c10d5e1f71e369fa5e9045b9ee384ba2b6df75da0bf7`; its two entries match the sdist and SBOM downloads.
- [x] Attestation `41605837` matches all three current assets and records SLSA provenance v1, Sigstore, and Rekor.
- [x] A downloaded-sdist Windows/Python 3.12 offline smoke was recorded locally.
- [ ] Tag commit equals artifact-build commit. **Known mismatch:** tag peels to `4f9b0b5…`; current assets are attested from `d34d785…`. Only the release workflow changed between them and it is excluded from the sdist, but the identities are not equal.

See `docs/release-v0.1.0.md` for exact public metadata and the rule for future tag-bound releases.

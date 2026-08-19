# -*- coding: utf-8 -*-
"""Clean-init release gate using the real CLI and an isolated config directory."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from atlas.config_init import CONFIG_TEMPLATES


def _subprocess_env() -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "HOME",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str],
         input_bytes: bytes | None = None, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def run_clean_init_gate(project_root: Path, *, python_executable: str | None = None) -> dict:
    """Initialize absent active config twice and probe MCP stdout in isolation."""
    root = Path(project_root).resolve()
    source_config = root / "config"
    python = python_executable or sys.executable
    env = _subprocess_env()

    with tempfile.TemporaryDirectory(prefix="atlas-clean-init-") as temp_name:
        config_dir = Path(temp_name) / "config"
        config_dir.mkdir()
        for template_name, active_name in CONFIG_TEMPLATES:
            source = source_config / template_name
            if not source.is_file():
                raise AssertionError(f"missing config template: {source}")
            shutil.copyfile(source, config_dir / template_name)
            if (config_dir / active_name).exists():
                raise AssertionError(f"active config unexpectedly exists: {active_name}")

        command = [python, "-m", "atlas.cli", "init", "--config-dir", str(config_dir)]
        first = _run(command, cwd=root, env=env)
        if first.returncode != 0:
            raise AssertionError(
                f"first atlas init failed ({first.returncode}): {first.stderr.decode('utf-8', 'replace')}")

        active_mtimes: dict[str, int] = {}
        for template_name, active_name in CONFIG_TEMPLATES:
            template = config_dir / template_name
            active = config_dir / active_name
            if not active.is_file() or active.read_bytes() != template.read_bytes():
                raise AssertionError(f"atlas init did not copy exact template bytes: {active_name}")
            active_mtimes[active_name] = active.stat().st_mtime_ns

        agents = json.loads((config_dir / "agents.json").read_text(encoding="utf-8"))
        if agents.get("runner") != "fail_closed":
            raise AssertionError("clean init must preserve the fail_closed agent runner")

        second = _run(command, cwd=root, env=env)
        if second.returncode != 0:
            raise AssertionError(
                f"second atlas init failed ({second.returncode}): {second.stderr.decode('utf-8', 'replace')}")
        for template_name, active_name in CONFIG_TEMPLATES:
            active = config_dir / active_name
            if active.read_bytes() != (config_dir / template_name).read_bytes():
                raise AssertionError(f"second atlas init changed {active_name}")
            if active.stat().st_mtime_ns != active_mtimes[active_name]:
                raise AssertionError(f"second atlas init rewrote {active_name}")

        probe = r'''
import sys
from pathlib import Path
import atlas.config_init as config_init

original = config_init.initialize_runtime_config
config_dir = Path(sys.argv[1])
config_init.initialize_runtime_config = lambda: original(config_dir)

def deny_non_loopback(event, args):
    if event != "socket.connect":
        return
    address = args[1]
    host = address[0] if isinstance(address, tuple) and address else None
    if host not in {"127.0.0.1", "::1"}:
        raise AssertionError(f"MCP stdout probe attempted network access: {address!r}")

# Windows asyncio creates a loopback socket pair for its event loop. Permit only
# that local control channel; every non-loopback connection remains forbidden.
sys.addaudithook(deny_non_loopback)

from atlas.mcp import main
main()
'''
        mcp = _run(
            [python, "-c", probe, str(config_dir)],
            cwd=root,
            env=env,
            input_bytes=b"",
            timeout=30,
        )
        if mcp.returncode != 0:
            raise AssertionError(
                f"MCP stdout probe failed ({mcp.returncode}): {mcp.stderr.decode('utf-8', 'replace')}")
        if mcp.stdout != b"":
            raise AssertionError(
                f"MCP initialization wrote {len(mcp.stdout)} bytes to stdout")

    return {
        "templates_checked": [active for _, active in CONFIG_TEMPLATES],
        "first_init_returncode": first.returncode,
        "second_init_returncode": second.returncode,
        "agents_runner": "fail_closed",
        "mcp_stdout_bytes": 0,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--project-root", type=Path,
        default=Path(__file__).resolve().parent.parent)
    args = parser.parse_args()
    print(json.dumps(run_clean_init_gate(args.project_root), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

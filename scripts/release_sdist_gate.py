# -*- coding: utf-8 -*-
"""Scan an sdist and smoke-install it offline into a clean Python 3.12 venv."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import textwrap
import tomllib
from pathlib import Path, PurePosixPath

_ACTIVE_CONFIG = {
    "config/.env", "config/providers.json", "config/models.reference.json",
    "config/capabilities.json", "config/pricing.json", "config/agents.json",
}
_ALLOWED_ENV_TEMPLATES = {"config/.env.example"}
_BANNED_COMPONENTS = {
    ".git", ".zcode", ".cursor", "__pycache__", ".pytest_cache", ".ruff_cache",
    ".mypy_cache", "node_modules", ".venv", "venv", "coverage", "htmlcov",
}
_BANNED_SUFFIXES = (
    ".pyc", ".pyo", ".whl", ".key", ".pem", ".sqlite", ".sqlite-wal",
    ".sqlite-shm", ".db", ".db-wal", ".db-shm", ".log",
)
_REQUIRED = {
    "atlas/__init__.py",
    "pyproject.toml",
    "config/providers.example.json",
    "config/models.reference.example.json",
    "config/capabilities.example.json",
    "config/pricing.example.json",
    "config/agents.example.json",
    "config/.env.example",
    "scripts/release_workflow_gate.py",
    "scripts/release_clean_init_gate.py",
    "scripts/release_sdist_gate.py",
    ".github/workflows/ci.yml",
    ".github/workflows/release.yml",
}
_EXPECTED_WORKFLOWS = {
    "workflows/code-change-review-approve.yaml",
    "workflows/human-approval-pipeline.yaml",
    "workflows/map-reduce-document-analysis.yaml",
    "workflows/multi-vendor-debate-judge.yaml",
    "workflows/parallel-research-synthesis.yaml",
    "workflows/proposal-review-repair-loop.yaml",
}
_CONTENT_CHECKS = {
    "private key": re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "AWS access key": re.compile(rb"\bAKIA[0-9A-Z]{16}\b"),
    "GitHub token": re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b"),
    # Long provider-specific prefixes keep examples such as sk-test from matching.
    "OpenAI API key": re.compile(
        rb"\bsk-" + rb"(?:proj-|svcacct-)?[A-Za-z0-9_-]{40,}\b"),
    "Anthropic API key": re.compile(
        rb"\bsk-ant-" + rb"(?:api[0-9]{2}-)?[A-Za-z0-9_-]{40,}\b"),
    "Google API key": re.compile(rb"\bAI" + rb"za[0-9A-Za-z_-]{35}\b"),
    "Slack token": re.compile(rb"\bxox" + rb"[baprs]-[0-9A-Za-z-]{30,}\b"),
    "private Windows user path": re.compile(
        rb"(?i)(?<![A-Za-z])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s]+[\\/]"),
    "private Unix home": re.compile(rb"/(?:Users|home)/[^/\s]+/"),
    # Split literals so the scanner does not match its own pattern source.
    "repository placeholder": re.compile(
        rb"(?:OWNER|ORG)/" + rb"(?:REPOSITORY|REPO)"
        + rb"|<atlas-" + rb"repository-url>"
        + rb"|https://github" + rb"\.com/?(?:\s|$)"),
}


def _relative_members(archive: Path) -> tuple[list[tarfile.TarInfo], dict[str, tarfile.TarInfo]]:
    with tarfile.open(archive, "r:gz") as bundle:
        members = bundle.getmembers()
    if not members:
        raise AssertionError("sdist is empty")
    roots: set[str] = set()
    relative: dict[str, tarfile.TarInfo] = {}
    for member in members:
        if "\\" in member.name:
            raise AssertionError(f"sdist entry uses backslashes: {member.name}")
        path = PurePosixPath(member.name)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise AssertionError(f"unsafe sdist entry: {member.name}")
        roots.add(path.parts[0])
        rel = PurePosixPath(*path.parts[1:]).as_posix()
        if rel and rel in relative:
            raise AssertionError(f"duplicate sdist entry: {rel}")
        if rel:
            relative[rel] = member
    if len(roots) != 1:
        raise AssertionError(f"sdist must have one root directory, found {sorted(roots)}")
    return members, relative


def _banned_reason(name: str) -> str | None:
    path = PurePosixPath(name)
    parts = path.parts
    filename = path.name
    if name in _ACTIVE_CONFIG:
        return "active configuration"
    if filename == ".mcp.json":
        return "private machine configuration"
    if filename == ".env" or (filename.startswith(".env.") and name not in _ALLOWED_ENV_TEMPLATES):
        return "private environment file"
    if any(part in _BANNED_COMPONENTS for part in parts):
        return "generated/private directory"
    if parts and parts[0] in {"runs", "dist", "build"}:
        return "generated top-level directory"
    if len(parts) >= 2 and parts[:2] == ("web", "dist"):
        return "built web output"
    if name.startswith("config/.atlas-init-"):
        return "runtime initialization state"
    if filename.endswith(_BANNED_SUFFIXES):
        return "generated or secret file type"
    if (filename == ".coverage" or filename.startswith(".coverage.")
            or filename == "coverage.xml" or filename.endswith(".egg-info")):
        return "private/generated file"
    return None


def scan_sdist(archive: Path) -> dict:
    archive = Path(archive).resolve()
    if not archive.is_file():
        raise AssertionError(f"source archive not found: {archive}")
    members, relative = _relative_members(archive)
    leaked = [f"{name} ({reason})" for name in sorted(relative)
              if (reason := _banned_reason(name))]
    if leaked:
        raise AssertionError("banned files in sdist: " + ", ".join(leaked))

    missing = sorted((_REQUIRED | _EXPECTED_WORKFLOWS) - set(relative))
    if missing:
        raise AssertionError(f"required release files missing from sdist: {missing}")
    shipped_workflows = {
        name for name in relative
        if name.startswith("workflows/") and name.endswith(".yaml")}
    if shipped_workflows != _EXPECTED_WORKFLOWS:
        raise AssertionError(
            f"sdist workflow set differs from shipped set: {sorted(shipped_workflows)}")

    findings: list[str] = []
    with tarfile.open(archive, "r:gz") as bundle:
        for name, member in sorted(relative.items()):
            if member.issym() or member.islnk() or member.isdev():
                findings.append(f"{name}: link/device entry")
                continue
            if not member.isfile():
                continue
            extracted = bundle.extractfile(member)
            if extracted is None:
                findings.append(f"{name}: unreadable regular file")
                continue
            data = extracted.read()
            for label, pattern in _CONTENT_CHECKS.items():
                if pattern.search(data):
                    findings.append(f"{name}: {label}")
    if findings:
        raise AssertionError("sdist content scan failed: " + "; ".join(findings))

    return {
        "archive": archive.name,
        "entries": len(members),
        "regular_files": sum(member.isfile() for member in members),
        "workflows": len(shipped_workflows),
        "findings": 0,
    }


def _archive_version(archive: Path) -> str:
    _, relative = _relative_members(archive)
    member = relative.get("pyproject.toml")
    if member is None:
        raise AssertionError("pyproject.toml missing from sdist")
    with tarfile.open(archive, "r:gz") as bundle:
        extracted = bundle.extractfile(member)
        if extracted is None:
            raise AssertionError("cannot read pyproject.toml from sdist")
        return tomllib.loads(extracted.read().decode("utf-8"))["project"]["version"]


def _clean_env() -> dict[str, str]:
    allowed = {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "USERPROFILE",
        "LOCALAPPDATA", "APPDATA", "HOME", "UV_CACHE_DIR",
    }
    env = {key: value for key, value in os.environ.items() if key in allowed}
    env.update({"PYTHONUTF8": "1", "UV_NO_PROGRESS": "1"})
    return env


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True,
        timeout=180, check=False)


def smoke_install_sdist(archive: Path, *, python_version: str = "3.12") -> dict:
    """Install locked dependencies offline, then exercise the installed package."""
    archive = Path(archive).resolve()
    expected_version = _archive_version(archive)
    uv = shutil.which("uv")
    if uv is None:
        raise AssertionError("uv is required for the clean sdist smoke")
    env = _clean_env()

    with tempfile.TemporaryDirectory(prefix="atlas-sdist-smoke-") as temp_name:
        temp = Path(temp_name)
        venv = temp / "venv"
        created = _run(
            [uv, "--no-python-downloads", "venv", "--python", python_version, str(venv)],
            cwd=temp, env=env)
        if created.returncode != 0:
            raise AssertionError(f"clean Python {python_version} venv failed: {created.stderr}")
        python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

        installed = _run(
            [uv, "pip", "install", "--offline", "--python", str(python),
             str(archive)],
            cwd=temp, env=env)
        if installed.returncode != 0:
            raise AssertionError(f"offline sdist install failed: {installed.stderr}")

        probe = textwrap.dedent(r'''
            import asyncio
            import importlib.metadata as metadata
            import json
            import sys
            import tempfile
            from pathlib import Path

            import atlas
            import atlas.engine
            import atlas.events
            import atlas.mcp
            import atlas.web
            from atlas.config_init import CONFIG_TEMPLATES, initialize_runtime_config
            from atlas.spec import spec_from_yaml

            spec = spec_from_yaml("""name: smoke
            nodes:
              - id: only
                type: llm
                model: Smoke:model
                prompt: smoke
                consumes: [task]
            edges:
              - from: only
                to: END
            """)
            with tempfile.TemporaryDirectory() as temp_name:
                config_dir = Path(temp_name) / "config"
                config_dir.mkdir()
                for template, _active in CONFIG_TEMPLATES:
                    content = "# env\n" if template.startswith(".") else "\n"
                    (config_dir / template).write_text(content, encoding="utf-8")
                initialized = initialize_runtime_config(config_dir)
                created = sorted(initialized.created)

            tools = sorted(tool.name for tool in asyncio.run(atlas.mcp.server.list_tools()))
            dist = metadata.distribution("atlas")
            print(json.dumps({
                "version": dist.version,
                "python": list(sys.version_info[:2]),
                "scripts": sorted(e.name for e in dist.entry_points if e.group == "console_scripts"),
                "tools": tools,
                "spec": spec.name,
                "initialized": created,
            }))
        ''')
        checked = _run([str(python), "-I", "-c", probe], cwd=temp, env=env)
        if checked.returncode != 0:
            raise AssertionError(f"installed-package import failed: {checked.stderr}")
        try:
            result = json.loads(checked.stdout)
        except json.JSONDecodeError as exc:
            raise AssertionError(f"installed-package probe returned invalid JSON: {checked.stdout}") from exc
        if result.get("version") != expected_version:
            raise AssertionError(
                f"installed version {result.get('version')} != {expected_version}")
        if result.get("python") != [3, 12]:
            raise AssertionError(f"smoke used Python {result.get('python')}, expected 3.12")
        expected_scripts = {"atlas", "atlas-mcp", "atlas-web"}
        if set(result.get("scripts", [])) != expected_scripts:
            raise AssertionError(f"installed console scripts differ: {result.get('scripts')}")

        expected_tools = {
            "atlas_get_run", "atlas_list_workflows", "atlas_resume_run",
            "atlas_run_workflow", "atlas_save_workflow", "atlas_validate_workflow",
        }
        if set(result.get("tools", [])) != expected_tools:
            raise AssertionError(f"installed MCP tools differ: {result.get('tools')}")
        if result.get("spec") != "smoke":
            raise AssertionError("installed spec parser did not return the smoke workflow")
        expected_initialized = {
            "providers.json", "models.reference.json", "capabilities.json",
            "pricing.json", "agents.json", ".env",
        }
        if set(result.get("initialized", [])) != expected_initialized:
            raise AssertionError(
                f"installed config initializer differs: {result.get('initialized')}")

    return {
        "version": expected_version,
        "python": "3.12",
        "offline": True,
        "dependencies_installed": True,
        "console_scripts": sorted(expected_scripts),
        "mcp_tools": sorted(expected_tools),
        "spec_parser": True,
        "config_init": True,
    }


def _find_archive(dist_dir: Path) -> Path:
    archives = sorted(Path(dist_dir).glob("*.tar.gz"))
    if len(archives) != 1:
        raise AssertionError(f"expected one sdist in {dist_dir}, found {len(archives)}")
    return archives[0]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path)
    parser.add_argument("--dist-dir", type=Path, default=Path("dist"))
    parser.add_argument("--skip-install", action="store_true")
    args = parser.parse_args()
    archive = args.archive or _find_archive(args.dist_dir)
    result = {"scan": scan_sdist(archive)}
    if not args.skip_install:
        result["install"] = smoke_install_sdist(archive)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

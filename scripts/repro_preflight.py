# -*- coding: utf-8 -*-
"""Reproduce the exact production agent preflight for the code-change spec."""
import re
import shutil
import subprocess
import sys

from atlas.engine import prepare_production_agent_runner
from atlas.spec import spec_from_yaml_file

spec = spec_from_yaml_file("workflows/code-change-review-approve.yaml")
try:
    runner = prepare_production_agent_runner(spec)
    print("PREFLIGHT OK:", runner)
except Exception as e:
    print("PREFLIGHT FAILED:", type(e).__name__, str(e)[:300])

import atlas.nodes.local_cli as lc
prog = shutil.which("claude")
print("resolved:", prog)
env = lc._base_child_env()
p = subprocess.run([prog, "--help"], capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=15, env=env)
text = p.stdout or ""
print("rc:", p.returncode, "stdout_len:", len(text))
pattern_line = [ln for ln in
                open("atlas/nodes/local_cli.py", encoding="utf-8")
                .read().splitlines() if "_REQUIRED_CLAUDE_FLAGS" in ln or
                "re.search(rf" in ln]
print("shipped check lines:")
for ln in pattern_line:
    print("   ", ln.strip())
missing = [f for f in lc._REQUIRED_CLAUDE_FLAGS
           if re.search(rf"(?<![\w-]){re.escape(f)}(?=[\s,=<]|$)", text) is None]
print("missing with single-backslash regex:", missing)

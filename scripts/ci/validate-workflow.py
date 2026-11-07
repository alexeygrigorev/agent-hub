#!/usr/bin/env python3
"""Validate the canonical Hub2 release contract; actionlint checks YAML semantics."""
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
def validate():
    result = subprocess.run(["node", "--test", "test/release-contract.test.mjs"],
                            cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise ValueError(result.stdout + result.stderr)
    for name in ("deploy.yml", "host-canary.yml", "rollback.yml"):
        content = (ROOT / ".github/workflows" / name).read_text()
        if "contents: read" not in content:
            raise ValueError(name + ": read-only repository permission required")
        if re.search(r"(?i)(bearer\s+[a-f0-9]{32,}|cfpat-[a-z0-9_-]{20,})", content):
            raise ValueError(name + ": possible literal credential")
        # Reusable jobs inherit the called workflow timeout; executable jobs must bound it.
        for block in re.split(r"(?m)^  [a-zA-Z][\w-]*:\n", content.split("jobs:\n", 1)[1])[1:]:
            if "runs-on:" in block and "timeout-minutes:" not in block:
                raise ValueError(name + ": executable job timeout missing")
        print(name + " passed all checks")
    print("All CI/CD workflow validations PASSED")
if __name__ == "__main__":
    try:
        validate()
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)

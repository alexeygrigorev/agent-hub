#!/usr/bin/env python3
"""Build the self-contained Developer Hub CLI zipapp (stdlib only).

Bundles host/agent_hub_host (including the vendored DTC device-login loop)
into public/cli/hub.pyz + sha256 + manifest.json. No network, no secrets,
no private GitHub keys, no Hetzner paths. Head rebuilds from checkout before
`wrangler deploy` so ./public assets publish /cli/*.

Usage:
  python3 scripts/build-hub-cli.py [--out-dir public/cli] [--version X.Y.Z]

Outputs (exact filenames for the server allowlist):
  install.sh is tracked source (not generated); this script writes:
  hub.pyz, hub.pyz.sha256, manifest.json
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
HOST_PKG = REPO / "host" / "agent_hub_host"
DEFAULT_OUT = REPO / "public" / "cli"
# Fixed zip timestamp for deterministic builds (zip min date is 1980).
FIXED_DATE = (1980, 1, 1, 0, 0, 0)
PYTHON_REQUIRES = ">=3.9"
SHARED_COMMIT = "c8ffec32e83de3a08435a993bc7cfca364244514"
SHARED_SOURCE = "DataTalksClub/auth dtc_device_login/__init__.py"


def repo_version():
    init = HOST_PKG / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("__version__"):
            return line.split("=")[1].strip().strip("'\"")
    return "0.1.0"


def git_sha():
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=str(REPO),
            capture_output=True, text=True, timeout=10,
        )
        sha = out.stdout.strip()
        if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
            return sha
    except Exception:
        pass
    return "unknown"


def collect_files():
    files = []
    for root, dirs, names in os.walk(HOST_PKG):
        dirs.sort()
        for name in sorted(names):
            if name.endswith(".pyc") or name == "__pycache__":
                continue
            p = Path(root) / name
            if "__pycache__" in p.parts:
                continue
            files.append(p)
    files.sort(key=lambda p: p.relative_to(REPO).as_posix())
    return files


def build(out_dir: Path, version: str):
    if sys.version_info < (3, 9):
        raise SystemExit("Python >=3.9 required to build hub.pyz")
    if not HOST_PKG.is_dir():
        raise SystemExit(f"Missing package dir {HOST_PKG}")
    out_dir.mkdir(parents=True, exist_ok=True)
    sha = git_sha()
    build_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    tmp_base = REPO / ".local" / "tmp"
    tmp_base.mkdir(parents=True, exist_ok=True)
    import tempfile
    staging = Path(tempfile.mkdtemp(prefix="hub-cli-", dir=str(tmp_base)))
    try:
        pkg_dst = staging / "agent_hub_host"
        shutil.copytree(HOST_PKG, pkg_dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        (pkg_dst / "_build_info.py").write_text(
            f'"""Build information stamped during zipapp creation."""\nBUILD_COMMIT = "{sha}"\nBUILD_DATE = "{build_date}"\n',
            encoding="utf-8",
        )
        (staging / "__main__.py").write_text(
            "from agent_hub_host.cli import main\nif __name__ == '__main__':\n    main()\n",
            encoding="utf-8",
        )
        pyz_tmp = staging / "hub.pyz.tmp"
        with zipfile.ZipFile(pyz_tmp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            # Deterministic entry order + timestamps.
            entries = []
            for root, dirs, names in os.walk(staging):
                if Path(root).name == "__pycache__":
                    continue
                dirs.sort()
                for name in sorted(names):
                    if name.endswith(".tmp") or name == "hub.pyz.tmp":
                        continue
                    full = Path(root) / name
                    arc = full.relative_to(staging).as_posix()
                    entries.append((arc, full))
            entries.sort(key=lambda t: t[0])
            for arc, full in entries:
                info = zipfile.ZipInfo(arc, date_time=FIXED_DATE)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.create_system = 3
                # Regular file 644; __main__ still runnable via python3 hub.pyz.
                info.external_attr = (0o644 << 16)
                zf.writestr(info, full.read_bytes())
        # Prepend shebang for direct execution.
        raw = pyz_tmp.read_bytes()
        blob = b"#!/usr/bin/env python3\n" + raw[raw.index(b"PK\x03\x04"):]
        digest = hashlib.sha256(blob).hexdigest()
        final = out_dir / "hub.pyz"
        final_tmp = out_dir / "hub.pyz.tmp"
        final_tmp.write_bytes(blob)
        os.replace(final_tmp, final)
        try:
            os.chmod(final, 0o755)
        except Exception:
            pass
        sha_path = out_dir / "hub.pyz.sha256"
        sha_tmp = out_dir / "hub.pyz.sha256.tmp"
        sha_tmp.write_text(f"{digest}  hub.pyz\n", encoding="utf-8")
        os.replace(sha_tmp, sha_path)
        manifest = {
            "name": "hub",
            "version": version,
            "sha256": digest,
            "python_requires": PYTHON_REQUIRES,
            "default_base_url": "https://hub.dtcdev.click",
            "vendored_device_login": {
                "module": "agent_hub_host._vendored_device_login",
                "source": SHARED_SOURCE,
                "commit": SHARED_COMMIT,
                "protocol": "POST /api/device/code + POST /api/device/token (hub paths)",
            },
            "built_from_commit": sha,
            "build_date": build_date,
            "files": ["install.sh", "install.ps1", "hub.pyz", "hub.pyz.sha256", "manifest.json"],
        }
        manifest_path = out_dir / "manifest.json"
        manifest_tmp = out_dir / "manifest.json.tmp"
        manifest_tmp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(manifest_tmp, manifest_path)
        print(f"Built {final} sha256={digest} version={version}")
        return digest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--version", default=None)
    args = ap.parse_args()
    version = args.version or repo_version()
    build(Path(args.out_dir), version)


if __name__ == "__main__":
    main()

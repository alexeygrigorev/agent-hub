#!/usr/bin/env python3
"""Deterministic stdlib-only builder for the staged Hub2 quota dispatcher archive."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "hub2" / "host" / "agent_hub_host"
FIXED_DATE_TIME = (2026, 1, 1, 0, 0, 0)
SOURCE_CLOSURE = (
    "__init__.py",
    "alert_authority.py",
    "alert_broker.py",
    "alert_collector.py",
    "quota_dispatch.py",
    "quota_dispatch_manager.py",
    "quota_dispatch_entry.py",
)
MAIN_STUB = (
    "from agent_hub_host.quota_dispatch_entry import main\n"
    "raise SystemExit(main())\n"
)


def _zip_info(arcname, *, mode=0o644):
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def build_archive(output_path, *, manifest_out=None):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {}
    tmp_path = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    try:
        with zipfile.ZipFile(tmp_path, "w") as zf:
            for rel in sorted(SOURCE_CLOSURE):
                src_file = PKG_ROOT / rel
                data = src_file.read_bytes()
                sources[f"agent_hub_host/{rel}"] = hashlib.sha256(data).hexdigest()
                zf.writestr(_zip_info(f"agent_hub_host/{rel}"), data)
            main_bytes = MAIN_STUB.encode("utf-8")
            sources["__main__.py"] = hashlib.sha256(main_bytes).hexdigest()
            zf.writestr(_zip_info("__main__.py"), main_bytes)
            manifest_bytes = json.dumps(
                {"v": 1, "entry": "agent_hub_host.quota_dispatch_entry:main", "sources": sources},
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            zf.writestr(_zip_info("MANIFEST.json"), manifest_bytes)
        os.chmod(tmp_path, 0o444)
        os.replace(tmp_path, output)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    archive_bytes = output.read_bytes()
    result = {
        "output": str(output),
        "size": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "sources": sources,
    }
    if manifest_out is not None:
        Path(manifest_out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build-quota-dispatcher")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".local" / "dist" / "agent-hub-quota-dispatcher.pyz",
    )
    parser.add_argument("--manifest-out", type=Path, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(build_archive(args.output, manifest_out=args.manifest_out)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

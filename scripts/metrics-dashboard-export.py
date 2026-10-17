#!/usr/bin/env python3
"""Read-only Dashboard -> sanitized Hub metrics snapshot exporter.

Reads the ACTUAL Dashboard via explicit source root only
(``dashboard.server._load_hourly_payload/_load_usage_payload/_load_features_payload``
with actual UTC now), projects a strict sanitized DTO with
``agent_hub_host.metrics.project_snapshot`` (quota percentages never become
tokens, money/cost dropped, unknown/partial preserved with as_of/provenance),
and writes the owned private snapshot JSON under /home.

Never mutates the dashboard repo, services, config, or global inputs.
Never prints secrets, raw reports, or tokens. Bounded output only.

Usage:
  python3 scripts/metrics-dashboard-export.py --dashboard-src /home/.../agent-dashboard/src \
      --out /home/.../private/metrics/metrics-snapshot.json [--as-of 2026-10-17T12:00:00.000Z]
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys


def _fail(msg: str) -> int:
    sys.stderr.write("metrics-dashboard-export: %s\n" % msg)
    return 2


def _is_home(path: str) -> bool:
    return isinstance(path, str) and path.startswith("/home/") and path == path.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dashboard-src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--host-id", default=None)
    args = parser.parse_args(argv)

    dashboard_src = args.dashboard_src
    out_path = args.out
    if not _is_home(dashboard_src) or not _is_home(out_path):
        return _fail("paths must be absolute under /home")
    if "/global/data" in out_path:
        return _fail("refusing global/data output")
    try:
        src_real = os.path.realpath(dashboard_src)
        out_real_parent = os.path.realpath(os.path.dirname(os.path.abspath(out_path)))
    except OSError:
        return _fail("unresolvable path")
    if not src_real.startswith("/home/") or not out_real_parent.startswith("/home/"):
        return _fail("paths escape /home")
    if out_real_parent == src_real or out_real_parent.startswith(src_real + os.sep):
        return _fail("refusing output inside dashboard source")
    server_py = os.path.join(src_real, "dashboard", "server.py")
    if not (os.path.isfile(server_py) and os.access(server_py, os.R_OK)):
        return _fail("dashboard source unreadable")
    # Output confinement: real output parent must be the claimed parent
    # (no symlink swap between check and write; write uses private_fs).
    if not os.path.isdir(dashboard_src):
        return _fail("dashboard source unreadable")

    # Import Dashboard read-only via explicit source root (no global install).
    sys.path.insert(0, src_real)
    try:
        from dashboard.server import (  # type: ignore
            _load_features_payload,
            _load_hourly_payload,
            _load_usage_payload,
        )
    except Exception:
        return _fail("dashboard import failed")

    # Host projector via repo host package (stdlib only).
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    host_root = os.path.join(repo_root, "host")
    if host_root not in sys.path:
        sys.path.insert(0, host_root)
    try:
        from agent_hub_host import metrics as _metrics
    except Exception:
        return _fail("host metrics import failed")

    # Actual UTC now unless operator passes an explicit strict as_of.
    if args.as_of:
        as_of = _metrics.parse_utc(args.as_of)
        if as_of is None:
            return _fail("invalid as_of")
    else:
        as_of = datetime.datetime.now(datetime.timezone.utc)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=datetime.timezone.utc)

    try:
        hourly = _load_hourly_payload(as_of)
        usage = _load_usage_payload(as_of)
        features = _load_features_payload(as_of)
    except Exception:
        return _fail("dashboard load failed")

    if not isinstance(hourly, dict) or not isinstance(usage, dict) or not isinstance(features, dict):
        return _fail("dashboard load failed")

    try:
        snapshot = _metrics.project_snapshot(
            hourly, usage, features,
            host_id=args.host_id if isinstance(args.host_id, str) and args.host_id else None,
        )
    except Exception:
        return _fail("projection failed")

    try:
        body = json.dumps(snapshot, separators=(",", ":"), sort_keys=True).encode("utf-8")
    except Exception:
        return _fail("projection failed")
    if len(body) > _metrics.MAX_SNAPSHOT_BYTES:
        return _fail("snapshot too large")

    # Owned private write only (0700 parents, 0600 file, no-follow, bounded).
    try:
        from agent_hub_host import private_fs as _pfs
        _pfs.atomic_write(out_path, body, _metrics.MAX_SNAPSHOT_BYTES)
    except Exception:
        return _fail("output write failed")

    # Bounded ack only: no report data, no paths, no argv.
    as_of_out = snapshot.get("as_of") if isinstance(snapshot, dict) else None
    sys.stdout.write(json.dumps({"event": "metrics_export_ok", "as_of": as_of_out}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

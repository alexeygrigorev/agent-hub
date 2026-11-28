#!/usr/bin/env python3
"""Pytest recorder plugin and CLI for the legacy CLI CI shards.

As a plugin (`-p legacy_cli_shards --legacy-cli-collected-out=FILE`) it records the node
ids left in session.items after deselection. As a CLI it plans shards from the canonical
collection, emits shard selector files, writes bounded shard evidence and runs the
fail-closed aggregate. See legacy_cli_manifest.py for the logic.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

_state = {"collect_errors": 0}


def pytest_addoption(parser):
    parser.addoption("--legacy-cli-collected-out", default=None,
                     help="write the post-deselection collected node ids to this JSON file")


def pytest_collectreport(report):
    if report.failed:
        _state["collect_errors"] += 1


def pytest_sessionfinish(session, exitstatus):
    out = session.config.getoption("legacy_cli_collected_out")
    if not out:
        return
    data = {"schema": 1, "kind": "legacy-cli-collected", "exitstatus": int(exitstatus),
            "collect_errors": _state["collect_errors"], "node_ids": [i.nodeid for i in session.items]}
    tmp = out + ".tmp"
    with open(tmp, "w") as handle:
        json.dump(data, handle)
    os.replace(tmp, out)


def _source(args, manifest):
    if args.sha:
        sha, tree = args.sha, args.tree
    else:
        sha = os.environ.get("GITHUB_SHA", "")
        git = lambda rev: subprocess.run(["git", "rev-parse", rev], check=True, capture_output=True,
                                         text=True).stdout.strip()
        if git("HEAD") != sha:
            raise manifest.Invalid("checked-out HEAD is not GITHUB_SHA")
        tree = git("HEAD^{tree}")
    return manifest.source_binding(sha, tree, args.run_id or os.environ.get("GITHUB_RUN_ID", ""))


def main(argv=None):
    import legacy_cli_manifest as m
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sha")
    parser.add_argument("--tree")
    parser.add_argument("--run-id")
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("canonical")
    c.add_argument("--dir", required=True)
    s = sub.add_parser("shard-args")
    s.add_argument("--manifest", required=True)
    s.add_argument("--shard", type=int, required=True)
    s.add_argument("--out", required=True)
    e = sub.add_parser("shard-evidence")
    e.add_argument("--dir", required=True)
    e.add_argument("--manifest", required=True)
    e.add_argument("--shard", type=int, required=True)
    e.add_argument("--outcome", required=True)
    a = sub.add_parser("aggregate")
    a.add_argument("--evidence-root", required=True)
    a.add_argument("--summary", required=True)
    args = parser.parse_args(argv)
    try:
        source = _source(args, m)
        if args.command == "canonical":
            d = Path(args.dir)
            ids = m.load_collected(d / "canonical-collected.json")
            m.dump(d / "manifest.json", m.build_manifest(ids, source))
            print("canonical: %d nodes planned into %d shards" % (len(ids), m.SHARD_COUNT))
        elif args.command == "shard-args":
            manifest = m.load_json(args.manifest, "legacy-cli-manifest")
            if manifest.get("source") != source or manifest.get("shard_count") != m.SHARD_COUNT:
                raise m.Invalid("manifest is bound to a different source or shard count")
            plan = {p["shard"]: p for p in manifest["shards"]}[args.shard]
            for unit in plan["units"]:
                if not m.UNIT_RE.fullmatch(unit):
                    raise m.Invalid("unsafe selector in manifest")
            Path(args.out).write_text("".join(u + "\n" for u in plan["units"]))
            print("shard %d: %d selectors, %d planned nodes" % (args.shard, len(plan["units"]), plan["planned_nodes"]))
        elif args.command == "shard-evidence":
            d = Path(args.dir)
            manifest_sha = hashlib.sha256(m.read_file(args.manifest, m.MAX_JSON_BYTES)).hexdigest()
            ids = m.load_collected(d / "collected.json", True)
            stats, rows = m.junit_stats(d / "junit.xml")
            m.dump(d / "durations.json", m.durations(args.shard, rows))
            m.dump(d / "evidence.json", m.shard_evidence(args.shard, source, manifest_sha, args.outcome, ids, stats))
            print("shard %d: %d collected, junit %s" % (args.shard, len(ids), stats))
        else:
            try:
                needs = json.loads(os.environ.get("NEEDS_JSON", ""))
            except ValueError:
                needs = None
            summary, errors = m.verify(args.evidence_root, source, needs)
            m.dump(args.summary, summary)
            for error in errors:
                print("::error::" + error.replace("\n", " "))
            print("aggregate %s: canonical=%s union=%s" % ("OK" if not errors else "FAILED",
                  summary.get("canonical_nodes"), summary.get("union_nodes")))
            return 1 if errors else 0
    except (m.Invalid, OSError, subprocess.CalledProcessError, KeyError, StopIteration) as error:
        print("::error::legacy-cli shards: %s: %s" % (type(error).__name__, error))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Stdlib selection, partition and coverage logic for the legacy CLI CI shards.

The canonical node list comes from ONE unpartitioned pytest collection that uses the
original selector and quarantine. Shards re-collect independently; the aggregate
requires the disjoint union of shard collections to equal the canonical list exactly.
Nothing here defines "canonical" as the concatenation of the partition.
"""
import hashlib
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path

SCHEMA = 1
SHARD_COUNT = 4
CANONICAL_DIR = "legacy-cli-canonical"
SHARD_DIR = "legacy-cli-shard-{}"
CANONICAL_FILES = {"canonical-collected.json", "manifest.json"}
SHARD_FILES = {"collected.json", "evidence.json", "junit.xml", "durations.json"}
NEEDS = ("legacy-cli-canonical", "legacy-cli-shard")

# The seven explicit nodes of the original unpartitioned selector (python-tests.yml
# canonical step). Their files are NOT globbed, so they stay node-level units.
EXPLICIT_NODES = (
    "host/tests/test_credentials.py::test_scoped_authoritative_generation_replaces_old_rotation_without_writeback",
    "host/tests/test_credentials.py::test_scoped_materialization_refuses_conflicts_without_mutation",
    "host/tests/test_credentials.py::test_scoped_materialization_state_publication_failure_is_resumable",
    "host/tests/test_credentials.py::test_scoped_materialization_missing_first_state_and_file_repair",
    "host/tests/test_credentials.py::test_scoped_materialization_existing_scalar_state_never_means_missing",
    "host/tests/test_provisioning.py::test_scoped_install_refuses_newer_state_before_hooks_or_package_writes",
    "host/tests/test_provisioning.py::test_scoped_install_scalar_state_refuses_before_package_and_hooks",
)

MAX_JSON_BYTES = 8 << 20
MAX_JUNIT_BYTES = 32 << 20
MAX_NODE_IDS = 50000
MAX_ID_LENGTH = 1024
MAX_REPORTED = 20
MAX_DURATION_ROWS = 200
MAX_SLOWEST = 50
UNIT_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*\.py(::[A-Za-z_][A-Za-z0-9_]*)?")
SHA_RE = re.compile(r"[0-9a-f]{40}")
RUN_RE = re.compile(r"[0-9]{1,20}")


class Invalid(ValueError):
    pass


def source_binding(sha, tree, run_id):
    if not (SHA_RE.fullmatch(str(sha)) and SHA_RE.fullmatch(str(tree)) and RUN_RE.fullmatch(str(run_id))):
        raise Invalid("source binding needs 40-hex sha, 40-hex tree and numeric run id")
    return {"sha": sha, "tree": tree, "run_id": str(run_id)}


def check_ids(ids):
    if not isinstance(ids, list) or not ids or len(ids) > MAX_NODE_IDS:
        raise Invalid("node ids must be a non-empty list of at most %d" % MAX_NODE_IDS)
    for node_id in ids:
        if (not isinstance(node_id, str) or not node_id or len(node_id) > MAX_ID_LENGTH
                or node_id.startswith("-") or any(ord(c) < 32 for c in node_id)):
            raise Invalid("invalid node id: %r" % (str(node_id)[:80],))


def ids_digest(ids):
    return hashlib.sha256("\n".join(sorted(ids)).encode()).hexdigest()


def unit_for(node_id, explicit=EXPLICIT_NODES):
    """Selector unit of a node: its explicit selector, otherwise its whole module."""
    for selector in explicit:
        if node_id == selector or node_id.startswith((selector + "[", selector + "::")):
            return selector
    return node_id.split("::", 1)[0]


def plan(ids, shards=SHARD_COUNT, explicit=EXPLICIT_NODES):
    """Deterministic longest-count-first assignment; node counts are a proxy, not runtime."""
    counts = Counter(unit_for(i, explicit) for i in ids)
    for unit in counts:
        if not UNIT_RE.fullmatch(unit) or ".." in unit.split("/"):
            raise Invalid("unit is not a safe pytest selector: %r" % (unit[:80],))
    if len(counts) < shards:
        raise Invalid("only %d selector units for %d shards" % (len(counts), shards))
    loads, units = [0] * shards, [[] for _ in range(shards)]
    for unit, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        k = min(range(shards), key=lambda j: (loads[j], j))
        loads[k] += count
        units[k].append(unit)
    return [{"shard": k, "units": sorted(units[k]), "planned_nodes": loads[k]} for k in range(shards)]


def build_manifest(ids, source, shards=SHARD_COUNT, explicit=EXPLICIT_NODES):
    check_ids(ids)
    if len(set(ids)) != len(ids):
        raise Invalid("canonical collection contains duplicate node ids")
    for selector in explicit:
        if not any(unit_for(i, explicit) == selector for i in ids):
            raise Invalid("explicit selector collected nothing: " + selector)
    return {"schema": SCHEMA, "kind": "legacy-cli-manifest", "source": source, "shard_count": shards,
            "canonical": {"count": len(ids), "sha256": ids_digest(ids)},
            "explicit_nodes": list(explicit), "shards": plan(ids, shards, explicit),
            "balance": "planned_nodes is a collected-node-count proxy, not measured runtime"}


def dump(path, data):
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if len(text) > MAX_JSON_BYTES:
        raise Invalid("refusing to write oversized json")
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def read_file(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise Invalid("%s is not a regular file" % path.name)
    if path.stat().st_size > limit:
        raise Invalid("%s exceeds %d bytes" % (path.name, limit))
    return path.read_bytes()


def load_json(path, kind):
    raw = read_file(path, MAX_JSON_BYTES)
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError, UnicodeDecodeError) as error:
        raise Invalid("%s is corrupt json: %s" % (Path(path).name, type(error).__name__)) from None
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("kind") != kind:
        raise Invalid("%s has wrong schema or kind" % Path(path).name)
    return data


def load_collected(path, executed=False):
    """Collection record. Canonical (collect-only) must exit 0; an executed shard may exit 1
    for failing tests (reported by its outcome and JUnit) but never with collection errors."""
    data = load_json(path, "legacy-cli-collected")
    ids = data.get("node_ids")
    check_ids(ids)
    if data.get("exitstatus") not in ((0, 1) if executed else (0,)) or data.get("collect_errors") != 0:
        raise Invalid("%s records a failed or errored session" % Path(path).name)
    return ids


def junit_stats(path):
    """Return (stats, per-testcase rows) from a JUnit file."""
    try:
        root = ET.fromstring(read_file(path, MAX_JUNIT_BYTES))
    except ET.ParseError:
        raise Invalid("junit.xml is corrupt") from None
    stats = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "time": 0.0}
    rows = []
    for case in root.iter("testcase"):
        tags = {child.tag for child in case}
        try:
            seconds = float(case.get("time", "0"))
        except ValueError:
            raise Invalid("junit.xml has a non-numeric time") from None
        stats["tests"] += 1
        stats["failures"] += "failure" in tags
        stats["errors"] += "error" in tags
        stats["skipped"] += "skipped" in tags
        stats["time"] += seconds
        rows.append((case.get("classname", ""), case.get("name", ""), seconds))
    stats["time"] = round(stats["time"], 3)
    return stats, rows


def durations(shard, rows):
    modules = {}
    for classname, _, seconds in rows:
        entry = modules.setdefault(classname, [0, 0.0])
        entry[0] += 1
        entry[1] += seconds
    top = sorted(modules.items(), key=lambda kv: (-kv[1][1], kv[0]))[:MAX_DURATION_ROWS]
    slowest = sorted(rows, key=lambda r: (-r[2], r[0], r[1]))[:MAX_SLOWEST]
    return {"schema": SCHEMA, "kind": "legacy-cli-durations", "shard": shard,
            "modules": [{"module": m, "tests": n, "seconds": round(t, 3)} for m, (n, t) in top],
            "slowest": [{"id": c + "::" + n, "seconds": round(t, 3)} for c, n, t in slowest]}


def shard_evidence(shard, source, manifest_sha, outcome, ids, stats):
    return {"schema": SCHEMA, "kind": "legacy-cli-shard-evidence", "source": source, "shard": shard,
            "manifest_sha256": manifest_sha, "pytest_outcome": outcome,
            "collected": {"count": len(ids), "sha256": ids_digest(ids)}, "junit": stats}


def _exact_files(directory, names, errors, label):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        errors.append("%s: artifact missing" % label)
        return False
    found = {p.name for p in directory.iterdir()}
    if found != names:
        errors.append("%s: expected files %s, found %s" % (label, sorted(names), sorted(found)))
        return False
    return True


def _guard(errors, label, func, *args):
    try:
        return func(*args)
    except (Invalid, OSError) as error:
        errors.append("%s: %s" % (label, error))
        return None


def _sample(items):
    items = sorted(items)
    return items[:MAX_REPORTED] + (["... %d more" % (len(items) - MAX_REPORTED)] if len(items) > MAX_REPORTED else [])


def verify(root, source, needs, shards=SHARD_COUNT, explicit=EXPLICIT_NODES):
    """Fail-closed aggregate. Returns (summary, errors); no errors means full proven coverage."""
    root, errors = Path(root), []
    if not isinstance(needs, dict):
        errors.append("needs: not an object")
        needs = {}
    for job in NEEDS:
        result = needs.get(job, {}).get("result") if isinstance(needs.get(job), dict) else None
        if result != "success":
            errors.append("needs: %s result is %r, not success" % (job, result))
    extra = {p.name for p in root.iterdir()} - {CANONICAL_DIR} - {SHARD_DIR.format(k) for k in range(shards)} \
        if root.is_dir() else set()
    if not root.is_dir():
        errors.append("evidence root missing")
    if extra:
        errors.append("unexpected artifacts: %s" % sorted(extra))

    canonical, manifest, manifest_sha = None, None, None
    cdir = root / CANONICAL_DIR
    if _exact_files(cdir, CANONICAL_FILES, errors, "canonical"):
        canonical = _guard(errors, "canonical", load_collected, cdir / "canonical-collected.json")
        manifest = _guard(errors, "manifest", load_json, cdir / "manifest.json", "legacy-cli-manifest")
        if manifest is not None:
            manifest_sha = hashlib.sha256((cdir / "manifest.json").read_bytes()).hexdigest()
    expected_plan = None
    if canonical is not None:
        if len(set(canonical)) != len(canonical):
            errors.append("canonical: duplicate node ids")
        else:
            try:
                expected_plan = build_manifest(canonical, source, shards, explicit)
            except Invalid as error:
                errors.append("canonical: %s" % error)
    if manifest is not None and expected_plan is not None and manifest != expected_plan:
        errors.append("manifest: differs from plan recomputed from the canonical collection "
                      "(source binding, digest, explicit nodes or shard assignment)")
        expected_plan = None
    plan_by_shard = {p["shard"]: p for p in (expected_plan or {}).get("shards", [])}

    seen, per_shard = [], []
    for k in range(shards):
        label, sdir = "shard %d" % k, root / SHARD_DIR.format(k)
        if not _exact_files(sdir, SHARD_FILES, errors, label):
            continue
        ids = _guard(errors, label, load_collected, sdir / "collected.json", True)
        evidence = _guard(errors, label, load_json, sdir / "evidence.json", "legacy-cli-shard-evidence")
        stats_rows = _guard(errors, label, junit_stats, sdir / "junit.xml")
        _guard(errors, label, load_json, sdir / "durations.json", "legacy-cli-durations")
        if evidence is not None:
            for field, want in (("source", source), ("shard", k), ("manifest_sha256", manifest_sha),
                                ("pytest_outcome", "success")):
                if evidence.get(field) != want:
                    errors.append("%s: evidence %s is %r, expected %r" % (label, field, evidence.get(field), want))
        if ids is None:
            continue
        if len(set(ids)) != len(ids):
            dup = [i for i, n in Counter(ids).items() if n > 1]
            errors.append("%s: duplicate node ids within shard: %s" % (label, _sample(dup)))
        if evidence is not None and evidence.get("collected") != {"count": len(ids), "sha256": ids_digest(ids)}:
            errors.append("%s: collected ids do not match evidence digest" % label)
        if plan_by_shard:
            units = set(plan_by_shard[k]["units"])
            stray = [i for i in ids if unit_for(i, explicit) not in units]
            if stray:
                errors.append("%s: collected nodes outside its planned units: %s" % (label, _sample(stray)))
        if stats_rows is not None:
            stats = stats_rows[0]
            if evidence is not None and evidence.get("junit") != stats:
                errors.append("%s: junit totals differ from evidence" % label)
            if stats["failures"] or stats["errors"]:
                errors.append("%s: junit records %d failures, %d errors" % (label, stats["failures"], stats["errors"]))
            if stats["tests"] < len(ids):
                errors.append("%s: junit has %d testcases for %d collected nodes" % (label, stats["tests"], len(ids)))
        seen.extend(ids)
        per_shard.append({"shard": k, "nodes": len(ids), "junit_tests": stats_rows[0]["tests"] if stats_rows else None})

    summary = {"schema": SCHEMA, "kind": "legacy-cli-aggregate", "source": source,
               "canonical_nodes": len(canonical) if canonical else None, "shards": per_shard}
    if canonical is not None:
        canon, union = set(canonical), set(seen)
        duplicated = [i for i, n in Counter(seen).items() if n > 1]
        if duplicated:
            errors.append("duplicate nodes across shards: %s" % _sample(duplicated))
        if canon - union:
            errors.append("missing from shards: %s" % _sample(canon - union))
        if union - canon:
            errors.append("unexpected in shards: %s" % _sample(union - canon))
        summary["union_nodes"] = len(union)
    summary["ok"] = not errors
    summary["errors"] = errors[:100]
    return summary, errors

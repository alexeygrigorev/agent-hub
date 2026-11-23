"""Legacy CLI CI shard selection and fail-closed coverage, using small real pytest runs.

Fixture projects are tiny pytest trees under tmp_path; the real legacy suite is never
collected here. The production partition is never hardcoded: shards are planned from a
fresh canonical collection of the fixture."""
import copy
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from unittest import mock

import pytest

REPO = Path(__file__).resolve().parents[2]
CI = REPO / "scripts/ci"
spec = importlib.util.spec_from_file_location("legacy_cli_manifest_under_test", CI / "legacy_cli_manifest.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
SOURCE = {"sha": "a" * 40, "tree": "b" * 40, "run_id": "123"}
GOOD_NEEDS = {"legacy-cli-canonical": {"result": "success"}, "legacy-cli-shard": {"result": "success"}}


def write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def build_fixture(root, failing=False):
    """Mirror the production selector shape: whole modules plus the seven explicit nodes."""
    tests = root / "host/tests"
    write(root / "pytest.ini", "[pytest]\n")
    write(tests / "test_cli_a.py", "import pytest\n@pytest.mark.parametrize('v', ['x y', 'a::b', '[z]', 3])\n"
          "def test_param(v):\n    assert v\ndef test_plain():\n    pass\n")
    write(tests / "test_cli_b.py", "def test_one():\n    pass\ndef test_quarantined():\n    assert False\n")
    write(tests / "test_cli_c.py", "def test_a():\n    pass\ndef test_b():\n    pass\n"
          "def test_c():\n    pass\ndef test_skipped():\n    import pytest; pytest.skip('legit')\n")
    write(tests / "test_intake_d.py", "def test_d():\n    assert %s\n" % ("False" if failing else "True"))
    write(tests / "test_session_control.py", "def test_s():\n    pass\n")
    explicit = [s.split("::") for s in m.EXPLICIT_NODES]
    for file in sorted({f for f, _ in explicit}):
        body = "import pytest\n"
        for _, name in (e for e in explicit if e[0] == file):
            body += "@pytest.mark.parametrize('p', [1, 2])\ndef %s(p):\n    pass\n" % name
        body += "def test_not_in_original_selector():\n    assert False\n"
        write(root / file, body)
    return ["host/tests/test_cli*.py", "host/tests/test_session_control.py", "host/tests/test_intake*.py",
            *m.EXPLICIT_NODES]


def pytest_run(root, args, extra_env=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTEST_")}
    env.update(PYTHONPATH=str(CI), HOME=str(root / "home"), **(extra_env or {}))
    done = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "legacy_cli_shards",
                           "--rootdir", str(root), *args], cwd=root, env=env, capture_output=True, text=True)
    return done


def cli(root, *args, env=None):
    base = {k: v for k, v in os.environ.items() if k != "GITHUB_SHA"}
    base.update(env or {})
    return subprocess.run([sys.executable, str(CI / "legacy_cli_shards.py"), "--sha", SOURCE["sha"],
                           "--tree", SOURCE["tree"], "--run-id", SOURCE["run_id"], *args],
                          cwd=root, env=base, capture_output=True, text=True)


def globbed(root, selector):
    return sorted(str(p.relative_to(root)) for p in root.glob(selector)) if "*" in selector else [selector]


def make_evidence(tmp_path, failing=False):
    """Run the real flow: canonical collection, plan, per-shard execution, evidence."""
    root = tmp_path / "project"
    selector = build_fixture(root, failing)
    sel = [x for s in selector for x in globbed(root, s)]
    evidence = tmp_path / "evidence"
    canonical = evidence / m.CANONICAL_DIR
    canonical.mkdir(parents=True)
    deselect = ["--deselect", "host/tests/test_cli_b.py::test_quarantined"]
    done = pytest_run(root, [*deselect, *sel, "--collect-only",
                             "--legacy-cli-collected-out=" + str(canonical / "canonical-collected.json")])
    assert done.returncode == 0, done.stdout + done.stderr
    assert cli(root, "canonical", "--dir", str(canonical)).returncode == 0
    manifest = canonical / "manifest.json"
    for k in range(m.SHARD_COUNT):
        out = evidence / m.SHARD_DIR.format(k)
        out.mkdir()
        selectors = out.parent / ("selectors-%d.txt" % k)
        assert cli(root, "shard-args", "--manifest", str(manifest), "--shard", str(k),
                   "--out", str(selectors)).returncode == 0
        run = pytest_run(root, [*deselect, "--junitxml=" + str(out / "junit.xml"), "--durations=3",
                                "--legacy-cli-collected-out=" + str(out / "collected.json"),
                                *selectors.read_text().splitlines()])
        outcome = "success" if run.returncode == 0 else "failure"
        done = cli(root, "shard-evidence", "--dir", str(out), "--manifest", str(manifest), "--shard", str(k),
                   "--outcome", outcome)
        assert done.returncode == 0, done.stdout + done.stderr
        selectors.unlink()
    return root, evidence


@pytest.fixture(scope="module")
def good(tmp_path_factory):
    return make_evidence(tmp_path_factory.mktemp("good"))


def copy_evidence(good, tmp_path):
    target = tmp_path / "evidence"
    shutil.copytree(good[1], target)
    return target


def load(path):
    return json.loads(Path(path).read_text())


def rewrite_ids(evidence, shard, change):
    """Edit a shard's collected ids and keep its evidence digest consistent."""
    d = evidence / m.SHARD_DIR.format(shard)
    collected = load(d / "collected.json")
    collected["node_ids"] = change(collected["node_ids"])
    (d / "collected.json").write_text(json.dumps(collected))
    ev = load(d / "evidence.json")
    ev["collected"] = {"count": len(collected["node_ids"]), "sha256": m.ids_digest(collected["node_ids"])}
    (d / "evidence.json").write_text(json.dumps(ev))


def verdict(evidence, needs=GOOD_NEEDS, source=SOURCE):
    summary, errors = m.verify(evidence, source, needs)
    return errors


def has(errors, text):
    return any(text in e for e in errors)


def test_real_flow_collects_original_selection_and_proves_union(good):
    root, evidence = good
    assert verdict(evidence) == []
    canonical = load(evidence / m.CANONICAL_DIR / "canonical-collected.json")["node_ids"]
    # Original semantics: explicit nodes keep pytest parametrization, other tests of their files are not selected.
    assert "host/tests/test_cli_a.py::test_param[x y]" in canonical
    assert "host/tests/test_cli_a.py::test_param[a::b]" in canonical
    assert "host/tests/test_cli_a.py::test_param[3]" in canonical
    assert not any("test_not_in_original_selector" in i for i in canonical)
    assert not any("test_quarantined" in i for i in canonical)
    for selector in m.EXPLICIT_NODES:
        assert [i for i in canonical if i.startswith(selector + "[")] == [selector + "[1]", selector + "[2]"]
    shards = [load(evidence / m.SHARD_DIR.format(k) / "collected.json")["node_ids"] for k in range(4)]
    assert sorted(i for ids in shards for i in ids) == sorted(canonical)
    assert all(shards)
    skipped = sum(load(evidence / m.SHARD_DIR.format(k) / "evidence.json")["junit"]["skipped"] for k in range(4))
    assert skipped == 1  # a legitimate in-test skip stays visible and does not fail coverage
    d = load(evidence / m.SHARD_DIR.format(0) / "durations.json")
    assert d["slowest"] and len(d["slowest"]) <= m.MAX_SLOWEST


def test_plan_is_deterministic_and_keeps_units_whole():
    ids = ["host/tests/test_cli_%s.py::t%d" % (c, i) for c, n in zip("abcdefg", (9, 7, 5, 3, 3, 1, 1)) for i in range(n)]
    ids += [e + "[%d]" % i for e in m.EXPLICIT_NODES[:2] for i in range(2)]
    first = m.plan(ids)
    assert first == m.plan(list(reversed(ids)))
    seen = [u for p in first for u in p["units"]]
    assert len(seen) == len(set(seen)) == 7 + 2
    for node in ids:
        assert sum(m.unit_for(node) in p["units"] for p in first) == 1
    assert sum(p["planned_nodes"] for p in first) == len(ids)
    with pytest.raises(m.Invalid):
        m.plan(ids[:2])  # fewer units than shards would leave an empty shard that runs everything
    with pytest.raises(m.Invalid):
        m.plan(["../evil.py::t", "a.py::t", "b.py::t", "c.py::t"])
    with pytest.raises(m.Invalid):
        m.check_ids(["-k", "x"])


def test_duplicate_missing_and_unexpected_nodes_fail(good, tmp_path):
    ev = copy_evidence(good, tmp_path)
    ids0 = load(ev / m.SHARD_DIR.format(0) / "collected.json")["node_ids"]
    rewrite_ids(ev, 1, lambda ids: ids + [ids0[0]])
    assert has(verdict(ev), "duplicate nodes across shards")
    ev = copy_evidence(good, tmp_path / "x")
    rewrite_ids(ev, 0, lambda ids: ids[1:])
    assert has(verdict(ev), "missing from shards")
    ev = copy_evidence(good, tmp_path / "y")
    rewrite_ids(ev, 0, lambda ids: ids + ["host/tests/test_extra.py::test_new"])
    errors = verdict(ev)
    assert has(errors, "unexpected in shards") and has(errors, "outside its planned units")
    ev = copy_evidence(good, tmp_path / "z")
    rewrite_ids(ev, 0, lambda ids: ids + ids[:1])
    assert has(verdict(ev), "duplicate node ids within shard")


def test_failed_skipped_missing_and_corrupt_evidence_fail(good, tmp_path):
    ev = copy_evidence(good, tmp_path / "a")
    for job, result in (("legacy-cli-shard", "failure"), ("legacy-cli-canonical", "skipped"),
                        ("legacy-cli-shard", "cancelled"), ("legacy-cli-shard", None)):
        needs = {**GOOD_NEEDS, job: {"result": result}}
        assert has(verdict(ev, needs), job), result
    assert has(verdict(ev, None), "needs")
    assert has(verdict(ev, {}), "needs")
    path = ev / m.SHARD_DIR.format(2) / "evidence.json"
    bad = load(path)
    bad["pytest_outcome"] = "failure"
    path.write_text(json.dumps(bad))
    assert has(verdict(ev), "pytest_outcome")
    shutil.rmtree(ev / m.SHARD_DIR.format(3))
    assert has(verdict(ev), "shard 3: artifact missing")
    (ev / m.CANONICAL_DIR / "manifest.json").write_text("{not json")
    assert has(verdict(ev), "corrupt json")
    ev2 = copy_evidence(good, tmp_path / "b")
    (ev2 / m.SHARD_DIR.format(1) / "junit.xml").write_text("<testsuite><oops")
    assert has(verdict(ev2), "junit.xml is corrupt")
    ev3 = copy_evidence(good, tmp_path / "c")
    (ev3 / m.SHARD_DIR.format(1) / "extra.txt").write_text("x")
    (ev3 / "legacy-cli-shard-9").mkdir()
    errors = verdict(ev3)
    assert has(errors, "expected files") and has(errors, "unexpected artifacts")
    ev4 = copy_evidence(good, tmp_path / "d")
    shutil.rmtree(ev4 / m.CANONICAL_DIR)
    assert has(verdict(ev4), "canonical: artifact missing")


def test_junit_failure_and_short_junit_fail(good, tmp_path):
    ev = copy_evidence(good, tmp_path)
    junit = ev / m.SHARD_DIR.format(0) / "junit.xml"
    junit.write_text(re.sub(r"(<testcase [^>]*?)/>", r"\1><failure message='x'/></testcase>", junit.read_text(), count=1))
    errors = verdict(ev)
    assert has(errors, "junit records 1 failures") and has(errors, "junit totals differ")
    ev = copy_evidence(good, tmp_path / "s")
    junit = ev / m.SHARD_DIR.format(0) / "junit.xml"
    junit.write_text(re.sub(r"<testcase .*?(/>|</testcase>)", "", junit.read_text(), count=1, flags=re.S))
    assert has(verdict(ev), "testcases for")


def test_source_binding_manifest_tamper_and_digests(good, tmp_path):
    ev = copy_evidence(good, tmp_path)
    assert has(verdict(ev, source={**SOURCE, "sha": "c" * 40}), "source")
    assert has(verdict(ev, source={**SOURCE, "run_id": "999"}), "source")
    manifest = load(ev / m.CANONICAL_DIR / "manifest.json")
    moved = copy.deepcopy(manifest)
    moved["shards"][1]["units"].append(moved["shards"][0]["units"].pop())
    (ev / m.CANONICAL_DIR / "manifest.json").write_text(json.dumps(moved))
    errors = verdict(ev)
    assert has(errors, "differs from plan recomputed") and has(errors, "manifest_sha256")
    ev2 = copy_evidence(good, tmp_path / "c")
    cc = ev2 / m.CANONICAL_DIR / "canonical-collected.json"
    data = load(cc)
    data["node_ids"].append("host/tests/test_cli_a.py::injected")
    cc.write_text(json.dumps(data))
    assert has(verdict(ev2), "differs from plan recomputed")
    ev3 = copy_evidence(good, tmp_path / "d")
    data = load(ev3 / m.CANONICAL_DIR / "canonical-collected.json")
    data["node_ids"].append(data["node_ids"][0])
    (ev3 / m.CANONICAL_DIR / "canonical-collected.json").write_text(json.dumps(data))
    assert has(verdict(ev3), "duplicate node ids")


def test_canonical_collection_failure_and_oversize_fail(good, tmp_path):
    ev = copy_evidence(good, tmp_path)
    cc = ev / m.CANONICAL_DIR / "canonical-collected.json"
    data = load(cc)
    data["exitstatus"] = 2
    cc.write_text(json.dumps(data))
    assert has(verdict(ev), "failed or errored session")
    data.update(exitstatus=0, collect_errors=1)
    cc.write_text(json.dumps(data))
    assert has(verdict(ev), "failed or errored session")
    ev2 = copy_evidence(good, tmp_path / "o")
    with mock.patch.object(m, "MAX_JSON_BYTES", 200):
        assert has(verdict(ev2), "exceeds")
    link = ev2 / m.SHARD_DIR.format(0) / "durations.json"
    link.unlink()
    link.symlink_to(ev2 / m.SHARD_DIR.format(1) / "durations.json")
    assert has(verdict(ev2), "not a regular file")


def test_real_collection_error_is_recorded_and_refused(tmp_path):
    root = tmp_path / "p"
    write(root / "pytest.ini", "[pytest]\n")
    write(root / "host/tests/test_cli_ok.py", "def test_ok():\n    pass\n")
    write(root / "host/tests/test_cli_broken.py", "this is not python\n")
    out = tmp_path / "canonical-collected.json"
    done = pytest_run(root, ["host/tests", "--collect-only", "--legacy-cli-collected-out=" + str(out)])
    assert done.returncode != 0
    with pytest.raises(m.Invalid):
        m.load_collected(out)
    assert load(out)["collect_errors"] == 1


def test_real_failing_shard_fails_aggregate(tmp_path):
    root, evidence = make_evidence(tmp_path, failing=True)
    errors = verdict(evidence)
    assert has(errors, "pytest_outcome") and has(errors, "junit records 1 failures")


def test_cli_aggregate_exit_code_and_summary(good, tmp_path):
    ev = copy_evidence(good, tmp_path)
    needs = json.dumps(GOOD_NEEDS)
    summary = tmp_path / "summary.json"
    done = cli(tmp_path, "aggregate", "--evidence-root", str(ev), "--summary", str(summary), env={"NEEDS_JSON": needs})
    assert done.returncode == 0, done.stdout
    assert load(summary)["ok"] is True
    for needs_json in ("", "garbage", json.dumps({**GOOD_NEEDS, "legacy-cli-shard": {"result": "skipped"}})):
        done = cli(tmp_path, "aggregate", "--evidence-root", str(ev), "--summary", str(summary),
                   env={"NEEDS_JSON": needs_json})
        assert done.returncode == 1 and load(summary)["ok"] is False
    done = cli(tmp_path, "aggregate", "--evidence-root", str(tmp_path / "absent"), "--summary", str(summary),
               env={"NEEDS_JSON": needs})
    assert done.returncode == 1
    done = cli(tmp_path, "shard-args", "--manifest", str(ev / m.CANONICAL_DIR / "manifest.json"), "--shard", "9",
               "--out", str(tmp_path / "s.txt"))
    assert done.returncode == 1 and not (tmp_path / "s.txt").exists()
    done = subprocess.run([sys.executable, str(CI / "legacy_cli_shards.py"), "--sha", "d" * 40, "--tree", SOURCE["tree"],
                           "--run-id", "123", "shard-args", "--manifest", str(ev / m.CANONICAL_DIR / "manifest.json"),
                           "--shard", "0", "--out", str(tmp_path / "s.txt")], capture_output=True, text=True)
    assert done.returncode == 1 and "different source" in done.stdout


WORKFLOW = (REPO / ".github/workflows/python-tests.yml").read_text()


def test_workflow_keeps_stable_gate_and_original_selector():
    assert "continue-on-error" not in WORKFLOW
    assert re.search(r"^permissions:\n  contents: read\n", WORKFLOW, re.M)
    assert "  hub2-host-python:\n    name: hub2-host-python\n" in WORKFLOW
    gate = WORKFLOW.split("  legacy-cli-python:\n", 1)[1]
    assert "    name: legacy-cli-python\n" in gate
    assert "needs: [legacy-cli-canonical, legacy-cli-shard]" in gate and "if: ${{ always() && (" in gate
    assert "NEEDS_JSON: ${{ toJSON(needs) }}" in gate
    canonical = WORKFLOW.split("  legacy-cli-canonical:\n", 1)[1].split("  legacy-cli-shard:\n", 1)[0]
    for node in m.EXPLICIT_NODES:
        assert node in canonical
    explicit_in_workflow = re.findall(r"host/tests/test_(?:credentials|provisioning)\.py::\w+", WORKFLOW)
    assert sorted(set(explicit_in_workflow)) == sorted(m.EXPLICIT_NODES)
    for glob in ("host/tests/test_cli*.py", "host/tests/test_session_control.py", "host/tests/test_intake*.py",
                 "host/tests/test_native_claude_import.py", "host/tests/test_stage_credential_monitor.py"):
        assert glob in canonical
    assert "host/tests/QUARANTINE-cli.md" in canonical
    assert canonical.count("--collect-only") == 1
    shard = WORKFLOW.split("  legacy-cli-shard:\n", 1)[1].split("  legacy-cli-python:\n", 1)[0]
    assert "--collect-only" not in shard and "needs: legacy-cli-canonical" in shard
    assert "matrix:\n        shard: [0, 1, 2, 3]" in shard and "fail-fast: false" in shard
    assert "host/tests/QUARANTINE-cli.md" in shard and 'mapfile -t selectors' in shard
    assert WORKFLOW.count("retention-days: 7") == WORKFLOW.count("upload-artifact@v4")
    # No expression is interpolated into a shell command line (selectors/ids stay data).
    for line in WORKFLOW.splitlines():
        if "${{" in line:
            assert re.match(r"\s*(- )?[\w-]+:\s", line), line


def test_glob_selection_includes_this_module_and_the_original_modules():
    names = {p.name for pattern in ("test_cli*.py", "test_intake*.py") for p in (REPO / "host/tests").glob(pattern)}
    names |= {"test_session_control.py", "test_native_claude_import.py", "test_stage_credential_monitor.py"}
    assert "test_cli_ci_shards.py" in names
    assert len(names) >= 49
    assert all((REPO / "host/tests" / n).is_file() for n in names)

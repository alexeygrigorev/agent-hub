"""500-row intake acceptance through the published CLI build and production Node entry.

Runs in the required Node 24 / Python 3.12 e2e job. Only an isolated loopback
server and disposable SQLite files are used for the intake test. The concurrency
tests below additionally run the real Python host daemon with the stub native
backend from hub2/tests/e2e (explicit shell engine; no provider, credential or
production endpoint).
"""
import importlib.util
import json
import os
import re
import shutil
import signal
import http.server
import threading
import time
import types
from pathlib import Path
from urllib.parse import quote

import pytest


def test_real_cli_pages_500_unique_intakes_and_searches_across_projects():
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "intake_node_harness", repo / "hub2/tests/e2e/harness.py")
    harness = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(harness)
    root = harness.scratch_root()
    server = harness.Server(root)
    try:
        zipapp = harness.build_cli(root / "cli")
        server.start()
        cli = harness.Cli(zipapp, root, server)
        projects = ("cli500/project-a", "cli500/project-b")
        seeded, sparse_ids = set(), set()
        context_id = None
        for project, count in zip(projects, (270, 230)):
            server.ok("PATCH", "/api/repos/" + quote(project, safe=""), {
                "canonical_url": "https://github.com/" + project,
                "purpose": "Isolated 500-intake CLI acceptance", "status": "active"})
            for index in range(count):
                result = server.ok("POST", "/api/intake/tasks?project=" + quote(project, safe=""), {
                    "key": "cli500-" + project.rsplit("/", 1)[1] + "-" + str(index),
                    "title": "Intake %s %04d" % (project, index),
                    "text": "old sparse cli500 needle" if index == 0 else "Ordinary intake body",
                    "acceptance_criteria": ["Retain this isolated test intake"],
                })
                intake_id = result["intake"]["id"]
                assert intake_id not in seeded
                seeded.add(intake_id)
                if index == 0:
                    sparse_ids.add(intake_id)
                if project == projects[1] and index == 1:
                    context_id = intake_id
        assert len(seeded) == 500
        server.ok("POST", "/api/intake/tasks/" + context_id + "/append", {
            "key": "cli500-context-note", "note": "cli500 context-only marker"})

        observed, cursors = [], set()
        cursor = None
        pages = 0
        while True:
            args = ["intake", "list", "--limit", "50"]
            if cursor is not None:
                args += ["--cursor", cursor]
            page = cli.json(*args)
            rows = page["items"]
            assert len(rows) == 50, page
            assert all(row["project"] in projects for row in rows)
            page_ids = [row["id"] for row in rows]
            if pages == 0:
                assert sparse_ids.isdisjoint(page_ids), "Sparse matches must be beyond the first page"
            observed.extend(page_ids)
            pages += 1
            assert pages <= 10, "Listing failed to finish its 500-row cursor walk"
            cursor = page["next"]
            if cursor is None:
                break
            assert isinstance(cursor, str) and cursor and cursor not in cursors
            cursors.add(cursor)
        assert pages == 10
        assert len(observed) == len(set(observed)) == 500
        assert set(observed) == seeded

        complete = cli.json("intake", "list", "--all", "--limit", "50")
        complete_ids = [row["id"] for row in complete["items"]]
        assert len(complete_ids) == len(set(complete_ids)) == 500
        assert set(complete_ids) == seeded
        assert complete["next"] is None

        sparse = cli.json("intake", "list", "--all", "--limit", "50",
                          "--search", "old sparse cli500 needle")
        assert {row["id"] for row in sparse["items"]} == sparse_ids
        assert {row["project"] for row in sparse["items"]} == set(projects)
        context = cli.json("intake", "list", "--all", "--limit", "50",
                           "--search", "cli500 context-only marker")
        assert [row["id"] for row in context["items"]] == [context_id]
    finally:
        server.stop()
        shutil.rmtree(root)


def _load_harness():
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "concurrency_node_harness", repo / "hub2/tests/e2e/harness.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Hub:
    """Isolated Node server + real host daemon + built CLI; always torn down by close()."""

    def __init__(self, harness):
        self.h = harness
        self.root = harness.scratch_root()
        self.server = harness.Server(self.root)
        self.host = None
        try:
            self.cli = None
            self.zipapp = harness.build_cli(self.root / "cli")
            self.server.start()
            token = harness.configure_hub(self.server)
            self.host = harness.Host(self.root, self.server, token)
            self.cli = harness.Cli(self.zipapp, self.root, self.server)
            self.host.start()
            harness.wait_for(self._connected, 30, label="host connected")
        except BaseException:
            self.close()
            raise

    def _connected(self):
        assert self.host.alive(), "host daemon exited; log:\n" + self.host.tail()
        rows = self.server.ok("GET", "/api/fleet/routing/machines")["machines"]
        return any(r.get("machine_id") == self.h.MACHINE and r.get("connected") is True for r in rows)

    def start_shell(self, command, *extra):
        return self.cli.run("sessions", "start", "--engine", "shell", "--workspace", str(self.host.workspace),
                            "--prompt", command, "--key", self.h.new_key(), *extra)

    def show(self, session_id):
        return self.cli.json("sessions", "show", session_id, "--json")["session"]

    def native_by_tag(self):
        return {row.get("tag"): row for row in self.host.native_rows()}

    def log_lines(self, pattern):
        text = self.host.log_path.read_text(errors="replace")
        return re.findall(pattern, text, flags=re.M)

    def close(self):
        try:
            if self.host:
                try:
                    self.host.control(start_delay_s=0)
                except OSError:
                    pass
                self.host.kill()
                self.host.kill_all_native()
        finally:
            try:
                self.server.stop()
            finally:
                shutil.rmtree(self.root, ignore_errors=True)


def _offer_result_counts(hub, session_id):
    offers = hub.log_lines(r"Host event type=session\.offer session=%s$" % re.escape(session_id))
    results = hub.log_lines(r"Host event complete session=%s$" % re.escape(session_id))
    return len(offers), len(results)


def _wait_offer_result_counts(hub, session_ids, expected=(1, 1), timeout=30):
    """Host logging is asynchronous to the server view: wait (monotonic deadline) for the
    exact per-session counts. Any count above the expected one fails at once, so a
    duplicate offer or result is never hidden by waiting."""
    deadline = time.monotonic() + timeout
    while True:
        counts = {session_id: _offer_result_counts(hub, session_id) for session_id in session_ids}
        over = {k: v for k, v in counts.items() if v[0] > expected[0] or v[1] > expected[1]}
        assert not over, "duplicate offer/result log lines %s\n%s" % (over, hub.host.tail(100))
        if all(v == expected for v in counts.values()):
            return counts
        assert time.monotonic() < deadline, "offer/result counts never reached %s: %s\n%s" % (
            expected, counts, hub.host.tail(100))
        time.sleep(0.1)


# The saga state a launch holds while the host's native `a start` handshake is still
# blocked: offered -> claimed by the host, no native id, no completion yet.
PENDING_DISPATCH_STATE = "claimed"


def _native_start_pids(tag):
    """Live stub `a start` handshake processes for this exact tag (Linux /proc)."""
    pids = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            status = (entry / "status").read_text()
        except OSError:
            continue
        argv = [a.decode(errors="replace") for a in argv if a]
        if "State:\tZ" in status or "start" not in argv or "--tag" not in argv:
            continue
        if any(a.endswith("/e2e/bin/a") for a in argv) and argv[argv.index("--tag") + 1] == tag:
            pids.append(int(entry.name))
    return pids


def _pending_evidence(hub, session_id, marker):
    tag = "task-hub2-" + session_id
    row = hub.show(session_id)
    return {
        "state": row.get("state"), "native_id": row.get("native_id"), "attention": row.get("attention"),
        "marker": marker.read_text().strip() if marker.exists() else None,
        "native_listed": tag in hub.native_by_tag(),
        "handshake_alive": bool(_native_start_pids(tag)),
        "offer_result": _offer_result_counts(hub, session_id),
    }


def _assert_genuinely_pending(hub, session_id, marker):
    """A sticky marker plus state != running is not enough: the native start may already
    have timed out (host runner limit 35 s) or failed, and the server keeps such a saga
    `claimed` (with attention) until its 120 s start deadline. Require the exact
    non-terminal dispatch state with no attention, a live handshake for this tag and
    offer/result counts (1, 0)."""
    evidence = _pending_evidence(hub, session_id, marker)
    assert evidence == {
        "state": PENDING_DISPATCH_STATE, "native_id": None, "attention": None, "marker": "task-hub2-" + session_id,
        "native_listed": False, "handshake_alive": True, "offer_result": (1, 0),
    }, "launch is not genuinely pending: %s\n%s" % (evidence, hub.host.tail(100))
    return evidence


def _legacy_pending_check(hub, session_id, marker):
    """The pre-fix predicate (sticky marker, no native row, state != running)."""
    return (marker.exists() and marker.read_text().strip() == "task-hub2-" + session_id
            and "task-hub2-" + session_id not in hub.native_by_tag()
            and hub.show(session_id)["state"] != "running")


def test_ten_barrier_released_cli_starts_all_reach_running_with_unique_ids():
    harness = _load_harness()
    hub = _Hub(harness)
    try:
        count = 10
        barrier = threading.Barrier(count)
        outcomes = [None] * count

        def worker(index):
            try:
                barrier.wait(timeout=60)
                outcomes[index] = hub.start_shell("printf CONCURRENT-%d; sleep 300" % index)
            except BaseException as error:  # reported by the main thread
                outcomes[index] = error

        threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(count)]
        began = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=240)
        elapsed = time.monotonic() - began
        assert not any(t.is_alive() for t in threads), "a CLI start never returned"
        created = []
        for index, result in enumerate(outcomes):
            assert not isinstance(result, BaseException), (index, repr(result))
            assert result.returncode == 0, "start %d rc=%s\n%s\n%s\nhost:\n%s" % (
                index, result.returncode, result.stdout[-1500:], result.stderr[-1500:], hub.host.tail())
            created.append(json.loads(result.stdout))
        ids = [row["id"] for row in created]
        assert len(set(ids)) == count, ids
        assert all(row.get("state") == "running" for row in created), [row.get("state") for row in created]

        deadline = time.monotonic() + 60
        shown = {}
        while len(shown) < count and time.monotonic() < deadline:
            for session_id in ids:
                row = hub.show(session_id)
                if row.get("state") == "running" and row.get("native_id"):
                    shown[session_id] = row
            time.sleep(0.5)
        assert len(shown) == count, "not all sessions running: %s" % sorted(set(ids) - set(shown))
        native_ids = [shown[i]["native_id"] for i in ids]
        assert len(set(native_ids)) == count, native_ids
        assert all(shown[i].get("machine_id") == harness.MACHINE for i in ids), shown
        natives = hub.native_by_tag()
        for session_id in ids:
            native = natives.get("task-hub2-" + session_id)
            assert native and native["phase"] == "running", (session_id, list(natives))
            assert native["id"] == shown[session_id]["native_id"]
            # The host isolates each launch in a worktree under the requested workspace.
            assert Path(native["workspace"]).resolve().is_relative_to(hub.host.workspace.resolve()), native
        listed = {row["id"]: row for row in hub.cli.json("sessions", "list", "--json")["sessions"]}
        for session_id in ids:
            row = listed[session_id]
            assert row.get("machine") == harness.MACHINE or row.get("machine_id") == harness.MACHINE, row
            assert row.get("workspace") and Path(row["workspace"]).resolve().is_relative_to(hub.host.workspace.resolve()), row
        _wait_offer_result_counts(hub, ids)
        print("ten concurrent starts running in %.2fs wall" % elapsed)
    finally:
        hub.close()


def test_real_cli_stop_is_confirmed_within_10s_while_another_launch_is_pending():
    harness = _load_harness()
    hub = _Hub(harness)
    try:
        running = json.loads(hub.start_shell("printf OWNED-RUNNING; sleep 300").stdout)
        owned_id = running["id"]
        owned = harness.wait_for(
            lambda: (lambda row: row if row.get("state") == "running" and row.get("native_id") else None)(hub.show(owned_id)),
            60, label="owned session running")
        assert hub.native_by_tag().get("task-hub2-" + owned_id), hub.host.native_rows()
        marker = hub.host.state / "start-pending"
        assert not marker.exists()

        # Every further native `a start` now blocks inside the stub (pending marker), as a slow launch.
        hub.host.control(start_delay_s=600)
        pending = hub.start_shell("printf PENDING; sleep 300", "--no-wait")
        assert pending.returncode == 0, (pending.stdout[-1500:], pending.stderr[-1500:])
        pending_id = json.loads(pending.stdout)["id"]
        harness.wait_for(marker.exists, 60, label="second launch blocked inside native start")
        before = _assert_genuinely_pending(hub, pending_id, marker)

        stop_started = time.monotonic()
        stop = hub.cli.run("sessions", "stop", owned_id, "--timeout", "10")
        stop_elapsed = time.monotonic() - stop_started
        assert stop.returncode == 0, (stop.stdout[-1500:], stop.stderr[-1500:], hub.host.tail())
        stopped = harness.wait_for(
            lambda: (lambda row: row if row.get("state") in ("stopped", "failed") else None)(hub.show(owned_id)),
            max(0.5, 10 - stop_elapsed), interval=0.2, label="stopped state")
        total = time.monotonic() - stop_started
        assert stopped["state"] == "stopped", stopped
        assert total < 10, "stop confirmed after %.2fs" % total
        # The other launch is still genuinely pending right after the confirmed STOP.
        after = _assert_genuinely_pending(hub, pending_id, marker)
        assert after == before
        assert "task-hub2-" + owned_id not in hub.native_by_tag(), "owned native still listed after stop"
        _wait_offer_result_counts(hub, [owned_id])

        # Mutation evidence for the old false positive: end the pending handshake so the
        # launch fails. The sticky marker stays and the legacy predicate still passes,
        # while the exact pending assertion rejects the failed launch.
        tag = "task-hub2-" + pending_id
        handshakes = _native_start_pids(tag)
        assert handshakes, "no live handshake to end for " + tag
        for pid in handshakes:
            os.kill(pid, signal.SIGKILL)
        _wait_offer_result_counts(hub, [pending_id])  # the host finished (failed) this offer
        failed = harness.wait_for(
            lambda: (lambda row: row if row.get("attention") else None)(hub.show(pending_id)),
            30, interval=0.2, label="pending launch failure recorded after handshake kill")
        assert failed["attention"]["reason"] == "native_start_refused", failed
        assert not failed.get("native_id"), failed
        # Observed today: the failed saga keeps the claimed state until its start deadline,
        # so the dispatch state alone would not distinguish it either.
        assert failed["state"] != "running", failed
        assert _legacy_pending_check(hub, pending_id, marker), "legacy predicate no longer reproduces the false positive"
        try:
            _assert_genuinely_pending(hub, pending_id, marker)
        except AssertionError:
            pass
        else:
            raise AssertionError("failed launch %s was accepted as pending" % failed)
        print("stop confirmed in %.2fs (cli %.2fs) with a pending launch; pending evidence %s; "
              "after handshake kill state=%s attention=%s" % (
                  total, stop_elapsed, before, failed["state"], failed["attention"]["reason"]))
    finally:
        hub.close()


# ---------------------------------------------------------------------------
# Real built-CLI contracts for the generic start flags, native output, typed
# Hub2 refusals and the build hint. Same stack as the concurrency tests above
# (production Node entry, real host daemon, stub `a`/`codex`, built zipapp,
# synthetic credentials); no provider, paid service or live Hub is contacted.
# ---------------------------------------------------------------------------

ALT, SOL, ASTRA = "gpt-e2e-alt", "gpt-6.1-sol", "gpt-6.1-astra"


@pytest.fixture
def plain_operator_env(monkeypatch):
    """A Hub worker shell exports its own launch identity; the CLI under test must run as the
    operator profile the harness configures, exactly as in CI."""
    for name in ("APLEXER_TAG", "APLEXER_WORKSPACE", "APLEXER_SESSION_ID", "HUB2_CONFIG"):
        monkeypatch.delenv(name, raising=False)


def _route_policy(hub):
    """Four controlled routes on the one synthetic account: default, a low-effort alternative,
    a deep-work model and a review-only model."""
    h, account = hub.h, hub.h.ACCOUNT
    version = hub.server.ok("GET", "/api/model-policy")["version"]
    hub.server.ok("PUT", "/api/model-policy", {"expected_version": version, "candidates": [
        {"id": "e2e-codex", "engine": "codex", "model": h.MODEL, "account_id": account, "weight": 1,
         "capabilities": [], "enabled": True},
        {"id": "e2e-alt", "engine": "codex", "model": ALT, "effort": "low", "account_id": account, "weight": 1,
         "capabilities": [], "enabled": True},
        {"id": "e2e-sol", "engine": "codex", "model": SOL, "effort": "medium", "account_id": account, "weight": 1,
         "capabilities": [], "enabled": True},
        {"id": "e2e-astra", "engine": "codex", "model": ASTRA, "effort": "medium", "account_id": account,
         "weight": 1, "capabilities": ["review-only"], "enabled": True}]})


def _model_start(hub, *flags, workspace=None):
    return hub.cli.run("sessions", "start", "--workspace", workspace or str(hub.host.workspace),
                       "--prompt", "E2E flag contract brief", "--key", hub.h.new_key(), *flags)


def _running(hub, result):
    assert result.returncode == 0, (result.stdout[-1500:], result.stderr[-1500:], hub.host.tail(40))
    created = json.loads(result.stdout)
    row = hub.h.wait_for(
        lambda: (lambda r: r if r.get("state") == "running" and r.get("native_id") else None)(hub.show(created["id"])),
        60, label="session running with a native id")
    native = hub.native_by_tag()["task-hub2-" + created["id"]]
    assert native["id"] == row["native_id"] and native["phase"] == "running", native
    return created, row, native


def _list_cells(hub, session_id):
    """Cells of one row of the human `hub sessions list` table, cut at the header's column offsets."""
    result = hub.cli.run("sessions", "list")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    header = lines[0]
    names = header.split()
    starts = [header.index(name) for name in names]
    row = next(line for line in lines if session_id in line)
    return {name: row[start:(starts[i + 1] if i + 1 < len(starts) else None)].strip()
            for i, (name, start) in enumerate(zip(names, starts))}


def _session_ids(hub):
    return {row["id"] for row in hub.cli.json("sessions", "list", "--json")["sessions"]}


def test_real_cli_start_flags_change_route_cwd_native_output_and_policy(plain_operator_env):
    harness = _load_harness()
    hub = _Hub(harness)
    try:
        _route_policy(hub)
        subdir = hub.host.workspace / "sub"
        subdir.mkdir()

        # Resolve a registered label with no --cwd override. A wrong label must
        # fail before native execution; otherwise an ignored label could pass.
        label = hub.host.workspace.name
        label_result = _model_start(hub, "--model", ALT, "--effort", "low", workspace=label)
        label_created, label_row, label_native = _running(hub, label_result)
        assert label_row["requested_workspace"] == label, label_row
        assert Path(label_native["cwd"]).resolve().is_relative_to(hub.host.workspace.resolve()), label_native
        assert not Path(label_native["cwd"]).resolve().is_relative_to(subdir.resolve()), label_native
        assert Path(label_row["workspace"]).resolve() == Path(label_native["cwd"]).resolve(), label_row
        native_before = set(hub.native_by_tag())
        missing_label = _model_start(hub, "--model", ALT, "--effort", "low", workspace="missing-label-e2e")
        assert missing_label.returncode != 0, missing_label.stdout
        assert "workspace_label_unavailable" in missing_label.stdout + missing_label.stderr
        assert "stage=host_launch" in missing_label.stderr, missing_label.stderr
        assert set(hub.native_by_tag()) == native_before

        # --cwd deliberately overrides the label, independently of the label-only case.
        started = _model_start(hub, "--model", ALT, "--effort", "low", "--cwd", str(subdir), workspace=label)
        created, row, native = _running(hub, started)
        assert (created["engine"], created["model"], created["effort"], created["policy_id"]) == (
            "codex", ALT, "low", "e2e-alt"), created
        assert [item["policy_id"] for item in created["picker"]["pool"]] == ["e2e-alt"], created["picker"]
        assert (row["model"], row["effort"], row["machine_id"]) == (ALT, "low", harness.MACHINE), row
        # Human output: picked route and the native pane id, on stderr (stdout stays the JSON receipt).
        assert "Picked route: codex/%s/low (policy e2e-alt, host %s)" % (ALT, harness.MACHINE) in started.stderr
        assert "Native pane: " + row["native_id"] in started.stderr, started.stderr
        # The accepted flags reached native execution: engine args and an isolated cwd under --cwd.
        argv = native["command"]
        assert "--engine-arg=" + ALT in argv and '--engine-arg=model_reasoning_effort="low"' in argv, argv
        assert Path(native["cwd"]).resolve().is_relative_to(subdir.resolve()), native["cwd"]
        assert Path(row["workspace"]).resolve() == Path(native["cwd"]).resolve()
        assert Path(row["requested_workspace"]).resolve() == subdir.resolve(), row
        capture = hub.cli.run("sessions", "capture", created["id"], "--screen", "--timeout", "60")
        assert capture.returncode == 0 and "STUB-CODEX-OK model=" + ALT in capture.stdout, capture.stdout[-1000:]
        # Human NATIVE column carries exactly the native id the host runs.
        cells = _list_cells(hub, created["id"])
        assert cells["NATIVE"] == native["id"] and cells["MODEL"] == ALT and cells["EFFORT"] == "low", cells
        assert cells["HOST"] == harness.MACHINE and cells["STATE"] == "running", cells

        # Historical short intake show resolves to the full record and its dispatch evidence.
        intake_id = created["intake_id"]
        short = hub.cli.json("intake", "show", intake_id[:8])["intake"]
        assert short["id"] == intake_id and short["session_id"] == created["id"], short
        assert short["dispatch"]["route"] == "codex/%s/low" % ALT and short["dispatch"]["native_id"] == native["id"]

        # Negative effect: the same flags on a model whose route has another effort select nothing,
        # fail typed, and leave no session behind.
        before = _session_ids(hub)
        refused = _model_start(hub, "--model", harness.MODEL, "--effort", "low")
        assert refused.returncode != 0 and "model_policy_unavailable" in refused.stderr + refused.stdout, (
            refused.stdout[-800:], refused.stderr[-800:])
        assert "Traceback" not in refused.stderr
        assert _session_ids(hub) == before
    finally:
        hub.close()


def test_real_cli_purpose_gates_review_only_model_and_tier_weights_the_pick(plain_operator_env):
    harness = _load_harness()
    hub = _Hub(harness)
    try:
        _route_policy(hub)
        before = _session_ids(hub)
        # purpose=implement (the default) can never draw the review-only model, even when named.
        refused = _model_start(hub, "--model", ASTRA)
        assert refused.returncode != 0 and "astra_not_for_implementation" in refused.stderr + refused.stdout, (
            refused.stdout[-800:], refused.stderr[-800:])
        assert _session_ids(hub) == before, "a refused implement start left a session"
        explicit = _model_start(hub, "--model", ASTRA, "--purpose", "implement")
        assert explicit.returncode != 0 and "astra_not_for_implementation" in explicit.stderr + explicit.stdout
        # purpose=review makes that exact model eligible and it really runs natively.
        created, row, native = _running(hub, _model_start(hub, "--model", ASTRA, "--purpose", "review"))
        assert (created["model"], created["effort"], created["policy_id"]) == (ASTRA, "medium", "e2e-astra"), created
        assert "--engine-arg=" + ASTRA in native["command"], native["command"]
        capture = hub.cli.run("sessions", "capture", created["id"], "--screen", "--timeout", "60")
        assert capture.returncode == 0 and "STUB-CODEX-OK model=" + ASTRA in capture.stdout, capture.stdout[-1000:]

        # tier: public picker evidence. No tier -> equal shares; deep -> the deep-work model outweighs the rest.
        plain = {r["policy_id"]: r["share"] for r in hub.cli.json("sessions", "pick", "--json")["pool"]}
        deep = {r["policy_id"]: r["share"] for r in hub.cli.json("sessions", "pick", "--tier", "deep", "--json")["pool"]}
        assert set(plain) == set(deep) == {"e2e-codex", "e2e-alt", "e2e-sol"}, (plain, deep)
        assert max(plain.values()) - min(plain.values()) < 1e-9, plain
        assert deep["e2e-sol"] == pytest.approx(0.576, abs=0.005), deep
        assert deep["e2e-codex"] == deep["e2e-alt"] < plain["e2e-sol"] < deep["e2e-sol"], (plain, deep)
        human = hub.cli.run("sessions", "pick", "--tier", "deep").stdout
        assert re.search(r"pool\s+e2e-sol\s+57\.\d%", human), human
        # The same tier on a start is recorded in the accepted route's pool weight (3x for the deep model).
        flat = json.loads(_model_start(hub, "--model", SOL, "--no-wait").stdout)["picker"]["pool"][0]
        deeper = json.loads(_model_start(hub, "--model", SOL, "--tier", "deep", "--no-wait").stdout)["picker"]["pool"][0]
        assert flat["policy_id"] == deeper["policy_id"] == "e2e-sol"
        assert deeper["weight"] == pytest.approx(3 * flat["weight"]), (flat, deeper)
        invalid = hub.cli.run("sessions", "start", "--workspace", str(hub.host.workspace), "--prompt", "x",
                              "--key", harness.new_key(), "--tier", "bogus")
        assert invalid.returncode == 2 and "invalid choice" in invalid.stderr, invalid.stderr[-500:]
    finally:
        hub.close()


class _ManifestServer:
    """Loopback stand-in for the public /cli/manifest.json; records every path requested."""

    def __init__(self, commit):
        outer = self
        self.paths = []
        self.commit = commit

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                outer.paths.append(self.path)
                body = json.dumps({"built_from_commit": outer.commit}).encode() \
                    if self.path == "/cli/manifest.json" else b"{}"
                self.send_response(200 if self.path == "/cli/manifest.json" else 404)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.origin = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


def test_real_cli_typed_hub2_refusals_and_fresh_versus_stale_build_hint(plain_operator_env):
    harness = _load_harness()
    root = harness.scratch_root()
    manifests = []
    try:
        zipapp = harness.build_cli(root / "cli")
        built = json.loads((root / "cli/manifest.json").read_text())["built_from_commit"]
        assert re.fullmatch("[0-9a-f]{40}", built), built
        version = harness.Cli(zipapp, root, types.SimpleNamespace(origin="http://127.0.0.1:9")).run("--version")
        assert version.returncode == 0 and built[:12] in version.stdout, "zipapp is not stamped with its commit"

        def cli_for(server):
            """A fresh isolated HOME per CLI, pointed at the loopback manifest instead of a Hub."""
            home_root = root / ("home-%d" % len(manifests))
            home_root.mkdir()
            manifests.append(server)
            return harness.Cli(zipapp, home_root, types.SimpleNamespace(origin=server.origin))

        def run(manifest_commit):
            server = _ManifestServer(manifest_commit)
            return cli_for(server).run("webhooks", "list"), server

        # Fresh: the deployed manifest names this very build -> no hint. The Hub1-only route is refused
        # on the client with the typed code, never as a raw 404 or traceback, and never leaves the process.
        fresh, fresh_server = run(built)
        text = fresh.stdout + fresh.stderr
        assert fresh.returncode != 0 and "not_supported_on_hub2" in text, text
        assert "Traceback" not in text and "404" not in text and "not found" not in text.lower(), text
        assert "differs from the deployed CLI" not in text and "hub update" not in text, text
        assert fresh_server.paths == ["/cli/manifest.json"], fresh_server.paths

        # Stale: a different deployed build -> exactly one note naming both short commits and the fix.
        other = "0123456789abcdef" * 2 + "01234567"
        assert other != built
        stale, stale_server = run(other)
        stale_text = stale.stdout + stale.stderr
        note = "Note: this hub CLI build (%s) differs from the deployed CLI (%s); run 'hub update'." % (
            built[:12], other[:12])
        assert stale_text.count(note) == 1 and note in stale.stderr, stale_text
        assert stale.returncode != 0 and "not_supported_on_hub2" in stale_text and "Traceback" not in stale_text
        assert stale_server.paths == ["/cli/manifest.json"], stale_server.paths

        # Another retired Hub1 family gets its own hint, through the same typed refusal.
        prompts = cli_for(fresh_server).run("prompts", "list")
        assert prompts.returncode != 0 and "not_supported_on_hub2; /api/prompts" in prompts.stdout + prompts.stderr
        assert "Traceback" not in prompts.stderr
        assert set(fresh_server.paths) == {"/cli/manifest.json"}, fresh_server.paths
    finally:
        for server in manifests:
            server.close()
        shutil.rmtree(root, ignore_errors=True)


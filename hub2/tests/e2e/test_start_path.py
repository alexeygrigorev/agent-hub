"""Integrated Hub2 start-path test (fix plan item 1.1, RC1).

Real Node server (hub2/node/main.ts) + real Python host daemon (hub2/host)
+ real `hub` CLI zipapp (host/), with only the aplexer `a` binary and the
`codex` engine stubbed (hub2/tests/e2e/bin). Each side is exercised against
the other instead of against a hand-written fake, so protocol drift such as
a `heartbeat(resources=...)` vs `native_sessions=` mismatch turns this red.

Assertions that depend on later plan items go through `later_plan_item` or
`xfail(strict=False)` with the plan item: they xfail today and pass once fixed.
Plan: ~/tmp/agent-reports/2026-11-10-fable-hub-audit.md.
"""
import json
import shutil
import time

import pytest

from harness import (MACHINE, MODEL, Cli, Host, Server, build_cli, configure_hub, new_key, scratch_root,
                     wait_for)

BRIEF = "E2E start-path brief: print a marker and keep running"
START_DEADLINE_S = 60      # accept -> running on an idle loopback host
STOP_DEADLINE_S = 60       # stop -> native exit confirmed
RECONNECT_DEADLINE_S = 45  # server restart -> host connected again
# Server rule today: a claimed session without a native id fails after 120 s
# (session-liveness.ts); plan 2.3 keeps one 120 s start deadline.
LAUNCH_FAIL_DEADLINE_S = 150


@pytest.fixture(scope="session")
def zipapp():
    root = scratch_root()
    try:
        yield build_cli(root / "cli")
    finally:
        shutil.rmtree(root, ignore_errors=True)


class Hub:
    """One isolated Hub: server, configured operator state, enrolled host, CLI."""

    def __init__(self, zipapp, start_host=True):
        self.root = scratch_root()
        self.server = Server(self.root)
        self.host = None
        try:
            self.server.start()
            self.host_token = configure_hub(self.server)
            self.host = Host(self.root, self.server, self.host_token)
            self.cli = Cli(zipapp, self.root, self.server)
            if start_host:
                self.start_host()
        except BaseException:
            self.close()
            raise

    def start_host(self):
        self.host.start()
        self.wait_connected()

    def machine(self):
        rows = self.server.ok("GET", "/api/fleet/routing/machines")["machines"]
        return next((row for row in rows if row.get("machine_id") == MACHINE), {})

    def wait_connected(self, timeout=30):
        def connected():
            assert self.host.alive(), "host daemon exited before connecting; log:\n" + self.host.tail()
            return self.machine().get("connected") is True
        wait_for(connected, timeout, label="host %s connected" % MACHINE)

    def show(self, session_id):
        return self.cli.json("sessions", "show", session_id, "--json")["session"]

    def wait_state(self, session_id, states, timeout, check_host=True):
        def reached():
            if check_host:
                assert self.host.alive(), "host daemon exited; log:\n" + self.host.tail()
            row = self.show(session_id)
            return row if row.get("state") in states else None
        try:
            return wait_for(reached, timeout, interval=1, label="session %s in %s" % (session_id, states))
        except AssertionError as error:
            raise AssertionError("%s\nlast=%s\nhost log:\n%s\nserver log:\n%s" % (
                error, json.dumps(self.show(session_id)), self.host.tail(), self.server.tail())) from None

    def start(self, *extra):
        result = self.cli.run("sessions", "start", "--workspace", str(self.host.workspace),
                              "--prompt", BRIEF, "--key", new_key(), *extra)
        return result

    def start_ok(self):
        result = self.start()
        assert result.returncode == 0, "model-less start failed rc=%s\n%s\n%s" % (
            result.returncode, result.stdout[-2000:], result.stderr[-2000:])
        created = json.loads(result.stdout)
        assert created.get("id"), created
        return created

    def native_row(self, session_id):
        return next((row for row in self.host.native_rows() if row.get("tag") == "task-hub2-" + session_id), None)

    def close(self):
        try:
            if self.host:
                self.host.kill()
                self.host.kill_all_native()
        finally:
            self.server.stop()
            shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def hub(zipapp):
    instance = Hub(zipapp)
    yield instance
    instance.close()


@pytest.fixture
def hub_without_host(zipapp):
    instance = Hub(zipapp, start_host=False)
    yield instance
    instance.close()


def list_table_row(hub, session_id):
    result = hub.cli.run("sessions", "list")
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    header = lines[0]
    columns = header.split()
    starts = [header.index(name) for name in columns]
    row = next((line for line in lines if session_id in line), None)
    assert row, "session %s missing from `hub sessions list`:\n%s" % (session_id, result.stdout)
    cells = {}
    for index, name in enumerate(columns):
        end = starts[index + 1] if index + 1 < len(starts) else None
        cells[name] = row[starts[index]:end].strip()
    return cells


def test_model_less_start_list_show_capture_stop(hub):
    created = hub.start_ok()
    session_id = created["id"]
    assert created.get("engine") == "codex" and created.get("model") == MODEL, created
    assert created.get("machine_id") == MACHINE, created

    running = hub.wait_state(session_id, {"running"}, START_DEADLINE_S)
    native = hub.native_row(session_id)
    assert native and native["phase"] == "running", hub.host.native_rows()
    assert running.get("native_id") == native["id"], (running, native)
    assert running.get("machine_id") == MACHINE

    listed = hub.cli.json("sessions", "list", "--json")
    row = next((s for s in listed["sessions"] if s.get("id") == session_id), None)
    assert row and row.get("state") == "running", listed
    cells = list_table_row(hub, session_id)
    assert cells["STATE"] == "running" and cells["ENGINE"] == "codex", cells

    capture = hub.cli.run("sessions", "capture", session_id, "--screen", "--timeout", "60")
    assert capture.returncode == 0, (capture.stdout[-2000:], capture.stderr[-2000:])
    assert "STUB-CODEX-OK model=" + MODEL in capture.stdout, capture.stdout[-2000:]

    stop = hub.cli.run("sessions", "stop", session_id, "--timeout", str(STOP_DEADLINE_S))
    assert stop.returncode == 0, (stop.stdout[-2000:], stop.stderr[-2000:])
    assert stop.elapsed < STOP_DEADLINE_S + 10, stop.elapsed
    stopped = hub.wait_state(session_id, {"stopped", "failed", "completed-awaiting-review", "done", "exited"},
                             STOP_DEADLINE_S)
    assert stopped["state"] == "stopped", stopped
    assert hub.native_row(session_id) is None, "native workload still listed after stop: %s" % hub.host.native_rows()


def later_plan_item(item, reason, check):
    """Run an assertion that only a later plan item makes true: pass when it holds, else xfail (non-strict)."""
    try:
        check()
    except AssertionError as error:
        pytest.xfail("plan %s: %s (%s)" % (item, reason, str(error).splitlines()[0][:300]))


def test_list_shows_host_and_workspace(hub):
    session_id = hub.start_ok()["id"]
    hub.wait_state(session_id, {"running"}, START_DEADLINE_S)
    cells = list_table_row(hub, session_id)

    def columns_filled():
        assert cells["HOST"] == MACHINE, cells
        assert cells["WORKSPACE"] not in ("", "-"), cells
    later_plan_item("3.2", "`hub sessions list` HOST/WORKSPACE are empty for Hub2 rows (CLI reads machine/workspace)",
                    columns_filled)


def test_start_wait_without_connected_host_fails_within_deadline(hub_without_host):
    hub = hub_without_host
    assert hub.machine().get("connected") is not True
    result = hub.start("--wait", "--timeout", "20", "--interval", "2")
    assert result.returncode != 0, "start --wait reported success with no connected host:\n" + result.stdout
    assert result.elapsed < 40, "start --wait did not give up within its deadline: %.1fs" % result.elapsed
    # Today the CLI only says its wait timed out; the typed reason is plan 2.3/2.4 (next test).
    assert "session_start_failed" in result.stderr or "timed out" in result.stderr, result.stderr[-2000:]


@pytest.mark.xfail(strict=False, reason="plan 2.3: POST /api/sessions must refuse immediately with 503 no_connected_host instead of offering to a disconnected host")
def test_start_without_connected_host_is_refused_typed(hub_without_host):
    hub = hub_without_host
    result = hub.start()
    assert result.elapsed < 10, result.elapsed
    assert result.returncode != 0, "start accepted with no connected host:\n" + result.stdout
    assert "no_connected_host" in result.stderr + result.stdout, result.stderr[-2000:]


def test_unclaimed_offer_reaches_terminal_failure(hub_without_host):
    hub = hub_without_host
    result = hub.start()
    if result.returncode != 0:
        assert "no_connected_host" in result.stderr + result.stdout, result.stderr[-2000:]
        return  # plan 2.3 refusal path: nothing was offered at all
    session_id = json.loads(result.stdout)["id"]

    def terminal():
        row = hub.wait_state(session_id, {"failed"}, LAUNCH_FAIL_DEADLINE_S, check_host=False)
        assert "host_no_response" in json.dumps(row), row
    later_plan_item("2.3", "an unclaimed offer must end failed host_no_response within the start deadline "
                    "(today it loops offered/expired)", terminal)


def test_host_killed_mid_launch_fails_within_deadline(hub):
    # The stub engine holds its first action, so the host is inside launch when it dies.
    hub.host.control(first_action_delay_s=600)
    session_id = hub.start_ok()["id"]
    wait_for(lambda: hub.native_row(session_id), START_DEADLINE_S, label="native workload spawned")
    assert hub.show(session_id)["state"] in ("claimed", "offered"), hub.show(session_id)
    hub.host.kill()

    def failed_in_time():
        row = hub.wait_state(session_id, {"failed", "stopped", "lost"}, LAUNCH_FAIL_DEADLINE_S, check_host=False)
        assert row["state"] == "failed", row
    later_plan_item("2.3", "a claimed session whose host died mid-launch must fail (launch_timeout_no_native) "
                    "within the 120 s start deadline; it stays claimed", failed_in_time)


def test_server_restart_mid_session_host_reconnects(hub):
    session_id = hub.start_ok()["id"]
    hub.wait_state(session_id, {"running"}, START_DEADLINE_S)
    hub.server.restart()
    hub.wait_connected(timeout=RECONNECT_DEADLINE_S)
    assert hub.host.alive(), hub.host.tail()
    row = hub.show(session_id)
    assert row["state"] == "running", row
    listed = hub.cli.json("sessions", "list", "--json")
    assert any(s.get("id") == session_id for s in listed["sessions"]), listed
    stop = hub.cli.run("sessions", "stop", session_id, "--timeout", str(STOP_DEADLINE_S))
    assert stop.returncode == 0, (stop.stdout[-2000:], stop.stderr[-2000:])
    assert hub.wait_state(session_id, {"stopped", "failed"}, STOP_DEADLINE_S)["state"] == "stopped"

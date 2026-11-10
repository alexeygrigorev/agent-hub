"""Integrated Hub2 start-path test (fix plan item 1.1, RC1).

Real Node server (hub2/node/main.ts) + real Python host daemon (hub2/host)
+ real `hub` CLI zipapp (host/), with only the aplexer `a` binary and the
`codex` engine stubbed (hub2/tests/e2e/bin). Each side is exercised against
the other instead of against a hand-written fake, so protocol drift such as
a `heartbeat(resources=...)` vs `native_sessions=` mismatch turns this red.

Every assertion is strict and states the target behaviour of fix plan
items 2.3 (typed, bounded start failures) and 3.2 (HOST/WORKSPACE in the
list); there is no xfail or skip. Plan: ~/tmp/agent-reports/2026-11-10-fable-hub-audit.md.
"""
import json
import shutil
import time

import pytest

from harness import (MACHINE, MODEL, Cli, Host, Server, build_cli, configure_hub, new_key, scratch_root,
                     stale_heartbeat_host_copy, wait_for)

BRIEF = "E2E start-path brief: print a marker and keep running"
START_DEADLINE_S = 60      # accept -> running on an idle loopback host
STOP_DEADLINE_S = 60       # stop -> native exit confirmed
RECONNECT_DEADLINE_S = 45  # server restart -> host connected again
# Plan 2.3: one 120 s start deadline from ACCEPTANCE (POST /api/sessions 2xx) to
# native id. Measured from t0 = the server's accepted timestamp when the response
# or session view carries one, else the client clock right after the 2xx.
START_DEADLINE_FROM_ACCEPT_S = 120
# Ruling (plan owner for 1.1, 2026-11-10): the server deadline is exactly 120 s after
# acceptance, and the typed terminal state is recorded when it expires; the alarm may
# fire after the deadline but no later than deadline + ALARM_SLACK_S.
ALARM_SLACK_S = 5
DEADLINE_FIELDS = ("start_deadline_at", "start_deadline", "deadline_at", "deadline_ms")
NO_HOST_REFUSAL_S = 2      # plan 2.3: no connected host -> 503 no_connected_host in <2 s


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
        self.accepted_client_ms = int(time.time() * 1000)
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


def iso_ms(value):
    from datetime import datetime
    if isinstance(value, (int, float)) and value > 10**12:
        return int(value)
    if isinstance(value, str) and value:
        try:
            return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
        except ValueError:
            return None
    return None


def acceptance_ms(hub, created):
    """t0: server accepted timestamp from the start response or session view, else client time after the 2xx."""
    for source in (created, hub.show(created["id"])):
        for key in ("accepted_at", "created_at"):
            stamp = iso_ms(source.get(key))
            if stamp:
                return stamp, "server:" + key
    return hub.accepted_client_ms, "client_after_2xx"


def stated_deadline_ms(*sources):
    for source in sources:
        for container in (source, source.get("attention") or {}, source.get("launch") or {}):
            for key in DEADLINE_FIELDS:
                stamp = iso_ms(container.get(key))
                if stamp:
                    return stamp, key
    return None, None


def assert_failed_within_start_deadline(hub, created, reason):
    """Deadline = acceptance + 120 s exactly; failed with attention.reason == reason in [deadline, deadline + 5 s]."""
    session_id = created["id"]
    t0, source = acceptance_ms(hub, created)
    until = t0 / 1000 + START_DEADLINE_FROM_ACCEPT_S + ALARM_SLACK_S + 3
    row = hub.show(session_id)
    while row.get("state") not in ("failed", "stopped", "lost", "running", "completed-awaiting-review") \
            and time.time() < until:
        time.sleep(1)
        row = hub.show(session_id)
    attention = row.get("attention") or {}
    observed = iso_ms(attention.get("observed_at"))
    deadline, field = stated_deadline_ms(created, row)
    detail = "t0=%s (%s) deadline=%s (%s) observed_at=%s row=%s\nhost log:\n%s\nserver log:\n%s" % (
        t0, source, deadline, field, observed, json.dumps(row), hub.host.tail(), hub.server.tail())
    assert row.get("state") == "failed", "not failed by deadline + %ss: %s" % (ALARM_SLACK_S, detail)
    assert attention.get("reason") == reason, "attention.reason != %s: %s" % (reason, detail)
    assert observed, "failed row has no attention.observed_at: " + detail
    if deadline is not None:
        assert abs(deadline - t0 - START_DEADLINE_FROM_ACCEPT_S * 1000) < 1000, \
            "server deadline is not acceptance + 120 s: " + detail
    else:
        # GAP: the API does not expose the start deadline; pin it to acceptance + 120 s instead.
        print("GAP: no start deadline field (%s) on the start response or session view" % ", ".join(DEADLINE_FIELDS))
        deadline = t0 + START_DEADLINE_FROM_ACCEPT_S * 1000
    assert deadline <= observed <= deadline + ALARM_SLACK_S * 1000, \
        "terminal observation outside [deadline, deadline + %ss]: %s" % (ALARM_SLACK_S, detail)
    print("%s: failed %s at deadline%+.3fs (deadline field=%s, t0 %s)" % (
        session_id, reason, (observed - deadline) / 1000, field or "GAP", source))
    return row


def test_list_shows_host_and_workspace_for_every_row(hub):
    session_id = hub.start_ok()["id"]
    hub.wait_state(session_id, {"running"}, START_DEADLINE_S)
    cells = list_table_row(hub, session_id)
    assert cells["HOST"] == MACHINE, cells
    assert cells["WORKSPACE"] not in ("", "-"), cells
    for row in hub.cli.json("sessions", "list", "--json")["sessions"]:
        assert row.get("machine") == MACHINE or row.get("machine_id") == MACHINE, row
        assert row.get("workspace"), row


def test_start_without_connected_host_is_refused_typed(hub_without_host):
    hub = hub_without_host
    assert hub.machine().get("connected") is not True
    result = hub.start()
    assert result.returncode != 0, "start accepted with no connected host:\n" + result.stdout
    assert "no_connected_host" in result.stderr + result.stdout, (result.stdout[-2000:], result.stderr[-2000:])
    direct_started = time.monotonic()
    direct = hub.server.api("POST", "/api/sessions", {"key": new_key(), "workspace": str(hub.host.workspace),
                                                      "repo": "e2e-owner/e2e-repo", "brief": BRIEF})
    assert time.monotonic() - direct_started < NO_HOST_REFUSAL_S, "refusal took too long"
    assert direct.status == 503 and direct.body.get("error") == "no_connected_host", direct
    shell = hub.start("--engine", "shell", "--reason", "e2e: explicit shell start must not bypass the connected-host check")
    assert shell.returncode != 0, "explicit shell start accepted with no connected host:\n" + shell.stdout
    assert "no_connected_host" in shell.stderr + shell.stdout, (shell.stdout[-2000:], shell.stderr[-2000:])
    shell_started = time.monotonic()
    direct_shell = hub.server.api("POST", "/api/sessions", {
        "key": new_key(), "workspace": str(hub.host.workspace), "repo": "e2e-owner/e2e-repo", "brief": BRIEF,
        "engine": "shell", "engine_override_reason": "e2e: explicit shell start must not bypass the connected check"})
    assert time.monotonic() - shell_started < NO_HOST_REFUSAL_S, "shell refusal took too long"
    assert direct_shell.status == 503 and direct_shell.body.get("error") == "no_connected_host", direct_shell
    assert hub.server.ok("GET", "/api/sessions?limit=100")["sessions"] == [], "a refused start left a session row"


def test_unclaimed_offer_fails_host_no_response(hub):
    # A hung daemon: socket stays open, nothing claims the offer.
    hub.host.freeze()
    created = hub.start_ok()
    session_id = created["id"]
    assert created.get("state") in ("offered", "staged", "pending"), created
    assert_failed_within_start_deadline(hub, created, "host_no_response")


def test_host_killed_mid_launch_fails_typed(hub):
    # `a start` blocks inside the daemon, so the host dies before any native id exists.
    hub.host.control(start_delay_s=600)
    created = hub.start_ok()
    session_id = created["id"]
    wait_for(lambda: (hub.host.state / "start-pending").exists(), START_DEADLINE_S, label="host inside `a start`")
    assert hub.show(session_id)["state"] in ("claimed", "admitted"), hub.show(session_id)
    hub.host.kill()
    assert_failed_within_start_deadline(hub, created, "launch_timeout_no_native")
    assert hub.native_row(session_id) is None, hub.host.native_rows()


def test_stale_heartbeat_signature_is_caught(zipapp):
    """Mutation proof: a host whose Client.heartbeat lost `native_sessions=` (the stale
    build/lib copy) must fail this harness, so the start-path test cannot silently pass."""
    hub = Hub(zipapp, start_host=False)
    try:
        hub.host.source = stale_heartbeat_host_copy(hub.root)
        hub.host.start()
        wait_for(lambda: "unexpected keyword argument 'native_sessions'" in hub.host.tail(200), 30,
                 label="stale heartbeat TypeError in the host log")
        with pytest.raises(AssertionError):
            hub.wait_connected(timeout=20)
        assert hub.machine().get("connected") is not True, hub.host.tail()
    finally:
        hub.close()


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

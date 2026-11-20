"""Offline tests for the hub start canary dev-alert occurrence observer
integration (scripts/hub_start_canary.py: main/_managed_observer,
_CanaryAttempt/apply_alerting, replacing the record_dev_alert_occurrence
no-op).

The managed observer authority is always a clearly simulated in-process
stand-in (SimulatedObserver / RaisingObserver / NegativeAckObserver below)
implementing the bounded observer contract emit(phase, **fields) -> bool plus
fail()/dirty. No real broker, identity, provider, Hub instance or SMTP is ever
contacted: mail renders to an owned sink and canary state lives in an owned
temp HUB_CANARY_STATE_DIR.

Emitted decision vocabulary is validated against the REAL existing producer
contract in scripts/hub_alert_client.py (imported stdlib-only, never
connected): decision must be one of ram.DECISIONS (only alert/alert_dedup/
recovery increment occurrences; observation is coverage-only), condition must
be one of the fixed CONDITIONS literals, recipient_class is dev/none. The
attempt lifecycle follows the alert authority's canary rule: begin, exactly
one decision, terminal per run. Natural (unmanaged) invocations are covered to
stay observation-silent.

Run: scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- python3 -m unittest host.tests.test_cli_canary_alert_occurrence
"""
import contextlib, io, json, os, pathlib, shlex, shutil, sys, tempfile, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import hub_start_canary as canary  # noqa: E402
from scripts import hub_alert_client as alert_client  # noqa: E402

SINK = '#!/usr/bin/env bash\n{ printf "ARGS\\t%s\\n" "$*"; cat; printf "\\n---END---\\n"; } >> "$MAIL_SINK"\n[[ "${MAIL_FAIL:-0}" != 1 ]]\n'


def verdict(mode="shell", ok=False, stage="start_cli", reason="boom", run_id="run-000000", **extra):
    result = {"ok": ok, "mode": mode, "stage": stage, "reason": reason,
              "timestamp": "2026-11-20T04:00:00Z", "run_id": run_id, "cleanup": "clean"}
    result.update(extra)
    return result


class SimulatedObserver:
    """Clearly simulated managed observer authority (test double for the bounded
    managed observer contract; never a real broker or identity)."""

    def __init__(self):
        self.events = []
        self.dirty = False

    def emit(self, phase, **fields):
        self.events.append({"phase": phase, **fields})
        return True

    def fail(self):
        self.dirty = True

    def decisions(self):
        return [e["decision"] for e in self.events if e["phase"] == "decision"]

    def decision_events(self):
        return [e for e in self.events if e["phase"] == "decision"]

    def assert_valid_contract(self, testcase):
        """Every recorded decision must satisfy the real producer contract:
        counted decisions (alert/alert_dedup/recovery) increment occurrences,
        observation never does; conditions are fixed literals."""
        for event in self.events:
            testcase.assertIn(event["phase"], ("begin", "decision", "terminal"))
            if event["phase"] != "decision":
                testcase.assertEqual(set(event), {"phase"})
                continue
            testcase.assertIn(event["decision"], alert_client.ram.DECISIONS)
            testcase.assertIn(event["condition"], alert_client.CONDITIONS)
            testcase.assertIn(event["recipient_class"], ("dev", "none"))


class RaisingObserver(SimulatedObserver):
    """Simulated observer whose emit always fails (failure/timeout evidence)."""

    def __init__(self, error=None):
        super().__init__()
        self.error = error or RuntimeError("simulated observer failure")

    def emit(self, phase, **fields):
        raise self.error


class NegativeAckObserver(SimulatedObserver):
    """Simulated observer that answers every emit with a negative ACK."""

    def emit(self, phase, **fields):
        self.events.append({"phase": phase, "negative_ack": True, **fields})
        self.fail()
        return False


class OccurrenceObserverTest(unittest.TestCase):
    def setUp(self):
        base = ROOT / ".local" / "tmp"
        base.mkdir(parents=True, exist_ok=True)
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="canary-observer-test.", dir=base))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sink = self.tmp / "mail.sink"
        mailer = self.tmp / "mailer.sh"
        mailer.write_text(SINK)
        mailer.chmod(0o755)
        env_patch = mock.patch.dict(os.environ, {
            "HUB_CANARY_MAILER": str(mailer), "MAIL_SINK": str(self.sink),
            "HUB_CANARY_STATE_DIR": str(self.tmp / "state"),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def mails(self):
        return self.sink.read_text().split("---END---")[:-1] if self.sink.exists() else []

    def state(self, mode="shell"):
        path = pathlib.Path(os.environ["HUB_CANARY_STATE_DIR"]) / f"state-{mode}.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def seed_state(self, failures=0, successes=0, incident_open=False, incident_id="-", mode="shell"):
        state_dir = pathlib.Path(os.environ["HUB_CANARY_STATE_DIR"])
        state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        (state_dir / f"state-{mode}.json").write_text(json.dumps(
            {"failures": failures, "successes": successes,
             "incident_open": incident_open, "incident_id": incident_id}))

    def seed_failures(self, failures, mode="shell"):
        self.seed_state(failures=failures, mode=mode)

    def run_main(self, argv, observer, result=None):
        with mock.patch.object(canary.Canary, "execute", return_value=result or verdict()) as execute:
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = canary.main(list(argv), _managed_observer=observer)
        return code, out.getvalue(), execute

    # -- begin/decision/terminal attempt shape and occurrence vocabulary

    def test_critical_attempt_counts_alert_before_mail_exactly_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        code, out, _ = self.run_main(["--mode", "shell"], obs)
        self.assertEqual(code, 1)
        phases = [e["phase"] for e in obs.events]
        self.assertEqual(phases[0], "begin")
        self.assertEqual(phases[-1], "terminal")
        self.assertEqual(phases, ["begin", "decision", "terminal"])  # exactly one decision
        self.assertEqual(obs.decisions(), ["alert"])
        decision = obs.decision_events()[0]
        self.assertEqual(decision["recipient_class"], "dev")
        self.assertEqual(decision["condition"], "critical")
        obs.assert_valid_contract(self)
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])

    def test_decision_is_observed_before_mail_delivery(self):
        order = self.tmp / "order.log"
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        real_emit = obs.emit

        def emit(phase, **fields):
            with open(order, "a", encoding="utf-8") as fh:
                fh.write(f"observe:{phase}\n")
            return real_emit(phase, **fields)

        obs.emit = emit
        sink_mailer = os.environ["HUB_CANARY_MAILER"]
        wrapped = self.tmp / "order-mailer.sh"
        wrapped.write_text("#!/usr/bin/env bash\n"
                           f'printf "mail\\n" >> {shlex.quote(str(order))}\n'
                           f"exec {shlex.quote(sink_mailer)} \"$@\"\n")
        wrapped.chmod(0o755)
        os.environ["HUB_CANARY_MAILER"] = str(wrapped)
        canary.main(["--mode", "shell"], _managed_observer=obs)
        self.assertEqual(order.read_text().splitlines(),
                         ["observe:begin", "observe:decision", "mail", "observe:terminal"])

    def test_incident_lifecycle_mails_exactly_once_per_transition(self):
        # Full lifecycle with an observer on every run: critical mails once,
        # dedupe adds none, recovery mails once, pass adds none.
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        open_obs = SimulatedObserver()
        self.run_main(["--mode", "shell"], open_obs)
        self.assertEqual(open_obs.decisions(), ["alert"])
        dedup_obs = SimulatedObserver()
        self.run_main(["--mode", "shell"], dedup_obs)
        self.assertEqual(dedup_obs.decision_events()[0]["decision"], "alert_dedup")
        self.assertEqual(dedup_obs.decision_events()[0]["recipient_class"], "dev")
        self.assertEqual(dedup_obs.decision_events()[0]["condition"], "critical_dedup")
        self.assertEqual(len(self.mails()), 1)
        recover_obs = SimulatedObserver()
        self.run_main(["--mode", "shell"], recover_obs, verdict(ok=True, stage="capture", reason="canary-ok"))
        self.assertEqual(recover_obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(recover_obs.decision_events()[0]["condition"], "recovering")
        self.run_main(["--mode", "shell"], recover_obs,
                      verdict(ok=True, stage="capture", reason="canary-ok", run_id="run-000002"))
        self.assertEqual(recover_obs.decisions(), ["observation", "recovery"])
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertIn("[RECOVERED]", mails[1])
        self.assertIn("To: dev@dtcdev.click", mails[1])
        self.assertFalse(self.state()["incident_open"])
        pass_obs = SimulatedObserver()
        self.run_main(["--mode", "shell"], pass_obs,
                      verdict(ok=True, stage="capture", reason="canary-ok", run_id="run-000003"))
        self.assertEqual(pass_obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(pass_obs.decision_events()[0]["condition"], "pass")
        self.assertEqual(len(self.mails()), 2)
        for obs, runs in ((open_obs, 1), (dedup_obs, 1), (recover_obs, 2), (pass_obs, 1)):
            obs.assert_valid_contract(self)
            self.assertEqual([e["phase"] for e in obs.events],
                             ["begin", "decision", "terminal"] * runs)  # one attempt per run

    def test_observation_only_branches_never_count(self):
        self.seed_failures(0)
        below = SimulatedObserver()
        self.run_main(["--mode", "shell"], below)
        self.assertEqual(below.decision_events()[0]["decision"], "observation")
        self.assertEqual(below.decision_events()[0]["condition"], "failure_below_threshold")
        self.assertEqual(below.decision_events()[0]["recipient_class"], "none")
        self.assertEqual(self.mails(), [])
        for event in below.decision_events():
            self.assertFalse(alert_client.ram.DECISIONS[event["decision"]])
        below.assert_valid_contract(self)

    def test_hook_vocabulary_mapping_table(self):
        for decision, counted, condition, recipient in (
            ("critical", "alert", "critical", "dev"),
            ("critical_dedup", "alert_dedup", "critical_dedup", "dev"),
            ("recovery", "recovery", "recovery", "none"),
            ("pass", "observation", "pass", "none"),
            ("recovering", "observation", "recovering", "none"),
            ("failure_below_threshold", "observation", "failure_below_threshold", "none"),
        ):
            obs = SimulatedObserver()
            attempt = canary._CanaryAttempt(obs)
            canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision=decision, _attempt=attempt)
            self.assertEqual(obs.decisions(), [counted], decision)
            self.assertEqual(obs.decision_events()[0]["condition"], condition)
            self.assertEqual(obs.decision_events()[0]["recipient_class"], recipient)
            self.assertEqual(alert_client.ram.DECISIONS[counted], counted in ("alert", "alert_dedup", "recovery"))
            obs.assert_valid_contract(self)

    def test_single_decision_per_attempt_is_enforced(self):
        obs = SimulatedObserver()
        attempt = canary._CanaryAttempt(obs)
        canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision="critical", _attempt=attempt)
        canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision="recovery", _attempt=attempt)
        attempt.decision("observation", "error")
        self.assertEqual(obs.decisions(), ["alert"])  # first decision wins, later ones drop
        obs.assert_valid_contract(self)

    # -- observer failure must never gate, suppress or retry the business action

    def test_observer_failure_keeps_mail_action_exactly_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = RaisingObserver(RuntimeError("simulated observer failure"))
        code, _out, _ = self.run_main(["--mode", "shell"], obs)
        self.assertEqual(code, 1)
        self.assertTrue(obs.dirty)  # evidence unknown, marked failed
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])

    def test_observer_timeout_failure_still_delivers_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = RaisingObserver(TimeoutError("simulated observer timeout"))
        self.run_main(["--mode", "shell"], obs)
        self.assertTrue(obs.dirty)
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])

    def test_negative_ack_observer_never_gates_mail(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = NegativeAckObserver()
        self.run_main(["--mode", "shell"], obs)
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])

    # -- no-mail/deploy-gate/metadata/lock/error paths

    def test_deploy_gate_run_observes_gate_without_mail(self):
        obs = SimulatedObserver()
        code, out, _ = self.run_main(["--deploy-gate"], obs)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["deploy_gate"], True)
        self.assertEqual([e["phase"] for e in obs.events], ["begin", "decision", "terminal"])
        self.assertEqual(obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(obs.decision_events()[0]["condition"], "deploy_gate")
        self.assertEqual(obs.decision_events()[0]["recipient_class"], "none")
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])

    def test_deploy_gate_lock_busy_branch_observes_lock_busy_without_mail(self):
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(ok=False, stage="lock", reason="deploy_gate_lock_busy"),
                              _attempt=canary._CanaryAttempt(obs))
        self.assertEqual(obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(obs.decision_events()[0]["condition"], "lock_busy")
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])
        self.assertEqual(self.state()["last_lock_busy"]["reason"], "deploy_gate_lock_busy")
        self.assertEqual(self.state()["last_lock_busy"]["run_id"], "run-000000")

    def test_alert_state_lock_error_records_error_observation_and_typed_result(self):
        obs = SimulatedObserver()
        with mock.patch.object(canary, "acquire_lock_fd",
                               side_effect=canary.StageFailure("lock", "alert_state_lock_busy")):
            code, out, _ = self.run_main(["--mode", "shell"], obs)
        verdict_out = json.loads(out.strip().splitlines()[-1])
        self.assertEqual(code, 1)
        self.assertEqual(verdict_out["alert_state_error"], "lock:alert_state_lock_busy")
        self.assertEqual([e["phase"] for e in obs.events], ["begin", "decision", "terminal"])
        self.assertEqual(obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(obs.decision_events()[0]["condition"], "error")
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])

    def test_crash_before_any_branch_decision_still_yields_error_observation(self):
        obs = SimulatedObserver()
        with mock.patch.object(canary.Canary, "execute", side_effect=RuntimeError("simulated crash")):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError):
                    canary.main(["--mode", "shell"], _managed_observer=obs)
        self.assertEqual([e["phase"] for e in obs.events], ["begin", "decision", "terminal"])
        self.assertEqual(obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(obs.decision_events()[0]["condition"], "error")
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])

    def test_terminal_failure_preserves_original_business_exception(self):
        obs = SimulatedObserver()
        def emit(phase, **fields):
            raise RuntimeError("simulated observer failure")
        obs.emit = emit
        with mock.patch.object(canary.Canary, "execute", side_effect=RuntimeError("simulated crash")):
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(RuntimeError) as raised:
                    canary.main(["--mode", "shell"], _managed_observer=obs)
        self.assertEqual(str(raised.exception), "simulated crash")  # not the observer error
        self.assertEqual(self.mails(), [])

    # -- main() trusted-caller injection and legacy unmanaged defaults

    def test_main_injects_observer_and_counts_critical_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        code, out, _ = self.run_main(["--mode", "shell"], obs)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.strip().splitlines()[-1])["ok"], False)
        self.assertEqual(obs.decisions(), ["alert"])
        self.assertEqual(len(self.mails()), 1)

    def test_main_without_observer_matches_legacy_defaults(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        with mock.patch.object(canary.Canary, "execute", return_value=verdict()):
            with contextlib.redirect_stdout(io.StringIO()):
                code = canary.main(["--mode", "shell"])
        self.assertEqual(code, 1)
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])

    def test_unmanaged_apply_alerting_is_observation_silent_and_identical(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        canary.apply_alerting(verdict())  # no attempt adapter: legacy behavior only
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])
        # The hook itself stays a safe no-op for unmanaged callers.
        canary.record_dev_alert_occurrence(None, "shell", verdict(), {}, decision="critical")
        canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision="critical")

    def test_apply_alerting_accepts_private_attempt_without_lifecycle(self):
        # The attempt lifecycle (begin/terminal) belongs to main; apply_alerting
        # contributes exactly the branch decision when handed a private attempt.
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(), _attempt=canary._CanaryAttempt(obs))
        self.assertEqual([e["phase"] for e in obs.events], ["decision"])
        self.assertEqual(obs.decisions(), ["alert"])
        self.assertEqual(len(self.mails()), 1)


if __name__ == "__main__":
    unittest.main()

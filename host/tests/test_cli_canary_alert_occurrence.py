"""Offline tests for the hub start canary dev-alert occurrence observer
integration (scripts/hub_start_canary.py: apply_alerting/main/_managed_observer,
replacing the record_dev_alert_occurrence no-op).

The managed observer authority is always a clearly simulated in-process stand-in
(SimulatedObserver / RaisingObserver / NegativeAckObserver below) implementing
the bounded observer contract emit(phase, **fields) -> bool plus fail()/dirty.
No real broker, identity, provider, Hub instance or SMTP is ever contacted:
mail renders to an owned sink and canary state lives in an owned temp
HUB_CANARY_STATE_DIR. Occurrence counting (alert/alert_dedup/recovery count;
observation is coverage-only) happens on the receiving side of the bounded
observer API, so these tests assert the emitted decision vocabulary and that
the business/mail action stays exact-once even when observation fails.
Natural (unmanaged) invocations are covered to stay observation-silent.

Run: scripts/capped-test-run.sh -- scripts/with-temp-home.sh -- python3 -m unittest host.tests.test_cli_canary_alert_occurrence
"""
import contextlib, io, json, os, pathlib, shlex, shutil, sys, tempfile, unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from scripts import hub_start_canary as canary  # noqa: E402

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

    # -- begin/decision/terminal shape and occurrence vocabulary

    def test_critical_emits_counted_alert_before_mail_exactly_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual([e["phase"] for e in obs.events][0], "begin")
        self.assertEqual([e["phase"] for e in obs.events][-1], "terminal")
        self.assertEqual(obs.decisions(), ["alert"])
        decision = obs.decision_events()[0]
        self.assertEqual(decision["recipient_class"], "dev")
        self.assertEqual(decision["condition"], "hub_start_canary:shell:critical")
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
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual(order.read_text().splitlines(),
                         ["observe:begin", "observe:decision", "mail", "observe:terminal"])

    def test_critical_dedup_counts_without_new_mail(self):
        self.seed_state(failures=canary.FAIL_THRESHOLD - 1, incident_open=True, incident_id="inc-1")
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual(obs.decisions(), ["alert_dedup"])
        self.assertEqual(obs.decision_events()[0]["recipient_class"], "dev")
        self.assertEqual(obs.decision_events()[0]["condition"], "hub_start_canary:shell:critical_dedup")
        self.assertEqual(self.mails(), [])  # dedupe: no second mail
        st = self.state()
        self.assertTrue(st["incident_open"])
        self.assertEqual(st["failures"], canary.FAIL_THRESHOLD)

    def test_incident_lifecycle_mails_exactly_once_per_transition(self):
        # Full unmanaged-opener lifecycle with an observer on every run:
        # critical mails once, dedupe adds none, recovery mails once.
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        open_obs = SimulatedObserver()
        canary.apply_alerting(verdict(), _managed_observer=open_obs)
        self.assertEqual(open_obs.decisions(), ["alert"])
        dedup_obs = SimulatedObserver()
        canary.apply_alerting(verdict(run_id="run-000002"), _managed_observer=dedup_obs)
        self.assertEqual(dedup_obs.decisions(), ["alert_dedup"])
        self.assertEqual(len(self.mails()), 1)
        recover_obs = SimulatedObserver()
        for run_id in ("run-000003", "run-000004"):
            canary.apply_alerting(verdict(ok=True, stage="capture", reason="canary-ok", run_id=run_id),
                                  _managed_observer=recover_obs)
        self.assertEqual(recover_obs.decisions(), ["observation", "recovery"])
        mails = self.mails()
        self.assertEqual(len(mails), 2)
        self.assertIn("[RECOVERED]", mails[1])
        self.assertIn("To: dev@dtcdev.click", mails[1])
        self.assertFalse(self.state()["incident_open"])
        pass_obs = SimulatedObserver()
        canary.apply_alerting(verdict(ok=True, stage="capture", reason="canary-ok", run_id="run-000005"),
                              _managed_observer=pass_obs)
        self.assertEqual(pass_obs.decisions(), ["observation"])  # pass, not another recovery
        self.assertEqual(len(self.mails()), 2)

    def test_observation_only_branches_never_count(self):
        passing = SimulatedObserver()
        canary.apply_alerting(verdict(ok=True), _managed_observer=passing)
        self.assertEqual(passing.decisions(), ["observation"])
        self.assertEqual(passing.decision_events()[0]["condition"], "hub_start_canary:shell:pass")
        self.assertEqual(self.mails(), [])
        below = SimulatedObserver()
        canary.apply_alerting(verdict(), _managed_observer=below)
        self.assertEqual(below.decisions(), ["observation"])
        self.assertEqual(below.decision_events()[0]["condition"], "hub_start_canary:shell:failure_below_threshold")
        self.assertEqual(self.mails(), [])
        recovering = SimulatedObserver()
        recovering_state = {"failures": 0, "successes": 0, "incident_open": True, "incident_id": "inc-1"}
        with mock.patch.object(canary, "load_state", return_value=recovering_state):
            canary.apply_alerting(verdict(ok=True, stage="capture", reason="canary-ok"), _managed_observer=recovering)
        self.assertEqual(recovering.decisions(), ["observation"])
        self.assertEqual(recovering.decision_events()[0]["condition"], "hub_start_canary:shell:recovering")
        for obs in (passing, below, recovering):
            for event in obs.decision_events():
                self.assertEqual(event["decision"], "observation")
                self.assertEqual(event["recipient_class"], "none")

    def test_hook_vocabulary_mapping_table(self):
        for decision, counted, recipient in (
            ("critical", "alert", "dev"),
            ("critical_dedup", "alert_dedup", "dev"),
            ("recovery", "recovery", "none"),
            ("pass", "observation", "none"),
            ("recovering", "observation", "none"),
            ("failure_below_threshold", "observation", "none"),
        ):
            obs = SimulatedObserver()
            canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision=decision, _observer=obs)
            self.assertEqual(obs.decisions(), [counted], decision)
            self.assertEqual(obs.decision_events()[0]["recipient_class"], recipient)

    # -- observer failure must never gate, suppress or retry the business action

    def test_observer_failure_keeps_mail_action_exactly_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = RaisingObserver(RuntimeError("simulated observer failure"))
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertTrue(obs.dirty)  # evidence unknown, marked failed
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])

    def test_observer_timeout_failure_still_delivers_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = RaisingObserver(TimeoutError("simulated observer timeout"))
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertTrue(obs.dirty)
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])

    def test_negative_ack_observer_never_gates_mail(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = NegativeAckObserver()
        canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])

    # -- no-mail/deploy-gate/metadata/lock/error paths

    def test_deploy_gate_lock_busy_branch_has_no_decision_and_no_mail(self):
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(ok=False, stage="lock", reason="deploy_gate_lock_busy"),
                              _managed_observer=obs)
        self.assertEqual([e["phase"] for e in obs.events], ["begin", "terminal"])
        self.assertEqual(self.mails(), [])
        self.assertEqual(self.state()["last_lock_busy"]["reason"], "deploy_gate_lock_busy")
        self.assertEqual(self.state()["last_lock_busy"]["run_id"], "run-000000")

    def test_lock_error_emits_begin_and_terminal_then_propagates(self):
        obs = SimulatedObserver()
        with mock.patch.object(canary, "acquire_lock_fd",
                               side_effect=canary.StageFailure("lock", "alert_state_lock_busy")):
            with self.assertRaises(canary.StageFailure):
                canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual([e["phase"] for e in obs.events], ["begin", "terminal"])
        self.assertEqual(self.mails(), [])

    def test_terminal_failure_preserves_original_business_exception(self):
        obs = SimulatedObserver()
        def emit(phase, **fields):
            raise RuntimeError("simulated observer failure")
        obs.emit = emit
        with mock.patch.object(canary, "acquire_lock_fd",
                               side_effect=canary.StageFailure("lock", "alert_state_lock_busy")):
            with self.assertRaises(canary.StageFailure):
                canary.apply_alerting(verdict(), _managed_observer=obs)
        self.assertEqual(self.mails(), [])

    # -- main() trusted-caller injection and legacy unmanaged defaults

    def test_main_injects_observer_and_counts_critical_once(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = SimulatedObserver()
        with mock.patch.object(canary.Canary, "execute", return_value=verdict()):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = canary.main(["--mode", "shell"], _managed_observer=obs)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(out.getvalue().strip().splitlines()[-1])["ok"], False)
        self.assertEqual(obs.decisions(), ["alert"])
        phases = [e["phase"] for e in obs.events]
        self.assertEqual(phases[0], "begin")
        self.assertEqual(phases[-1], "terminal")
        self.assertEqual(len(self.mails()), 1)

    def test_main_deploy_gate_never_alerts_and_never_observes(self):
        obs = SimulatedObserver()
        with mock.patch.object(canary.Canary, "execute", return_value=verdict()):
            with contextlib.redirect_stdout(io.StringIO()):
                code = canary.main(["--deploy-gate"], _managed_observer=obs)
        self.assertEqual(code, 1)
        self.assertEqual(obs.events, [])  # no begin/decision/terminal on the gate path
        self.assertEqual(self.mails(), [])

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
        canary.apply_alerting(verdict())
        (mail,) = self.mails()
        self.assertIn("[CRITICAL]", mail)
        self.assertTrue(self.state()["incident_open"])
        # The hook itself stays a safe no-op for unmanaged callers.
        canary.record_dev_alert_occurrence(None, "shell", verdict(), {}, decision="critical")
        canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision="critical")


if __name__ == "__main__":
    unittest.main()

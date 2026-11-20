"""Offline tests for the hub start canary dev-alert occurrence observer
integration (scripts/hub_start_canary.py: main/_managed_observer,
_CanaryAttempt/apply_alerting, replacing the record_dev_alert_occurrence
no-op).

The managed observer authority is always a clearly simulated in-process
stand-in (SimulatedObserver / RaisingObserver / NegativeAckObserver /
NoneAckObserver below) implementing the bounded observer contract
emit(phase, **fields) -> bool plus fail()/dirty. Negative ACKs do NOT
self-mark: the source under test must call fail() on any non-True
acknowledgement itself. No real broker, identity, provider, Hub instance or
SMTP is ever contacted: mail renders to an owned sink and canary state lives
in an owned temp HUB_CANARY_STATE_DIR.

Every recorded event is additionally rebuilt as a full API-v1 frame and
validated with the REAL maintained producer validator
scripts/hub_alert_client.checked (imported stdlib-only, never connected):
exact frame shape, closed CONDITIONS/FLAGS vocabulary, bool flag values,
dev/none recipient_class, seq bounds, begin/terminal carrying no decision.
Counted decisions (alert/alert_dedup/recovery) increment occurrences,
observation never does. The attempt lifecycle follows the alert authority's
canary rule: begin, exactly one decision, terminal per run; no-mail branches
carry the truthful no_mail flag and never pretend mail happened. Natural
(unmanaged) invocations are covered to stay observation-silent.

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

    def api_frames(self):
        """Rebuild every recorded event as the exact API-v1 frame the
        maintained producer client would put on the wire (simulated 32-hex
        invocation identity, role canary, one seq per frame, clean
        observer_ok)."""
        return [dict(v=1, invocation_id="0" * 32, parent_id=None, role="canary",
                     phase=event["phase"], seq=index,
                     decision=event.get("decision"), condition=event.get("condition"),
                     flags=event.get("flags", {}), recipient_class=event["recipient_class"],
                     observer_ok=True)
                for index, event in enumerate(self.events)]

    def assert_valid_contract(self, testcase):
        """Every recorded event must pass the REAL maintained producer frame
        validator (alert_client.checked: exact shape, closed vocabulary, bool
        flags, dev/none recipient, seq bounds, begin/terminal with no
        decision); counted decisions increment occurrences, observation never
        does, and no-mail branches claim no mail."""
        for frame in self.api_frames():
            alert_client.checked(frame)
        for event in self.events:
            if event["phase"] != "decision":
                continue
            testcase.assertEqual(alert_client.ram.DECISIONS[event["decision"]],
                                 event["decision"] != "observation")
            if event["decision"] == "observation":
                testcase.assertEqual(event["flags"], {"no_mail": True})
            else:
                testcase.assertEqual(event["flags"], {})


class RaisingObserver(SimulatedObserver):
    """Simulated observer whose emit always fails (failure/timeout evidence)."""

    def __init__(self, error=None):
        super().__init__()
        self.error = error or RuntimeError("simulated observer failure")

    def emit(self, phase, **fields):
        raise self.error


class NegativeAckObserver(SimulatedObserver):
    """Simulated observer that answers every emit with a negative ACK and
    deliberately does NOT mark itself: the source under test must notice the
    non-True acknowledgement and call fail() itself."""

    def emit(self, phase, **fields):
        self.events.append({"phase": phase, "negative_ack": True, **fields})
        return False


class NoneAckObserver(SimulatedObserver):
    """Simulated observer returning a malformed (None) acknowledgement; like a
    negative ACK this must make the source mark the evidence failed."""

    def emit(self, phase, **fields):
        self.events.append({"phase": phase, **fields})
        return None


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
        recovery_decision = recover_obs.decision_events()[1]
        self.assertEqual(recovery_decision["recipient_class"], "dev")  # recovery mails to dev
        self.assertEqual(recovery_decision["flags"], {})  # emitted before the single mail action
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
            ("recovery", "recovery", "recovery", "dev"),  # recovery mail also goes to dev
            ("pass", "observation", "pass", "none"),
            ("recovering", "observation", "recovering", "none"),
            ("failure_below_threshold", "observation", "failure_below_threshold", "none"),
        ):
            obs = SimulatedObserver()
            attempt = canary._CanaryAttempt(obs)
            canary.record_dev_alert_occurrence(self.tmp, "shell", verdict(), {}, decision=decision, _attempt=attempt)
            self.assertEqual(obs.decisions(), [counted], decision)
            event = obs.decision_events()[0]
            self.assertEqual(event["condition"], condition)
            self.assertEqual(event["recipient_class"], recipient)
            # Counted decisions claim nothing about mail; observation branches
            # truthfully carry no_mail (this hook never mails).
            self.assertEqual(event["flags"],
                             {} if counted != "observation" else {"no_mail": True})
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

    def test_negative_ack_observer_is_failed_by_source_and_mail_still_once(self):
        # The double answers False WITHOUT self-marking; the source itself
        # must call fail() on the non-True acknowledgement (evidence unknown),
        # and the original mail action still happens exactly once.
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = NegativeAckObserver()
        self.run_main(["--mode", "shell"], obs)
        self.assertTrue(obs.dirty)  # marked by the source, not by the double
        self.assertEqual([e["phase"] for e in obs.events],
                         ["begin", "decision", "terminal"])
        self.assertEqual(obs.decisions(), ["alert"])
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])
        obs.assert_valid_contract(self)

    def test_malformed_none_ack_is_treated_like_observation_failure(self):
        self.seed_failures(canary.FAIL_THRESHOLD - 1)
        obs = NoneAckObserver()
        self.run_main(["--mode", "shell"], obs)
        self.assertTrue(obs.dirty)
        self.assertEqual(len(self.mails()), 1)
        self.assertTrue(self.state()["incident_open"])
        obs.assert_valid_contract(self)

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
        self.assertEqual(obs.decision_events()[0]["flags"], {"no_mail": True})
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])

    def test_deploy_gate_lock_busy_branch_observes_lock_busy_without_mail(self):
        obs = SimulatedObserver()
        canary.apply_alerting(verdict(ok=False, stage="lock", reason="deploy_gate_lock_busy"),
                              _attempt=canary._CanaryAttempt(obs))
        self.assertEqual(obs.decision_events()[0]["decision"], "observation")
        self.assertEqual(obs.decision_events()[0]["condition"], "lock_busy")
        self.assertEqual(obs.decision_events()[0]["flags"], {"no_mail": True})
        obs.assert_valid_contract(self)
        self.assertEqual(self.mails(), [])
        self.assertEqual(self.state()["last_lock_busy"]["reason"], "deploy_gate_lock_busy")
        self.assertEqual(self.state()["last_lock_busy"]["run_id"], "run-000000")

    def test_metadata_previews_emit_metadata_only_without_mail(self):
        # The read-only previews (--physical-admission-check,
        # --print-helper-sha256) run inside the attempt lifecycle: begin,
        # one metadata_only observation, terminal. No mail, no occurrence,
        # verdict/exit codes unchanged.
        cases = (
            (["--physical-admission-check"],
             mock.patch.object(canary, "physical_admission_check",
                               return_value={"ok": False, "stage": "admission",
                                             "reason": "simulated-preview"}),
             1),
            (["--physical-admission-check"],
             mock.patch.object(canary, "physical_admission_check",
                               return_value={"ok": True}),  # simulated verdict fixture
             0),
            (["--print-helper-sha256"],
             mock.patch.object(canary, "compute_helper_sha256", return_value="0" * 64),
             0),
        )
        for argv, patcher, expected_code in cases:
            with patcher:
                obs = SimulatedObserver()
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    code = canary.main(list(argv), _managed_observer=obs)
                self.assertEqual(code, expected_code, argv)
                self.assertTrue(out.getvalue().strip(), argv)
            self.assertEqual([e["phase"] for e in obs.events],
                             ["begin", "decision", "terminal"], argv)
            decision = obs.decision_events()[0]
            self.assertEqual(decision["decision"], "observation", argv)
            self.assertEqual(decision["condition"], "metadata_only", argv)
            self.assertEqual(decision["recipient_class"], "none", argv)
            self.assertEqual(decision["flags"], {"no_mail": True}, argv)
            obs.assert_valid_contract(self)
            self.assertEqual(self.mails(), [], argv)
            self.assertEqual(self.state(), {}, argv)  # previews write no canary state

    def test_unmanaged_metadata_preview_stays_observation_silent(self):
        with mock.patch.object(canary, "compute_helper_sha256", return_value="a" * 64):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                code = canary.main(["--print-helper-sha256"])
        self.assertEqual(code, 0)
        self.assertIn("a" * 64, out.getvalue())
        self.assertEqual(self.mails(), [])

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

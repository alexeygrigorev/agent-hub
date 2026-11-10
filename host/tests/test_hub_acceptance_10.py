"""Offline tests for scripts/hub_acceptance_10.py.

Fake `hub`, `a` and alert-sample binaries are put on PATH; their responses are cloned from real-CLI fixtures in
host/tests/fixtures/acceptance/. No Hub access, no real session is started or stopped."""
import importlib.util
import io
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
FIX = HERE / 'fixtures' / 'acceptance'
spec = importlib.util.spec_from_file_location('acc10', HERE.parents[1] / 'scripts/hub_acceptance_10.py')
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)

SOL, GLM, MUSE, LUNA = 'gpt-6.1-sol', 'glm-5.3-flash', 'opencode-go/muse-spark-1.3-contributor', 'gpt-6-luna'  # fixture pool
OK_MODELS = [SOL, GLM, MUSE, SOL, GLM, MUSE, SOL, GLM, LUNA, SOL]


def fixture(name):
    return json.loads((FIX / name).read_text())


def evidence(commit='abc', **kw):
    ev = {'canary': 'success', 'canary_commit': commit, 'ci_e2e': 'success', 'ci_e2e_commit': commit}
    ev.update(kw)
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    json.dump(ev, f)
    f.close()
    unittest.addModuleCleanup(os.unlink, f.name)
    return f.name


class Harness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='acc10-', dir=os.environ.get('TMPDIR') or None))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / 'bin'
        self.bin.mkdir()
        for name in ('hub', 'a', 'alerts'):
            (self.bin / name).symlink_to(FIX / 'fake_hub.py')
        self.cfg_path = self.tmp / 'cfg.json'
        env = {'PATH': '%s:%s' % (self.bin, os.environ['PATH']), 'FAKE_CFG': str(self.cfg_path)}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name, val in (('GRACE_S', 1), ('CAPTURE_BUDGET_S', 2)):
            p = mock.patch.object(acc, name, val)
            p.start()
            self.addCleanup(p.stop)

    def state(self):
        path = Path(str(self.cfg_path) + '.state')
        return json.loads(path.read_text()) if path.exists() else {'sessions': {}}

    def go(self, **cfg):
        cfg.setdefault('models', OK_MODELS)
        Path(str(self.cfg_path) + '.state').unlink(missing_ok=True)
        self.cfg_path.write_text(json.dumps(cfg))
        out = io.StringIO()
        r = acc.Runner(alerts_cmd=str(self.bin / 'alerts'))
        rc = acc.run(r, evidence(), 'abc', sleep=lambda s: time.sleep(0.01), out=out, intake='intake-1')
        return rc, out.getvalue()

    def assertAllStopped(self):
        st = self.state()
        self.assertTrue(st['sessions'])
        for s in st['sessions'].values():
            self.assertTrue(s['stopped'], 'leaked session %s' % s['id'])


class AcceptanceTests(Harness):
    def test_pass_prints_shares_stops_all_and_passes_intake(self):
        rc, out = self.go()
        self.assertEqual(rc, 0, out)
        self.assertIn('ACCEPTANCE PASS', out)
        self.assertEqual(out.count('shares(pick-before-start)='), 10)
        self.assertAllStopped()
        self.assertEqual(len(self.state()['sessions']), 10)
        self.assertEqual(self.state()['log'].count('start:--intake-task-id'), 10)

    def test_start_response_pool_is_preferred_source(self):
        pool = fixture('hub_sessions_pick.json')['pool']
        rc, out = self.go(start_pool=pool)
        self.assertEqual(rc, 0, out)
        self.assertIn('shares(start)=', out)

    def test_refuses_without_ready_env(self):
        os.environ.pop('HUB_ACCEPTANCE_READY', None)
        self.assertEqual(acc.main(['--live']), 2)
        self.assertEqual(acc.main([]), 2)

    def test_acceptance_criteria_source_required(self):
        self.cfg_path.write_text(json.dumps({'models': OK_MODELS}))
        out = io.StringIO()
        rc = acc.run(acc.Runner(alerts_cmd=str(self.bin / 'alerts')), evidence(), 'abc', out=out)
        self.assertEqual(rc, 1)
        self.assertIn('acceptance criteria source missing', out.getvalue())
        self.assertEqual(self.state()['sessions'], {}, 'must not start anything')

    def test_start_failure_ends_run(self):
        rc, out = self.go(fail_start_at=3)
        self.assertEqual(rc, 1)
        self.assertIn('start failed', out)
        # 3 good starts + the refused one, which the key-replay reconcile may have created; all must be stopped
        self.assertGreaterEqual(len(self.state()['sessions']), 3)
        self.assertAllStopped()

    def test_wait_failure_with_id_leaks_nothing(self):
        rc, out = self.go(leak_at=2)
        self.assertEqual(rc, 1)
        self.assertIn('after creating session', out)
        self.assertAllStopped()

    def test_wait_failure_without_id_reconciled_by_key(self):
        rc, out = self.go(leak_at=0, leak_with_id=False)
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.state()['sessions']), 1)
        self.assertAllStopped()

    def test_blocking_start_hits_deadline_and_is_cleaned_up(self):
        with mock.patch.object(acc, 'RUNNING_BUDGET_S', 1):
            t0 = time.monotonic()
            rc, out = self.go(hang_start_at=0)
            self.assertLess(time.monotonic() - t0, 25)
        self.assertEqual(rc, 1)
        self.assertIn('timeout', out)
        self.assertAllStopped()

    def test_astra_picked(self):
        rc, out = self.go(models=['astra-1'] + OK_MODELS[1:])
        self.assertEqual(rc, 1)
        self.assertIn('astra picked', out)

    def test_luna_over_three(self):
        rc, out = self.go(models=[LUNA] * 4 + [SOL, GLM, MUSE, SOL, GLM, MUSE])
        self.assertEqual(rc, 1)
        self.assertIn('luna 4 > 3', out)

    def test_luna_on_degraded_pool(self):
        one = [fixture('hub_sessions_pick.json')['pool'][0]]
        one[0]['share'] = 1.0
        rc, out = self.go(pool=one, models=[LUNA] * 10)
        self.assertEqual(rc, 1)
        self.assertIn('luna picked from a degraded one-model pool', out)

    def test_too_few_distinct_models(self):
        rc, out = self.go(models=[SOL, GLM] * 5)
        self.assertEqual(rc, 1)
        self.assertIn('2 distinct models < 3', out)

    def test_missing_or_empty_pool_fails(self):
        rc, out = self.go(pool=[{'policy_id': 'p', 'model': 'sol', 'share': 0}])
        self.assertEqual(rc, 1)
        self.assertIn('invalid or zero share', out)
        rc, out = self.go(pool_missing=True, pick_fail=True)
        self.assertEqual(rc, 1)
        self.assertIn('pick failed', out)

    def test_model_not_in_pool_shares(self):
        rc, out = self.go(models=['unlisted'] * 10)
        self.assertEqual(rc, 1)
        self.assertIn('not in the picker pool shares', out)

    def test_missing_host_and_workspace(self):
        rc, out = self.go(list_no_host=True)
        self.assertIn("host None != hetzner-aplexer", out)
        rc, out = self.go(list_no_workspace=True)
        self.assertIn('workspace path missing', out)
        self.assertEqual(rc, 1)
        rc, out = self.go(host='other-host')
        self.assertIn("host 'other-host'", out)

    def test_flat_show_is_rejected(self):
        rc, out = self.go(flat_show=True)
        self.assertEqual(rc, 1)
        self.assertIn('show response is not', out)

    def test_capture_prompt_only(self):
        rc, out = self.go(capture_prompt_only=True)
        self.assertEqual(rc, 1)
        self.assertIn('no tool output beyond the prompt', out)

    def test_stop_not_confirmed(self):
        rc, out = self.go(stop_not_confirmed=True)
        self.assertEqual(rc, 1)
        self.assertIn('!= stopped', out)

    def test_native_lingers_after_stop_but_exited_rows_do_not_count(self):
        with mock.patch.object(acc, 'STOP_BUDGET_S', 6):
            rc, out = self.go(native_lingers=True, fail_start_at=1)
        self.assertEqual(rc, 1)
        self.assertIn('still listed', out)
        rc, out = self.go(a_list_includes_exited=True)
        self.assertEqual(rc, 0, out)

    def test_alert_during_run(self):
        rc, out = self.go(alert_during=True)
        self.assertEqual(rc, 1)
        self.assertIn('dev alerts during run: alert-during', out)
        self.assertNotIn('ACCEPTANCE PASS', out)

    def test_preexisting_alert_does_not_count_but_broken_sampler_fails_closed(self):
        rc, out = self.go(alerts_before=['old'])
        self.assertEqual(rc, 0, out)
        rc, out = self.go(alerts_broken=True)
        self.assertEqual(rc, 1)
        self.assertIn('alert baseline unavailable', out)
        self.assertEqual(self.state()['sessions'], {})


class ParserTests(unittest.TestCase):
    """Parsers against shapes captured from the real CLIs."""

    def test_show_is_wrapped(self):
        sess = acc.session_of(fixture('hub_sessions_show.json'))
        self.assertEqual(sess['machine_id'], 'hetzner-aplexer')
        self.assertEqual(acc.row_host(sess), 'hetzner-aplexer')
        with self.assertRaises(RuntimeError):
            acc.session_of(sess)  # flat shape

    def test_list_rows_and_workspace_from_evidence(self):
        rows = acc.list_rows(fixture('hub_sessions_list.json'))
        self.assertEqual(acc.row_host(rows[0]), 'hetzner-aplexer')
        self.assertTrue(acc.row_workspace(rows[0]).startswith('/'))
        with self.assertRaises(RuntimeError):
            acc.list_rows({'nothing': 1})

    def test_pool_fixture_valid_and_bad_pools_rejected(self):
        self.assertEqual(acc.pool_problems(fixture('hub_sessions_pick.json')['pool']), [])
        for bad in (None, [], [{'share': 0}], [{'share': float('nan')}], [{'share': 0.2}, {'share': 0.2}]):
            self.assertTrue(acc.pool_problems(bad), bad)

    def test_live_natives_ignores_exited(self):
        rows = fixture('a_list_running.json')
        rows.append(dict(rows[0], id='gone', state='exited'))

        class R:
            def json(self, *a):
                return rows
        self.assertEqual(acc.live_natives(R(), 5), {rows[0]['id']})

    def test_evidence_fail_closed(self):
        for ev, commit, msg in [(None, 'abc', 'evidence'), (evidence(commit='zzz'), 'abc', '!='),
                                (evidence(canary='failure'), 'abc', 'not green'),
                                (evidence(), None, 'commit unknown'), ('/nonexistent', 'abc', 'unreadable')]:
            problems = acc.check_evidence(ev, commit)
            self.assertTrue(any(msg in p for p in problems), (msg, problems))
        self.assertEqual(acc.check_evidence(evidence(), 'abc'), [])

    def test_probe_ignores_prompt_echo(self):
        prompt, exp = acc.make_probe()
        self.assertFalse(acc.probe_output_ok(prompt, prompt, exp))
        self.assertTrue(acc.probe_output_ok(prompt + '\n' + exp, prompt, exp))


if __name__ == '__main__':
    unittest.main()

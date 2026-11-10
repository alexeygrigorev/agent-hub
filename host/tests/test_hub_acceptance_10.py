"""Offline tests for scripts/hub_acceptance_10.py; a fake runner, no Hub access."""
import importlib.util
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

spec = importlib.util.spec_from_file_location('acc10', Path(__file__).resolve().parents[2] / 'scripts/hub_acceptance_10.py')
acc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(acc)

MODELS = ['sol', 'opus', 'sonnet', 'luna']


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Fake:
    def __init__(self, models=None, start_s=20, host='hetzner-aplexer', capture=True, pool=3,
                 stop_state='stopped', gone=True, clock=None, fail_at=None):
        self.models = models or [MODELS[i % 3] for i in range(10)]
        self.start_s, self.host, self.capture, self.pool = start_s, host, capture, pool
        self.stop_state, self.gone, self.clock, self.fail_at = stop_state, gone, clock, fail_at
        self.calls, self.sessions, self.prompts, self.stopped, self.n = [], {}, {}, [], 0

    def run(self, tool, args, timeout=300):
        self.calls.append((tool, list(args)))
        if tool == 'a' and args[0] == 'capture':
            sid = next(k for k, v in self.sessions.items() if v['native_id'] == args[1])
            if not self.capture:
                return 0, '> ' + self.prompts[sid] + '\n', ''
            exp = re.search(r'\$\(\((\d+)\*(\d+)\)\)', self.prompts[sid])
            return 0, '> %s\nran echo\n%d\n' % (self.prompts[sid], int(exp[1]) * int(exp[2])), ''
        return 0, json.dumps(self.json(tool, args, timeout)), ''

    def json(self, tool, args, timeout=300):
        if tool == 'a':
            live = [v['native_id'] for k, v in self.sessions.items() if k not in self.stopped or not self.gone]
            return {'sessions': [{'id': n} for n in live]}
        sub = args[1]
        if sub == 'pick':
            return {'route': 'x', 'pool': [{'policy_id': 'p%d' % i, 'share': 1 / self.pool} for i in range(self.pool)]}
        if sub == 'start':
            if self.fail_at == self.n:
                raise RuntimeError('boom')
            i = self.n
            self.n += 1
            sid = 's%d' % i
            self.sessions[sid] = {'id': sid, 'native_id': 'nat%d' % i, 'model_id': self.models[i], 'engine': 'claude',
                                  'state': 'running', 'machine': self.host, 'workspace': '/w/%d' % i}
            self.prompts[sid] = args[args.index('--prompt') + 1]
            if self.clock:
                self.clock.t += self.start_s
            return {'id': sid, 'route': 'r'}
        if sub == 'show':
            row = dict(self.sessions[args[2]])
            if args[2] in self.stopped:
                row['state'] = self.stop_state
            return row
        if sub == 'list':
            return {'sessions': list(self.sessions.values())}
        if sub == 'stop':
            self.stopped.append(args[2])
            return {'state': 'stopped'}
        raise AssertionError(args)


def evidence(commit='abc', **kw):
    ev = {'canary': 'success', 'canary_commit': commit, 'ci_e2e': 'success', 'ci_e2e_commit': commit, 'dev_alerts': 0}
    ev.update(kw)
    f = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
    json.dump(ev, f)
    f.close()
    unittest.addModuleCleanup(os.unlink, f.name)
    return f.name


class AcceptanceTests(unittest.TestCase):
    def go(self, fake, ev=None, commit='abc'):
        clock = fake.clock or Clock()
        fake.clock = clock
        out = io.StringIO()
        rc = acc.run(fake, ev or evidence(), commit, clock=clock, sleep=clock.sleep, out=out)
        return rc, out.getvalue()

    def test_pass(self):
        f = Fake()
        rc, out = self.go(f)
        self.assertEqual(rc, 0, out)
        self.assertIn('ACCEPTANCE PASS', out)
        self.assertEqual(out.count('shares='), 10)
        self.assertEqual(len(f.stopped), 10)
        starts = [c for c in f.calls if c[1][:2] == ['sessions', 'start']]
        for _, a in starts:
            for flag in ('--engine', '--model', '--tier', '--machine'):
                self.assertNotIn(flag, a)

    def test_refuses_without_ready_env(self):
        os.environ.pop('HUB_ACCEPTANCE_READY', None)
        self.assertEqual(acc.main(['--live'], runner=Fake()), 2)
        self.assertEqual(acc.main([], runner=Fake()), 2)

    def test_too_few_models(self):
        rc, out = self.go(Fake(models=['sol', 'opus'] * 5))
        self.assertEqual(rc, 1)
        self.assertIn('distinct models', out)

    def test_astra_and_luna(self):
        rc, out = self.go(Fake(models=['astra-1'] + ['sol', 'opus', 'sonnet'] * 3))
        self.assertIn('astra picked', out)
        rc, out = self.go(Fake(models=['luna'] * 4 + ['sol', 'opus', 'sonnet', 'sol', 'opus', 'sonnet']))
        self.assertIn('luna 4 > 3', out)

    def test_degraded_pool_luna(self):
        rc, out = self.go(Fake(pool=1, models=['luna'] * 10))
        self.assertEqual(rc, 1)
        self.assertIn('degraded', out)

    def test_slow_start(self):
        rc, out = self.go(Fake(start_s=151, clock=Clock()))
        self.assertEqual(rc, 1)
        self.assertIn('> 150s', out)

    def test_wrong_host(self):
        rc, out = self.go(Fake(host='other'))
        self.assertIn('host', out)
        self.assertEqual(rc, 1)

    def test_capture_prompt_only(self):
        rc, out = self.go(Fake(capture=False))
        self.assertEqual(rc, 1)
        self.assertIn('no tool output', out)

    def test_stop_not_stopped_or_native_left(self):
        rc, out = self.go(Fake(stop_state='running'))
        self.assertIn('!= stopped', out)
        rc, out = self.go(Fake(gone=False))
        self.assertIn('still listed', out)
        self.assertEqual(rc, 1)

    def test_failed_start_ends_run_and_stops_own(self):
        f = Fake(fail_at=3)
        rc, out = self.go(f)
        self.assertEqual(rc, 1)
        self.assertEqual(len(f.stopped), 3)
        self.assertEqual(set(f.stopped), set(f.sessions))

    def test_evidence_fail_closed(self):
        for ev, commit, msg in [(None, 'abc', 'evidence'), (evidence(commit='zzz'), 'abc', '!='),
                                (evidence(canary='failure'), 'abc', 'not green'),
                                (evidence(dev_alerts=2), 'abc', 'dev alerts'),
                                (evidence(), None, 'commit unknown'), ('/nonexistent', 'abc', 'unreadable')]:
            problems = acc.check_evidence(ev, commit)
            self.assertTrue(any(msg in p for p in problems), (msg, problems))
        self.assertEqual(acc.check_evidence(evidence(), 'abc'), [])
        bad = evidence()
        with open(bad) as fh:
            d = json.load(fh)
        del d['dev_alerts']
        with open(bad, 'w') as fh:
            json.dump(d, fh)
        self.assertTrue(acc.check_evidence(bad, 'abc'))

    def test_probe_ignores_prompt_echo(self):
        prompt, exp = acc.make_probe()
        self.assertFalse(acc.probe_output_ok(prompt, prompt, exp))
        self.assertTrue(acc.probe_output_ok(prompt + '\n' + exp, prompt, exp))


if __name__ == '__main__':
    unittest.main()

"""Merge evidence collection (intake 2e00e01f). Fake runner; no git, gh or Hub."""
import json
import subprocess
import unittest

from agent_hub_host import merge_evidence as me

A = 'a1' * 20
B = 'b2' * 20


class FakeRunner:
    def __init__(self, in_main=(), runs=None, subject='feat: x', cherry='+ ' + A + '\n', fetch_rc=0):
        self.in_main, self.runs, self.subject, self.cherry, self.fetch_rc = set(in_main), runs or [], subject, cherry, fetch_rc
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        out, rc = '', 0
        if argv[:2] == ['git', 'fetch']:
            rc = self.fetch_rc
        elif argv[:2] == ['git', 'rev-parse'] and '--abbrev-ref' in argv:
            out = 'demo-wt-1\n'
        elif argv[:2] == ['git', 'rev-parse'] and '--verify' in argv:
            out = argv[-1].split('^')[0] + '\n'
        elif argv[:2] == ['git', 'rev-parse']:
            out = A + '\n'
        elif argv[:2] == ['git', 'cherry']:
            out = self.cherry
        elif argv[:3] == ['git', 'merge-base', '--is-ancestor']:
            sha, ref = argv[3], argv[4]
            rc = 0 if (ref == 'origin/main' and sha in self.in_main) or (ref != 'origin/main' and sha == ref) else 1
        elif argv[:2] == ['git', 'log']:
            out = self.subject + '\n'
        elif argv[0] == 'gh':
            out = json.dumps(self.runs)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr='')


class MergeEvidenceTests(unittest.TestCase):
    def test_unmerged_commit_is_not_in_main_and_deploy_missing(self):
        out = me.collect('/r', [], FakeRunner())
        self.assertEqual(out['head'], A)
        self.assertEqual(out['unmerged'], 1)
        self.assertFalse(out['commits'][0]['in_main'])
        self.assertEqual(out['deploy']['state'], 'missing')

    def test_merged_and_deploy_success(self):
        runner = FakeRunner(in_main=[A], cherry='', runs=[{'headSha': A, 'conclusion': 'success', 'status': 'completed', 'url': 'https://example.invalid/r/1'}])
        out = me.collect('/r', [], runner)
        self.assertTrue(out['commits'][0]['in_main'])
        self.assertEqual(out['deploy'], {'state': 'success', 'sha': A, 'run': 'https://example.invalid/r/1'})

    def test_pending_failure_and_skip_ci(self):
        pend = FakeRunner(in_main=[A], runs=[{'headSha': A, 'conclusion': None, 'status': 'in_progress', 'url': 'u'}])
        self.assertEqual(me.collect('/r', [], pend)['deploy']['state'], 'pending')
        red = FakeRunner(in_main=[A], runs=[{'headSha': A, 'conclusion': 'failure', 'status': 'completed', 'url': 'u'}])
        self.assertEqual(me.collect('/r', [], red)['deploy']['state'], 'failure')
        skip = FakeRunner(in_main=[A], subject='docs: x [skip ci]', runs=[])
        self.assertEqual(me.collect('/r', [], skip)['deploy']['state'], 'skipped')

    def test_gh_failure_is_unknown_never_success(self):
        class NoGh(FakeRunner):
            def __call__(self, argv, **kw):
                if argv[0] == 'gh':
                    return subprocess.CompletedProcess(argv, 1, stdout='', stderr='boom')
                return super().__call__(argv, **kw)
        self.assertEqual(me.collect('/r', [], NoGh(in_main=[A]))['deploy']['state'], 'unknown')

    def test_fetch_failure_refuses_to_report(self):
        with self.assertRaises(RuntimeError):
            me.collect('/r', [], FakeRunner(fetch_rc=1))

    def test_partial_merge_waits_for_deploy(self):
        out = me.collect('/r', [B], FakeRunner(in_main=[B]))
        self.assertEqual({c['sha']: c['in_main'] for c in out['commits']}, {B: True, A: False})
        self.assertEqual(out['deploy']['state'], 'pending')

    def test_done_note_shas(self):
        self.assertEqual(me.shas_from_text('DONE 317e1d34 Live check: x Result: ok beef1234 cafe'), ['317e1d34', 'beef1234'])
        self.assertEqual(me.shas_from_text('no commits 12345678'), [])


if __name__ == '__main__':
    unittest.main()

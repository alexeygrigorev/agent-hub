"""Merge evidence collection (intake 2e00e01f). Fake runner; no git, gh or Hub."""
import json
import subprocess
import unittest

from agent_hub_host import merge_evidence as me

A = 'a1' * 20
B = 'b2' * 20


class FakeRunner:
    def __init__(self, in_main=(), runs=None, subject='feat: x', cherry='+ ' + A + '\n', fetch_rc=0, ancestors=None):
        self.in_main, self.runs, self.subject, self.cherry, self.fetch_rc = set(in_main), runs or [], subject, cherry, fetch_rc
        self.ancestors = {sha: set(items) for sha, items in (ancestors or {}).items()}
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
            rc = 0 if (ref == 'origin/main' and sha in self.in_main) or (ref != 'origin/main' and (sha == ref or sha in self.ancestors.get(ref, set()))) else 1
        elif argv[:2] == ['git', 'log']:
            out = self.subject + '\n'
        elif argv[0] == 'gh':
            out = json.dumps(self.runs)
        return subprocess.CompletedProcess(argv, rc, stdout=out, stderr='')


class MergeEvidenceTests(unittest.TestCase):
    def test_hub_recorded_head_yields_deploy_evidence(self):
        self.assertEqual(me.hub_recorded_shas({'head': A.upper(), 'commits': [{'sha': B}, {'sha': 'zz'}, 'x']}), [A, B])
        self.assertEqual(me.hub_recorded_shas(None), [])
        self.assertEqual(me.hub_recorded_shas({'head': A, 'commits': 7}), [A])
        runner_ok = FakeRunner(in_main=[A], runs=[{'databaseId': 41479858903, 'headSha': A, 'conclusion': 'success', 'status': 'completed', 'url': 'u'}])
        out = me.collect('/r', me.hub_recorded_shas({'head': A}), runner_ok, fetch=False, include_head=False)
        self.assertEqual(out['deploy']['state'], 'success')
        self.assertEqual(out['deploy']['run'], 'u')
        self.assertEqual(out['deploy']['run_id'], '41479858903')
        self.assertEqual(out['deploy']['conclusion'], 'success')
        self.assertEqual(out['deploy']['deployed_sha'], A)

    def test_unmerged_commit_is_not_in_main_and_deploy_missing(self):
        out = me.collect('/r', [], FakeRunner())
        self.assertEqual(out['head'], A)
        self.assertEqual(out['unmerged'], 1)
        self.assertFalse(out['commits'][0]['in_main'])
        self.assertEqual(out['deploy']['state'], 'missing')

    def test_merged_and_deploy_success(self):
        runner = FakeRunner(in_main=[A, B], cherry='', ancestors={B: [A]}, runs=[{'databaseId': 41, 'headSha': B, 'conclusion': 'success', 'status': 'completed', 'url': 'https://example.invalid/r/1'}])
        out = me.collect('/r', [], runner)
        self.assertTrue(out['commits'][0]['in_main'])
        self.assertEqual(out['deploy'], {'state': 'success', 'sha': A, 'run': 'https://example.invalid/r/1',
                                         'run_id': '41', 'conclusion': 'success', 'deployed_sha': B})

    def test_older_success_does_not_prove_newer_pending_worktree_head(self):
        runs = [
            {'databaseId': 45, 'headSha': B, 'conclusion': None, 'status': 'in_progress', 'url': 'new'},
            {'databaseId': 44, 'headSha': A, 'conclusion': 'success', 'status': 'completed', 'url': 'old'},
        ]
        runner = FakeRunner(in_main=[A, B], cherry='', ancestors={B: [A]}, runs=runs)
        out = me.collect('/r', [A], runner, fetch=False, include_head=False, preferred_deploy_sha=B)
        self.assertEqual(out['deploy']['state'], 'pending')
        self.assertEqual(out['deploy']['deployed_sha'], B)

    def test_pending_failure_and_skip_ci(self):
        pend = FakeRunner(in_main=[A], runs=[{'databaseId': 42, 'headSha': A, 'conclusion': None, 'status': 'in_progress', 'url': 'u'}])
        self.assertEqual(me.collect('/r', [], pend)['deploy']['state'], 'pending')
        red = FakeRunner(in_main=[A], runs=[{'databaseId': 43, 'headSha': A, 'conclusion': 'failure', 'status': 'completed', 'url': 'u'}])
        self.assertEqual(me.collect('/r', [], red)['deploy']['state'], 'failure')
        skip = FakeRunner(in_main=[A], subject='docs: x [skip ci]', runs=[])
        self.assertEqual(me.collect('/r', [], skip)['deploy']['state'], 'skipped')

    def test_skip_ci_commit_is_proven_by_later_deploy_containing_it(self):
        later = FakeRunner(
            in_main=[A, B], subject='docs: x [skip ci]', cherry='', ancestors={B: [A]},
            runs=[{'databaseId': 46, 'headSha': B, 'conclusion': 'success', 'status': 'completed', 'url': 'later'}],
        )
        self.assertEqual(me.deploy_state('/r', A, later), {
            'state': 'success', 'sha': A, 'run': 'later', 'run_id': '46',
            'conclusion': 'success', 'deployed_sha': B,
        })

    def test_cancelled_own_run_is_proven_by_descendant_deploy(self):
        cancelled_then_deployed = FakeRunner(
            in_main=[A, B], cherry='', ancestors={B: [A]},
            runs=[
                {'databaseId': 47, 'headSha': A, 'conclusion': 'cancelled', 'status': 'completed', 'url': 'cancelled-own'},
                {'databaseId': 48, 'headSha': B, 'conclusion': 'success', 'status': 'completed', 'url': 'descendant'},
            ],
        )
        self.assertEqual(me.deploy_state('/r', A, cancelled_then_deployed), {
            'state': 'success', 'sha': A, 'run': 'descendant', 'run_id': '48',
            'conclusion': 'success', 'deployed_sha': B,
        })

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


class CommitExtractionTests(unittest.TestCase):
    def test_active_report_references_excludes_only_explicit_prior_reports(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        other_id = '33333333-3333-4333-8333-333333333333'
        old_sha = 'a1' * 20
        new_sha = 'b2' * 20
        other_sha = 'c3' * 20
        history = [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': other_id, 'kind': 'report', 'reference': other_sha},
            {'id': new_id, 'kind': 'report', 'reference': new_sha, 'supersedes': [old_id]},
            {'id': '44444444-4444-4444-8444-444444444444', 'kind': 'commit', 'reference': old_sha},
        ]
        self.assertEqual(me.active_report_references(history), [other_sha, new_sha, old_sha])

    def test_malformed_or_dangling_supersession_fails_closed(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha = 'a1' * 20
        history = [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': new_id, 'kind': 'report', 'reference': 'b2' * 20, 'supersedes': ['missing-id']},
        ]
        self.assertEqual(me.active_report_references(history), [old_sha, 'b2' * 20])

    def test_malformed_superseding_report_cannot_drop_prior_reference(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        new_id = '22222222-2222-4222-8222-222222222222'
        old_sha = 'a1' * 20
        history = [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': new_id, 'kind': 'report', 'reference': 'not-a-sha', 'supersedes': [old_id]},
        ]
        self.assertEqual(me.active_report_references(history), [old_sha])

    def test_competing_supersession_claims_keep_prior_report_required(self):
        old_id = '11111111-1111-4111-8111-111111111111'
        first_id = '22222222-2222-4222-8222-222222222222'
        second_id = '33333333-3333-4333-8333-333333333333'
        old_sha = 'a1' * 20
        history = [
            {'id': old_id, 'kind': 'report', 'reference': old_sha},
            {'id': first_id, 'kind': 'report', 'reference': 'b2' * 20,
             'supersedes': [old_id]},
            {'id': second_id, 'kind': 'report', 'reference': 'c3' * 20,
             'supersedes': [old_id]},
        ]

        self.assertEqual(me.active_report_references(history), [old_sha, 'b2' * 20, 'c3' * 20])

    def test_uuid_and_known_id_fragments_are_not_candidates(self):
        text = ('DONE afab8367 intake 5e2cc166-0b5c-4a7d-9f11-34a66405e0e7 session c30f1f98 commit 89f55bd7 '
                'and 0c934b64e1 task a5821c84')
        got = me.shas_from_text(text, exclude=('afab8367-0000-4000-8000-000000000000', 'c30f1f98'))
        self.assertNotIn('afab8367', got)
        self.assertNotIn('c30f1f98', got)
        self.assertNotIn('34a66405e0e7', got)
        self.assertNotIn('5e2cc166', got)
        self.assertIn('89f55bd7', got)

    def test_unknown_hex_tokens_are_dropped_by_cat_file(self):
        calls = []

        def runner(argv, **kw):
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0 if argv[-1].startswith('89f55bd7') else 128, stdout='', stderr='')
        self.assertEqual(me.existing_commits('/r', ['a5821c84', '89f55bd7'], runner), ['89f55bd7'])
        self.assertEqual(calls[0][:3], ['git', 'cat-file', '-e'])

    def test_explicit_commit_refs_only_report_and_commit_sha_fields(self):
        sha = '89f55bd76b37b85a8b2e862302ffd8d3b7c0fb9e'
        newer_report = 'fedcba9876543210fedcba9876543210fedcba98'
        history = [
            {'kind': 'report', 'reference': sha},
            {'kind': 'commit', 'reference': '  0C934B64 '},
            {'kind': 'report', 'reference': newer_report},
            {'kind': 'note', 'reference': sha},
            {'kind': 'report', 'reference': 'c30f1f98-06ba-4f58-8a9b-3b3fec17e2a8'},
            {'kind': 'commit', 'reference': 'not-a-commit'},
            {'kind': 'commit', 'reference': ''},
            {'kind': 'commit'},
            {'kind': 'report', 'reference': 'abc123'},
            'not-a-dict',
            None,
        ]
        self.assertEqual(me.explicit_commit_refs(history), ['0c934b64', newer_report])
        self.assertEqual(me.explicit_commit_refs([]), [])
        self.assertEqual(me.explicit_commit_refs(None), [])

    def test_rebased_report_supersedes_older_checkpoint_but_keeps_commit_events(self):
        old_head = '1111111111111111111111111111111111111111'
        current_head = '2222222222222222222222222222222222222222'
        shipped_commit = '3333333333333333333333333333333333333333'
        history = [
            {'kind': 'report', 'reference': old_head},
            {'kind': 'commit', 'reference': shipped_commit},
            {'kind': 'report', 'reference': current_head},
        ]
        self.assertEqual(me.explicit_commit_refs(history), [shipped_commit, current_head])

    def test_collect_include_head_false_skips_head_and_cherry(self):
        runner = FakeRunner(in_main=[B], cherry='', runs=[{'headSha': B, 'conclusion': 'success', 'status': 'completed', 'url': 'u'}])
        out = me.collect('/r', [B], runner, fetch=False, include_head=False)
        self.assertNotIn('head', out)
        self.assertEqual([c['sha'] for c in out['commits']], [B])
        self.assertEqual(out['deploy']['state'], 'success')
        self.assertFalse(any(c[:2] == ['git', 'cherry'] for c in runner.calls))


class OwnCloneTests(unittest.TestCase):
    def test_clone_made_outside_caller_cwd_and_fetch_failure_is_reported(self):
        import os, tempfile
        calls = []

        def runner(argv, **kw):
            calls.append((argv, kw.get('cwd')))
            return subprocess.CompletedProcess(argv, 0, stdout='', stderr='')
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get('XDG_CACHE_HOME')
            os.environ['XDG_CACHE_HOME'] = tmp
            try:
                repo = me.own_clone(runner, remote='file:///nonexistent')
            finally:
                if old is None:
                    os.environ.pop('XDG_CACHE_HOME')
                else:
                    os.environ['XDG_CACHE_HOME'] = old
            self.assertTrue(repo.startswith(tmp))
            self.assertEqual(calls[0][0][:3], ['git', 'clone', '--quiet'])
            self.assertTrue(calls[0][1].startswith(tmp))

        def bad(argv, **kw):
            return subprocess.CompletedProcess(argv, 128, stdout='', stderr='')
        with self.assertRaises(RuntimeError):
            me.fetch_origin('/tmp', bad)


if __name__ == '__main__':
    unittest.main()

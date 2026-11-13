"""Hub2 merge gate from the installed CLI (intake 52f5e130). Fake gh and Hub; no network."""
import argparse
import json
import subprocess
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from agent_hub_host import hub2_merge_evidence as hme
from agent_hub_host import intake

HEAD = 'a1' * 20
MERGE = 'b2' * 20
MAIN = 'c3' * 20
REPO = 'org/project'
PR = f'https://github.com/{REPO}/pull/7'
NOW = lambda: datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class FakeGh:
    def __init__(self, merged=True, runs=(('completed', 'success'),), statuses=(), compare='ahead', pulls=None, fail=(), workflows=0):
        self.merged, self.runs, self.statuses, self.compare, self.pulls, self.fail = merged, runs, statuses, compare, pulls or [], set(fail)
        self.workflows = workflows
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        assert argv[0] == 'gh', argv
        key, out = ' '.join(argv[1:3]), None
        if argv[1:3] == ['pr', 'view']:
            out = {'url': PR, 'baseRefName': 'main', 'headRefOid': HEAD, 'mergedAt': '2026-11-13T10:00:00Z' if self.merged else None,
                   'mergeCommit': {'oid': MERGE} if self.merged else None}
        elif argv[1:3] == ['pr', 'list']:
            out = self.pulls
        elif argv[-1].endswith('/check-runs'):
            key = 'check-runs'
            out = [{'check_runs': [{'status': s, 'conclusion': c} for s, c in self.runs]}]
        elif argv[-1].endswith('/status'):
            key = 'status'
            out = {'state': 'success' if all(s == 'success' for s in self.statuses) and self.statuses else 'pending',
                   'statuses': [{'state': s} for s in self.statuses]}
        elif argv[-1].endswith('/branches/main'):
            key = 'main'
            out = {'commit': {'sha': MAIN}}
        elif argv[-1].endswith('/actions/workflows'):
            key = 'workflows'
            out = {'total_count': self.workflows}
        elif '/compare/' in argv[-1]:
            key = 'compare'
            out = {'status': self.compare}
        if key in self.fail or out is None:
            return subprocess.CompletedProcess(argv, 1, stdout='', stderr='boom')
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(out), stderr='')


class CollectTests(unittest.TestCase):
    def test_merged_green_pr_is_complete_evidence(self):
        e = hme.collect(REPO, PR, runner=FakeGh(), now=NOW)
        self.assertEqual((e['pr_url'], e['base_ref'], e['head_sha'], e['checks_head_sha'], e['checks_state']), (PR, 'main', HEAD, HEAD, 'success'))
        self.assertEqual((e['merge_commit_sha'], e['main_head_sha'], e['merge_commit_in_main']), (MERGE, MAIN, True))
        self.assertEqual((e['checks_total'], e['checks_successful'], e['checks_pending'], e['checks_failed']), (1, 1, 0, 0))
        self.assertNotIn('observation_error', e)
        self.assertEqual(e['checked_at'], '2026-11-13T12:00:00Z')

    def test_merged_pr_without_any_ci_is_recorded_as_none_never_success(self):
        e = hme.collect(REPO, PR, runner=FakeGh(runs=()), now=NOW)
        self.assertEqual((e['checks_state'], e['checks_total']), ('none', 0))
        skipped = hme.collect(REPO, PR, runner=FakeGh(runs=(), workflows=3), now=NOW)
        self.assertEqual(skipped['checks_state'], 'failure', 'a repo with workflows but no checks on the head stays blocked')
        unknown = hme.collect(REPO, PR, runner=FakeGh(runs=(), fail={'workflows'}), now=NOW)
        self.assertIn('workflows', unknown['observation_error'])

    def test_red_pending_unmerged_and_diverged_are_failures(self):
        self.assertEqual(hme.collect(REPO, PR, runner=FakeGh(runs=(('completed', 'failure'),)))['checks_state'], 'failure')
        self.assertEqual(hme.collect(REPO, PR, runner=FakeGh(runs=(('in_progress', None),)))['checks_state'], 'pending')
        open_pr = hme.collect(REPO, PR, runner=FakeGh(merged=False, runs=()))
        self.assertEqual((open_pr['merged_at'], open_pr['merge_commit_in_main'], open_pr['checks_state']), (None, False, 'failure'))
        self.assertFalse(hme.collect(REPO, PR, runner=FakeGh(compare='diverged'))['merge_commit_in_main'])

    def test_unreadable_github_is_named_not_guessed(self):
        e = hme.collect(REPO, PR, runner=FakeGh(fail={'check-runs', 'compare'}))
        self.assertIn('checks could not be read', e['observation_error'])
        self.assertIn('ancestry', e['observation_error'])
        self.assertFalse(e['merge_commit_in_main'])
        none = hme.collect(REPO, None, runner=FakeGh())
        self.assertIn('pass --pr', none['observation_error'])
        self.assertEqual(hme.collect('not a repo', PR)['repository'], 'unknown/unknown')

    def test_pr_discovery_prefers_newest_cited_pr_of_the_project_then_salvaged_branch(self):
        events = [{'body': {'note': f'old https://github.com/{REPO}/pull/3'}},
                  {'body': {'evidence': {'acceptance': [{'proof': 'https://github.com/other/repo/pull/9'}]}}},
                  {'body': {'note': f'merged https://github.com/{REPO}/pull/4 and done'}}]
        self.assertEqual(hme.find_pr(REPO, events, runner=FakeGh()), f'https://github.com/{REPO}/pull/4')
        salvaged = [{'body': {'evidence': {'git_salvage': {'branch': 'hub/intake-abc'}}}}]
        gh = FakeGh(pulls=[{'url': f'https://github.com/{REPO}/pull/11', 'mergedAt': None}, {'url': PR, 'mergedAt': '2026-11-13T10:00:00Z'}])
        self.assertEqual(hme.find_pr(REPO, salvaged, runner=gh), PR)
        self.assertIn('hub/intake-abc', gh.calls[0])
        self.assertIsNone(hme.find_pr(REPO, [], runner=FakeGh()))


class CliTests(unittest.TestCase):
    def parse(self, *argv):
        parser = argparse.ArgumentParser()
        intake.add_commands(parser.add_subparsers(dest='cmd', required=True))
        return parser.parse_args(['intake', *argv])

    def test_hub2_merge_check_posts_canonical_evidence_and_returns_merge_state(self):
        calls = []

        def fake(method, path, payload=None, **kw):
            calls.append((method, path, payload))
            if path.endswith('/merge-state'):
                return {'merge_gate': {'ok': True}}
            if '/history' in path:
                return {'events': [{'seq': 1, 'body': {'note': 'PR ' + PR}}], 'next': None}
            if method == 'GET':
                return {'intake': {'id': 'i-1', 'project': REPO}, 'task': {'id': 'i-1'}}
            return {'appended': True}
        with patch.object(intake, 'request', side_effect=fake), \
                patch.object(hme, 'collect', return_value={'kind': 'merge_gate', 'repository': REPO}) as collect:
            result = intake.execute(self.parse('merge-check', 'i-1', '--key', 'mc-1'))
        collect.assert_called_once_with(REPO, PR)
        self.assertEqual(result, {'merge_gate': {'ok': True}})
        post = [c for c in calls if c[0] == 'POST']
        self.assertEqual(post, [('POST', '/api/intake/tasks/i-1/merge-evidence', {'key': 'mc-1', 'evidence': {'kind': 'merge_gate', 'repository': REPO}})])

    def test_dry_run_and_explicit_pr_do_not_post(self):
        with patch.object(intake, 'request', return_value={'intake': {'id': 'i-1', 'project': REPO}}) as request, \
                patch.object(hme, 'collect', return_value={'kind': 'merge_gate'}) as collect:
            result = intake.execute(self.parse('merge-check', 'i-1', '--pr', '7', '--dry-run'))
        collect.assert_called_once_with(REPO, '7')
        self.assertTrue(result['dry_run'])
        self.assertEqual([c.args[0] for c in request.call_args_list], ['GET'])

    def test_merge_state_and_reevaluate_routes(self):
        with patch.object(intake, 'request', return_value={}) as request:
            intake.execute(self.parse('merge-state', 'i-1'))
            intake.execute(self.parse('reevaluate', 'i-1', '--reason', 'PR merged', '--key', 'r-1'))
        self.assertEqual(request.call_args_list[0].args, ('GET', '/api/intake/tasks/i-1/merge-state'))
        self.assertEqual(request.call_args_list[1].args, ('POST', '/api/intake/tasks/i-1/reevaluate', {'key': 'r-1', 'reason': 'PR merged'}))


if __name__ == '__main__':
    unittest.main()

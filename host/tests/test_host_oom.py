import json
import subprocess
import unittest
from unittest.mock import patch

from agent_hub_host import host_oom
from agent_hub_host.runtime_diagnostics import runtime_diagnostic


def event(constraint='CONSTRAINT_MEMCG', limit='/user.slice/user-1000.slice/user@1000.service/aplexer-workload-3237056a.scope'):
    return f'oom-kill:constraint={constraint},nodemask=(null),cpuset=/,oom_memcg={limit},task_memcg={limit},task=codex,pid=42,uid=1000'


class HostOomTests(unittest.TestCase):
    def setUp(self):
        host_oom._seen = set()

    def test_limiting_group_not_victim_defines_host_oom(self):
        for limit in ('/', '/user.slice', '/user.slice/user-1000.slice',
                      '/user.slice/user-1000.slice/user@1000.service', '/system.slice'):
            self.assertTrue(host_oom.is_host_oom(event(limit=limit)), limit)
        for constraint in ('CONSTRAINT_NONE', 'CONSTRAINT_CPUSET', 'CONSTRAINT_MEMORY_POLICY'):
            self.assertTrue(host_oom.is_host_oom(event(constraint)))
        for limit in ('/user.slice/session-123.scope', '/user.slice/aplexer-workload-abc.scope',
                      '/user.slice/aplexer-workload-abc.scope/engine', '/system.slice/launcher-abc.scope'):
            self.assertFalse(host_oom.is_host_oom(event(limit=limit)), limit)
        for text in ('private CONSTRAINT_NONE', event('UNKNOWN'), event(limit='(null)'),
                     event(limit='/../'), 'oom-kill:constraint=CONSTRAINT_MEMCG'):
            self.assertFalse(host_oom.is_host_oom(text))

    def test_session_failure_record_survives_without_host_failure(self):
        native = '2505c5ae-605e-426a-8908-75a293bdc4c8'
        record = dict(native_id=native, incarnation=1000000, runtime_exit={'oom_killed': True})
        result = runtime_diagnostic(record, dict(finished_at_ms=3000000), query=lambda _: '')
        self.assertEqual(result['reason'], 'engine_oom_killed')
        self.assertFalse(host_oom.is_host_oom(event()))

    def test_recent_global_or_slice_event_counts_once_and_ignores_session_noise(self):
        rows = [dict(MESSAGE=event(), __REALTIME_TIMESTAMP='999000000'),
                dict(MESSAGE=event('CONSTRAINT_NONE'), __REALTIME_TIMESTAMP='998000000'),
                dict(MESSAGE=event(limit='/user.slice'), __REALTIME_TIMESTAMP='997000000'),
                dict(MESSAGE=event('CONSTRAINT_NONE'), __REALTIME_TIMESTAMP='1001000000'),
                dict(MESSAGE=event('CONSTRAINT_NONE'), __REALTIME_TIMESTAMP='699000000')]
        query = lambda _: '\n'.join(json.dumps(row) for row in rows)
        self.assertEqual(host_oom.read_host_oom_delta(query=query, now=1000), 2)
        self.assertEqual(host_oom.read_host_oom_delta(query=query, now=1000), 0)
        self.assertEqual(host_oom.read_host_oom_delta(query=query, now=1400), 0)

    def test_unavailable_and_malformed_observation_are_unknown(self):
        for raw in ('invalid json', 'x' * 262145, '{}'):
            self.assertIsNone(host_oom.read_host_oom_delta(query=lambda _: raw, now=1000))
        for error in (OSError(), subprocess.TimeoutExpired('journalctl', 2)):
            with patch.object(host_oom.subprocess, 'run', side_effect=error):
                self.assertIsNone(host_oom.read_host_oom_delta())
        self.assertEqual(host_oom.read_host_oom_delta(query=lambda _: '', now=1000), 0)

    def test_real_command_empty_grep_is_zero_but_access_failures_are_unknown(self):
        for code, stderr, expected in ((1, b'', 0), (0, b'', 0),
                                       (1, b'sudo: access denied', None), (2, b'', None)):
            result = subprocess.CompletedProcess([], code, stdout=b'', stderr=stderr)
            with patch.object(host_oom.subprocess, 'run', return_value=result):
                self.assertEqual(host_oom.read_host_oom_delta(now=1000), expected)

    def test_scoped_counter_in_diagnostics_is_bounded_integer(self):
        from agent_hub_host.cpu_resources import sanitize_cpu_admission
        base = dict(expected_cpu_cores=1, ramping_cpu_cores=0, limit_cpu_cores=12, target_utilization=1)
        self.assertEqual(sanitize_cpu_admission(dict(base, host_oom_kills_delta=1))['host_oom_kills_delta'], 1)
        for value in (True, -1, 1.5, float('inf')):
            self.assertIsNone(sanitize_cpu_admission(dict(base, host_oom_kills_delta=value)))

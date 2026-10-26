import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

from agent_hub_host import service_health as health
from agent_hub_host.host_health_monitor import HostHealthMonitor

NOW = 1800000000
UNIT = {'service_id':'workerd', 'backend':'systemd', 'unit':'workerd.service', 'user':True}
DOCKER = {'service_id':'docker-service', 'backend':'docker', 'container':'long-lived'}


def unit(**changes):
    value = dict(Id='workerd.service', LoadState='loaded', ActiveState='failed', SubState='failed',
                 Result='timeout', ExecMainCode='2', ExecMainStatus='9',
                 ExecMainStartTimestampMonotonic='900000000', ExecMainExitTimestampMonotonic='990000000')
    value.update(changes)
    return '\n'.join(k+'='+v for k,v in value.items()).encode()


def stamp(seconds):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace('+00:00','Z')


def container(**changes):
    state = dict(Status='exited', Running=False, OOMKilled=False, ExitCode=1,
                 StartedAt=stamp(NOW-60), FinishedAt=stamp(NOW-10))
    state.update(changes)
    return json.dumps({'Id':'a'*64,'Name':'/long-lived','State':state}).encode()


class ServiceHealthTests(unittest.TestCase):
    def collect(self, definition=UNIT, raw=None, **kwargs):
        return health.collect_runtime_services([definition], now=NOW,
                    runner=lambda argv,timeout: raw if raw is not None else unit(), monotonic=lambda:1000, **kwargs)[0]

    def test_systemd_actual_recent_timeout_oom_and_crash(self):
        for result, code in [('timeout','2'),('oom-kill','2'),('core-dump','3'),('signal','2'),('exit-code','1')]:
            with self.subTest(result=result):
                row=self.collect(raw=unit(Result=result,ExecMainCode=code))
                self.assertEqual(row['worker'],'broken')
                self.assertEqual(row['reason'],'service_timeout' if result=='timeout' else 'service_crash')
                self.assertEqual(row['observed_at'],stamp(NOW-10))

    def test_systemd_normal_stop_unknown_error_and_stale_do_not_fabricate_failure(self):
        cases=[dict(ActiveState='inactive',Result='success'),dict(Result='success'),
               dict(Result='signal',ExecMainStatus='15'),dict(ExecMainExitTimestampMonotonic='600000000'),
               dict(ExecMainStartTimestampMonotonic='999000000'),dict(ExecMainExitTimestampMonotonic='1001000000'),
               dict(Id='other.service'),dict(LoadState='not-found'),dict(ExecMainExitTimestampMonotonic='oops')]
        for changes in cases:
            with self.subTest(changes=changes):
                self.assertEqual(self.collect(raw=unit(**changes))['worker'],'unknown')

    def test_verified_failed_invocation_during_automatic_restart_is_not_lost(self):
        self.assertEqual(self.collect(raw=unit(ActiveState='activating',SubState='auto-restart'))['worker'],'broken')
        self.assertEqual(self.collect(DOCKER,container(Status='restarting',Running=True,Restarting=True))['worker'],'broken')
        self.assertEqual(self.collect(DOCKER,container(Status='restarting',Running=True,Restarting=True,StartedAt=stamp(NOW-1)))['worker'],'unknown')

    def test_systemd_running_is_positive_observation(self):
        row=self.collect(raw=unit(ActiveState='active',SubState='running',Result='success'))
        self.assertEqual((row['worker'],row['reason'],row['observed_at']),('alive','ok',stamp(NOW)))

    def test_docker_same_container_recent_failure_and_oom(self):
        for changes in ({'ExitCode':1},{'ExitCode':137,'OOMKilled':True}):
            row=self.collect(DOCKER,container(**changes))
            self.assertEqual(row['worker'],'broken')
            self.assertEqual(row['observed_at'],stamp(NOW-10))

    def test_docker_normal_stop_stale_reset_and_unknown_are_not_choking(self):
        for changes in ({'ExitCode':0},{'ExitCode':143},{'ExitCode':130},
                        {'FinishedAt':stamp(NOW-400),'StartedAt':stamp(NOW-500)},
                        {'FinishedAt':stamp(NOW+5)}, {'StartedAt':stamp(NOW-1)},
                        {'FinishedAt':'2026-10-26T00:00:00'}, {'ExitCode':True},
                        {'Running':None}):
            with self.subTest(changes=changes):
                self.assertEqual(self.collect(DOCKER,container(**changes))['worker'],'unknown')

    def test_docker_identity_must_match_and_environment_never_requested(self):
        calls=[]
        def runner(argv,timeout):
            calls.append(argv)
            return container().replace(b'/long-lived',b'/different')
        row=health.collect_runtime_services([DOCKER],now=NOW,runner=runner)[0]
        self.assertEqual(row['worker'],'unknown')
        self.assertNotIn('.Config',str(calls))
        self.assertNotIn('Env',str(calls))
        self.assertEqual(calls[0][-2:],['--','long-lived'])

    def test_config_no_discovery_no_option_or_command_injection(self):
        runner=Mock(return_value=b'')
        for definitions in (None,{},[UNIT]*9,[dict(UNIT,unit='--system')],
                            [dict(DOCKER,container='--all')],[dict(UNIT,command='reboot')]):
            health.collect_runtime_services(definitions,runner=runner)
        runner.assert_not_called()

    def test_timeout_and_excess_and_invalid_output_remain_unknown(self):
        for bad in (TimeoutError('arbitrary secret'),OSError('private path'),ValueError('invalid'),
                    b'x'*(health.MAX_OUTPUT+1),b'\xff',b'{}'):
            def runner(argv,timeout):
                if isinstance(bad,Exception):raise bad
                return bad
            row=health.collect_runtime_services([UNIT],now=NOW,runner=runner)[0]
            self.assertEqual(row['worker'],'unknown')
            self.assertNotIn('arbitrary',json.dumps(row));self.assertNotIn('private',json.dumps(row))

    def test_total_probe_budget_and_duplicate_ids(self):
        clock=iter([0,1,2,9,10])
        runner=Mock(return_value=unit())
        rows=health.collect_runtime_services([UNIT,dict(DOCKER),UNIT],now=NOW,runner=runner,monotonic=lambda:next(clock))
        self.assertEqual(runner.call_count,1)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[1]['worker'],'unknown')

    def test_bounded_probe_reads_exact_command_and_reaps_only_own_child(self):
        self.assertEqual(health.bounded_probe([sys.executable,'-c','print("ok")'],1),b'ok\n')
        with self.assertRaises(ValueError):
            health.bounded_probe([sys.executable,'-c',f'print("x"*{health.MAX_OUTPUT+1})'],1)
        with self.assertRaises(TimeoutError):
            health.bounded_probe([sys.executable,'-c','import time; time.sleep(2)'],.05)

    def test_monitor_merges_runtime_services_without_erasing_broken(self):
        legacy=dict(service_id='same',worker='broken',reason='worker_broken',observed_at=stamp(NOW),status_file_age_s=0)
        runtime=[dict(legacy,worker='alive',reason='ok'),health._dto('other','broken','service_crash',stamp(NOW))]
        monitor=HostHealthMonitor(config={'runtime_services':[UNIT]},machine_id='test-host',time_fn=lambda:NOW)
        with patch('agent_hub_host.host_health_monitor.collect_supervisor_services',return_value=[legacy]), \
             patch.object(health,'collect_runtime_services',return_value=runtime) as collect:
            result=monitor.step(submit=False)
        self.assertFalse(result['posted'])
        self.assertEqual(len(result['payload']['services']),2)
        self.assertTrue(all(s['worker']=='broken' for s in result['payload']['services']))
        self.assertEqual(collect.call_args.args[0],[UNIT])


class ControllerHealthTests(unittest.TestCase):
    def setUp(self):
        path=Path(__file__).resolve().parents[2]/'scripts/session-host-supervisor.py'
        spec=importlib.util.spec_from_file_location('supervisor_service_health',path)
        self.module=importlib.util.module_from_spec(spec);spec.loader.exec_module(self.module)
        self.receipt=dict(id='00000000-0000-0000-0000-000000000001',tag='controller',workspace='/owned',engine='shell',created_at_ms=1,command=['python','host'])
        self.dead=dict(self.receipt,phase='broken',worker_alive=False)

    def test_exact_dead_child_only_and_unknown_or_changed_receipts_never_publish(self):
        self.assertEqual(self.module.controller_crash_service(self.dead,self.receipt)['worker'],'broken')
        for row in (dict(self.dead,id='other'),dict(self.dead,created_at_ms=2),
                    dict(self.dead,worker_alive=None),dict(self.dead,phase='starting'),
                    dict(self.dead,phase='running',worker_alive=True)):
            self.assertIsNone(self.module.controller_crash_service(row,self.receipt))

    def test_publication_preserves_other_services_and_uses_host_credentials(self):
        publish=Mock()
        other=health._dto('workerd','broken','service_crash',stamp(NOW))
        payload={'machine_id':'host','services':[other], 'observed_at':stamp(NOW)}
        factory=Mock();factory.return_value.step.return_value={'payload':payload}
        with patch('agent_hub_host.config.load_host_config',return_value={'machine_id':'host','base_url':'https://host.example','access_token':'synthetic-host'}):
            self.assertTrue(self.module.publish_controller_crash(self.dead,self.receipt,'/owned/health.json',collector_factory=factory,publish=publish))
        self.assertEqual([s['service_id'] for s in publish.call_args.args[0]['services']],['workerd','hub-controller'])
        self.assertEqual(publish.call_args.kwargs['token'],'synthetic-host')
        self.assertEqual(factory.call_args.kwargs['machine_id'],'host')

    def test_no_publication_for_reused_identity_disabled_collector_or_probe_failure(self):
        publish=Mock();factory=Mock();factory.return_value.step.return_value={'disabled':True}
        with patch('agent_hub_host.config.load_host_config',return_value={'machine_id':'host','base_url':'https://host.example','access_token':'synthetic'}):
            self.assertFalse(self.module.publish_controller_crash(dict(self.dead,id='other'),self.receipt,'unused',collector_factory=factory,publish=publish))
            factory.assert_not_called()
            self.assertFalse(self.module.publish_controller_crash(self.dead,self.receipt,'unused',collector_factory=factory,publish=publish))
        publish.assert_not_called()

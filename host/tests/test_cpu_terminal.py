"""CPU finalization regressions through real sampling/report cycle orchestration."""
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from agent_hub_host import resource_management as rm

NATIVE='11111111-1111-4111-8111-111111111111'


def entry(**changes):
    return dict(session_id='cpu-session', native_id=NATIVE, native_created_at_ms=1000000,
                state='running', started_at='1970-01-01T00:16:40Z', managed=False,
                workspace='/repo', tag='cpu', **changes)


def sample(counter, runtime):
    return dict(cpu_seconds=counter, cpu_identity='cg:exact-native:1', runtime_s=runtime,
                avg_cpu_cores=counter/runtime if runtime else None, rss_mb=30)


class CpuTerminalTests(unittest.TestCase):
    def cycle(self, journal, record, at, measured):
        posts=[]
        def request(method, path, payload=None):
            if method=='GET':return {'config':{}, 'sessions':[record]}
            posts.append(payload)
            return {'reported':True}
        native=SimpleNamespace(session_id=NATIVE,created_at_ms=1000000,phase='exited' if record['state']=='exited' else 'running')
        with patch('agent_hub_host.native_sessions.list_sessions',return_value=[native]), \
             patch.object(rm,'sample_session_tree',return_value=measured), \
             patch.object(rm,'is_reapable_session',return_value=False):
            result=rm.run_watchdog_cycle(request,journal_dir=journal,now_ms=at)
        self.assertTrue(result['ok'],result)
        return posts[-1]['samples'][0]

    def test_repeated_readable_terminal_counter_keeps_lifetime_p95_and_final_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            active=entry()
            self.cycle(directory,active,1000000,sample(0,0))
            live=self.cycle(directory,active,1100000,sample(200,100))
            self.assertEqual(live['p95_cpu_cores'],2)
            final=entry();final.update(state='exited',finished_at='1970-01-01T00:18:20Z')
            first=self.cycle(directory,final,1200123,sample(200,200))
            self.assertEqual(first['observed_at'],'1970-01-01T00:20:00.123Z')
            self.assertNotIn('current_cpu_cores',first)
            frozen=rm.load_state(directory)['peaks'][rm._peak_key('cpu-session',NATIVE)]
            for at in (3300000,7300000,13300000):
                row=self.cycle(directory,final,at,sample(999,at/1000-1000))
                self.assertEqual({k:row[k] for k in ('p95_cpu_cores','cpu_seconds','runtime_s','avg_cpu_cores')},
                                 {'p95_cpu_cores':2,'cpu_seconds':200,'runtime_s':100,'avg_cpu_cores':2})
                self.assertNotIn('current_cpu_cores',row)
            latest=rm.load_state(directory)['peaks'][rm._peak_key('cpu-session',NATIVE)]
            self.assertEqual(latest['cpu_histogram'],frozen['cpu_histogram'])
            self.assertEqual(latest['cpu_baseline'],frozen['cpu_baseline'])

    def test_missing_terminal_native_then_readable_remnant_does_not_resume_cpu(self):
        with tempfile.TemporaryDirectory() as directory:
            self.cycle(directory,entry(),1000000,sample(0,0))
            self.cycle(directory,entry(),1100000,sample(200,100))
            final=entry();final.update(state='exited',finished_at='1970-01-01T00:18:20Z')
            cached=self.cycle(directory,final,1200000,None)
            remnant=self.cycle(directory,final,3300000,sample(999,2300))
            for key in ('cpu_seconds','runtime_s','p95_cpu_cores'):
                self.assertEqual(remnant[key],cached[key])
            self.assertNotIn('current_cpu_cores',remnant)

    def test_first_terminal_without_cpu_keeps_unknown_instead_of_inventing_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            final=entry();final.update(state='exited',finished_at='1970-01-01T00:18:20Z')
            row=self.cycle(directory,final,1200000,None)
            self.assertNotIn('p95_cpu_cores',row)
            self.assertNotIn('cpu_seconds',row)
            self.assertNotIn('current_cpu_cores',row)
            row=self.cycle(directory,final,1300000,sample(100,300))
            self.assertNotIn('p95_cpu_cores',row)
            self.assertNotIn('cpu_seconds',row)

    def test_dispatcher_posts_heartbeat_before_fractional_cpu_sample_report(self):
        from agent_hub_host import fleet
        with tempfile.TemporaryDirectory() as directory:
            settings={'version':1,'workspaces':{'demo':'/repo'},'journal_dir':directory,
                      'subscriptions':[{'service':'codex','engine':'codex','account_label':'shared-codex','admission_argv':['gate']}]}
            events=[]
            def request(method,path,payload=None):
                events.append((path,payload))
                if path=='/api/fleet/resources/watchdog':return {'config':{},'sessions':[entry()]}
                if path=='/api/fleet/resources/report':return {'reported':True}
                return {'offers':[],'sessions':[]}
            def runner(argv,**kwargs):
                if argv[0]=='gate':return {'launch_allowed':True,'profile_supported':True,'account_label':'shared-codex','observed_at':fleet.utcnow()}
                return [] if argv[0]=='aplexer' else {}
            native=SimpleNamespace(session_id=NATIVE,created_at_ms=1000000,phase='running')
            with patch('agent_hub_host.system.get_system_info',return_value={'capabilities':{}}), \
                 patch('agent_hub_host.api.get_heartbeat_payload',return_value={'type':'heartbeat','cpu_usage':{'observed_at':'1970-01-01T00:18:20.123456Z','interval_s':.2}}), \
                 patch('agent_hub_host.native_sessions.list_sessions',return_value=[native]), \
                 patch.object(rm,'sample_session_tree',return_value=sample(200,100)), \
                 patch.object(rm.time,'time',return_value=1101.789), \
                 patch.object(rm,'is_reapable_session',return_value=False):
                fleet.Dispatcher(settings,request,runner).once()
            paths=[event[0] for event in events]
            self.assertLess(paths.index('/api/host/poll'),paths.index('/api/fleet/resources/report'))
            report=next(payload for path,payload in events if path=='/api/fleet/resources/report')
            self.assertEqual(report['samples'][0]['observed_at'],'1970-01-01T00:18:21.789Z')

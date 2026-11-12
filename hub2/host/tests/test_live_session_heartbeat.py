import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent_hub_host.agent import Bridge, Journal, LIVE_SESSION_HEARTBEAT_SECONDS


class _StopServe(BaseException):
    pass


class _IdleSocket:
    def __init__(self, clock):
        self.clock = clock
        self.timeouts = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def recv(self, timeout=None):
        self.timeouts.append(timeout)
        if len(self.timeouts) == 1:
            self.clock[0] += timeout
            raise TimeoutError()
        raise _StopServe()


class _Client:
    def __init__(self, socket):
        self.idle_socket = socket
        self.heartbeats = []
        self.requests = []

    def heartbeat(self, labels, engines=None, capabilities=None, native_sessions=None):
        self.heartbeats.append((labels, engines, capabilities, native_sessions))

    def socket(self, machine_id):
        self.machine_id = machine_id
        return self.idle_socket

    def request(self, method, path, body=None, key=None):
        self.requests.append((method, path, body, key))
        return {}


class LiveSessionHeartbeatTests(unittest.TestCase):
    def test_idle_socket_refreshes_live_session_snapshot_without_offer_polling(self):
        self.assertEqual(LIVE_SESSION_HEARTBEAT_SECONDS, 30)
        clock = [0.0]
        socket = _IdleSocket(clock)
        client = _Client(socket)
        with tempfile.TemporaryDirectory() as temp:
            bridge = Bridge.__new__(Bridge)
            bridge.client = client
            bridge.journal = Journal(Path(temp) / "journal.sqlite")
            bridge.launcher = SimpleNamespace(capabilities=lambda: {"shell": {"headless"}})
            bridge.engines = {"shell"}
            bridge.machine_id = None
            bridge.budget_paused_until = 0
            bridge.budget_safety_paused_until = 0
            bridge.executable = "unused"
            bridge.runner = None

            try:
                with (
                    patch("agent_hub_host.agent.time.monotonic", side_effect=lambda: clock[0]),
                    patch("agent_hub_host.agent.time.time", return_value=1000),
                    self.assertRaises(_StopServe),
                ):
                    bridge.serve("machine-1", ["worker"])
            finally:
                bridge.journal.db.close()

        self.assertEqual(len(client.heartbeats), 2)
        self.assertEqual(socket.timeouts[0], LIVE_SESSION_HEARTBEAT_SECONDS)
        self.assertEqual(client.heartbeats[1][3], [])
        self.assertNotIn(("GET", "/api/offers", None, None), client.requests)


    def test_poison_first_row_does_not_block_serve_heartbeat_outbox_or_reconnect(self):
        class Socket(_IdleSocket):
            def __init__(self,clock,disconnect=False):
                super().__init__(clock);self.disconnect=disconnect;self.frames=[]
            def send(self,text):self.frames.append(json.loads(text))
            def recv(self,timeout=None):
                if self.disconnect:raise RuntimeError('synthetic_disconnect')
                self.timeouts.append(timeout)
                if len(self.timeouts)<=6:
                    self.clock[0]+=timeout
                    raise TimeoutError()
                raise _StopServe()
        class Client(_Client):
            def __init__(self,sockets):
                super().__init__(sockets[0]);self.sockets=iter(sockets)
            def socket(self,machine_id):return next(self.sockets)
        clock=[0.0];first=Socket(clock,True);second=Socket(clock);client=Client([first,second])
        root=Path(__file__).resolve().parents[3]/'.local/tmp';root.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=root,prefix='observation-serve-') as temp:
            bridge=Bridge.__new__(Bridge);bridge.client=client
            bridge.journal=Journal(Path(temp)/'journal.sqlite')
            bridge.launcher=SimpleNamespace(capabilities=lambda:{'shell':{'headless'}})
            bridge.engines={'shell'};bridge.machine_id=None
            bridge.budget_paused_until=bridge.budget_safety_paused_until=0
            bridge.executable='synthetic-native'
            identities={}
            for session,native in [('a-poison','11111111-1111-4111-8111-111111111111'),
                                   ('z-healthy','22222222-2222-4222-8222-222222222222')]:
                bridge.journal.db.execute('INSERT INTO launches(operation_id,session_id,epoch,digest,workspace,tag,native_id,state,native_dispatch_started) VALUES(?,?,?,?,?,?,?,?,?)',
                    ('op-'+session,session,1,'d'*64,temp,'task-'+session,native,'running',1))
                identities[native]={'id':native,'tag':'task-'+session,'workspace':temp,'phase':'running'}
            bridge.journal.db.commit()
            identities['11111111-1111-4111-8111-111111111111']['workspace']='/foreign-private-workspace'
            before=dict(bridge.journal.db.execute("SELECT * FROM launches WHERE session_id='a-poison'").fetchone())
            bridge.journal.enqueue('existing','/api/sessions/z-healthy/commands/queued/ack',
                                   {'epoch':1,'result':{'error':'synthetic_queued_failure'}})
            effects=[]
            def runner(argv,**kwargs):
                effects.append(argv[1:]);self.assertEqual(argv[1],'status')
                return subprocess.CompletedProcess(argv,0,json.dumps(identities[argv[2]]),'')
            bridge.runner=runner
            try:
                with patch('agent_hub_host.agent.time.monotonic',side_effect=lambda:clock[0]), \
                        patch('agent_hub_host.agent.time.time',return_value=1000), \
                        patch('agent_hub_host.agent.time.sleep'), \
                        self.assertLogs('agent_hub_host.agent',level='ERROR'),self.assertRaises(_StopServe):
                    bridge.serve('machine-1',['worker'])
                self.assertEqual(dict(bridge.journal.db.execute("SELECT * FROM launches WHERE session_id='a-poison'").fetchone()),before)
                self.assertFalse(bridge.journal.db.execute('SELECT 1 FROM outbox').fetchone())
            finally:bridge.journal.close_thread()
        self.assertGreaterEqual(len(client.heartbeats),3)
        for heartbeat in client.heartbeats:
            self.assertEqual([row['session_id'] for row in heartbeat[3]],['z-healthy'])
        self.assertEqual(len(client.requests),1)
        self.assertEqual(client.requests[0][1],'/api/sessions/z-healthy/commands/queued/ack')
        for socket in [first,second]:
            errors=[f for f in socket.frames if f['type']=='host.error']
            self.assertEqual(len(errors),1)
            self.assertEqual(errors[0]['stage'],'host_observation')
            self.assertNotIn('foreign-private-workspace',json.dumps(errors))
        self.assertTrue(all(argv[0]=='status' for argv in effects))


if __name__ == "__main__":
    unittest.main()

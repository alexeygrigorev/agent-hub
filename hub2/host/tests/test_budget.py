import json,tempfile,threading,time,unittest
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock,patch
from agent_hub_host.agent import Bridge,Journal
from agent_hub_host.client import Client,ApiError

class BudgetTests(unittest.TestCase):
    def test_real_http_budget_deadline_and_no_secret_error(self):
        for body,expected in [
            ({'error':'budget_paused','retry_at':int(time.time()*1000)+60000},True),
            ({'error':'budget_paused','retry_at':True},False),
            ({'error':'budget_paused','retry_at':int(time.time()*1000)+2*86400000},False),
            ({'error':'quota_refused','retry_at':int(time.time()*1000)+60000},False)]:
            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    self.send_response(429);self.end_headers();self.wfile.write(json.dumps(body).encode())
                def log_message(self,*args):pass
            server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                with self.assertRaises(ApiError) as caught:
                    Client(f'http://127.0.0.1:{server.server_port}','private-bearer').request('GET','/api/offers')
                error=caught.exception
                self.assertEqual(bool(error.retry_at),expected)
                self.assertNotIn('private-bearer',str(error))
                self.assertEqual(error.status,429)
            finally:server.shutdown();server.server_close();thread.join()

    def test_paused_host_keeps_stop_socket_and_flushes_safety_past_ordinary_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            journal=Journal(Path(directory)/'private/journal.sqlite')
            journal.emit('ordinary','/api/sessions/session-1/commands/cmd-1/ack',{'result':'retained'})
            journal.emit('started','/api/sessions/session-1/started',{'epoch':1,'operation_id':'operation-1','native_id':'11111111-1111-4111-8111-111111111111'})
            journal.emit('finish','/api/sessions/session-1/finish',{'native_exit_confirmed':True})
            stop_path='/api/sessions/session-1/commands/cmd-2/ack?safety=operator_stop'
            journal.emit('stop',stop_path,{'result':{'stopped':True}})
            calls=[];client=Mock()
            def request(method,path,body,key):
                calls.append((method,path,key))
                if not (path.endswith('/finish') or path.endswith('/started') or path==stop_path):
                    raise AssertionError('ordinary network during pause')
                return {}
            client.request.side_effect=request
            wire=Mock();client.socket.return_value.__enter__=Mock(return_value=wire)
            client.socket.return_value.__exit__=Mock(return_value=False)
            stop={'id':'cmd-3','kind':'stop','session_id':'session-1','epoch':1}
            wire.recv.side_effect=[json.dumps({'type':'session.offer','offer':{}}),
                json.dumps({'type':'session.command','command':{'kind':'capture'}}),
                json.dumps({'type':'session.command','command':stop}),KeyboardInterrupt]
            bridge=Bridge.__new__(Bridge);bridge.client=client;bridge.journal=journal
            bridge.budget_paused_until=int(time.time()*1000)+60000;bridge.budget_safety_paused_until=0
            bridge.reconcile=Mock();bridge.heartbeat=Mock();bridge.command=Mock()
            with self.assertRaises(KeyboardInterrupt):bridge.serve('machine-1',[])
            self.assertGreaterEqual(bridge.reconcile.call_count,4);bridge.heartbeat.assert_not_called()
            bridge.command.assert_called_once_with(stop)
            client.socket.assert_called_once_with('machine-1',safety=True)
            self.assertEqual(calls,[('POST','/api/sessions/session-1/started','started'),('POST','/api/sessions/session-1/finish','finish'),('POST',stop_path,'stop')])
            self.assertEqual([r['key'] for r in journal.db.execute('SELECT key FROM outbox')],['ordinary'])
            journal.db.close()

    def test_exhausted_safety_budget_waits_without_reconnect_or_remote_flush(self):
        bridge=Bridge.__new__(Bridge);bridge.client=Mock();bridge.journal=Mock()
        bridge.budget_paused_until=int(time.time()*1000)+60000
        bridge.budget_safety_paused_until=bridge.budget_paused_until
        bridge.reconcile=Mock();bridge.heartbeat=Mock()
        with patch('agent_hub_host.agent.time.sleep',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):bridge.serve('machine-1',[])
        bridge.reconcile.assert_called_once();bridge.client.socket.assert_not_called()
        bridge.journal.flush.assert_not_called();bridge.heartbeat.assert_not_called()

    def test_real_websocket_handshake_preserves_pause_and_exact_safety_query(self):
        deadline=int(time.time()*1000)+60000;seen=[]
        class Handler(BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_GET(self):
                seen.append(self.path)
                body=json.dumps({'error':'budget_paused','retry_at':deadline}).encode()
                self.send_response(429);self.send_header('Content-Length',str(len(body)))
                self.send_header('Connection','close');self.end_headers();self.wfile.write(body)
            def log_message(self,*args):pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with self.assertRaises(ApiError) as caught:
                Client(f'http://127.0.0.1:{server.server_port}','private-bearer').socket('machine-1',safety=True)
            self.assertEqual(caught.exception.retry_at,deadline)
            self.assertEqual(seen,['/api/connect?safety=operator_stop'])
            self.assertNotIn('private-bearer',str(caught.exception))
        finally:server.shutdown();server.server_close();thread.join()

    def test_dispatch_does_not_swallow_budget_pause(self):
        bridge=Bridge.__new__(Bridge)
        error=ApiError('Hub HTTP 429',429,'budget_paused',int(time.time()*1000)+60000)
        bridge.command=Mock(side_effect=error);socket=Mock()
        with self.assertRaises(ApiError) as caught:
            bridge.dispatch({'type':'session.command','command':{}},socket,Mock())
        self.assertIs(caught.exception,error);socket.send.assert_not_called()

if __name__=='__main__':unittest.main()

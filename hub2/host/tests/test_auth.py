import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from agent_hub_host.cli import main
from agent_hub_host.client import save_config


class AuthHandler(BaseHTTPRequestHandler):
    def log_message(self,*args):pass

    def do_POST(self):
        body=json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        self.server.calls.append((self.path,body,self.headers.get('Authorization')))
        status=200
        if self.path=='/api/device/code':
            value={'device_code':'d'*48,'user_code':'PUBLIC1234','verification_uri_complete':self.server.base+'/device?user_code=PUBLIC1234','expires_in':600,'interval':5}
            if self.server.bad_uri:value['verification_uri_complete']='https://foreign.invalid/device?user_code=PUBLIC1234'
        elif self.path=='/api/device/token':
            self.server.polls+=1
            if self.server.polls==1:status,value=428,{'error':'authorization_pending'}
            elif self.server.polls==2:status,value=429,{'error':'slow_down'}
            else:value={'access_token':'a'*48,'refresh_token':'r'*48,'token_type':'Bearer','expires_in':3600}
        elif self.path=='/api/token/refresh':
            self.server.refreshes+=1
            if self.server.lose_refresh:status,value=503,{'error':'temporarily_unavailable'}
            else:value={'access_token':'b'*48,'refresh_token':'s'*48,'token_type':'Bearer','expires_in':3600}
        elif self.path=='/api/token/revoke':
            if self.server.expired and self.headers.get('Authorization')=='Bearer '+'a'*48:status,value=401,{'error':'invalid_credentials'}
            else:value={'revoked':True}
        else:status,value=404,{'error':'not_found'}
        data=json.dumps(value).encode();self.send_response(status);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.config=Path(self.temp.name)/'client.json'
        self.server=ThreadingHTTPServer(('127.0.0.1',0),AuthHandler)
        self.server.base=f'http://127.0.0.1:{self.server.server_port}'
        self.server.calls=[];self.server.polls=0;self.server.refreshes=0
        self.server.lose_refresh=self.server.expired=self.server.bad_uri=False
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()

    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.thread.join();self.temp.cleanup()

    def invoke(self,action):
        out,err=io.StringIO(),io.StringIO()
        with contextlib.redirect_stdout(out),contextlib.redirect_stderr(err),patch.dict(os.environ,{'HUB2_TOKEN':''}),patch('agent_hub_host.auth.time.sleep') as sleep:
            code=main(['--server',self.server.base,'--config',str(self.config),'auth',action,'--no-browser'])
        for secret in ('a'*48,'b'*48,'r'*48,'s'*48,'d'*48):self.assertNotIn(secret,out.getvalue()+err.getvalue())
        return code,out.getvalue(),err.getvalue(),sleep

    def test_device_pending_slowdown_secure_persistence_refresh_and_revoke(self):
        code,out,err,sleep=self.invoke('login')
        self.assertEqual(code,0);self.assertTrue(json.loads(out)['authenticated'])
        self.assertIn('/device?user_code=PUBLIC1234',err)
        self.assertEqual([c.args[0] for c in sleep.call_args_list],[5,5,10])
        self.assertEqual(self.server.calls[0],('/api/device/code',{'client_id':'hub-cli'},None))
        self.assertEqual(self.server.calls[1][1],{'device_code':'d'*48})
        self.assertEqual(json.loads(self.config.read_text())['refresh_token'],'r'*48)
        if os.name!='nt':self.assertEqual(self.config.stat().st_mode&0o777,0o600)
        self.assertEqual(self.invoke('refresh')[0],0)
        self.assertEqual(json.loads(self.config.read_text())['token'],'b'*48)
        self.assertEqual(self.invoke('logout')[0],0)
        self.assertNotIn('token',json.loads(self.config.read_text()))
        self.assertEqual(self.server.calls[-1][2],'Bearer '+'b'*48)

    def test_lost_refresh_response_is_not_blindly_replayed(self):
        save_config(self.config,{'server':self.server.base,'token':'a'*48,'refresh_token':'r'*48})
        self.server.lose_refresh=True
        self.assertEqual(self.invoke('refresh')[0],1)
        self.assertTrue(json.loads(self.config.read_text())['refresh_pending'])
        self.assertEqual(self.invoke('refresh')[0],1)
        self.assertEqual(self.server.refreshes,1)

    def test_expired_access_refreshes_before_remote_logout(self):
        save_config(self.config,{'server':self.server.base,'token':'a'*48,'refresh_token':'r'*48})
        self.server.expired=True
        self.assertEqual(self.invoke('logout')[0],0)
        self.assertEqual([v[0] for v in self.server.calls],['/api/token/revoke','/api/token/refresh','/api/token/revoke'])

    def test_foreign_approval_url_refuses_before_browser_or_poll(self):
        self.server.bad_uri=True
        self.assertEqual(self.invoke('login')[0],1)
        self.assertEqual(self.server.polls,0)
        self.assertFalse(self.config.exists())

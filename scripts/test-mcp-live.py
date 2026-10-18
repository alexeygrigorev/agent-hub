#!/usr/bin/env python3
"""Opt-in real MCP lifecycle acceptance. Never prints credentials or captures.

A private JSON token file must contain access_token (e.g. an OAuth receipt),
or use a private file containing the raw bearer. Evidence is private JSON;
this driver only sends to/stops the session it creates in this invocation.
"""
import argparse
import hashlib
import json
import os
import stat
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

class MCPClient:
    def __init__(self, url, token):
        self.url, self.token, self.seq = url, token, 0

    def rpc(self, method, params=None):
        self.seq += 1
        body = {'jsonrpc': '2.0', 'id': self.seq, 'method': method}
        if params is not None:
            body['params'] = params
        req = urllib.request.Request(self.url, data=json.dumps(body).encode(), headers={
            'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json',
            'Accept': 'application/json, text/event-stream', 'MCP-Protocol-Version': '2025-11-25', 'User-Agent': 'hub-mcp-client/1.0'})
        try:
            with urllib.request.urlopen(req, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError('MCP HTTP %d' % error.code) from None
        if 'error' in result:
            raise RuntimeError('MCP protocol error %s' % result['error'].get('code'))
        return result['result']

    def call(self, name, arguments=None, allow_error=False):
        result = self.rpc('tools/call', {'name': name, 'arguments': arguments or {}})
        dto = json.loads(result['content'][0]['text'])
        if result.get('isError') and not allow_error:
            raise RuntimeError('Hub tool %s failed: HTTP %s %s' % (name, dto.get('status'), dto.get('data', {}).get('error', 'unknown')))
        return dto


def wait_for(fn, done, timeout):
    deadline = time.monotonic() + timeout
    while True:
        result = fn()
        if done(result):
            return result
        if time.monotonic() >= deadline:
            raise RuntimeError('Acknowledgement timed out; reconcile using private evidence keys')
        time.sleep(3)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--url', default='https://hub.dtcdev.click/mcp')
    p.add_argument('--token-file', required=True)
    p.add_argument('--evidence', required=True)
    p.add_argument('--lifecycle', action='store_true', help='Explicitly authorize creation/send/capture/stop of one new test session')
    p.add_argument('--workspace', required=True, help='An exact workspace identifier advertised by the selected host')
    p.add_argument('--machine')
    p.add_argument('--engine', required=True, help='Canonical Hub engine, not an unregistered local aplexer alias; never grok')
    p.add_argument('--timeout', type=int, default=600)
    args = p.parse_args()
    if args.engine.lower() == 'grok':
        p.error('Grok test sessions are prohibited')
    if not args.url.startswith('https://'):
        p.error('Real acceptance requires HTTPS')
    token_file = Path(args.token_file)
    if stat.S_IMODE(token_file.stat().st_mode) & 0o077:
        p.error('Token file must be private (0600)')
    text = token_file.read_text().strip()
    token = json.loads(text)['access_token'] if text.startswith('{') else text
    client = MCPClient(args.url, token)
    evidence = {'url': args.url, 'started_at': time.time(), 'keys': {}, 'lifecycle': bool(args.lifecycle)}
    path = Path(args.evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    def save():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(evidence, f, indent=2)
    session_id = None
    try:
        evidence['initialize'] = client.rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {}, 'clientInfo': {'name': 'hub-mcp-live-test', 'version': '1.0'}})
        tools = client.rpc('tools/list')['tools']
        evidence['tools'] = [t['name'] for t in tools]
        for name in ['sessions_tree', 'fleet', 'quota']:
            result = client.call(name)
            evidence[name + '_status'] = result['status']
        save()
        if not args.lifecycle:
            print('MCP initialize/list and read calls passed (%d tools)' % len(tools))
            return
        # Refresh quota immediately before the existing Hub/Launcher admission path.
        quota = subprocess.run(['quse', '--json'], capture_output=True, text=True, timeout=120)
        if quota.returncode:
            raise RuntimeError('Fresh quota query failed; no session started')
        evidence['fresh_quota_query'] = True
        key = 'mcp-e2e-' + str(uuid.uuid4())
        evidence['keys'] = {kind: key + '-' + kind for kind in ['start', 'send', 'capture', 'stop']}
        payload = {'key': evidence['keys']['start'], 'workspace': args.workspace, 'engine': args.engine,
                   'name': key, 'mode': 'headless', 'access': {'mode': 'read'},
                   'brief': ('This is an authorized bounded Hub MCP lifecycle test. Take your maintained Launcher first action immediately, including only its required private identity evidence; do not edit product files, start agents, change services or stop sessions. '
                             'Print MCP_START_OK. Remain running for up to 8 minutes while the test controller sends one follow-up to THIS newly created session. '
                             'Poll only your own a message inbox --json, sleeping 3 seconds between polls. Ignore unrelated messages. '
                             'For the message whose body begins Hub MCP test follow-up, read its actual native envelope. Reply ONLY to that test envelope using a message reply with body MCP_SEND_OK and --data JSON echoing its exact correlation_id and body_digest. '
                             'This correlated response to the test controller is the only outgoing message authorized by this test. Print MCP_SEND_OK after the reply. '
                             'Then keep polling/sleeping until the controller captures and stops this session; finish after the 8-minute bound if no stop arrives.')}
        if args.machine:
            payload['machine'] = args.machine
        created = client.call('sessions_start', {'payload': payload}, allow_error=True)
        evidence['start'] = created
        session_id = created.get('data', {}).get('id')
        evidence['session_id'] = session_id
        save()
        if not session_id or created['status'] >= 400:
            raise RuntimeError('Session admission was held/refused; see private evidence')
        def show():
            return client.call('sessions_show', {'id': session_id})['data']
        bound = wait_for(show, lambda s: s.get('state') in ['running', 'failed', 'refused', 'held', 'uncertain', 'stopped'], args.timeout)
        evidence['bound'] = bound
        save()
        if bound.get('state') != 'running':
            raise RuntimeError('No genuinely running session: ' + str(bound.get('state')))
        def command(kind, extra=None):
            queued = client.call('sessions_' + kind, {'id': session_id, 'payload': {'key': evidence['keys'][kind], **(extra or {})}})['data']
            evidence[kind + '_queued'] = queued
            save()
            command_id = queued.get('id') or queued.get('command_id')
            def get_command():
                data = client.call('sessions_commands', {'id': session_id, 'query': {'limit': 100}})['data']
                return next((c for c in data.get('commands', []) if c['id'] == command_id), {})
            done = wait_for(get_command, lambda c: c.get('state') in ['acked', 'refused', 'uncertain'], args.timeout)
            evidence[kind + '_result'] = done
            save()
            if done.get('state') != 'acked':
                raise RuntimeError('Command %s was not acknowledged: %s' % (kind, done.get('state')))
            return done
        sent = command('send', {'body': 'Hub MCP test follow-up. Reply MCP_SEND_OK. Do not edit files or take any other action.'})
        captured = command('capture', {'max_bytes': 16384})
        # Streamed tool events can scroll the reply out of a bounded tail.
        # Processing is proven by the exact correlated native reply, not by
        # incidental text (which could also appear in the original prompt).
        expected_reply = hashlib.sha256(b'MCP_SEND_OK').hexdigest()
        if sent.get('ack', {}).get('proof', {}).get('reply_digest') != expected_reply:
            raise RuntimeError('Native correlated reply does not match the test response')
        capture_ack = captured.get('ack', {})
        capture_text = capture_ack.get('capture')
        if (not isinstance(capture_text, str) or not capture_text.strip()
                or capture_ack.get('native', {}).get('native_id') != bound.get('binding', {}).get('native_id')):
            raise RuntimeError('Capture lacks output from the exact bound native session')
        command('stop')
        stopped = wait_for(show, lambda s: s.get('state') == 'stopped', args.timeout)
        evidence['final'] = stopped
        evidence['passed'] = True
        print('PASS: real MCP headless start/send/capture/confirmed stop; evidence saved privately')
    except Exception as error:
        evidence['passed'] = False
        evidence['error'] = str(error)
        raise
    finally:
        if session_id and not evidence.get('passed'):
            # Only this invocation's newly created id is eligible for cleanup.
            try:
                evidence['cleanup_stop'] = client.call('sessions_stop', {'id': session_id, 'payload': {'key': evidence['keys']['stop']}}, allow_error=True)
            except Exception:
                evidence['cleanup_stop'] = {'error': 'reconciliation_required'}
        evidence['finished_at'] = time.time()
        save()

if __name__ == '__main__':
    main()

"""Composed offline regression for MachineDO HOST errors and the maintained CLI."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from agent_hub_host import cli, session_control


ROOT = Path(__file__).resolve().parents[2]


MACHINE_DO_PROJECTION = r"""
import { DatabaseSync } from 'node:sqlite';
import { registerHooks } from 'node:module';
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
const diagnosticDetail = JSON.parse(readFileSync(0, 'utf8'));
registerHooks({resolve(specifier, context, next) {
  if (specifier.startsWith('.') && context.parentURL && !specifier.endsWith('.ts')) {
    const candidate = new URL(specifier+'.ts', context.parentURL);
    if (existsSync(fileURLToPath(candidate))) return next(candidate.href, context);
  }
  return next(specifier, context);
}});
const { MachineDO } = await import('./hub2/src/machine-do.ts');

const database = new DatabaseSync(':memory:');
const events = [];
const sockets = [];
let alarm = null;
const ctx = {
  id: {toString: () => 'machine-h1'},
  storage: {
    sql: {exec(query, ...args) {
      const statement = database.prepare(query);
      if (statement.columns().length) {
        const rows = statement.all(...args);
        return {toArray: () => rows, rowsRead: rows.length, rowsWritten: 0};
      }
      const result = statement.run(...args);
      return {toArray: () => [], rowsRead: 0, rowsWritten: Number(result.changes)};
    }},
    async getAlarm() { return alarm; },
    async setAlarm(value) { alarm = value; },
    async deleteAlarm() { alarm = null; },
    transactionSync(fn) { database.exec('BEGIN'); try { const value = fn(); database.exec('COMMIT'); return value; }
      catch (error) { database.exec('ROLLBACK'); throw error; }},
  },
  blockConcurrencyWhile(fn) { return Promise.resolve(fn()); },
  getWebSockets() { return sockets; },
};
const env = {
  CONTROL: {idFromName: id => id, get: () => ({fetch: async () => Response.json({authorized: true})})},
  MACHINES: {idFromName: id => id, get: () => ({fetch: async () => Response.json({})})},
  PROJECTS: {idFromName: id => id, get: () => ({fetch: async request => {
    events.push({path: new URL(request.url).pathname, body: await request.json()});
    return Response.json({ok: true});
  }})},
};
const machine = new MachineDO(ctx, env);
const machineActor = {id:'machine:h1',kind:'machine',resource_id:'h1',generation:'1',scopes:['machine:heartbeat','session:report']};
const call = (path, method='GET', body=undefined, actor=machineActor) => machine.fetch(new Request('https://machine.test'+path, {
  method, headers: {'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},
  ...(body === undefined ? {} : {body: JSON.stringify(body)}),
}));

const sessionId = 'composed-host-origin-failure';
const heartbeat = await call('/api/heartbeat','POST',{labels:[],engines:['codex'],capabilities:['headless'],native_sessions:[]});
if (!heartbeat.ok) throw new Error('heartbeat setup failed');
const reservation = await call('/_internal/reserve','POST',{operation_id:'op-'+sessionId,dispatch_id:'dispatch-'+sessionId,attempt:0,
  session_id:sessionId,project_id:'project-test',intake_id:'intake-'+sessionId,machine_id:'h1',request_digest:'digest-'+sessionId,
  title:'offline diagnostic test',acceptance_criteria:['projection carries host diagnostic'],account_id:'codex',
  launch:{workspace:'relay',engine:'codex',model:'gpt-6.1-sol',prompt:'offline fixture',interactive:false,timeout_seconds:1800}});
if (reservation.status !== 201) throw new Error('reservation setup failed: '+reservation.status);
const reserved = await reservation.json();
const activated = await call('/_internal/activate','POST',{operation_id:'op-'+sessionId,session_id:sessionId,epoch:reserved.epoch});
if (activated.status !== 201) throw new Error('activation setup failed: '+activated.status);

const socket = {readyState:1,deserializeAttachment:() => ({machine_id:'h1',actor_id:'machine:h1',generation:'1',connected_at:1,
  connection_id:'composed-cli-test'}),send:() => {}};
sockets.push(socket);
// This is the HOST host.error shape (it omits epoch, as the maintained host currently does).
await machine.webSocketMessage(socket, JSON.stringify({type:'host.error',session_id:sessionId,operation_id:'op-'+sessionId,
  stage:'host_launch',reason:'workspace_unavailable',detail:diagnosticDetail}));
const operator={id:'operator',kind:'operator',scopes:['operator:admin']};
const projectedResponse=await call('/api/sessions/'+sessionId,'GET',undefined,operator);
if (!projectedResponse.ok) throw new Error('session projection failed: '+projectedResponse.status);
console.log(JSON.stringify(await projectedResponse.json()));
"""


NODE_FENCE_PRIVACY_TESTS = r"""
import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
registerHooks({resolve(specifier, context, next) {
  if (specifier.startsWith('.') && context.parentURL && !specifier.endsWith('.ts')) {
    const candidate = new URL(specifier+'.ts', context.parentURL);
    if (existsSync(fileURLToPath(candidate))) return next(candidate.href, context);
  }
  return next(specifier, context);
}});
await import('./hub2/tests/host-start-diagnostics.test.ts');
"""


def machine_do_failed_projection(detail):
    result = subprocess.run(
        ['node', '--experimental-transform-types', '--input-type=module', '--eval', MACHINE_DO_PROJECTION],
        cwd=ROOT, input=json.dumps(detail), capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(f'MachineDO fixture failed ({result.returncode}): {result.stderr[-2000:]}')
    return json.loads(result.stdout)


def run_cli_wait(projection):
    session = projection['session']
    request = patch.object(session_control, 'request', side_effect=[{'id': session['id']}, projection])
    stderr = io.StringIO()
    argv = ['hub', 'sessions', 'start', '--workspace', 'relay', '--prompt', 'offline test', '--key', 'offline-key',
            '--timeout', '5', '--interval', '1']
    with request, patch.object(sys, 'argv', argv), contextlib.redirect_stderr(stderr):
        try:
            cli.main()
        except SystemExit as caught:
            return caught.code, stderr.getvalue()
    raise AssertionError('CLI start unexpectedly returned without reporting its terminal session')


class TestCliHostStartDiagnostics(unittest.TestCase):
    def test_machine_do_host_error_reaches_real_cli_wait_formatter_and_exits_nonzero(self):
        projection = machine_do_failed_projection('The requested workspace is unavailable on this host.')
        session = projection['session']
        self.assertEqual(session['state'], 'failed')
        self.assertEqual(session['attention']['reason'], 'workspace_unavailable')
        self.assertEqual(session['attention']['stage'], 'host_launch')
        self.assertEqual(session['attention']['detail'], 'The requested workspace is unavailable on this host.')

        status, output = run_cli_wait(projection)

        self.assertEqual(status, 1)
        self.assertIn('session_start_failed', output)
        self.assertIn('workspace_unavailable', output)
        self.assertIn('stage=host_launch', output)
        self.assertIn('The requested workspace is unavailable on this host.', output)

    def test_machine_do_and_real_cli_reject_credential_and_control_details(self):
        unsafe_details = (
            'Authorization: Bearer\x00 synthetic-private-token',
            'AWS_SECRET_ACCESS_KEY: synthetic-private-token',
        )
        for unsafe_detail in unsafe_details:
            with self.subTest(detail_kind='authorization' if 'Authorization' in unsafe_detail else 'secret-assignment'):
                projection = machine_do_failed_projection(unsafe_detail)
                session = projection['session']
                self.assertEqual(session['state'], 'failed')
                self.assertEqual(session['attention']['reason'], 'workspace_unavailable')
                self.assertEqual(session['attention']['stage'], 'host_launch')
                self.assertNotIn('detail', session['attention'])
                self.assertNotIn('synthetic-private-token', json.dumps(projection))

                # Exercise the real formatter against an older/malicious
                # projection carrying the same shape before CLI sanitization.
                unsafe_projection = json.loads(json.dumps(projection))
                unsafe_projection['session']['attention']['detail'] = unsafe_detail
                status, output = run_cli_wait(unsafe_projection)
                self.assertEqual(status, 1)
                self.assertIn('workspace_unavailable', output)
                self.assertIn('stage=host_launch', output)
                self.assertNotIn('synthetic-private-token', output)
                self.assertNotIn('AWS_SECRET_ACCESS_KEY', output)
                self.assertNotIn('Authorization', output)

    def test_machine_do_fence_and_privacy_cases_run_through_required_cli_check(self):
        result = subprocess.run(
            ['node', '--experimental-transform-types', '--input-type=module', '--eval', NODE_FENCE_PRIVACY_TESTS],
            cwd=ROOT, capture_output=True, text=True, timeout=60, check=False)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output[-4000:])
        self.assertIn('host.error refuses stale attempts and epochs', output)
        self.assertIn('host.error rejects authorization header split by NUL', output)


if __name__ == '__main__':
    unittest.main()

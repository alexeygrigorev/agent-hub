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
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
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
  stage:'host_launch',reason:'workspace_unavailable',detail:'The requested workspace is unavailable on this host.'}));
const operator={id:'operator',kind:'operator',scopes:['operator:admin']};
const projectedResponse=await call('/api/sessions/'+sessionId,'GET',undefined,operator);
if (!projectedResponse.ok) throw new Error('session projection failed: '+projectedResponse.status);
console.log(JSON.stringify(await projectedResponse.json()));
"""


def machine_do_failed_projection():
    result = subprocess.run(
        ['node', '--experimental-transform-types', '--input-type=module', '--eval', MACHINE_DO_PROJECTION],
        cwd=ROOT, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise AssertionError(f'MachineDO fixture failed ({result.returncode}): {result.stderr[-2000:]}')
    return json.loads(result.stdout)


class TestCliHostStartDiagnostics(unittest.TestCase):
    def test_machine_do_host_error_reaches_real_cli_wait_formatter_and_exits_nonzero(self):
        projection = machine_do_failed_projection()
        session = projection['session']
        self.assertEqual(session['state'], 'failed')
        self.assertEqual(session['attention']['reason'], 'workspace_unavailable')
        self.assertEqual(session['attention']['stage'], 'host_launch')
        self.assertEqual(session['attention']['detail'], 'The requested workspace is unavailable on this host.')

        request = patch.object(session_control, 'request', side_effect=[{'id': session['id']}, projection])
        stderr = io.StringIO()
        argv = ['hub', 'sessions', 'start', '--workspace', 'relay', '--prompt', 'offline test', '--key', 'offline-key',
                '--timeout', '5', '--interval', '1']
        with request, patch.object(sys, 'argv', argv), contextlib.redirect_stderr(stderr):
            with self.assertRaises(SystemExit) as caught:
                cli.main()

        self.assertEqual(caught.exception.code, 1)
        output = stderr.getvalue()
        self.assertIn('session_start_failed', output)
        self.assertIn('workspace_unavailable', output)
        self.assertIn('stage=host_launch', output)
        self.assertIn('The requested workspace is unavailable on this host.', output)


if __name__ == '__main__':
    unittest.main()

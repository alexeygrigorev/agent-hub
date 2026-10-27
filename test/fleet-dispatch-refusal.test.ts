import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = { caller: { isAdmin: true, actor: 'owner', kind: 'human' }, scopes: ['admin'] };
const host = { caller: { machineId: 'hetzner', kind: 'host', actor: 'machine:hetzner' }, scopes: ['host'] };
const reason = 'dispatch_local_command_failed';
const evidence = () => ({ reason, message: 'A local dispatch prerequisite command failed before native start.',
  stage: 'dispatch_route', error_type: 'LocalCommandError', command: 'quse', exit_code: 23,
  stdout_tail: 'quota probe context', stderr_tail: 'token=fixture-secret sk-abcdefghijk invalid option' });

async function fixture(check: (fleet: FleetService, sessions: SessionControlService, sql: any, id: string) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('command-diagnostic-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb)); fleet.init();
    const sessions = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb)); sessions.init();
    const id = crypto.randomUUID(), at = new Date().toISOString();
    hub.sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
      VALUES(?,'owner','human','diagnostic',?,'{}','hetzner','codex','shared-codex','headless','demo','','offered',?,?)`, id, id, at, at);
    hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,mode,state,created_at,updated_at)
      VALUES(?,'owner',?,'{}',?,'hetzner','headless','offered',?,?)`, crypto.randomUUID(), id, id, at, at);
    await check(fleet, sessions, hub.sql, id);
  });
}
const report = (id: string, diagnostic: any, result = false) => new Request(
  'https://hub.test/api/fleet/launches/' + id + (result ? '/result' : '/reject'),
  { method: 'POST', body: JSON.stringify(result ? { state: 'uncertain', diagnostic } : { reason, diagnostic }) });

describe('bounded local command diagnostics (488b)', () => {
  it('persists command/exit/output evidence and shows it on the failed session, re-redacting host input', async () => fixture(async (fleet, sessions, sql, id) => {
    const response = await fleet.handle(report(id, evidence()), host);
    expect(response.status).toBe(200);
    const stored = JSON.parse(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id=?', id).one().launch_error_json);
    expect(stored).toMatchObject({ reason, command: 'quse', exit_code: 23, stdout_tail: 'quota probe context' });
    expect(stored.stderr_tail).toContain('invalid option');
    expect(JSON.stringify(stored)).not.toContain('fixture-secret');
    expect(JSON.stringify(stored)).not.toContain('sk-abcdefghijk');
    const shown = await (await sessions.handle(new Request('https://hub.test/api/sessions/' + id), admin)).json();
    expect(shown).toMatchObject({ state: 'failed', reason, launch_error: stored });
    expect(JSON.stringify(await response.json())).not.toContain('fixture-secret');
  }));

  it('rejects oversized, control-bearing or malformed command evidence before changing state', async () => fixture(async (fleet, _sessions, sql, id) => {
    for (const change of [{ command: '/private/path/quse' }, { command: ['quse', '--secret'] },
      { stdout_tail: 'x'.repeat(513) }, { stderr_tail: 'line\nsecret' }, { stderr_tail: null },
      { exit_code: 1.5 }, { argv: ['quse', '--secret'] }]) {
      expect((await fleet.handle(report(id, { ...evidence(), ...change }), host)).status).toBe(400);
      expect(sql.exec('SELECT state,launch_error_json FROM fleet_launches WHERE id=?', id).one()).toMatchObject({ state: 'offered', launch_error_json: null });
    }
  }));

  it('retains the same bounded command fields for postclaim uncertain launch errors', async () => fixture(async (fleet, _sessions, sql, id) => {
    sql.exec("UPDATE fleet_launches SET state='starting' WHERE id=?", id);
    const diagnostic = { ...evidence(), reason: 'launcher_command_failed',
      message: 'The launcher command failed; unrecognized output was withheld.', stage: 'dispatch_headless' };
    expect((await fleet.handle(report(id, diagnostic, true), host)).status).toBe(200);
    const stored = JSON.parse(sql.exec('SELECT launch_error_json FROM fleet_launches WHERE id=?', id).one().launch_error_json);
    expect(stored).toMatchObject({ command: 'quse', exit_code: 23, stdout_tail: 'quota probe context' });
    expect(JSON.stringify(stored)).not.toContain('fixture-secret');
  }));
});

import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService } from '../src/session_control';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';
import { seedFixtureIntake, withFixtureIntake } from './tbm-fixture';

const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const rejected = [{ machine: 'hetzner', engine: 'codex', reason: 'cpu_pressure' }];

function request(path: string, data?: any, method = 'POST') {
  return new Request('https://hub.test' + path, {
    method,
    body: data === undefined ? undefined : JSON.stringify(withFixtureIntake(path, data, method)),
  });
}

async function fixture(fn: (service: SessionControlService, sql: any) => Promise<void>) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName('session-control-supersede-' + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const service = new SessionControlService(hub.sql, cb => state.storage.transactionSync(cb), Date.now, {
      reserve: () => ({ status: 409, value: { error: 'no_eligible_route', rejected } }),
    });
    service.init();
    seedFixtureIntake(hub.sql);
    const at = new Date().toISOString();
    const far = new Date(Date.now() + 30 * 86400000).toISOString();
    hub.sql.exec(`INSERT OR IGNORE INTO credentials
      (id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at)
      VALUES ('host-cred-1','host','hash-host','refresh-host','host-family-1','test-host','hetzner',NULL,'["host"]','test',?,?)`, at, far);
    hub.sql.exec("INSERT OR IGNORE INTO machines (id,name,hostname,credential_id,registered_at) VALUES ('hetzner','hetzner','hetzner','host-cred-1',?)", at);
    hub.sql.exec("INSERT OR IGNORE INTO agents (tag,project,role,machine_id,registered_at) VALUES ('assistant','demo','worker',NULL,?)", at);
    hub.sql.exec(`INSERT OR IGNORE INTO credentials
      (id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,registry_actor)
      VALUES ('cred-1','agent','hash-agent','refresh-agent','agent-family-1','demo-agent',NULL,'demo:assistant','["fleet:launch"]','test',?,?,'demo:assistant')`, at, far);
    await fn(service, hub.sql);
  });
}

async function createHeld(service: SessionControlService, sql: any, intakeId: string): Promise<any> {
  sql.exec('INSERT OR REPLACE INTO intake_tasks (id, data) VALUES (?, ?)', intakeId, JSON.stringify({
    id: intakeId, project: 'demo', status: 'open', acceptance_criteria: 'Verify held placement recovery.', history: [],
  }));
  const response = await service.handle(request('/api/sessions', {
    key: crypto.randomUUID(), workspace: 'demo', mode: 'interactive', intake_task_id: intakeId,
  }), admin);
  const data = await response.json() as any;
  expect(response.status, JSON.stringify(data)).toBe(409);
  expect(data.state).toBe('held');
  return data;
}

function insertLiveLinkedSession(sql: any, id: string, createdAt: string, fields: any = {}) {
  sql.exec(`INSERT INTO session_control_sessions
    (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES (?,?,?, ?,?,?, 'interactive','running',?,?,?)`,
    id, 'test-admin', 'linked-' + id, JSON.stringify(fields.request ?? {}), fields.machine_id ?? null,
    fields.engine ?? 'codex', fields.binding ? JSON.stringify(fields.binding) : null,
    createdAt, fields.updated_at ?? createdAt);
}

function insertIntakeLinks(sql: any, intakeId: string, heldId: string, linkedIds: string | string[]) {
  sql.exec('CREATE TABLE IF NOT EXISTS intake_tasks (id TEXT PRIMARY KEY, data TEXT)');
  sql.exec('INSERT OR REPLACE INTO intake_tasks (id,data) VALUES (?,?)', intakeId, JSON.stringify({
    id: intakeId, project: 'demo', status: 'open', acceptance_criteria: 'Verify held placement recovery.',
    history: [
      { kind: 'link', hub_session_id: heldId },
      ...(Array.isArray(linkedIds) ? linkedIds : [linkedIds]).map(hub_session_id => ({ kind: 'link', hub_session_id })),
    ],
  }));
}

describe('held-session intake supersession', () => {
  it('does not let older or same-time live linked sessions supersede a held start', async () => {
    await fixture(async (service, sql) => {
      const intakeId = crypto.randomUUID();
      const held = await createHeld(service, sql, intakeId);
      const heldCreated = Date.parse(sql.exec('SELECT created_at FROM session_control_sessions WHERE id=?', held.id).toArray()[0].created_at);
      const olderId = crypto.randomUUID();
      const sameTimeId = crypto.randomUUID();
      insertLiveLinkedSession(sql, olderId, new Date(heldCreated - 1000).toISOString());
      insertLiveLinkedSession(sql, sameTimeId, new Date(heldCreated).toISOString());
      insertIntakeLinks(sql, intakeId, held.id, [olderId, sameTimeId]);

      const shown = await (await service.handle(request('/api/sessions/' + held.id, undefined, 'GET'), admin)).json() as any;
      expect(shown.state).toBe('held');
      expect(shown.reason).toBe('cpu_pressure');
      expect(shown.superseded_by).toBeUndefined();
    });
  });

  it('does not let a stale observed-native shell session supersede a held start', async () => {
    await fixture(async (service, sql) => {
      const intakeId = crypto.randomUUID();
      const held = await createHeld(service, sql, intakeId);
      const heldCreated = Date.parse(sql.exec('SELECT created_at FROM session_control_sessions WHERE id=?', held.id).toArray()[0].created_at);
      const shellId = crypto.randomUUID();
      const staleAt = new Date(Date.now() - 6 * 60 * 60 * 1000).toISOString();
      const nativeId = crypto.randomUUID();
      sql.exec('CREATE TABLE IF NOT EXISTS fleet_catalog_seen (machine_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL)');
      sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen (machine_id,observed_at) VALUES (?,?)', 'stale-shell-host', staleAt);
      insertLiveLinkedSession(sql, shellId, new Date(heldCreated + 1000).toISOString(), {
        engine: 'shell', machine_id: 'stale-shell-host',
        request: { provenance: 'observed_native', observation_only: true },
        binding: { native_id: nativeId, tag: 'rr-stale', workspace: 'demo', provenance: 'observed_native', observed_at: staleAt },
        updated_at: staleAt,
      });
      insertIntakeLinks(sql, intakeId, held.id, shellId);

      const shown = await (await service.handle(request('/api/sessions/' + held.id, undefined, 'GET'), admin)).json() as any;
      expect(shown.state).toBe('held');
      expect(shown.reason).toBe('cpu_pressure');
      expect(shown.superseded_by).toBeUndefined();
    });
  });

  it('cancels a held start for a truly newer live link and shows its full superseding id', async () => {
    await fixture(async (service, sql) => {
      const intakeId = crypto.randomUUID();
      const held = await createHeld(service, sql, intakeId);
      const heldCreated = Date.parse(sql.exec('SELECT created_at FROM session_control_sessions WHERE id=?', held.id).toArray()[0].created_at);
      const newerId = crypto.randomUUID();
      insertLiveLinkedSession(sql, newerId, new Date(heldCreated + 1000).toISOString());
      insertIntakeLinks(sql, intakeId, held.id, newerId);

      const shown = await (await service.handle(request('/api/sessions/' + held.id, undefined, 'GET'), admin)).json() as any;
      expect(shown.state).toBe('cancelled');
      expect(shown.reason).toBe('superseded_by_newer_session');
      expect(shown.superseded_by).toBe(newerId);
    });
  });
});

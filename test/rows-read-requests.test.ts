/**
 * Request-path rows-read budgets (intake e8a22f4c, phase 2). Host polls run
 * every few seconds per host; their SQL must be indexed so history in `jobs`
 * and `session_control_commands` never changes the rows a poll reads.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';

const NOW = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();
const HISTORY = 2000;

function seedHistory(sql: any) {
  for (let i = 0; i < HISTORY; i++) {
    sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_machine_id,finished_at)
      VALUES(?,'root-run',?,'{}','finished',?,?,'t','budget-host',?)`, 'j' + i, 'jk' + i, iso(NOW - i * 1000), iso(NOW), iso(NOW));
  }
  sql.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,offer_machine_id,offer_expires_at)
    VALUES('joffer','root-run','jkoffer','{}','offered',?,?,'t','budget-host',?)`, iso(NOW), iso(NOW + 60000), iso(NOW + 60000));
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('sess-0','operator','k0','{}','budget-host','codex','interactive','running','{}',?,?)`, iso(NOW), iso(NOW));
  for (let i = 0; i < HISTORY; i++) {
    sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
      VALUES(?,'sess-0','send','operator',?,'{}','acked',1,'c',?,?)`, 'c' + i, 'rk' + i, iso(NOW - i * 1000), iso(NOW));
  }
}

const withHub = (fn: (hub: Hub) => Promise<void>) => runInDurableObject(
  (env as any).HUB.get((env as any).HUB.idFromName('rows-req-' + crypto.randomUUID())), async (hub: Hub) => { seedHistory(hub.sql); await fn(hub); });

const read = (hub: Hub, q: string, ...b: any[]) => { const c: any = hub.sql.exec(q, ...b); const rows = c.toArray(); return { rows, read: Number(c.rowsRead) }; };

describe('host poll queries ignore history', () => {
  it('offered-job poll reads only offered rows', () => withHub(async hub => {
    const r = read(hub, `SELECT id, kind, payload, status, deadline_at, offer_host_id, offer_machine_id, offer_expires_at, created_at, actor, role_name, role_generation
      FROM jobs WHERE status = 'offered' AND (offer_host_id = ? OR offer_machine_id = ?) AND (offer_expires_at IS NULL OR offer_expires_at > ?) ORDER BY created_at DESC`,
      'budget-host', 'budget-host', iso(NOW));
    console.log('poll jobs rows', r.rows.length, r.read);
    expect(r.rows).toHaveLength(1);
    expect(r.read).toBeLessThanOrEqual(10);
  }));

  it('session-command offers read only live commands', () => withHub(async hub => {
    const a = read(hub, `SELECT c.* FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
      WHERE s.machine_id = ? AND c.state = 'recorded' ORDER BY c.created_at, c.id LIMIT 20`, 'budget-host');
    const b = read(hub, `SELECT c.id FROM session_control_commands c JOIN session_control_sessions s ON s.id = c.session_id
      WHERE s.machine_id = ? AND c.state IN ('claimed','recorded','transported') AND c.claim_token IS NOT NULL AND c.ack_json IS NULL AND c.holder_machine = ? AND (c.claim_expires_at IS NULL OR c.claim_expires_at <= ?) LIMIT 100`, 'budget-host', 'budget-host', iso(NOW));
    const c = read(hub, `UPDATE session_control_commands SET state = 'recorded', poll_at = NULL, updated_at = ?
      WHERE state = 'transported' AND ack_json IS NULL AND holder_machine IS NULL AND poll_at IS NOT NULL AND poll_at < ?
      AND session_id IN (SELECT id FROM session_control_sessions WHERE machine_id = ?)`, iso(NOW), iso(NOW), 'budget-host');
    console.log('offers rows', a.read, b.read, c.read);
    expect(a.read).toBeLessThanOrEqual(10);
    expect(b.read).toBeLessThanOrEqual(10);
    expect(c.read).toBeLessThanOrEqual(10);
  }));
});

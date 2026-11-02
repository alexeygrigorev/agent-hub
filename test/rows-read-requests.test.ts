/**
 * Request-path rows-read budgets (intake e8a22f4c, phase 2). Host polls run
 * every few seconds per host; their SQL must be indexed so history in `jobs`
 * never changes the rows a poll reads. (No index on session_control_commands.state:
 * claim checks rely on rowsWritten === 1, which counts index maintenance.)
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
});

describe('per-statement attribution', () => {
  it('names the SQL behind a scope and sums its rows', () => withHub(async hub => {
    hub.meter.runSync('probe:stmt', () => {
      hub.sql.exec('SELECT id FROM jobs ORDER BY id LIMIT 5').toArray();
      hub.sql.exec('SELECT id FROM jobs WHERE id IN (?, ?, ?)', 'j1', 'j2', 'j3').toArray();
      hub.sql.exec('SELECT id FROM jobs WHERE id IN (?, ?)', 'j4', 'j5').toArray();
    });
    const top = hub.meter.statementTop(500).filter(s => s.scope === 'probe:stmt');
    // IN lists of any width fold into one statement shape.
    expect(top.map(s => s.sql).sort()).toEqual(['SELECT id FROM jobs ORDER BY id LIMIT 5', 'SELECT id FROM jobs WHERE id IN (?...)']);
    const byShape = Object.fromEntries(top.map(s => [s.sql, s]));
    expect(byShape['SELECT id FROM jobs WHERE id IN (?...)'].calls).toBe(2);
    expect(byShape['SELECT id FROM jobs ORDER BY id LIMIT 5'].rows_read).toBe(5);
  }));
});

describe('session-command offers ignore command history', () => {
  it('reads only live commands, not the machine session history', () => withHub(async hub => {
    // 1,500 more finished sessions on the machine, each with an acked command, plus one live recorded command.
    for (let i = 1; i <= 1500; i++) {
      hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
        VALUES(?,'operator',?,'{}','budget-host','codex','interactive','stopped','{}',?,?)`, 's' + i, 'k' + i, iso(NOW - i * 1000), iso(NOW));
      hub.sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
        VALUES(?,?,'send','operator','x','{}','acked',1,'c',?,?)`, 'sc' + i, 's' + i, iso(NOW - i * 1000), iso(NOW));
    }
    hub.sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
      VALUES('live','sess-0','send','operator','live','{}','recorded',1,'c',?,?)`, iso(NOW), iso(NOW));
    const offers = read(hub, `SELECT c.* FROM session_control_commands c INDEXED BY session_control_commands_state CROSS JOIN session_control_sessions s ON s.id = c.session_id
      WHERE s.machine_id = ? AND c.state = 'recorded' AND (c.not_before IS NULL OR c.not_before <= ?) ORDER BY c.created_at, c.id LIMIT 20`, 'budget-host', iso(NOW));
    const stale = read(hub, `SELECT c.id, c.claim_expires_at, s.host_family, s.machine_id FROM session_control_commands c INDEXED BY session_control_commands_state CROSS JOIN session_control_sessions s ON s.id = c.session_id
      WHERE s.machine_id = ? AND c.state IN ('claimed','recorded','transported') AND c.claim_token IS NOT NULL AND c.ack_json IS NULL AND c.holder_machine = ? AND (c.claim_expires_at IS NULL OR c.claim_expires_at <= ?) LIMIT 100`, 'budget-host', 'budget-host', iso(NOW));
    const revert = read(hub, `UPDATE session_control_commands SET state = 'recorded', poll_at = NULL, updated_at = ? WHERE state = 'transported' AND ack_json IS NULL AND holder_machine IS NULL
      AND poll_at IS NOT NULL AND poll_at < ? AND EXISTS (SELECT 1 FROM session_control_sessions s WHERE s.id = session_control_commands.session_id AND s.machine_id = ?)`, iso(NOW), iso(NOW), 'budget-host');
    expect(offers.rows.map((r: any) => r.id)).toEqual(['live']);
    const over = Object.entries({ offers: offers.read, stale: stale.read, revert: revert.read }).filter(([, n]) => n > 10);
    expect(over, 'statements reading more than 10 rows (rows read)').toEqual([]);
  }));
});

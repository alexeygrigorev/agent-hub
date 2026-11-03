/**
 * Credential-poll rows-read budgets (intake e8a22f4c, phase 2). Hosts poll
 * GET /api/session-messaging/credentials and GET /api/session-intake/credentials
 * every 2 minutes per host; their SQL must be indexed so credential and agent
 * history never changes what a poll reads, and the ended-session revoke
 * sweeps must be write-driven (backstop only) instead of running per poll.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import { WorkerAccess } from '../src/worker-access';
import { pollSessionCredentials } from '../src/session-messaging';

const NOW = Date.now();
const iso = (ms: number) => new Date(ms).toISOString();
const STALE_AGENTS = 300, DEAD_CREDS = 2000;

/** Wrap sql so every cursor is kept; rowsRead is final once consumed. */
function tracked(sql: any) {
  const calls: string[] = [];
  const cursors: { q: string; cursor: any }[] = [];
  const proxy = new Proxy(sql, { get(t: any, p: string) {
    if (p === 'exec') return (q: string, ...b: any[]) => {
      const qq = String(q).replace(/\s+/g, ' ');
      calls.push(qq);
      const cursor = t.exec(q, ...b);
      cursors.push({ q: qq, cursor });
      return cursor;
    };
    const v = t[p]; return typeof v === 'function' ? v.bind(t) : v;
  } });
  /** Rows read by statements issued since `from` (a prior `mark()`). */
  const report = (from = 0) => {
    let total = 0;
    for (const { cursor } of cursors.slice(from)) total += Number(cursor.rowsRead) || 0;
    return total;
  };
  const mark = () => cursors.length;
  return { proxy, calls, report, mark };
}

function seed(sql: any) {
  sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('h0','h0','h0',?,?)", iso(NOW), iso(NOW));
  const binding = (native: string) => JSON.stringify({ native_id: native, tag: 'n-' + native, workspace: '/w', native_created_at_ms: NOW - 3600000 });
  const sessions: [string, string][] = [['s0', 'running'], ['s1', 'running'], ['s2', 'running'], ['s3', 'stopped']];
  for (const [id, state] of sessions) {
    sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
      VALUES(?,'operator',?,'{}','h0','codex','interactive',?, ?,?,?)`, id, 'k' + id, state, binding('n-' + id), iso(NOW - 3600000), iso(NOW));
    sql.exec('INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,?,\'h0\',?)', id, 'session', iso(NOW - 3600000));
  }
  // Stale identities on other machines: an unindexed poll would scan these.
  for (let i = 0; i < STALE_AGENTS; i++) {
    sql.exec('INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,?,?,?)', 'stale-' + i, 'session', 'other-' + (i % 7), iso(NOW - 86400000));
  }
  const cred = (id: string, family: string, registry: string | null, revoked: boolean) => sql.exec(
    `INSERT INTO credentials(id,kind,token_hash,refresh_hash,family_id,subject,machine_id,actor,scopes,created_by_sub,created_at,expires_at,revoked_at,registry_actor)
     VALUES(?, 'agent', ?, ?, ?, ?, 'h0', ?, '[]', 'host:h0', ?, ?, ?, ?)`,
    id, 'th-' + id, 'rh-' + id, family, registry ?? 'session:' + id, registry ?? 'session:' + id,
    iso(NOW - 3600000), iso(NOW + 3600000), revoked ? iso(NOW - 1000) : null, registry);
  cred('c-live', 'f-live', 'session:s0', false); // s0 already has a credential
  cred('c-revoked', 'f-revoked', 'session:s2', true); // s2's credential is dead
  cred('c-stopped', 'f-stopped', 'session:s3', false); // s3 ended: the sweep must revoke this
  // Dead credential history: the polls must never scan it.
  for (let i = 0; i < DEAD_CREDS; i++) cred('c-dead-' + i, 'f-dead-' + i, 'session:gone-' + i, true);
  // Native intake-credential path: s-needy runs with a verified open link and no
  // credential yet; s-covered already has one; s-gone ended with a live family.
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('s-needy','operator','kn','{}','h0','codex','interactive','running',?,?,?)`, binding('n-needy'), iso(NOW - 3600000), iso(NOW));
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('s-covered','operator','kc','{}','h0','codex','interactive','running',?,?,?)`, binding('n-covered'), iso(NOW - 3600000), iso(NOW));
  sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
    VALUES('s-gone','operator','kg','{}','h0','codex','interactive','stopped',?,?,?)`, binding('n-gone'), iso(NOW - 3600000), iso(NOW));
  sql.exec("INSERT INTO worker_access(family_id,launch_id,session_id,task_id,workspace,expires_at) VALUES('fn-live','s-covered','s-covered','t-open','/w',?)", NOW + 3600000);
  sql.exec("INSERT INTO worker_access(family_id,launch_id,session_id,task_id,workspace,expires_at) VALUES('fn-dead','s-gone','s-gone','t-open','/w',?)", NOW + 3600000);
  for(const session of ['s-covered','s-gone'])sql.exec("INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,'hub-native-workers','h0',?)",'native-'+session,iso(NOW-3600000));
  cred('c-fn-live','fn-live','hub-native-workers:native-s-covered',false);
  cred('c-fn-dead','fn-dead','hub-native-workers:native-s-gone',false);
  sql.exec("INSERT INTO intake_tasks(id,data) VALUES('t-open',?)", JSON.stringify({ id: 't-open', project: 'agent-hub', title: 'open', status: 'open', version: 1, acceptance_criteria: 'AC', history: [], created_at: NOW, updated_at: NOW }));
  sql.exec("INSERT INTO intake_tasks(id,data) VALUES('t-done',?)", JSON.stringify({ id: 't-done', project: 'agent-hub', title: 'done', status: 'done', version: 1, acceptance_criteria: 'AC', history: [], created_at: NOW, updated_at: NOW }));
  sql.exec("INSERT INTO intake_session_links(session_key,task_id,authority) VALUES('s-needy','t-open','hub_launch_verified')");
  sql.exec("INSERT INTO intake_session_links(session_key,task_id,authority) VALUES('n-needy','t-open','hub_launch_verified')");
  sql.exec("INSERT INTO intake_session_links(session_key,task_id,authority) VALUES('s-covered','t-open','hub_launch_verified')");
}

const withHub = (fn: (hub: Hub) => Promise<void>) => runInDurableObject(
  (env as any).HUB.get((env as any).HUB.idFromName('rows-cred-' + crypto.randomUUID())), async (hub: Hub) => { seed(hub.sql); await fn(hub); });

const read = (hub: Hub, q: string, ...b: any[]) => { const c: any = hub.sql.exec(q, ...b); const rows = c.toArray(); return { rows, read: Number(c.rowsRead) }; };
const MESSAGING_TABLES = ['agents', 'session_control_sessions', 'credentials'] as const;

describe('credential poll indexes', () => {
  it('native lookup uses deployed launch identity and actor indexes, not an unindexed family join', () => withHub(async hub => {
    const plan = (q: string, ...b: any[]) => (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...b).toArray() as any[]).map(r => String(r.detail)).join(' | ');
    expect(plan('SELECT family_id,session_id FROM worker_access WHERE launch_id=?','s-covered')).toMatch(/SEARCH.*USING INDEX/);
    const credentials=plan('SELECT id,family_id,revoked_at,kind,machine_id FROM credentials INDEXED BY credentials_registry_actor WHERE registry_actor=? LIMIT 129','hub-native-workers:native-s-covered');
    expect(credentials).toContain('credentials_registry_actor');
    expect(credentials).not.toMatch(/SCAN\s+credentials/);
  }));

  it('identity backstops use the deployed expression project/tag key, not a new registry index', () => withHub(async hub => {
    const plan = (q: string, ...b: any[]) => (hub.sql.exec('EXPLAIN QUERY PLAN ' + q, ...b).toArray() as any[]).map(r => String(r.detail)).join(' | ');
    const agents=plan("SELECT tag FROM agents INDEXED BY idx_agents_project_tag_unique WHERE COALESCE(project,'')=? AND tag>? ORDER BY tag LIMIT 65",'session','');
    expect(agents).toContain('idx_agents_project_tag_unique');
    expect(agents).not.toMatch(/SCAN\s+agents/);
    const live=plan('SELECT id,family_id,revoked_at,kind,machine_id FROM credentials INDEXED BY credentials_registry_actor WHERE registry_actor=? LIMIT 129','session:s0');
    expect(live).toContain('credentials_registry_actor');
  }));
});

describe('write-driven credential sweeps', () => {
  it('the sweep gate opens once, then stays shut until a source table is written', () => withHub(async hub => {
    const changed = (...t: string[]) => (hub as any).credentialSweepChanged('probe', ...t);
    const done = (...t: string[]) => (hub as any).credentialSweepDone('probe', ...t);
    expect(changed(...MESSAGING_TABLES)).not.toBeNull(); // cold start: backfill once
    done(...MESSAGING_TABLES);
    expect(changed(...MESSAGING_TABLES)).toBeNull();
    hub.sql.exec('INSERT INTO agents(tag,project,machine_id,registered_at) VALUES(?,?,?,?)', 'fresh', 'session', 'h9', iso(NOW));
    expect(changed(...MESSAGING_TABLES)).not.toBeNull();
    done(...MESSAGING_TABLES);
    expect(changed(...MESSAGING_TABLES)).toBeNull();
  }));

  it('a repeat messaging poll skips the sweep and reads zero rows', () => withHub(async hub => {
    const t = tracked(hub.sql);
    // Same composition as the GET route in hub.ts: changed/done bracket the sweep.
    const poll = () => {
      const changed = (hub as any).credentialSweepChanged('messaging-credential', ...MESSAGING_TABLES);
      const out = pollSessionCredentials(t.proxy as any, 'h0', NOW, changed !== null);
      if (changed !== null) (hub as any).credentialSweepDone('messaging-credential', ...MESSAGING_TABLES);
      return out;
    };
    const first = poll();
    expect(first.map(s => s.session_id).sort()).toEqual(['s1', 's2']);
    // The backstop revoked the ended session's credential on the first poll.
    expect(read(hub, "SELECT revoked_at FROM credentials WHERE id = 'c-stopped'").rows[0].revoked_at).not.toBeNull();
    const sweeped = t.calls.filter(q => /FROM agents INDEXED BY idx_agents_project_tag_unique.*tag>\?/i.test(q));
    expect(sweeped.length).toBeGreaterThan(0);
    // The fair backstop intentionally continues across the retained identity
    // census before its source-stamp gate can become idle.
    for(let i=0;i<Math.ceil((STALE_AGENTS+4)/64);i++)poll();
    const from = t.mark();
    const second = poll();
    expect(second.map(s => s.session_id).sort()).toEqual(['s1', 's2']);
    // No sweep statement re-ran: identical exec calls would still be recorded
    // by the tracker even on a memo hit, so absence proves the gate skipped it.
    expect(t.calls.slice(from).filter(q => /FROM agents INDEXED BY idx_agents_project_tag_unique.*tag>\?/i.test(q))).toEqual([]);
    // And the repeat poll read zero rows (memoized zero-read cursors).
    expect(t.report(from)).toBe(0);
  }));

  it('ending a session re-arms the sweep and drops it from the poll', () => withHub(async hub => {
    const t = tracked(hub.sql);
    const poll = () => {
      const changed = (hub as any).credentialSweepChanged('messaging-credential', ...MESSAGING_TABLES);
      const out = pollSessionCredentials(t.proxy as any, 'h0', NOW, changed !== null);
      if (changed !== null) (hub as any).credentialSweepDone('messaging-credential', ...MESSAGING_TABLES);
      return out;
    };
    poll();
    hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id = 's1'");
    const again = poll();
    expect(again.map(s => s.session_id)).toEqual(['s2']);
  }));

  it('a repeat native list skips the sweep and serves the cached link', () => withHub(async hub => {
    const t = tracked(hub.sql);
    const access = new WorkerAccess(t.proxy as any, fn => fn(), () => true,
      { changed: (key, ...tables) => (hub as any).credentialSweepChanged(key, ...tables),
        done: (key, ...tables) => (hub as any).credentialSweepDone(key, ...tables) });
    const list = () => access.listNative({ kind: 'host', machineId: 'h0' } as any, ['host']);
    const first: any = await list().json();
    expect(first.sessions.map((s: any) => s.session_id)).toEqual(['s-needy']);
    // The backstop revoked the ended native session's family on the first poll.
    expect(read(hub, "SELECT revoked_at FROM credentials WHERE id = 'c-fn-dead'").rows[0].revoked_at).not.toBeNull();
    const from = t.mark();
    const second: any = await list().json();
    expect(second.sessions).toEqual(first.sessions);
    expect(t.calls.slice(from).filter(q => /FROM worker_access w LEFT JOIN/i.test(q))).toEqual([]);
    // The repeat list read zero rows (memoized zero-read cursors).
    expect(t.report(from)).toBe(0);
    // Ending the sessions re-arms the sweep, revokes the covered family, and
    // drops the needy session from the poll.
    hub.sql.exec("UPDATE session_control_sessions SET state = 'stopped' WHERE id IN ('s-needy','s-covered')");
    const third: any = await list().json();
    expect(third.sessions).toEqual([]);
    expect(read(hub, "SELECT revoked_at FROM credentials WHERE id = 'c-fn-live'").rows[0].revoked_at).not.toBeNull();
  }));
});

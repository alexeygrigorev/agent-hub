import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { SESSION_CONTROL_SCHEMA } from '../src/session_control';

const NOW = Date.UTC(2026, 10, 1, 22);
const inDO: any = runInDurableObject;
const admin = { caller: { isAdmin: true, actor: 'test-admin', kind: 'human' }, scopes: ['admin'] };
const agent = { caller: { actor: 'test-agent', kind: 'agent' }, scopes: ['fleet:launch'] };

function request(path: string) {
  return new Request('https://hub.test/api/fleet' + path, { method: 'GET' });
}

function tableSnapshot(sql: any) {
  const names = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")
    .toArray().map((row: any) => String(row.name));
  return names.map((name: string) => {
    const count = Number(sql.exec(`SELECT COUNT(*) AS n FROM "${name}"`).one().n);
    const columns = sql.exec(`PRAGMA table_info("${name}")`).toArray().map((column: any) => String(column.name));
    const updatedAt = columns.includes('updated_at')
      ? sql.exec(`SELECT updated_at FROM "${name}"`).toArray().map((row: any) => row.updated_at).sort((a: any, b: any) => String(a).localeCompare(String(b)))
      : null;
    return { name, count, updatedAt };
  });
}

it('returns every accounted reserved launch, including rows outside the latest-200 fleet view', async () => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName(crypto.randomUUID()));
  await inDO(stub, async (hub: any, state: any) => {
    const sql = hub.sql;
    const at = new Date(NOW).toISOString();
    for (const statement of SESSION_CONTROL_SCHEMA.split(';').filter(part => part.trim())) sql.exec(statement);
    sql.exec(`INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,drained,capabilities)
      VALUES('live-machine','fixture','fixture',?,?,0,?)`, at, at, JSON.stringify({ engines: ['claude'] }));
    sql.exec("INSERT INTO fleet_accounts(label,provider,max_concurrency,reserve_percent,enabled) VALUES('hub-claude','claude',300,0,1)");

    for (let i = 0; i < 205; i++) {
      const id = `launch-${String(i).padStart(3, '0')}`;
      const nativeId = `native-${i}`;
      const created = new Date(NOW - (205 - i) * 60_000).toISOString();
      sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
        VALUES(?,'fixture','human','fixture',?,'{}','live-machine','claude','hub-claude','headless','/repo','fixture','running',?,?,?)`,
        id, id, nativeId, created, at);
      sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,last_activity_at,workspace,account,created_at_ms)
        VALUES('live-machine',?,?,'claude','running',?,?,'/repo','hub-claude',?)`,
        nativeId, `task-hub-${id}`, at, at, NOW - (205 - i) * 60_000);
      if (i === 0) {
        sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at)
          VALUES('hub-session-old','fixture','key-old','{}',?,'live-machine','claude','headless','running',?,?)`, id, created, at);
      }
    }

    let maxBindings = 0;
    let queryPlan: string[] = [];
    const measuredSql = new Proxy(sql, {
      get(target, key) {
        if (key !== 'exec') return Reflect.get(target, key);
        return (query: string, ...bindings: any[]) => {
          if (query.includes('SELECT l.id, l.machine_id, l.state, l.native_id') && query.includes('fleet_sessions')) {
            maxBindings = Math.max(maxBindings, bindings.length);
            queryPlan = target.exec('EXPLAIN QUERY PLAN ' + query, ...bindings).toArray().map((row: any) => String(row.detail));
          }
          return target.exec(query, ...bindings);
        };
      },
    });
    const service = new FleetService(measuredSql, fn => state.storage.transactionSync(fn), () => NOW);
    service.init();

    const listedByAccountView = new Set(sql.exec('SELECT id FROM fleet_launches ORDER BY created_at DESC LIMIT 200').toArray().map((row: any) => row.id));
    const beforeReads = tableSnapshot(sql);
    const pages: any[] = [];
    let cursor: string | null = null;
    do {
      const suffix = cursor ? `&cursor=${encodeURIComponent(cursor)}` : '';
      const response = await service.handle(request(`/accounts/hub-claude/reserved-launches?limit=100${suffix}`), admin as any);
      expect(response.status).toBe(200);
      const page = await response.json() as any;
      expect(page.rows.length).toBeLessThanOrEqual(100);
      expect(page.live_ids).toEqual(['live-machine']);
      pages.push(...page.rows);
      cursor = page.next_cursor;
    } while (cursor);

    expect(tableSnapshot(sql)).toEqual(beforeReads);
    const status = service.accountStatus(sql.exec("SELECT * FROM fleet_accounts WHERE label='hub-claude'").one());
    expect(status.reserved).toBe(205);
    expect(pages).toHaveLength(status.reserved);
    expect(pages[0]).toMatchObject({
      id: 'launch-000', machine: 'live-machine', state: 'running', native_id: 'native-0',
      hub_session_id: 'hub-session-old', phase: 'running', last_activity_at: at,
      created_at_ms: NOW - 205 * 60_000,
    });
    expect(pages.some(row => row.id === 'launch-000' && !listedByAccountView.has(row.id))).toBe(true);
    expect(maxBindings).toBeLessThanOrEqual(100);
    expect(queryPlan.join('\n')).toMatch(/fleet_launch_account_state_id/);

    const forbidden = await service.handle(request('/accounts/hub-claude/reserved-launches?limit=100'), agent as any);
    expect(forbidden.status).toBe(403);
  });
});

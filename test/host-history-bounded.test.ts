import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { HostHistoryService, HISTORY_SCAN_LIMIT, HISTORY_COMPACT_WINDOWS } from '../src/host-history';
import { RowsMeter } from '../src/rows-meter';
import { WriteWatch } from '../src/write-watch';

const NOW = Date.parse('2026-11-03T20:00:00Z'), MINUTE = 60000, DAY = 86400000;

function fixture(fn: (history: HostHistoryService, sql: any, meter: RowsMeter) => void) {
  return (runInDurableObject as any)((env as any).HUB.get((env as any).HUB.idFromName('history-bounds-' + crypto.randomUUID())),
    async (_hub: any, state: any) => {
      // Seed via native storage so fixture size does not consume the guard's
      // operational budget. Only the operation under test is metered below.
      const sql = state.storage.sql, meter = new RowsMeter(() => NOW);
      const history = new HostHistoryService(meter.wrap(sql), { hostResources: () => [], cachedAccountStatus: () => ({}) } as any, () => NOW);
      history.init();
      await fn(history, sql, meter);
    });
}

function launches(sql: any, count: number, state = 'failed') {
  sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<?)
    INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
    SELECT ?||i,'test','human','test',?||i,'{}','a','codex','test','headless','/repo','',?,?,? FROM n`,
  count, state, state, state, new Date(NOW).toISOString(), new Date(NOW).toISOString());
}

describe('bounded host history maintenance', () => {
  it('keeps writes inside the minute throttle pending until a sample actually completes', () => fixture((_history, sql, meter) => {
    const watch = new WriteWatch(), tracked = meter.wrap(watch.wrap(sql));
    let now = NOW;
    const history = new HostHistoryService(tracked, { hostResources: () => [], cachedAccountStatus: () => ({}) } as any, () => now, watch);
    history.init(); history.sample();
    now += 30000;
    tracked.exec('INSERT INTO usage_quota_history VALUES(?,?,?,?,?)', 'test', 'codex', 'codex', now,
      JSON.stringify({ status: 'ok', windows: { weekly: { present: true, percent_remaining: 71 } } }));
    history.sample();
    expect(history.query('1h').series.some((s: any) => s.kind === 'quota_window')).toBe(false);
    now += 30000;
    history.sample();
    const weekly = history.query('1h').series.find((s: any) => s.kind === 'quota_window' && s.metric === 'weekly');
    expect(weekly.points.some((point: any) => point.value === 71)).toBe(true);
  }));

  it('does zero SQL work for unchanged sources after the old timer ceiling, but observes new quota journals', () => fixture((_history, sql, meter) => {
    const watch = new WriteWatch(), tracked = meter.wrap(watch.wrap(sql));
    let now = NOW;
    const history = new HostHistoryService(tracked, { hostResources: () => [], cachedAccountStatus: () => ({}) } as any, () => now, watch);
    history.init(); history.sample();
    now += DAY;
    meter.runSync('idle', () => history.sample());
    expect(meter.snapshot().idle?.calls ?? 0).toBe(0);
    tracked.exec('INSERT INTO usage_quota_history VALUES(?,?,?,?,?)', 'test', 'codex', 'codex', now,
      JSON.stringify({ status: 'ok', windows: { weekly: { present: true, percent_remaining: 70 } } }));
    meter.runSync('quota_changed', () => history.sample());
    expect(meter.snapshot().quota_changed.calls).toBeGreaterThan(0);
    expect(history.query('1h').series.some((s: any) => s.kind === 'quota_window' && s.metric === 'weekly')).toBe(true);
  }));

  it('reads only a bounded raw backfill prefix and exposes unknown counts on overflow', () => fixture((history, sql, meter) => {
    launches(sql, 2000);
    meter.runSync('backfill', () => history.backfill());
    const budget = meter.snapshot().backfill;
    // Scope max_read is the maximum whole invocation, not a per-statement
    // maximum: CI measured 133 = 129 launch rows + four metadata rows.
    const source = meter.statementTop(200).find(row => row.scope === 'backfill' && row.sql.startsWith('SELECT machine_id,state,updated_at FROM fleet_launches'))!;
    expect(source.max_read).toBeLessThanOrEqual(HISTORY_SCAN_LIMIT + 1);
    expect(budget.rows_read).toBeLessThan(400);
    console.log(`history backfill launch_source_rows=${source.max_read} total_rows=${budget.rows_read}`);
    const result = history.query('1h');
    expect(result.backfill_truncated).toBe(true);
    const failures = result.series.find((s: any) => s.kind === 'failures');
    expect(failures.points.every((p: any) => p.value === null)).toBe(true);
  }));

  it('caps failed, queued and native-running counts at indexed sentinels, preserving unknown', () => fixture((history, sql, meter) => {
    sql.exec("INSERT INTO resource_history_meta VALUES('backfill_v1',?)", NOW);
    sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('a','a','a',?,?)", new Date(NOW).toISOString(), new Date(NOW).toISOString());
    sql.exec("INSERT INTO fleet_catalog_seen VALUES('a',?)", new Date(NOW).toISOString());
    launches(sql, 2000); launches(sql, 2000, 'queued');
    sql.exec(`WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000)
      INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace)
      SELECT 'a','n'||i,'test','running',?,'/repo' FROM n`, new Date(NOW).toISOString());
    meter.runSync('sample', () => history.sample());
    const budget = meter.snapshot().sample;
    const counts = meter.statementTop(200).filter(row => row.scope === 'sample' && /^SELECT 1 FROM fleet_(launches|sessions) INDEXED BY/.test(row.sql)
      && (row.sql.includes('AND state=? LIMIT ?') || row.sql.includes("AND phase='running' LIMIT ?")));
    expect(counts.reduce((sum,row) => sum + row.calls,0)).toBe(3);
    for (const statement of counts) expect(statement.max_read).toBeLessThanOrEqual(HISTORY_SCAN_LIMIT + 1);
    expect(budget.rows_read).toBeLessThan(800);
    console.log(`history sample count_source_rows=${counts.map(row=>row.max_read).join(',')} total_rows=${budget.rows_read}`);
    const result = history.query('1h');
    expect(result.sample_truncated).toBe(true);
    for (const metric of ['running', 'queued', 'failed']) {
      expect(result.series.find((s: any) => s.kind === 'sessions' && s.metric === metric).points.every((p: any) => p.value === null)).toBe(true);
    }
    expect(sql.exec("SELECT COUNT(*) AS n FROM fleet_launches").one().n).toBe(4000);
  }));

  it('compacts a fixed number of windows and advances durable cursors across recreation', () => fixture((history, sql, meter) => {
    const begin = NOW - 9 * DAY;
    sql.exec(`WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<999)
      INSERT INTO resource_history(bucket_ms,payload_json) SELECT ?+i*?, '[]' FROM n`, begin, MINUTE);
    meter.runSync('compact', () => history.compact());
    const budget = meter.snapshot().compact;
    const reads = meter.statementTop(200).find(row => row.scope === 'compact' && row.sql.startsWith('SELECT bucket_ms,payload_json FROM resource_history'))!;
    expect(reads.max_read).toBeLessThanOrEqual(60);
    expect(reads.calls).toBeLessThanOrEqual(HISTORY_COMPACT_WINDOWS*3);
    expect(budget.rows_read).toBeLessThan(1300);
    console.log(`history compact window_source_rows=${reads.max_read} windows=${reads.calls} total_rows=${budget.rows_read}`);
    const cursor = Number(sql.exec("SELECT value FROM resource_history_meta WHERE key='compact_cursor_3600000'").one().value);
    expect(cursor).toBe(begin + HISTORY_COMPACT_WINDOWS * 3600000 - 1);
    const restored = new HostHistoryService(meter.wrap(sql), { hostResources: () => [] } as any, () => NOW);
    restored.compact();
    expect(Number(sql.exec("SELECT value FROM resource_history_meta WHERE key='compact_cursor_3600000'").one().value)).toBeGreaterThan(cursor);
  }));

  it('skips sparse old history with indexed seeks instead of rescanning the whole history', () => fixture((history, sql, meter) => {
    sql.exec(`WITH RECURSIVE n(i) AS (SELECT 0 UNION ALL SELECT i+1 FROM n WHERE i<399)
      INSERT INTO resource_history(bucket_ms,payload_json) SELECT ?+i*3600000, '[]' FROM n`, NOW - 30 * DAY);
    meter.runSync('sparse', () => history.compact());
    expect(meter.snapshot().sparse.rows_read).toBeLessThan(100);
    expect(sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n).toBe(400);
  }));
});

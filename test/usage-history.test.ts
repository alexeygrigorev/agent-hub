import { syncTbmFromServerAccounts, syncStoredTbmSnapshots } from '../src/tbm-ingestion';
import { recordUsageQuota, USAGE_RETENTION_MS } from '../src/usage-store';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { HostHistoryService, QUOTA_WINDOW_STALE_MS } from '../src/host-history';
import { displayAccount, displayAccountLabel, canonicalProviderKey } from '../src/account-display';
import { TOKEN_SOURCES, tokenEntity, tokenPostAt, validateTokenPayload } from '../src/token-history';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const NOW = Date.parse('2026-10-26T01:00:00Z'), DAY = 86400000, HOUR = 3600000;
const admin = { caller: { isAdmin: true, actor: 'test-admin' } };

async function fixture(fn: (h: HostHistoryService, hub: Hub, clock: { t: number }) => any) {
  const e = env as unknown as Env;
  await runInDurableObject(e.HUB.get(e.HUB.idFromName('usage-history-' + crypto.randomUUID())), async (hub: Hub) => {
    const clock = { t: NOW };
    const history = new HostHistoryService(hub.sql, hub.fleetService, () => clock.t);
    history.init();
    await fn(history, hub, clock);
  });
}

const quotaJson = (windows: Record<string, { pct: number | null; reset: string | null; present?: boolean }>) => JSON.stringify({
  status: 'ok',
  observed_at: new Date(NOW - 5 * 60000).toISOString(),
  windows: Object.fromEntries(Object.entries(windows).map(([name, w]) => [name, {
    percent_remaining: w.pct, reset_at: w.reset, present: w.present ?? w.pct !== null, rolling: false,
  }])),
});

describe('quota window history (AC1)', () => {
  it('preserves every known window and reset from one collection for each account', () => fixture((h, hub) => {
    const at = NOW - 5 * 60000;
    for (const [account, offset] of [['shared-go', 0], ['shared-claude', 10]] as const) {
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES(?,?,?,?)', account, account === 'shared-go' ? 'go' : 'claude',
        new Date(at).toISOString(), quotaJson({
          '5h': { pct: 50 + offset, reset: '2026-10-26T06:00:00Z' },
          '7d': { pct: 70 + offset, reset: '2026-11-01T00:00:00Z' },
          monthly: { pct: 80 + offset, reset: '2026-11-26T00:00:00Z' },
        }));
    }
    h.sample();
    for (const [account, offset] of [['shared-go', 0], ['shared-claude', 10]] as const) {
      const lines = h.query('24h').series.filter((s: any) => s.kind === 'quota_window' && s.entity === account);
      expect(lines.map((s: any) => s.metric).sort()).toEqual([
        '5h', '5h__reset_at', '7d', '7d__reset_at', 'monthly', 'monthly__reset_at',
      ].sort());
      for (const [metric, value] of Object.entries({
        '5h': 50 + offset, '7d': 70 + offset, monthly: 80 + offset,
        '5h__reset_at': Date.parse('2026-10-26T06:00:00Z'),
        '7d__reset_at': Date.parse('2026-11-01T00:00:00Z'),
        'monthly__reset_at': Date.parse('2026-11-26T00:00:00Z'),
      })) {
        const known = lines.find((s: any) => s.metric === metric).points.filter((p: any) => p.value !== null);
        expect(known.length).toBeGreaterThan(0);
        expect(known.every((p: any) => p.value === value)).toBe(true);
      }
    }
  }));
  it('preserves a common quota bucket identity across off-grid alias ingestion and real outages', () => fixture((h, hub, clock) => {
    const start=NOW-DAY;
    for(let minute=2;minute<24*60;minute+=10) {
      if(minute>=360 && minute<420) continue;
      clock.t=start+minute*60000;
      const account=minute<720?'urgent-muse':'shared-go';
      hub.sql.exec('DELETE FROM server_quota_snapshots');
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES(?,?,?,?)',account,'go',new Date(clock.t).toISOString(),
        quotaJson({'5h':{pct:80,reset:null}}));
      h.sample();
    }
    clock.t=NOW;
    const rows=h.query('24h').series.filter((s:any)=>s.kind==='quota_window'&&s.metric==='5h');
    expect(rows.map((s:any)=>s.entity).sort()).toEqual(['shared-go','urgent-muse']);
    const known=rows.flatMap((s:any)=>s.points.filter((p:any)=>p.value!==null));
    expect(new Set(known.map((p:any)=>p.bucket_at)).size).toBe(92);
    expect(known.every((p:any)=>p.at===Date.parse(p.observed_at)&&p.bucket_at<=p.at&&p.at<p.bucket_at+15*60000)).toBe(true);
    // Both alias grids expose the SAME identity for each genuinely empty bucket.
    for(let minute=360;minute<420;minute+=15) {
      const bucket=start+minute*60000;
      expect(rows.every((s:any)=>s.points.some((p:any)=>p.bucket_at===bucket&&p.value===null))).toBe(true);
    }
  }));

  it('uses a quota grid compatible with ten-minute samples while retaining outages', () => fixture((h) => {
    for(let ago=120;ago>=0;ago-=10) {
      if(ago>=40 && ago<=70) continue;
      h.record('quota_window','shared-codex','',NOW-ago*60000,{'5h':80},'server_quota_snapshots');
    }
    const row=h.query('24h').series.find((s:any)=>s.kind==='quota_window');
    expect(row.step_ms).toBe(15*60000);
    expect(row.points.some((p:any,i:number)=>p.value!==null && i && row.points[i-1].value!==null)).toBe(true);
    expect(row.points.some((p:any)=>p.at>NOW-70*60000 && p.at<NOW-40*60000 && p.value===null)).toBe(true);
  }));
  it('samples per account AND per window with reset times, sampled within 15 min and retained 32 days', () => fixture((h, hub) => {
    hub.sql.exec("INSERT INTO server_quota_snapshots VALUES('shared-codex','codex',?,?)",
      new Date(NOW - 5 * 60000).toISOString(), quotaJson({ '5h': { pct: null, reset: null }, '7d': { pct: 75, reset: '2026-11-01T06:10:58Z' }, monthly: { pct: null, reset: null } }));
    h.sample();
    const data = h.query('24h');
    const lines = data.series.filter((s: any) => s.kind === 'quota_window' && s.entity === 'shared-codex');
    expect(lines.map((s: any) => s.metric).sort()).toEqual(['7d', '7d__reset_at'].sort());
    // Null/unknown windows stay gaps: no fabricated line for 5h/monthly.
    expect(lines.some((s: any) => s.metric === '5h')).toBe(false);
    const remaining = lines.find((s: any) => s.metric === '7d');
    expect(remaining.points.filter((p: any) => p.value !== null).length).toBeGreaterThan(0);
    expect(remaining.points.filter((p: any) => p.value !== null).every((p: any) => p.value === 75)).toBe(true);
    const reset = lines.find((s: any) => s.metric === '7d__reset_at');
    expect(reset.points.filter((p: any) => p.value !== null).every((p: any) => p.value === Date.parse('2026-11-01T06:10:58Z'))).toBe(true);
    expect(data.retention_days).toBe(32);
  }));
  it('never holds stale snapshots forward: history keeps true timestamps only', () => fixture((h, hub, c) => {
    const staleAt = NOW - QUOTA_WINDOW_STALE_MS - 60000;
    hub.sql.exec("INSERT INTO server_quota_snapshots VALUES('shared-codex','codex',?,?)",
      new Date(staleAt).toISOString(), quotaJson({ '7d': { pct: 75, reset: null } }));
    h.sample();
    // The genuine 21-minute-old measurement stays at its true time; nothing
    // is invented after it (no hold-forward to "now").
    const lines = h.query('24h').series.filter((s: any) => s.kind === 'quota_window');
    const invented = lines.flatMap((l: any) => l.points).filter((p: any) => p.value !== null && p.at > staleAt);
    expect(invented).toEqual([]);
    // A fresh collection is picked up on the next tick (well within 15 min).
    c.t += 60000;
    hub.sql.exec("UPDATE server_quota_snapshots SET captured_at=?, quota_json=? WHERE account='shared-codex'",
      new Date(c.t - 60000).toISOString(), quotaJson({ '7d': { pct: 74, reset: null } }));
    h.sample();
    const fresh = h.query('24h').series.filter((s: any) => s.kind === 'quota_window' && s.metric === '7d');
    expect(fresh.length).toBe(1);
    expect(fresh[0].points.filter((p: any) => p.value === 74).length).toBeGreaterThan(0);
  }));
  it('retains at least 32 days of window history and compacts older points', () => fixture((h, hub, c) => {
    for (const day of [31, 10, 0]) {
      c.t = NOW - day * DAY;
      hub.sql.exec("DELETE FROM resource_history_meta WHERE key='sample'");
      hub.sql.exec("INSERT OR REPLACE INTO server_quota_snapshots VALUES('shared-codex','codex',?,?)",
        new Date(c.t - 60000).toISOString(), quotaJson({ '7d': { pct: 70 + day, reset: null } }));
      h.sample();
    }
    c.t = NOW;
    h.compact();
    const rows = hub.sql.exec('SELECT payload_json FROM resource_history').toArray() as any[];
    const kinds = new Set(rows.flatMap((r) => JSON.parse(r.payload_json).map((o: any) => o.kind)));
    expect(kinds.has('quota_window')).toBe(true);
    hub.sql.exec('DELETE FROM resource_history WHERE bucket_ms < ?', NOW - 32 * DAY);
    expect(hub.sql.exec('SELECT COUNT(*) AS n FROM resource_history').one().n).toBeGreaterThan(0);
  }));
  it('serves quota windows on the range API for 24h/7d/30d, rejecting bad ranges and anonymous callers', () => fixture(async (h, hub) => {
    hub.sql.exec("INSERT INTO server_quota_snapshots VALUES('shared-codex','codex',?,?)",
      new Date(NOW - 60000).toISOString(), quotaJson({ '7d': { pct: 75, reset: null } }));
    h.sample();
    for (const range of ['24h', '7d', '30d']) {
      const response = h.handle(new Request(`https://hub/api/monitoring/history?range=${range}`), admin);
      expect(response.status).toBe(200);
      const data: any = await response.json();
      expect(data.series.some((s: any) => s.kind === 'quota_window')).toBe(true);
    }
    expect(h.handle(new Request('https://hub/api/monitoring/history?range=90d'), admin).status).toBe(400);
    expect(h.handle(new Request('https://hub/api/monitoring/history?range=24h'), {}).status).toBe(401);
  }));
});

describe('account display mapping (AC4)', () => {
  it('reuses canonical quota labels without inventing sibling rename aliases', () => {
    expect(displayAccountLabel('codex-orin')).toBe('codex/codex codex-orin');
    expect(displayAccountLabel('go', 'urgent-muse')).toBe('opencode/go shared-go');
    expect(displayAccountLabel('zai', 'shared-zai')).toBe('codex/zai shared-zai');
    expect(displayAccountLabel('codex', 'shared-codex')).toBe('codex/codex shared-codex');
    expect(canonicalProviderKey('codex-orin')).toBe('codex');
    expect(displayAccountLabel('brand-new','my-acct')).toBe('brand-new/brand-new my-acct');
  });
});

const hourIso = (ms: number) => new Date(ms).toISOString().replace(/\.\d{3}Z$/, '.000Z');

describe('native token history (AC3)', () => {
  it('preserves hourly token lines and only the latest correction across all ranges', () => fixture((h) => {
    for (const [hoursAgo, value] of [[3, 100], [2, 200], [1, 300]]) {
      h.record('tokens', 'codex / shared-codex / agent-hub', 'hetzner', NOW-hoursAgo*HOUR,
        {input_tokens:value,output_tokens:10}, 'native_token_usage');
    }
    h.record('tokens', 'codex / shared-codex / agent-hub', 'hetzner', NOW-2*HOUR+10*60000,
      {input_tokens:250,output_tokens:20}, 'native_token_usage');
    for (const range of ['24h', '7d', '30d']) {
      const input=h.query(range).series.find((s:any)=>s.kind==='tokens' && s.metric==='input_tokens');
      expect(input.step_ms).toBe(HOUR);
      const known=input.points.filter((p:any)=>p.value!==null);
      expect(known.map((p:any)=>p.value)).toEqual([100,250,300]);
      expect(known[1].at-known[0].at).toBe(HOUR);
      expect(known[2].at-known[1].at).toBe(HOUR);
    }
  }));
  it('validates hourly buckets strictly and rejects unsupported engines', () => {
    const good = {
      schema_version: '1.0',
      hours: [{ hour_start: hourIso(NOW - HOUR).slice(0, 13) + ':00:00.000Z', engine: 'codex', account: 'shared-codex', project: 'agent-hub', input_tokens: 10, output_tokens: 2 }],
    };
    expect(validateTokenPayload(good, NOW, 32 * DAY).ok).toBe(true);
    expect(validateTokenPayload({ ...good, schema_version: '9' }, NOW, 32 * DAY).ok).toBe(false);
    expect(validateTokenPayload({ schema_version: '1.0', hours: [] }, NOW, 32 * DAY).ok).toBe(false);
    const gemini = JSON.parse(JSON.stringify(good));
    gemini.hours[0].engine = 'gemini';
    const rejected = validateTokenPayload(gemini, NOW, 32 * DAY);
    expect(rejected.ok).toBe(false);
    expect(rejected.error).toBe('unsupported_engine');
    const future = JSON.parse(JSON.stringify(good));
    future.hours[0].hour_start = hourIso(NOW + HOUR).slice(0, 13) + ':00:00.000Z';
    expect(validateTokenPayload(future, NOW, 32 * DAY).ok).toBe(false);
    const dup = JSON.parse(JSON.stringify(good));
    dup.hours.push(JSON.parse(JSON.stringify(good.hours[0])));
    expect(validateTokenPayload(dup, NOW, 32 * DAY).error).toBe('duplicate_hour');
  });
  it('labels every engine explicitly instead of a blanket Unknown', () => {
    const engines = TOKEN_SOURCES.map((s) => s.engine).sort();
    expect(engines).toEqual(['claude', 'codex', 'copilot', 'gemini', 'grok', 'opencode', 'zcodex']);
    for (const source of TOKEN_SOURCES) {
      expect(source.detail.length).toBeGreaterThan(10);
      if (!source.supported) expect(source.detail).toMatch(/no native/);
    }
  });
  it('canonicalizes token ingestion and exposes comparable receipt order across old aliases', () => fixture((h,hub,c) => {
    expect(tokenEntity({engine:'opencode',account:'urgent-muse',project:'p'})).toBe('opencode / shared-go / p');
    expect(tokenEntity({engine:'zcodex',account:'shared-zai',project:'p'})).toBe('codex / shared-zai / p');
    const hour=NOW-2*HOUR;
    h.record('tokens','opencode / urgent-muse / p','hetzner',hour+60000,{input_tokens:100},'native_token_usage');
    c.t++;
    h.record('tokens','opencode / shared-go / p','hetzner',hour,{input_tokens:200},'native_token_usage');
    const series=h.query('24h').series.filter((s:any)=>s.kind==='tokens');
    const point=(account:string)=>series.find((s:any)=>s.entity===`opencode / ${account} / p`).points.find((p:any)=>p.value!==null);
    const old=point('urgent-muse'), current=point('shared-go');
    expect(current.recorded_at>old.recorded_at).toBe(true);
    expect(current.observed_at<old.observed_at).toBe(true);
  }));
  it('keeps corrections independent across machines sharing an account and hour', () => fixture(async (h, hub) => {
    const entity = 'codex / shared-codex / multihost';
    const hour = NOW - 2 * HOUR;
    for (const [machine,value] of [['a',100],['a',150],['b',200],['a',200]] as const) {
      const at = tokenPostAt(hub.sql, entity, hour, value, 1, NOW, 32 * DAY, machine);
      h.record('tokens', entity, machine, at, {input_tokens:value,output_tokens:1}, 'native_token_usage');
      h.flush();
    }
    const series=h.query('24h').series.filter((s:any)=>s.entity===entity && s.metric==='input_tokens');
    expect(series).toHaveLength(2);
    for(const row of series) expect(row.points.filter((p:any)=>p.value!==null).map((p:any)=>p.value)).toEqual([200]);
  }));
  it('accepts corrections beyond the legacy thirtieth revision', () => fixture(async (h, hub) => {
    const entity = 'codex / shared-codex / revisions';
    const hour = NOW - 2 * HOUR;
    let previous = -1;
    for (let value = 0; value < 40; value++) {
      const at = tokenPostAt(hub.sql, entity, hour, value, 1, NOW, 32 * DAY);
      expect(at).toBeGreaterThan(previous);
      expect(at).toBeLessThan(hour + HOUR);
      h.record('tokens', entity, 'hetzner', at, {input_tokens:value,output_tokens:1}, 'native_token_usage');
      h.flush();
      previous = at;
    }
    const points = h.query('24h').series.find((s:any)=>s.entity===entity && s.metric==='input_tokens').points;
    expect(points.filter((p:any)=>p.value!==null).map((p:any)=>p.value)).toEqual([39]);
  }));
  it('records token hours into history and serves them on 24h/7d/30d ranges', () => fixture(async (h, hub) => {
    const entity = tokenEntity({ engine: 'codex', account: 'shared-codex', project: 'agent-hub' });
    expect(entity).toBe('codex / shared-codex / agent-hub');
    const hourMs = NOW - 2 * HOUR;
    const at = tokenPostAt(hub.sql, entity, hourMs, 100, 20, NOW, 32 * DAY);
    expect(at).toBe(hourMs);
    h.record('tokens', entity, 'hetzner', at, { input_tokens: 100, output_tokens: 20 }, 'native_token_usage');
    // Identical repost is idempotent (same instant, no duplicate revision).
    expect(tokenPostAt(hub.sql, entity, hourMs, 100, 20, NOW, 32 * DAY)).toBe(hourMs);
    // Genuine late correction gets a newer instant and overwrites.
    const at2 = tokenPostAt(hub.sql, entity, hourMs, 150, 20, NOW, 32 * DAY);
    expect(at2).toBeGreaterThan(hourMs);
    h.record('tokens', entity, 'hetzner', at2, { input_tokens: 150, output_tokens: 20 }, 'native_token_usage');
    h.flush();
    for (const range of ['24h', '7d', '30d']) {
      const data = h.query(range);
      const lines = data.series.filter((s: any) => s.kind === 'tokens' && s.entity === entity);
      expect(lines.map((s: any) => s.metric).sort()).toEqual(['input_tokens', 'output_tokens']);
      const input = lines.find((s: any) => s.metric === 'input_tokens');
      const known = input.points.filter((p: any) => p.value !== null);
      expect(known.length).toBeGreaterThan(0);
      expect(known[known.length - 1].value).toBe(150);
    }
  }));
});


describe('shared usage journal for TBM and charts',()=>{
  it('replays identical collections without spending journal row writes',()=>fixture((history,hub)=>{
    hub.sql.exec('CREATE TABLE usage_write_audit(n INTEGER)');hub.sql.exec('INSERT INTO usage_write_audit VALUES(0)');
    hub.sql.exec('CREATE TRIGGER usage_insert_audit AFTER INSERT ON usage_quota_history BEGIN UPDATE usage_write_audit SET n=n+1; END');
    hub.sql.exec('CREATE TRIGGER usage_update_audit AFTER UPDATE ON usage_quota_history BEGIN UPDATE usage_write_audit SET n=n+1; END');
    const quota=JSON.parse(quotaJson({'5h':{pct:50,reset:new Date(NOW+HOUR).toISOString()}}));
    recordUsageQuota(hub.sql,'shared-codex','codex',quota,NOW);
    for(let i=0;i<3;i++) recordUsageQuota(hub.sql,'shared-codex','codex',quota,NOW);
    expect((hub.sql.exec('SELECT n FROM usage_write_audit').toArray()[0] as any).n).toBe(1);
  }));

  it('does not turn unknown or stale native windows into fresh chart evidence',()=>fixture((history,hub)=>{
    for (const [account,status,stale] of [['unknown-account','unknown',false],['stale-account','ok',true]] as const) {
      const quota={status,details:{stale},windows:{'5h':{present:true,percent_remaining:50,reset_at:new Date(NOW+HOUR).toISOString()}}};
      recordUsageQuota(hub.sql,account,'codex',quota,NOW);
      hub.sql.exec('INSERT INTO server_quota_snapshots VALUES(?,?,?,?)',account,'codex',new Date(NOW).toISOString(),JSON.stringify(quota));
    }
    history.sample();
    expect(history.query('24h').series.filter((s:any)=>s.kind==='quota_window')).toEqual([]);
  }));

  it('persists account/engine/window evidence and replays it after latest-only sources disappear',()=>fixture((history,hub)=>{
    const at=NOW-10*60000;
    const quota=JSON.parse(quotaJson({'5h':{pct:50,reset:new Date(NOW+5*HOUR).toISOString()},'7d':{pct:75,reset:new Date(NOW+7*DAY).toISOString()}}));
    syncTbmFromServerAccounts(hub.sql,[{account:'shared-zai',provider:'zai',quota}],at);
    const stored:any=hub.sql.exec("SELECT * FROM usage_quota_history WHERE account='shared-zai'").toArray()[0];
    expect(stored.engine).toBe('codex');expect(stored.provider).toBe('zai');expect(stored.observed_at_ms).toBe(at);
    expect(JSON.parse(stored.quota_json).windows['5h'].percent_remaining).toBe(50);
    hub.sql.exec('DELETE FROM server_quota_snapshots');hub.sql.exec('DELETE FROM fleet_subscriptions');
    hub.sql.exec('DELETE FROM tbm_quota_pools');
    syncStoredTbmSnapshots(hub.sql,NOW);
    const pool:any=hub.sql.exec("SELECT * FROM tbm_quota_pools WHERE account='shared-zai' AND window_type='5h'").toArray()[0];
    expect(pool.remaining_percent).toBe(50);expect(pool.last_updated_ms).toBe(at);
    history.sample();
    const lines=history.query('24h').series.filter((s:any)=>s.entity==='shared-zai' && s.kind==='quota_window');
    expect(lines.find((s:any)=>s.metric==='5h').points.some((p:any)=>p.value===50)).toBe(true);
    expect(lines.find((s:any)=>s.metric==='7d').points.some((p:any)=>p.value===75)).toBe(true);
  }));
  it('retains 32 days, including unknown windows, without retaining unrelated input',()=>fixture((history,hub)=>{
    const quota={status:'unknown',private_secret:'must-not-be-stored',windows:{'5h':{present:false,percent_remaining:null,reset_at:null}}};
    recordUsageQuota(hub.sql,'shared-codex','codex',quota,NOW-USAGE_RETENTION_MS-1);
    recordUsageQuota(hub.sql,'shared-codex','codex',quota,NOW-USAGE_RETENTION_MS);
    recordUsageQuota(hub.sql,'shared-codex','codex',quota,NOW);
    const rows:any[]=hub.sql.exec('SELECT * FROM usage_quota_history ORDER BY observed_at_ms').toArray();
    expect(rows).toHaveLength(2);expect(rows[0].observed_at_ms).toBe(NOW-USAGE_RETENTION_MS);
    expect(rows[1].quota_json).not.toContain('must-not-be-stored');
    expect(JSON.parse(rows[1].quota_json).windows['5h'].percent_remaining).toBeNull();
  }));
});

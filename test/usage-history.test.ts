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

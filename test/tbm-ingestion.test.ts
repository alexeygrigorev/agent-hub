import { describe, expect, it } from "vitest";
import { runInDurableObject } from "cloudflare:test";
import { defaultEnv } from "./m1-focus-setup";
import type { Hub } from "../src/hub";
import { ensureTbmQuotaSchema } from "../src/quota";
import {
  inferCycleMs,
  resolveTokenCapacity,
  syncTbmFromServerAccounts,
  syncWindowsIntoTbm,
  tbmPoolId,
} from "../src/tbm-ingestion";

import { tickTbm } from '../src/tbm-controller';

const stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName("hub"));
const NOW = Date.UTC(2026, 9, 22, 12, 0, 0);
const DAY = 86_400_000;
const HOUR = 3_600_000;

function seedFleet(sql: any): void {
  sql.exec(
    "INSERT OR REPLACE INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('tbm-ingest-main', 'codex', 1, 15, 1)"
  );
  sql.exec(
    "INSERT OR REPLACE INTO fleet_accounts (label, provider, max_concurrency, reserve_percent, enabled) VALUES ('tbm-ingest-claude', 'claude', 1, 0, 1)"
  );
  for (const id of [1,2,3,4]) sql.exec("INSERT OR REPLACE INTO fleet_routes (machine_id,engine,account) VALUES (?,'codex','tbm-ingest-main')", `tbm-ingest-host-${id}`);
}

describe("AC1 tbm live ingestion", () => {
  it('preserves real banked-reset deadlines without inventing token quantities', async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      const expires = new Date(NOW + 2 * HOUR).toISOString();
      const quota = { status: 'ok', details: { banked_resets: [
        { available: true, expires_at: expires, label: 'Full reset' },
        { available: true, expires_at: expires, label: 'Full reset' },
      ] }, windows: { '7d': { present: true, percent_remaining: 72,
        reset_at: new Date(NOW + 7 * DAY).toISOString(), rolling: false } } };
      const input = [{ provider: 'codex', account: 'banked-test', quota }];
      syncTbmFromServerAccounts(sql, input, NOW);
      syncTbmFromServerAccounts(sql, input, NOW + 60000);
      const banks: any[] = sql.exec("SELECT * FROM tbm_banked_tranches WHERE pool_id='codex:banked-test:7d'").toArray();
      expect(banks).toHaveLength(2);
      expect(banks[0]).toMatchObject({ quantity_known: 0, remaining_tokens: 0,
        expires_at_ms: NOW + 2 * HOUR, status: 'active' });
      const pool: any = sql.exec("SELECT * FROM tbm_quota_pools WHERE pool_id='codex:banked-test:7d'").toArray()[0];
      expect(pool).toMatchObject({ quota_unit: 'percent', capacity_known: 0, remaining_percent: 72 });
    });
  });
  it('ingests only the explicit account named by a Hub observation', async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      sql.exec("INSERT OR REPLACE INTO fleet_accounts (label,provider,max_concurrency,reserve_percent,enabled) VALUES ('codex-a','codex',1,15,1)");
      sql.exec("INSERT OR REPLACE INTO fleet_accounts (label,provider,max_concurrency,reserve_percent,enabled) VALUES ('codex-b','codex',1,15,1)");
      const quota = { status: 'ok', details: { limit_reached: false }, windows: {
        '7d': { present: true, percent_remaining: 60, reset_at: new Date(NOW + 7 * DAY).toISOString(), rolling: false },
      } };
      syncTbmFromServerAccounts(sql, [{ provider: 'codex', account: 'codex-a', quota }], NOW);
      expect(sql.exec("SELECT pool_id FROM tbm_quota_pools WHERE account='codex-a'").toArray()).toHaveLength(1);
      expect(sql.exec("SELECT pool_id FROM tbm_quota_pools WHERE account='codex-b'").toArray()).toHaveLength(0);
    });
  });
  it("pure helpers: pool id, cycle inference and token capacity never fabricate", () => {
    expect(tbmPoolId("codex", "main", "7d")).toBe("codex:main:7d");
    // No single hardcoded 7d: each window maps to its own nominal cycle.
    expect(inferCycleMs("5h", null, NOW + 5 * HOUR)).toBe(5 * HOUR);
    expect(inferCycleMs("7d", null, NOW + 7 * DAY)).toBe(7 * DAY);
    expect(inferCycleMs("unknown-window", null, NOW + DAY)).toBeNull();
    // Observed reset delta wins over the name map (real cycle).
    expect(inferCycleMs("7d", NOW, NOW + 6 * DAY)).toBe(6 * DAY);
    // Percent-only stays unknown.
    expect(resolveTokenCapacity({ percent_remaining: 55, unit: null })).toMatchObject({ known: false });
    expect(resolveTokenCapacity({ percent_remaining: 55, unit: "percent" })).toMatchObject({
      known: false,
    });
    expect(resolveTokenCapacity({ percent_remaining: 55, unit: "credits" })).toMatchObject({
      known: false,
    });
    // Tokens with explicit numbers are known; percentages never convert.
    expect(
      resolveTokenCapacity({ unit: "tokens", credits_remaining: 800_000, spend_limit: 1_000_000 })
    ).toMatchObject({ known: true, total: 1_000_000, remaining: 800_000 });
    expect(
      resolveTokenCapacity({ unit: "tokens", spend_used: 200_000, spend_limit: 1_000_000 })
    ).toMatchObject({ known: true, total: 1_000_000, remaining: 800_000 });
    // Total unknown but remaining known: still not fully known, no fabrication.
    expect(
      resolveTokenCapacity({ unit: "tokens", credits_remaining: 800_000 })
    ).toMatchObject({ known: false, remaining: 800_000 });
  });

  it("Hub account observations fill pools per account/window with real reset and cycle", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:tbm-ingest-main:%'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:tbm-ingest-main:%'");
      const reset7d = new Date(NOW + 7 * DAY).toISOString();
      const counts = syncTbmFromServerAccounts(sql, [{ account: 'tbm-ingest-main', provider: 'codex', quota: {
        status: 'ok', error: null, observed_at: new Date(NOW - 1000).toISOString(),
        details: { limit_reached: false },
        windows: { '7d': { percent_remaining: 78.5, reset_at: reset7d, present: true, rolling: false } },
      } }], NOW);
      expect(counts.pools).toBe(1);
        const pool: any = sql
          .exec("SELECT * FROM tbm_quota_pools WHERE pool_id = ?", "codex:tbm-ingest-main:7d")
          .toArray()[0];
        expect(pool).toBeDefined();
        expect(pool.provider).toBe("codex");
        expect(pool.account).toBe("tbm-ingest-main");
        expect(pool.reset_at_ms).toBe(Date.parse(reset7d));
        expect(pool.cycle_ms).toBe(7 * DAY);
        expect(pool.window_type).toBe("7d");
        expect(pool.remaining_percent).toBeCloseTo(78.5);
        // Percent-only: unknown capacity stays unknown, never fabricated.
        expect(Number(pool.capacity_known)).toBe(0);
        expect(pool.total_capacity_tokens).toBe(0);
        expect(pool.remaining_tokens).toBe(0);
        expect(pool.headroom_percentage).toBeCloseTo(78.5);
        // No tranche for unknown capacity (no fabrication).
        expect(
          sql
            .exec("SELECT * FROM tbm_banked_tranches WHERE pool_id = ?", pool.pool_id)
            .toArray()
        ).toHaveLength(0);
    });
  });

  it("token-known primary quota is never duplicated into banked quota", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id = 'codex:tbm-ingest-main:7d'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id = 'codex:tbm-ingest-main:7d'");
      const reset = new Date(NOW + 7 * DAY).toISOString();
      syncTbmFromServerAccounts(sql, [{ account: 'tbm-ingest-main', provider: 'codex', quota: {
        status: 'ok', error: null, observed_at: new Date(NOW - 1000).toISOString(), details: { limit_reached: false },
        windows: { '7d': { percent_remaining: 80, reset_at: reset, present: true, rolling: false,
          unit: 'tokens', credits_remaining: 800_000, spend_limit: 1_000_000 } },
      } }], NOW);
        const pool: any = sql
          .exec("SELECT * FROM tbm_quota_pools WHERE pool_id = ?", "codex:tbm-ingest-main:7d")
          .toArray()[0];
        expect(pool).toMatchObject({
          total_capacity_tokens: 1_000_000,
          remaining_tokens: 800_000,
        });
        expect(Number(pool.capacity_known)).toBe(1);
        const tranches = sql
          .exec("SELECT * FROM tbm_banked_tranches WHERE pool_id = ?", pool.pool_id)
          .toArray() as any[];
        expect(tranches).toHaveLength(0);
    });
  });

  it("5h windows use a 5h cycle (not hardcoded 7d) and rolling windows bank nothing", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:tbm-ingest-main:5h'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:tbm-ingest-main:5h'");
      const reset = new Date(NOW + 5 * HOUR).toISOString();
      syncTbmFromServerAccounts(sql, [{ account: 'tbm-ingest-main', provider: 'codex', quota: {
        status: 'ok', error: null, observed_at: new Date(NOW - 1000).toISOString(), details: { limit_reached: false },
        windows: { '5h': { percent_remaining: 60, reset_at: reset, present: true, rolling: true,
          unit: 'tokens', credits_remaining: 600_000, spend_limit: 1_000_000 } },
      } }], NOW);
        const pool: any = sql
          .exec("SELECT * FROM tbm_quota_pools WHERE pool_id = ?", "codex:tbm-ingest-main:5h")
          .toArray()[0];
        expect(pool.cycle_ms).toBe(5 * HOUR);
        expect(pool.window_type).toBe("5h");
        expect(
          sql.exec("SELECT * FROM tbm_banked_tranches WHERE pool_id = ?", pool.pool_id).toArray()
        ).toHaveLength(0);
    });
  });

  it("older Hub observations do not refill TBM pools", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:%tbm-ingest%'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:%tbm-ingest%'");
      const reset = new Date(NOW + 7 * DAY).toISOString();
      const input = (percent: number) => [{ account: 'tbm-ingest-main', provider: 'codex', quota: {
        status: 'ok', error: null, observed_at: new Date(NOW - 1000).toISOString(), details: { limit_reached: false },
        windows: { '7d': { percent_remaining: percent, reset_at: reset, present: true, rolling: false } },
      } }];
      syncTbmFromServerAccounts(sql, input(70), NOW);
        const before: any = sql
          .exec(
            "SELECT last_updated_ms FROM tbm_quota_pools WHERE pool_id = ?",
            "codex:tbm-ingest-main:7d"
          )
          .toArray()[0];
      syncTbmFromServerAccounts(sql, input(20), NOW - 500);
        const after: any = sql
          .exec(
            "SELECT last_updated_ms FROM tbm_quota_pools WHERE pool_id = ?",
            "codex:tbm-ingest-main:7d"
          )
          .toArray()[0];
        expect(after.last_updated_ms).toBe(before.last_updated_ms);
    });
  });

  it("server collections fill pools per canonical account", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id = 'claude:tbm-ingest-claude:7d'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id = 'claude:tbm-ingest-claude:7d'");
      const reset = new Date(NOW + 7 * DAY).toISOString();
      const counts = syncTbmFromServerAccounts(
        sql,
        [
          {
            account: "tbm-ingest-claude",
            provider: "claude",
            quota: {
              status: "ok",
              error: null,
              observed_at: new Date(NOW - 1000).toISOString(),
              details: { limit_reached: false },
              windows: {
                "7d": { percent_remaining: 42, reset_at: reset, present: true, rolling: false },
              },
            },
          },
        ],
        NOW
      );
      expect(counts.pools).toBe(1);
      const pool: any = sql
        .exec("SELECT * FROM tbm_quota_pools WHERE pool_id = ?", "claude:tbm-ingest-claude:7d")
        .toArray()[0];
      expect(pool).toBeDefined();
      expect(pool.reset_at_ms).toBe(Date.parse(reset));
      expect(pool.cycle_ms).toBe(7 * DAY);
      expect(Number(pool.capacity_known)).toBe(0);
    });
  });
});


it('never marks a live tranche exhausted on an explicit banked-probe failure', async () => {
  // Intake 7bf6f6ee AC2/AC3: an ok-status route whose reset-credits sub-probe
  // failed carries an empty grant list plus banked_resets_error. That is
  // explicit unknown, not a consumed grant: the tranche stays active.
  await runInDurableObject(stub, (hub: Hub) => {
    const sql: any = hub.sql;
    const quota: any = { status: 'ok', details: { banked_resets: [
      { available: true, expires_at: new Date(NOW+2*HOUR).toISOString(), label: 'Full reset' },
    ] }, windows: { '5h': { present: true, percent_remaining: 80, reset_at: new Date(NOW+5*HOUR).toISOString() } } };
    syncTbmFromServerAccounts(sql, [{ provider: 'codex', account: 'banked-unknown', quota }], NOW);
    quota.details.banked_resets = [];
    quota.details.banked_resets_error = 'quota_unreachable';
    syncTbmFromServerAccounts(sql, [{ provider: 'codex', account: 'banked-unknown', quota }], NOW+60000);
    const rows: any[] = sql.exec("SELECT * FROM tbm_banked_tranches WHERE pool_id='codex:banked-unknown:5h'").toArray();
    expect(rows).toHaveLength(1);
    expect(rows[0].status).toBe('active');
  });
});


it('settles a banked expiry once even if a collection removes the expired grant before the next tick', async () => {
  await runInDurableObject(stub, (hub: Hub) => {
    const sql: any = hub.sql;
    const quota: any = { status: 'ok', details: { banked_resets: [
      { available: true, expires_at: new Date(NOW+HOUR).toISOString(), label: 'Full reset' },
    ] }, windows: { '5h': { present: true, percent_remaining: 80, reset_at: new Date(NOW+5*HOUR).toISOString() } } };
    syncTbmFromServerAccounts(sql, [{ provider: 'codex', account: 'expiry-order', quota }], NOW);
    quota.details.banked_resets = [];
    syncTbmFromServerAccounts(sql, [{ provider: 'codex', account: 'expiry-order', quota }], NOW+HOUR+60000);
    tickTbm(sql, NOW+HOUR+60000);
    tickTbm(sql, NOW+HOUR+2*60000);
    const rows: any[] = sql.exec("SELECT * FROM tbm_reward_events WHERE pool_id='codex:expiry-order:5h' AND banked_waste_penalty>0").toArray();
    expect(rows).toHaveLength(1);
    expect(rows[0].banked_waste_penalty).toBe(250);
  });
});


it('keeps a known cycle across a future deadline correction without treating the delta as a new cycle', async () => {
  await runInDurableObject(stub, (hub: Hub) => {
    const sql: any = hub.sql;
    const item = {provider:'codex',account:'deadline-adjustment',windowName:'5h',window:{present:true,percent_remaining:80,reset_at:new Date(NOW+HOUR).toISOString()}};
    syncWindowsIntoTbm(sql,[item],NOW);
    item.window.reset_at = new Date(NOW+2*HOUR).toISOString();
    syncWindowsIntoTbm(sql,[item],NOW+60000);
    expect(sql.exec("SELECT cycle_ms FROM tbm_quota_pools WHERE pool_id='codex:deadline-adjustment:5h'").one().cycle_ms).toBe(5*HOUR);
  });
});


it('does not recreate an obsolete reset after repeated collection of a corrected future deadline', async () => {
  await runInDurableObject(stub, (hub: Hub) => {
    const sql: any = hub.sql;
    const item = {provider:'codex',account:'repeat-correction',windowName:'5h',window:{present:true,percent_remaining:80,reset_at:new Date(NOW+HOUR).toISOString()}};
    syncWindowsIntoTbm(sql,[item],NOW);
    tickTbm(sql,NOW);
    item.window.reset_at = new Date(NOW+2*HOUR).toISOString();
    syncWindowsIntoTbm(sql,[item],NOW+59*60000);
    syncWindowsIntoTbm(sql,[item],NOW+61*60000);
    tickTbm(sql,NOW+61*60000);
    expect(sql.exec("SELECT * FROM tbm_reward_events WHERE pool_id='codex:repeat-correction:5h' AND event_kind='reset'").toArray()).toHaveLength(0);
    tickTbm(sql,NOW+2*HOUR+60000);
    tickTbm(sql,NOW+2*HOUR+2*60000);
    const events: any[] = sql.exec("SELECT * FROM tbm_reward_events WHERE pool_id='codex:repeat-correction:5h' AND event_kind='reset'").toArray();
    expect(events).toHaveLength(1);
    expect(events[0].timestamp_ms).toBe(NOW+2*HOUR);
  });
});


it('advances real quota after the expiry tick already captured the same boundary', async () => {
  await runInDurableObject(stub, (hub: Hub) => {
    const sql: any = hub.sql;
    const item = {provider:'codex',account:'tick-first-rollover',windowName:'5h',window:{present:true,percent_remaining:80,reset_at:new Date(NOW+HOUR).toISOString()}};
    syncWindowsIntoTbm(sql,[item],NOW);
    tickTbm(sql,NOW);
    tickTbm(sql,NOW+HOUR);
    item.window.percent_remaining=95;
    item.window.reset_at=new Date(NOW+6*HOUR).toISOString();
    syncWindowsIntoTbm(sql,[item],NOW+61*60000);
    syncWindowsIntoTbm(sql,[item],NOW+62*60000);
    tickTbm(sql,NOW+62*60000);
    expect(sql.exec("SELECT reset_at_ms FROM tbm_quota_pools WHERE pool_id='codex:tick-first-rollover:5h'").one().reset_at_ms).toBe(NOW+6*HOUR);
    const events: any[] = sql.exec("SELECT * FROM tbm_reward_events WHERE pool_id='codex:tick-first-rollover:5h' AND event_kind='reset'").toArray();
    expect(events).toHaveLength(1);
  });
});

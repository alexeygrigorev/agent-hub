import { describe, expect, it } from "vitest";
import { runInDurableObject } from "cloudflare:test";
import { defaultEnv } from "./m1-focus-setup";
import type { Hub } from "../src/hub";
import { ensureTbmQuotaSchema } from "../src/quota";
import {
  inferCycleMs,
  resolveTokenCapacity,
  syncTbmFromServerAccounts,
  accountsForHostProvider,
  tbmPoolId,
} from "../src/tbm-ingestion";

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

function hostSnap(hostId: string, capturedIso: string, routes: any): any {
  return {
    schema_version: "1.1",
    collector_version: "test/1.0",
    captured_at: capturedIso,
    host_id: hostId,
    status: "ok",
    error: null,
    routes,
  };
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
  it('never spreads one provider observation across multiple accounts', async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      sql.exec("INSERT OR REPLACE INTO fleet_accounts (label,provider,max_concurrency,reserve_percent,enabled) VALUES ('codex-a','codex',1,15,1)");
      sql.exec("INSERT OR REPLACE INTO fleet_accounts (label,provider,max_concurrency,reserve_percent,enabled) VALUES ('codex-b','codex',1,15,1)");
      expect(accountsForHostProvider(sql, 'ambiguous-host', 'codex')).toEqual([]);
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

  it("accepted host collection fills pools per account/window with real reset and cycle", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      const hostId = "tbm-ingest-host-1";
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:tbm-ingest-main:%'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:tbm-ingest-main:%'");
      hub.quotaStore.setClock(() => NOW);
      try {
        const captured = new Date(NOW - 1000).toISOString();
        const reset7d = new Date(NOW + 7 * DAY).toISOString();
        const res = hub.quotaStore.putSnapshot(
          hostId,
          hostSnap(hostId, captured, {
            codex: {
              status: "ok",
              error: null,
              observed_at: captured,
              details: { limit_reached: false },
              windows: {
                "7d": {
                  percent_remaining: 78.5,
                  reset_at: reset7d,
                  present: true,
                  rolling: false,
                },
              },
            },
          }),
          NOW
        );
        expect(res.ok).toBe(true);
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
      } finally {
        hub.quotaStore.setClock(undefined);
      }
    });
  });

  it("token-known primary quota is never duplicated into banked quota", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      const hostId = "tbm-ingest-host-2";
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id = 'codex:tbm-ingest-main:7d'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id = 'codex:tbm-ingest-main:7d'");
      hub.quotaStore.setClock(() => NOW);
      try {
        const captured = new Date(NOW - 1000).toISOString();
        const reset = new Date(NOW + 7 * DAY).toISOString();
        const res = hub.quotaStore.putSnapshot(
          hostId,
          hostSnap(hostId, captured, {
            codex: {
              status: "ok",
              error: null,
              observed_at: captured,
              details: { limit_reached: false },
              windows: {
                "7d": {
                  percent_remaining: 80,
                  reset_at: reset,
                  present: true,
                  rolling: false,
                  unit: "tokens",
                  credits_remaining: 800_000,
                  spend_limit: 1_000_000,
                },
              },
            },
          }),
          NOW
        );
        expect(res.ok).toBe(true);
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
      } finally {
        hub.quotaStore.setClock(undefined);
      }
    });
  });

  it("5h windows use a 5h cycle (not hardcoded 7d) and rolling windows bank nothing", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      const hostId = "tbm-ingest-host-3";
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:tbm-ingest-main:5h'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:tbm-ingest-main:5h'");
      hub.quotaStore.setClock(() => NOW);
      try {
        const captured = new Date(NOW - 1000).toISOString();
        const reset = new Date(NOW + 5 * HOUR).toISOString();
        const res = hub.quotaStore.putSnapshot(
          hostId,
          hostSnap(hostId, captured, {
            codex: {
              status: "ok",
              error: null,
              observed_at: captured,
              details: { limit_reached: false },
              windows: {
                "5h": {
                  percent_remaining: 60,
                  reset_at: reset,
                  present: true,
                  rolling: true,
                  unit: "tokens",
                  credits_remaining: 600_000,
                  spend_limit: 1_000_000,
                },
              },
            },
          }),
          NOW
        );
        expect(res.ok).toBe(true);
        const pool: any = sql
          .exec("SELECT * FROM tbm_quota_pools WHERE pool_id = ?", "codex:tbm-ingest-main:5h")
          .toArray()[0];
        expect(pool.cycle_ms).toBe(5 * HOUR);
        expect(pool.window_type).toBe("5h");
        expect(
          sql.exec("SELECT * FROM tbm_banked_tranches WHERE pool_id = ?", pool.pool_id).toArray()
        ).toHaveLength(0);
      } finally {
        hub.quotaStore.setClock(undefined);
      }
    });
  });

  it("rejected replay captures do not refill TBM pools", async () => {
    await runInDurableObject(stub, async (hub: Hub) => {
      const sql: any = hub.sql;
      ensureTbmQuotaSchema(sql);
      seedFleet(sql);
      const hostId = "tbm-ingest-host-4";
      sql.exec("DELETE FROM tbm_banked_tranches WHERE pool_id LIKE 'codex:%tbm-ingest%'");
      sql.exec("DELETE FROM tbm_quota_pools WHERE pool_id LIKE 'codex:%tbm-ingest%'");
      hub.quotaStore.setClock(() => NOW);
      try {
        const captured = new Date(NOW - 1000).toISOString();
        const reset = new Date(NOW + 7 * DAY).toISOString();
        const snap = hostSnap(hostId, captured, {
          codex: {
            status: "ok",
            error: null,
            observed_at: captured,
            details: { limit_reached: false },
            windows: {
              "7d": { percent_remaining: 70, reset_at: reset, present: true, rolling: false },
            },
          },
        });
        expect(hub.quotaStore.putSnapshot(hostId, snap, NOW).ok).toBe(true);
        const before: any = sql
          .exec(
            "SELECT last_updated_ms FROM tbm_quota_pools WHERE pool_id = ?",
            "codex:tbm-ingest-main:7d"
          )
          .toArray()[0];
        const replay = hub.quotaStore.putSnapshot(hostId, snap, NOW + 1000);
        expect(replay.ok).toBe(false);
        expect(replay.status).toBe(409);
        const after: any = sql
          .exec(
            "SELECT last_updated_ms FROM tbm_quota_pools WHERE pool_id = ?",
            "codex:tbm-ingest-main:7d"
          )
          .toArray()[0];
        expect(after.last_updated_ms).toBe(before.last_updated_ms);
      } finally {
        hub.quotaStore.setClock(undefined);
      }
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

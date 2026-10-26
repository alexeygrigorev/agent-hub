import { describe, expect, it } from "vitest";
import { env, runInDurableObject } from "cloudflare:test";
import type { Hub } from "../src/hub";
import type { Env } from "../src/env";
import { FleetService } from "../src/fleet";
import { resourceConfig } from "../src/resource-management";
import { freshCpu } from "./cpu-fixture";

const admin = { caller: { isAdmin: true, actor: "disk-test", kind: "human" }, scopes: ["admin"] };
const now = Date.now(), iso = () => new Date(now).toISOString();

async function fixture(fn: (f: FleetService, heartbeat: (v: any) => void) => Promise<void>) {
  const bindings = env as unknown as Env;
  await runInDurableObject(bindings.HUB.get(bindings.HUB.idFromName("disk-floor-" + crypto.randomUUID())), async (hub: Hub, state: DurableObjectState) => {
    const txn = <T>(cb: () => T) => state.storage.transactionSync(cb);
    const f = new FleetService(hub.sql, txn, () => now); f.init();
    const heartbeat = (extra: any) => hub.sql.exec("UPDATE machines SET last_heartbeat=? WHERE id=?", JSON.stringify({
      mem_avail_mb: 32000, disk_free_gb: 80, load: [1], cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() }, ...extra,
    }), "host");
    hub.sql.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES(?,?,?,?,?,?)",
      "host", "host", "host", iso(), iso(), JSON.stringify({ engines: ["codex"], fleet_modes: ["headless"], workspaces: ["demo"], cpu_count: 8 }));
    heartbeat({});
    hub.sql.exec("INSERT INTO host_quota_snapshots(host_id,schema_version,captured_at,captured_at_ms,received_at,received_at_ms,status,routes_json) VALUES(?,?,?,?,?,?,?,?)",
      "host", "1.0", iso(), now, iso(), now, "ok", JSON.stringify({ codex: { status: "ok", observed_at: iso(), details: { limit_reached: false }, windows: { weekly: { present: true, percent_remaining: 80, reset_at: new Date(now + 86400000).toISOString() } } } }));
    const configured = await f.handle(new Request("https://hub.test/api/fleet/accounts", { method: "PUT", body: JSON.stringify({
      accounts: [{ label: "disk-account", provider: "codex", max_concurrency: 5, reserve_percent: 15, enabled: true }],
      routes: [{ machine_id: "host", engine: "codex", account: "disk-account", profile: null }],
    }) }), admin as any);
    expect(configured.status).toBe(200);
    await fn(f, heartbeat);
  });
}
const plan = (f: FleetService) => f.plan({ workspace: "demo", mode: "headless", prompt: "noop" } as any) as any;

describe("root disk floor", () => {
  it("defaults to 15 GiB and accepts fractional config", () => {
    expect(resourceConfig().disk_floor_gb).toBe(15);
    expect(resourceConfig({ disk_floor_gb: 12.5 }).disk_floor_gb).toBe(12.5);
    expect(() => resourceConfig({ disk_floor_gb: 0 })).toThrow("invalid_resource_config");
  });
  it.each([50, 51, 16])("places at %i GiB free (above the default floor)", async (gb) => fixture(async (f, hb) => {
    hb({ disk_free_gb: gb });
    const p = plan(f);
    expect(p.rejected?.find((r: any) => r.reason === "disk_floor" || r.reason === "resource_limit")).toBeUndefined();
    expect(p.selected).toBeTruthy();
  }));
  it("rejects at 14 GiB with named disk_floor reason and numbers", async () => fixture(async (f, hb) => {
    hb({ disk_free_gb: 14 });
    const p = plan(f);
    expect(p.selected ?? null).toBeNull();
    expect(p.rejected).toContainEqual(expect.objectContaining({ reason: "disk_floor", disk_free_gb: 14, disk_floor_gb: 15 }));
  }));
  it("honours a configured floor", async () => fixture(async (f, hb) => {
    f.resources.configure({ disk_floor_gb: 52 });
    hb({ disk_free_gb: 51 });
    expect(plan(f).rejected).toContainEqual(expect.objectContaining({ reason: "disk_floor", disk_free_gb: 51, disk_floor_gb: 52 }));
  }));
});

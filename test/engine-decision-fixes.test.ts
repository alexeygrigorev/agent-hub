import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { applyNormalizedScores, formatScore } from '../src/pacing';
import { freshCpu } from './cpu-fixture';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const H = 3600_000;
const NOW = Date.parse('2026-11-02T17:15:00Z');
const iso = (d = 0) => new Date(NOW + d).toISOString();
const quota = (pct: number) => ({
  status: 'ok', error: null, observed_at: iso(), details: { limit_reached: false },
  windows: { '7d': { present: true, percent_remaining: pct, reset_at: iso(100.8 * H), rolling: false, error: null } },
});
const admin = { caller: { isAdmin: true, actor: 'human:admin', kind: 'human' }, scopes: ['admin'] } as any;

async function withFleet(name: string, fn: (fleet: FleetService, hub: Hub) => Promise<void> | void) {
  const bindings = env as unknown as Env;
  const stub = bindings.HUB.get(bindings.HUB.idFromName(name + crypto.randomUUID()));
  await runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    const fleet = new FleetService(hub.sql, cb => state.storage.transactionSync(cb), () => NOW);
    fleet.init();
    for (const machine of ['hetzner', 'laptop']) {
      hub.sql.exec(
        'INSERT INTO machines (id, name, hostname, registered_at, last_checkin_at, last_heartbeat, drained, capabilities) VALUES (?, ?, ?, ?, ?, ?, 0, ?)',
        machine, machine, machine.toUpperCase(), iso(), iso(),
        JSON.stringify({ mem_avail_mb: 32000, disk_free_gb: 80, cpu_usage: freshCpu(iso()), gate: { launch_allowed: true, at: iso() } }),
        JSON.stringify({ engines: ['claude', 'opencode'], fleet_modes: ['headless'], workspaces: ['agent-hub'], cpu_count: 12 }),
      );
    }
    const put = await fleet.handle(new Request('https://hub.test/api/fleet/accounts', {
      method: 'PUT',
      body: JSON.stringify({
        accounts: [
          { label: 'acct-a', provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true },
          { label: 'acct-b', provider: 'claude', max_concurrency: 8, reserve_percent: 0, enabled: true },
          { label: 'shared-go', provider: 'go', max_concurrency: 8, reserve_percent: 0, enabled: true },
        ],
        routes: ['hetzner', 'laptop'].flatMap(machine_id => [
          { machine_id, engine: 'claude', account: 'acct-a', profile: null },
          { machine_id, engine: 'claude', account: 'acct-b', profile: null },
          { machine_id, engine: 'opencode', account: 'shared-go', profile: 'hub-shared-go' },
        ]),
      }),
    }), admin);
    expect(put.status).toBe(200);
    for (const machine of ['hetzner', 'laptop']) {
      for (const [account, service] of [['acct-a', 'claude'], ['acct-b', 'claude'], ['shared-go', 'go']]) {
        hub.sql.exec('INSERT INTO fleet_subscriptions (machine_id,account,service,plan,observed_at,quota_json) VALUES (?,?,?,?,?,?)',
          machine, account, service, null, iso(), JSON.stringify(quota(60)));
      }
    }
    await fn(fleet, hub);
  });
}

let seq = 0;
function launch(hub: Hub, o: { machine: string; account: string; state: string; exit_reason?: string | null; ageMs?: number }) {
  const at = iso(-(o.ageMs ?? 60_000));
  const id = 'l' + (++seq);
  hub.sql.exec(
    `INSERT INTO fleet_launches (id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,exit_reason,created_at,updated_at)
     VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)`,
    id, 'a', 'agent', 's', 'k' + id, '{}', o.machine, 'claude', o.account, 'headless', 'agent-hub', 'p', o.state, o.exit_reason ?? null, at, at);
}

const find = (p: any, machine: string, account: string) =>
  p.candidates.find((c: any) => c.machine === machine && c.account === account);
const plan = (fleet: FleetService) => fleet.plan({ mode: 'headless', workspace: 'agent-hub', prompt: 'x' }) as any;

describe('recent failure factor is per account and machine (intake 83d27f69 live check)', () => {
  it('counts only recent, blamable failures of that exact route', async () => {
    await withFleet('fail-scope-', (fleet, hub) => {
      for (let i = 0; i < 3; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'engine_auth_expired' });
      launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'engine_auth_expired', ageMs: 2 * H }); // outside window
      for (let i = 0; i < 5; i++) launch(hub, { machine: 'hetzner', account: 'acct-b', state: 'failed', exit_reason: 'launcher_offer_expired' }); // not the account's fault
      launch(hub, { machine: 'laptop', account: 'acct-b', state: 'failed', exit_reason: 'engine_auth_expired' });
      const p = plan(fleet);
      expect(find(p, 'hetzner', 'acct-a').decision.factors.recent_failures).toBe(3);
      expect(find(p, 'laptop', 'acct-a').decision.factors.recent_failures).toBe(0);
      expect(find(p, 'hetzner', 'acct-b').decision.factors.recent_failures).toBe(0);
      expect(find(p, 'laptop', 'acct-b').decision.factors.recent_failures).toBe(1);
      expect(find(p, 'hetzner', 'acct-a').decision.factors.failure_factor).toBeCloseTo(1 / 2.5, 3);
      expect(find(p, 'hetzner', 'acct-a').decision_reason).toContain('fail=3(0.40x)');
    });
  });
});

describe('host factor counts launches in flight, not running sessions', () => {
  it('many running sessions do not crush the host score; starting launches do', async () => {
    await withFleet('host-factor-', (fleet, hub) => {
      for (let i = 0; i < 30; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'running' });
      let p = plan(fleet);
      const busy = find(p, 'hetzner', 'shared-go').decision.factors;
      const idle = find(p, 'laptop', 'shared-go').decision.factors;
      expect(busy.host_pending_launches).toBe(0);
      expect(busy.host_running_sessions).toBe(30);
      // bounded: 30 running sessions cost at most the 0.5 cap, not a 1/31 crush
      expect(busy.host_factor).toBeLessThan(idle.host_factor);
      expect(busy.host_factor).toBeGreaterThan(idle.host_factor * 0.6);
      launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'starting' });
      p = plan(fleet);
      const starting = find(p, 'hetzner', 'shared-go');
      expect(starting.decision.factors.host_pending_launches).toBe(1);
      expect(starting.decision.factors.host_factor).toBeLessThan(idle.host_factor * 0.6);
      expect(starting.decision_reason).toMatch(/host=cpu:[0-9.]+,pend:1,run:30\([0-9.]+x\)/);
    });
  });
});

describe('normalized score', () => {
  it('scales the best candidate to 100, keeps order, and shows norm= in every reason', async () => {
    await withFleet('norm-', (fleet, hub) => {
      for (let i = 0; i < 6; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'starting' });
      const p = plan(fleet);
      const scores = p.candidates.map((c: any) => c.decision.normalized_score);
      expect(scores[0]).toBe(100);
      expect(Math.min(...scores)).toBeGreaterThan(0);
      for (let i = 1; i < p.candidates.length; i++) expect(p.candidates[i].decision.score).toBeLessThanOrEqual(p.candidates[i - 1].decision.score);
      for (const c of p.candidates) {
        expect(c.decision_reason).toContain(`norm=${c.decision.normalized_score}/100`);
        expect(c.selection.decision_reason).toBe(c.decision_reason);
      }
      expect(p.selected.decision_reason).toContain('norm=100/100');
    });
  });

  it('pure helpers: relative scaling, tiny scores stay readable', () => {
    const mk = (score: number) => {
      const reason = `auto: x score=${formatScore(score)} [rem=1%]`;
      return { score, decision: { score, reason } as any, decision_reason: reason };
    };
    const list = [mk(8.4e-5), mk(7.4e-4), mk(0)];
    applyNormalizedScores(list);
    expect(list.map(c => c.decision.normalized_score)).toEqual([11.4, 100, 0]);
    expect(formatScore(8.4e-5)).toBe('8.40e-5');
    expect(formatScore(0.4204)).toBe('0.4204');
    applyNormalizedScores(list);
    expect(list[0].decision_reason.match(/norm=/g)!.length).toBe(1);
  });
});

describe('recommend endpoint', () => {
  it('previews every host, exposes normalized_score and accepts --repo', async () => {
    await withFleet('recommend-', async (fleet) => {
      const get = (qs: string) => fleet.handle(new Request('https://hub.test/api/engines/recommend' + qs), admin);
      const res = await get('?tier=daily');
      expect(res.status).toBe(200);
      const body: any = await res.json();
      expect(body.normalized_score).toBe(100);
      expect(new Set(body.candidates.map((c: any) => c.machine))).toEqual(new Set(['hetzner', 'laptop']));
      expect((await get('?repo=not-a-repo')).status).toBe(400);
      const repo = await get('?repo=DataTalksClub/agent-hub');
      expect(repo.status).toBe(200);
    });
  });
});

describe('route circuit breaker (intake 0e1b61c9)', () => {
  const rejectedFor = (p: any, machine: string) =>
    (p.rejected ?? []).some((r: any) => r.machine === machine && r.reason === 'route_quarantined');

  it('skips a route whose last 3 launches in 10 minutes all died at the launcher, and only that route', async () => {
    await withFleet('breaker-', (fleet, hub) => {
      for (let i = 0; i < 3; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'launcher_command_failed' });
      for (let i = 0; i < 2; i++) launch(hub, { machine: 'laptop', account: 'acct-a', state: 'failed', exit_reason: 'launcher_command_failed' });
      const p = plan(fleet);
      expect(find(p, 'hetzner', 'acct-a')).toBeUndefined();
      expect(find(p, 'laptop', 'acct-a')).toBeDefined(); // only 2 failures
      expect(find(p, 'hetzner', 'acct-b')).toBeDefined();
      expect(rejectedFor(p, 'hetzner')).toBe(true);
    });
  });

  it('re-opens after the window, and a later success resets it', async () => {
    await withFleet('breaker-reopen-', (fleet, hub) => {
      for (let i = 0; i < 3; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'launcher_command_failed', ageMs: 11 * 60_000 });
      expect(find(plan(fleet), 'hetzner', 'acct-a')).toBeDefined();
    });
    await withFleet('breaker-reset-', (fleet, hub) => {
      for (let i = 0; i < 3; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'launcher_command_failed', ageMs: 5 * 60_000 });
      launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'running', ageMs: 1000 });
      expect(find(plan(fleet), 'hetzner', 'acct-a')).toBeDefined();
    });
  });

  it('an operator pinning account and machine can still probe a quarantined route', async () => {
    await withFleet('breaker-pin-', (fleet, hub) => {
      for (let i = 0; i < 3; i++) launch(hub, { machine: 'hetzner', account: 'acct-a', state: 'failed', exit_reason: 'launcher_command_failed' });
      const p = fleet.plan({ mode: 'headless', workspace: 'agent-hub', prompt: 'x', account: 'acct-a', machine: 'hetzner' }) as any;
      expect(find(p, 'hetzner', 'acct-a')).toBeDefined();
    });
  });
});

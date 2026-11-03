/**
 * Intake 36c4cf38 (blocker of ccc50cca): a successor session of the SAME
 * intake is admitted to a worktree whose lock owner is terminal (stopped /
 * failed / cancelled / refused / finished with no live native process),
 * while a live owner still blocks.
 *
 * Re-land of the reverted 90336bb5 with the incident fix: the unplaced-owner
 * link matches the small indexed (machine_id, created_at) pair and compares
 * request_json in JS over a bounded candidate set — never as correlated
 * request_json text equality in SQL (that degraded the prod DO, intake note
 * 1c518e40). The rows-read budget test below pins that at production scale.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService, WORKTREE_OWNER_CONTENDERS_SQL } from '../src/fleet';
import { SessionControlService } from '../src/session_control';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';

const HOST = 'hetzner';
let seq = 0;

interface Ctx { stub: DurableObjectStub; hub: Hub; state: DurableObjectState }

async function fixture<T>(fn: (c: Ctx) => Promise<T>): Promise<T> {
  const b = env as unknown as Env;
  const stub = b.HUB.get(b.HUB.idFromName('36c4cf38-' + ++seq + '-' + Date.now()));
  return runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    await hub.fetch(new Request('https://hub/api/health'));
    return fn({ stub, hub, state });
  });
}

function seedLaunch(c: Ctx, request: Record<string, unknown>, opts: {
  machine?: string | null; launchMachine?: string; state?: string; launchState?: string; at?: string;
  binding?: Record<string, unknown> | null; nativePhase?: string | null; nativeId?: string | null;
  verificationStatus?: string | null; fleetLaunchId?: string | null;
} = {}) {
  const launchId = opts.fleetLaunchId === null ? null : (opts.fleetLaunchId ?? crypto.randomUUID());
  const sessionId = crypto.randomUUID();
  const at = opts.at ?? new Date().toISOString();
  const sessionMachine = opts.machine === undefined ? HOST : opts.machine;
  const launchMachine = opts.launchMachine ?? (sessionMachine ?? '');
  const state = opts.state ?? 'offered';
  const launchState = opts.launchState ?? 'offered';
  const requestJson = JSON.stringify(request);
  const bindingJson = opts.binding ? JSON.stringify(opts.binding) : null;
  const nativeId = opts.nativeId ?? (opts.binding?.native_id as string | undefined) ?? null;
  if (launchId !== null) {
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, native_id, verification_status, created_at, updated_at)
      VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude', 'acct', 'p', 'headless', 'demo', '', ?, ?, ?, ?, ?)`,
      launchId, 'key-' + launchId, requestJson, launchMachine, launchState, nativeId, opts.verificationStatus ?? null, at, at);
  }
  c.hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, binding_json, created_at, updated_at)
    VALUES (?, 'human:operator', ?, ?, ?, ?, 'claude', 'headless', ?, ?, ?, ?)`,
    sessionId, 'skey-' + sessionId, requestJson, launchId, sessionMachine, state, bindingJson, at, at);
  if (opts.nativePhase && opts.binding && sessionMachine) {
    c.hub.sql.exec(`INSERT OR REPLACE INTO fleet_sessions (machine_id, native_id, tag, engine, phase, agent_state, observed_at, workspace, cwd, created_at_ms)
      VALUES (?, ?, ?, 'claude', ?, 'idle', ?, ?, ?, ?)`,
      sessionMachine, opts.binding.native_id, opts.binding.tag, opts.nativePhase, at,
      opts.binding.workspace, opts.binding.workspace, opts.binding.native_created_at_ms);
  }
  return { launchId: launchId ?? '', sessionId, at, requestJson };
}

async function hostSessionControl(c: Ctx, path: string, machine = HOST) {
  const sc = new SessionControlService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
  sc.init();
  const res = await sc.handle(new Request('https://hub.test' + path, { method: 'GET' }),
    { caller: { kind: 'host', actor: 'machine:' + machine, machineId: machine, isAdmin: true }, scopes: ['host'] } as any);
  return { status: res.status, body: await res.json() as any };
}

describe('worktree-owner terminal attestation and exclusive successor ownership (36c4cf38 & ccc50cca)', () => {
  it('links an unplaced failed session to the host that ran an earlier attempt and exposes it in worktree-guards', async () => fixture(async c => {
    const service = new FleetService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
    service.init();
    const intakeId = crypto.randomUUID();
    const owner = seedLaunch(c, { intake_task_id: intakeId }, { machine: null, state: 'failed', launchState: 'failed' });
    const read = async (machine: string, requester?: string) => service.handle(
      new Request('https://hub.test/api/fleet/worktree-owner/' + owner.sessionId + (requester ? '?requester=' + requester : '')),
      { caller: { kind: 'host', actor: machine, machineId: machine }, scopes: ['host'] } as any);

    expect((await read(HOST)).status).toBe(404);
    // The earlier attempt shares request_json + created_at with the requeued
    // launch (a real retry preserves both); its request_key was mutated by
    // the retry bookkeeping, so the link must NOT depend on request_key.
    const earlier = crypto.randomUUID();
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
      'acct', 'p', 'headless', 'demo', '', 'exited', ?, ?)`, earlier, 'memory-retry-' + earlier, owner.requestJson, HOST, owner.at, owner.at);
    expect(await (await read(HOST)).json()).toEqual({ session_id: owner.sessionId, intake_id: intakeId, terminal: true });
    expect((await read('win35')).status).toBe(404);

    // worktree-guards exposes the requeued owner (explicit session_id read)
    // with its host launch tag so fallback paths work.
    const guards = await hostSessionControl(c, '/api/sessions/worktree-guards?session_id=' + owner.sessionId);
    expect(guards.status).toBe(200);
    const guardFact = guards.body.sessions.find((s: any) => s.session_id === owner.sessionId);
    expect(guardFact).toMatchObject({
      session_id: owner.sessionId,
      intake_task_id: intakeId,
      terminal: true,
      native_terminal: true,
      tag: 'task-hub-' + earlier,
    });

    // If the earlier attempt on HOST had spawned a native process, it is not unstarted.
    c.hub.sql.exec("UPDATE fleet_launches SET native_id='spawned-native' WHERE id=?", earlier);
    expect((await (await read(HOST)).json() as any).terminal).toBe(false);
    // Even if a newer same-request launch row on HOST has native_id=NULL,
    // worktree-guards must check all matching launches.
    const newer = crypto.randomUUID();
    const newerUpdated = new Date(Date.now() + 5000).toISOString();
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
      'acct', 'p', 'headless', 'demo', '', 'exited', NULL, ?, ?)`, newer, 'memory-retry-' + newer, owner.requestJson, HOST, owner.at, newerUpdated);
    expect((await (await read(HOST)).json() as any).terminal).toBe(false);
    const guardsWithSpawned = await hostSessionControl(c, '/api/sessions/worktree-guards?session_id=' + owner.sessionId);
    const guardWithSpawned = guardsWithSpawned.body.sessions.find((s: any) => s.session_id === owner.sessionId);
    expect(guardWithSpawned).toMatchObject({
      session_id: owner.sessionId,
      terminal: true,
      native_terminal: false,
      tag: 'task-hub-' + newer,
    });
    c.hub.sql.exec("DELETE FROM fleet_launches WHERE id=?", newer);
    c.hub.sql.exec("UPDATE fleet_launches SET native_id=NULL WHERE id=?", earlier);

    // A requeued session that is still waiting is not terminal.
    c.hub.sql.exec("UPDATE session_control_sessions SET state='queued' WHERE id=?", owner.sessionId);
    c.hub.sql.exec("UPDATE fleet_launches SET state='queued' WHERE id=?", owner.launchId);
    expect((await (await read(HOST)).json() as any).terminal).toBe(false);
  }));

  it('attests stopped canonical sessions across stop command acks, verified launch exits, and retired imported sessions (36c4cf38)', async () => fixture(async c => {
    const service = new FleetService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
    service.init();
    const intakeId = crypto.randomUUID();
    const read = async (sessionId: string, requester?: string) => (await service.handle(
      new Request('https://hub.test/api/fleet/worktree-owner/' + sessionId + (requester ? '?requester=' + requester : '')),
      { caller: { kind: 'host', actor: HOST, machineId: HOST }, scopes: ['host'] } as any)).json() as Promise<any>;

    const binding = {
      native_id: crypto.randomUUID(),
      tag: 'task-hub-stopped-1',
      workspace: '/home/alexey/git/demo-wt-12345678',
      native_created_at_ms: 1700000000000,
      generation: 1,
    };
    // 1. Bound session stopped while native is still 'running' and without confirmed stop -> false.
    const s1 = seedLaunch(c, { intake_task_id: intakeId }, {
      state: 'stopped', launchState: 'running', binding, nativePhase: 'running',
    });
    expect((await read(s1.sessionId)).terminal).toBe(false);

    // 2. Stop command acked with confirmed_exit=true -> terminal=true even if launch_state was 'running' and native row was pruned.
    c.hub.sql.exec('DELETE FROM fleet_sessions WHERE machine_id=? AND native_id=?', HOST, binding.native_id);
    c.hub.sql.exec(`INSERT INTO session_control_commands (id, session_id, kind, state, actor, request_key, payload_json, generation, correlation, ack_json, created_at, updated_at)
      VALUES (?, ?, 'stop', 'acked', 'human:operator', ?, '{}', 1, 'corr-1', ?, ?, ?)`,
      crypto.randomUUID(), s1.sessionId, 'stop-' + s1.sessionId,
      JSON.stringify({ outcome: 'delivered', confirmed_exit: true, native: { native_id: binding.native_id, tag: binding.tag, workspace: binding.workspace } }),
      s1.at, s1.at);
    expect((await read(s1.sessionId)).terminal).toBe(true);

    // 3. Imported/observed session (fleet_launch_id IS NULL) retired with retirement marker -> terminal=true.
    const importedId = crypto.randomUUID();
    const importedBinding = {
      native_id: importedId,
      tag: 'manual-worker',
      workspace: '/home/alexey/git/demo-wt-12345678',
      native_created_at_ms: 1700000005000,
      generation: 1,
      provenance: 'observed_native',
      retirement: {
        native_id: importedId,
        tag: 'manual-worker',
        workspace: '/home/alexey/git/demo-wt-12345678',
        created_at_ms: 1700000005000,
        engine: 'claude',
        generation: 1,
        cause: 'operator_stop',
        finished_at_ms: 1700000009000,
      },
    };
    const s2 = seedLaunch(c, { intake_task_id: intakeId }, {
      fleetLaunchId: null, state: 'stopped', binding: importedBinding,
    });
    expect((await read(s2.sessionId)).terminal).toBe(true);

    // 4. Unbound uncertain session stopped via POST /api/sessions/:id/stop (launch_state='exited', native_id=NULL) -> terminal=true.
    const s3 = seedLaunch(c, { intake_task_id: intakeId }, {
      state: 'stopped', launchState: 'exited', binding: null,
    });
    expect((await read(s3.sessionId)).terminal).toBe(true);
  }));

  it('enforces requester-bound exclusivity and counts explicit-cwd review sessions as contenders (Review-2 P1)', async () => fixture(async c => {
    const service = new FleetService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
    service.init();
    const intakeId = crypto.randomUUID();
    const wtPath = '/home/alexey/git/demo-wt-' + intakeId.slice(0, 8);
    const binding = {
      native_id: crypto.randomUUID(),
      tag: 'task-hub-owner',
      workspace: wtPath,
      native_created_at_ms: 1700000000000,
      generation: 1,
    };
    const owner = seedLaunch(c, { intake_task_id: intakeId }, {
      state: 'stopped', launchState: 'exited', binding, nativePhase: 'exited', verificationStatus: 'verified',
    });
    const read = async (requester?: string) => (await service.handle(
      new Request('https://hub.test/api/fleet/worktree-owner/' + owner.sessionId + (requester !== undefined ? '?requester=' + requester : '')),
      { caller: { kind: 'host', actor: HOST, machineId: HOST }, scopes: ['host'] } as any)).json() as Promise<any>;

    // Single ordinary successor B is admitted when bound via ?requester=B.
    const succB = seedLaunch(c, { intake_task_id: intakeId, workspace: wtPath }, { state: 'offered', launchState: 'offered' });
    expect((await read(succB.sessionId)).terminal).toBe(true);
    // Calling without ?requester= when B is already a live contender returns false.
    expect((await read()).terminal).toBe(false);
    // Unknown or self requester returns false.
    expect((await read(crypto.randomUUID())).terminal).toBe(false);
    expect((await read(owner.sessionId)).terminal).toBe(false);

    // A read-only review session WITHOUT explicit cwd runs in the main checkout and does not block B.
    const reviewReadOnly = seedLaunch(c, { intake_task_id: intakeId, task_kind: 'review', workspace: 'demo' }, { state: 'running', launchState: 'running' });
    expect((await read(succB.sessionId)).terminal).toBe(true);

    // Review session C WITH explicit cwd inside the checkout IS a contender:
    // both B (ordinary) and C (explicit-cwd review) are refused when both are non-terminal.
    const reviewExplicitCwd = seedLaunch(c, { intake_task_id: intakeId, task_kind: 'review', workspace: 'demo', cwd: wtPath }, { state: 'offered', launchState: 'offered' });
    expect((await read(succB.sessionId)).terminal).toBe(false);
    expect((await read(reviewExplicitCwd.sessionId)).terminal).toBe(false);

    // If B fails/stops, then C (now the sole worktree contender) is admitted.
    c.hub.sql.exec("UPDATE session_control_sessions SET state='failed' WHERE id=?", succB.sessionId);
    expect((await read(reviewExplicitCwd.sessionId)).terminal).toBe(true);

    // If C is running and another ordinary successor D is offered, D is refused while C runs.
    c.hub.sql.exec("UPDATE session_control_sessions SET state='running' WHERE id=?", reviewExplicitCwd.sessionId);
    const succD = seedLaunch(c, { intake_task_id: intakeId, workspace: wtPath }, { state: 'offered', launchState: 'offered' });
    expect((await read(succD.sessionId)).terminal).toBe(false);
    expect((await read(reviewExplicitCwd.sessionId)).terminal).toBe(false);

    // Clean up reviewReadOnly reference check: stopping C admits D even while read-only review runs.
    c.hub.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", reviewExplicitCwd.sessionId);
    expect(reviewReadOnly.sessionId).toBeTruthy();
    expect((await read(succD.sessionId)).terminal).toBe(true);
  }));
});

describe('worktree-owner/guards rows-read budget at production scale (36c4cf38 incident)', () => {
  it('reads bounded rows per call with 5k launches and 500 sessions', async () => fixture(async c => {
    const service = new FleetService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
    service.init();
    const hostCaller = { caller: { kind: 'host', actor: HOST, machineId: HOST }, scopes: ['host'] } as any;
    const base = Date.now();
    const iso = (ms: number) => new Date(ms).toISOString();
    const intakeId = crypto.randomUUID();
    // Production-shaped history: 10 launches per session, large request_json.
    const bigRequest = JSON.stringify({ intake_task_id: intakeId, workspace: 'demo', prompt: 'x'.repeat(4000) });
    for (let i = 0; i < 500; i++) {
      const sid = crypto.randomUUID();
      const state = i % 2 ? 'stopped' : 'failed';
      c.hub.sql.exec(`INSERT INTO session_control_sessions (id, actor, request_key, request_json, fleet_launch_id, machine_id, engine, mode, state, created_at, updated_at)
        VALUES (?, 'human:operator', ?, ?, NULL, ?, 'claude', 'headless', ?, ?, ?)`,
        sid, 'seed-' + i, bigRequest, i % 3 ? HOST : 'other-host-' + (i % 7), state, iso(base - i * 1000), iso(base - i * 500));
      for (let k = 0; k < 10; k++) {
        c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
          mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'seed', ?, ?, ?, 'claude', 'acct',
          'headless', 'demo', '', 'exited', ?, ?)`,
          crypto.randomUUID(), 'seed-key-' + i + '-' + k, bigRequest, k % 2 ? HOST : 'other-host-' + (k % 5), iso(base - (i * 10 + k) * 1000), iso(base - i * 500));
      }
    }
    // The unplaced lock owner: machine NULL, launch failed, earlier same-request attempt on HOST.
    const owner = seedLaunch(c, { intake_task_id: intakeId }, { machine: null, state: 'failed', launchState: 'failed' });
    const earlier = crypto.randomUUID();
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
      'acct', 'p', 'headless', 'demo', '', 'exited', ?, ?)`, earlier, 'memory-retry-' + earlier, owner.requestJson, HOST, owner.at, owner.at);

    const reads = (sql: string, ...args: any[]) => {
      const cursor: any = c.hub.sql.exec(sql, ...args);
      cursor.toArray();
      return Number(cursor.rowsRead ?? 0);
    };
    // EXPLAIN pins the indexed link: no full-table request_json comparison.
    const plan = (c.hub.sql.exec('EXPLAIN QUERY PLAN SELECT id, native_id, request_json, updated_at FROM fleet_launches WHERE machine_id=? AND created_at=? LIMIT 25',
      HOST, owner.at).toArray() as any[]).map(r => String(r.detail)).join('\n');
    expect(plan).toMatch(/USING INDEX|USING COVERING INDEX/i);
    expect(plan).not.toMatch(/SCAN fleet_launches/i);
    // The link itself reads a handful of rows, not the 5k-row history.
    expect(reads('SELECT id, native_id, request_json, updated_at FROM fleet_launches WHERE machine_id=? AND created_at=? LIMIT 25',
      HOST, owner.at)).toBeLessThanOrEqual(40);
    // The exclusivity read is bounded too: many live same-intake sessions on
    // the host cannot turn worktree-owner into a history scan.
    for (let i = 0; i < 30; i++) {
      seedLaunch(c, { intake_task_id: intakeId, workspace: 'demo' }, { state: 'running', launchState: 'running' });
    }
    const contenderPlan = (c.hub.sql.exec('EXPLAIN QUERY PLAN ' + WORKTREE_OWNER_CONTENDERS_SQL,
      HOST, owner.sessionId, null, null, intakeId).toArray() as any[]).map(r => String(r.detail)).join('\n');
    expect(contenderPlan).not.toMatch(/SCAN session_control_sessions/i);
    expect(reads(WORKTREE_OWNER_CONTENDERS_SQL, HOST, owner.sessionId, null, null, intakeId)).toBeLessThanOrEqual(60);

    const ownerOf = (id: string) => service.handle(
      new Request('https://hub.test/api/fleet/worktree-owner/' + id), hostCaller);
    const placed = seedLaunch(c, { intake_task_id: intakeId }, { state: 'stopped', launchState: 'exited', binding: null });
    const t0 = Date.now();
    expect(((await (await ownerOf(owner.sessionId)).json()) as any).terminal).toBe(true);
    expect(((await (await ownerOf(placed.sessionId)).json()) as any).terminal).toBe(true);
    const guards = await hostSessionControl(c, '/api/sessions/worktree-guards?session_id=' + owner.sessionId + ',' + placed.sessionId);
    expect(guards.status).toBe(200);
    expect(guards.body.sessions).toHaveLength(2);
    // Milliseconds, not seconds: every lookup is a PK or indexed range read.
    expect(Date.now() - t0).toBeLessThan(5000);
  }));
});

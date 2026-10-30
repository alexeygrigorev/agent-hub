import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { FleetService } from '../src/fleet';
import { IntakeVerifier } from '../src/intake-verifier';
import { SessionControlService } from '../src/session_control';
import type { Env } from '../src/env';
import type { Hub } from '../src/hub';

const HOST = 'hetzner';
let seq = 0;

interface Ctx { stub: DurableObjectStub; hub: Hub; state: DurableObjectState }

async function fixture<T>(fn: (c: Ctx) => Promise<T>): Promise<T> {
  const b = env as unknown as Env;
  const stub = b.HUB.get(b.HUB.idFromName('ccc50cca-' + ++seq + '-' + Date.now()));
  return runInDurableObject(stub, async (hub: Hub, state: DurableObjectState) => {
    await hub.fetch(new Request('https://hub/api/health'));
    return fn({ stub, hub, state });
  });
}

const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;

async function intake(c: Ctx, path: string, payload?: unknown) {
  const res = await c.hub.intakeService.handle(new Request('https://hub.test/api/intake' + path, payload !== undefined
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), async () => admin);
  return { status: res.status, body: await res.json() as any };
}

async function createTask(c: Ctx, extra: Record<string, unknown> = {}) {
  const r = await intake(c, '/tasks', { key: 'ccc-task-' + ++seq, title: 'Fix ' + seq, project: 'demo',
    text: 'synthetic', source: { type: 'unknown', reference: null, timestamp: null },
    acceptance_criteria: 'Synthetic AC: worktree merged and removed before close.', ...extra });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}

async function hostWorktree(c: Ctx, path: string, payload?: unknown, machine = HOST) {
  const res = await c.hub.intakeService.handleWorktreeHost(new Request('https://hub.test/api/worktree-host' + path, payload !== undefined
    ? { method: 'POST', body: JSON.stringify(payload) } : { method: 'GET' }), machine, 'machine:' + machine);
  return { status: res.status, body: await res.json() as any };
}

async function hostSessionControl(c: Ctx, path: string, machine = HOST) {
  const sc = new SessionControlService(c.hub.sql, cb => c.state.storage.transactionSync(cb));
  sc.init();
  const res = await sc.handle(new Request('https://hub.test' + path, { method: 'GET' }),
    { caller: { kind: 'host', actor: 'machine:' + machine, machineId: machine, isAdmin: true }, scopes: ['host'] } as any);
  return { status: res.status, body: await res.json() as any };
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

async function plan(c: Ctx, launchId: string, machine = HOST) {
  const r = await hostWorktree(c, '/plan', { launch_id: launchId }, machine);
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.plan;
}

describe('review sessions do not collide with the implementer intake worktree (ccc50cca)', () => {
  it('plans mode none (read_only) for task_kind review without recording or resuming the intake worktree', async () => fixture(async c => {
    const t = await createTask(c);
    const impl = seedLaunch(c, { intake_task_id: t.id, workspace: 'demo' });
    const suffix = 'wt-' + t.id.replace(/[^0-9a-f]/gi, '').slice(0, 8);
    const path = '/home/alexey/git/demo-' + suffix;
    expect(await plan(c, impl.launchId)).toEqual({ mode: 'create', task_id: t.id, suffix });
    expect((await hostWorktree(c, '/observe', { launch_id: impl.launchId, action: 'created', path, branch: 'demo-' + suffix })).status).toBe(200);

    const review = seedLaunch(c, { intake_task_id: t.id, task_kind: 'review', workspace: 'demo' });
    expect(await plan(c, review.launchId)).toEqual({ mode: 'none', task_id: t.id, read_only: true });
    const named = seedLaunch(c, { intake_task_id: t.id, task_kind: 'review', workspace: path });
    expect(await plan(c, named.launchId)).toEqual({ mode: 'none', task_id: t.id, read_only: true });
    const rows = (await intake(c, '/tasks/' + t.id + '/worktrees')).body.worktrees;
    expect(rows).toHaveLength(1);
    expect(rows[0].session_ids).not.toContain(review.sessionId);
    expect(rows[0].session_ids).not.toContain(named.sessionId);
  }));

  it('never starts the verifier inside an implementer worktree or nested subdirectory', async () => {
    for (const suffixPath of ['', '/sub/dir']) {
      await fixture(async c => {
        const t = await createTask(c, { verification: 'required' });
        const at = new Date(Date.now() - 120000).toISOString();
        c.hub.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
          VALUES(?,'fixture',?,?,'codex','interactive','running',?,?)`, 'impl-' + t.id, 'impl-' + t.id,
          JSON.stringify({ intake_task_id: t.id, workspace: '/home/alexey/git/demo-wt-' + t.id.slice(0, 8) + suffixPath }), at, at);
        const starts: any[] = [];
        const v = new IntakeVerifier(c.hub.sql, c.hub.intakeService, (f: any) => f(), {
          start: async (input: any) => { starts.push(input); return { status: 202, body: { id: 'verifier-' + starts.length, state: 'queued' } }; },
          write: (path: string, payload: any) => intake(c, path, payload),
          alert: async () => {},
        }, () => Date.now());
        v.init();
        const task = (await intake(c, '/tasks/' + t.id)).body.task;
        const noted = await intake(c, '/tasks/' + t.id + '/events', { key: 'ccc-note-' + ++seq, expected_version: task.version,
          note: { text: 'DONE\nevidence: demo@0123456789abcdef pushed\nlive check: curl https://example.test/health -> 200\nresult: AC1 proven' } });
        expect(noted.status, JSON.stringify(noted.body)).toBe(200);
        c.hub.sql.exec('UPDATE intake_verifier_meta SET last_tick=NULL WHERE id=1');
        const result = await v.tick();
        expect(starts, JSON.stringify(result)).toHaveLength(1);
        expect(starts[0]).toMatchObject({ task_kind: 'review', intake_task_id: t.id });
        expect(starts[0].workspace).not.toMatch(/-wt-/);
      });
    }
  });
});

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
    const earlier = crypto.randomUUID();
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
      'acct', 'p', 'headless', 'demo', '', 'exited', ?, ?)`, earlier, 'memory-retry-' + earlier, owner.requestJson, HOST, owner.at, owner.at);
    expect(await (await read(HOST)).json()).toEqual({ session_id: owner.sessionId, intake_id: intakeId, terminal: true });
    expect((await read('win35')).status).toBe(404);

    // worktree-guards also includes the requeued owner with its host launch tag so fallback paths work.
    const guards = await hostSessionControl(c, '/api/sessions/worktree-guards');
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
    // Even if a newer same-key launch row on HOST has native_id=NULL, worktree-guards must check all matching launches.
    const newer = crypto.randomUUID();
    const newerUpdated = new Date(Date.now() + 5000).toISOString();
    c.hub.sql.exec(`INSERT INTO fleet_launches (id, actor, requester_kind, summary, request_key, request_json, machine_id, engine, account,
      profile, mode, workspace, prompt, state, native_id, created_at, updated_at) VALUES (?, 'human:operator', 'human', 'fixture', ?, ?, ?, 'claude',
      'acct', 'p', 'headless', 'demo', '', 'exited', NULL, ?, ?)`, newer, 'memory-retry-' + newer, owner.requestJson, HOST, owner.at, newerUpdated);
    expect((await (await read(HOST)).json() as any).terminal).toBe(false);
    const guardsWithSpawned = await hostSessionControl(c, '/api/sessions/worktree-guards');
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

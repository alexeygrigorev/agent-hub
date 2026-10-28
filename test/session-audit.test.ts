import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionAuditService, makeHeuristicDecider, matchInputPrompt, AUDIT_INTERVAL, type AuditFeatures } from '../src/session-audit';
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const NOW = Date.parse('2026-10-28T12:00:00Z');
const HOUR = 3600000;
const iso = (ms: number) => new Date(ms).toISOString();
const T = 1800; // idle threshold seconds used everywhere below

function features(extra: Partial<AuditFeatures> = {}): AuditFeatures {
  return {
    session_id: 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', machine_id: 'm1', native_id: 'n1',
    intake: null, engine: 'codex', mode: 'headless', state: 'running', actor: 'agent:fixture-worker',
    bound: true, linked: true, nativePhase: 'running', protected_session: false,
    runtime_s: 10 * HOUR / 1000, idle_s: 2 * T, host_cpu: null, git: null,
    intake_state: null, screen_tail: null, screen_age_s: null, nudges: 0, prior: null,
    ...extra,
  };
}
const decide = (f: AuditFeatures) => makeHeuristicDecider(T).decide(f);

describe('idle audit heuristic rules (AC2)', () => {
  it('never acts when the operator protected the session', () => {
    expect(decide(features({ protected_session: true }))).toMatchObject({ rule: 'protected_session', action: 'observe' });
  });
  it('never acts on operator-started sessions (Alexey rule)', () => {
    const f = features({ actor: 'human:admin-token:e42978c858a7a6bd', git: { dirty: true, dirty_count: 5, unpushed: 2, checked_at: iso(NOW) }, screen_tail: 'Press enter to continue', screen_age_s: 60, nudges: 3 });
    expect(decide(f)).toMatchObject({ rule: 'operator_started', action: 'observe' });
  });
  it('never acts on an intake blocked on a human, whatever else it looks like', () => {
    const f = features({ intake: 't1', intake_state: 'blocked', git: { dirty: true, dirty_count: 5, unpushed: 2, checked_at: iso(NOW) }, nudges: 5, screen_tail: 'press enter to continue', screen_age_s: 60 });
    const d = decide(f);
    expect(d).toMatchObject({ rule: 'human_blocked_intake', action: 'observe' });
    expect(decide(features({ intake: 't1', intake_state: 'pending_approval' })).action).toBe('observe');
  });
  it('observes recent progress and unknown observations', () => {
    expect(decide(features({ idle_s: 600 }))).toMatchObject({ rule: 'recent_progress', action: 'observe' });
    expect(decide(features({ idle_s: null }))).toMatchObject({ rule: 'recent_progress', action: 'observe' });
  });
  it('observes non-running intents instead of auditing them for action', () => {
    expect(decide(features({ state: 'held', linked: false }))).toMatchObject({ rule: 'intent_not_running', action: 'observe' });
  });
  it('escalates unbound running sessions (no control channel)', () => {
    expect(decide(features({ bound: false, linked: false }))).toMatchObject({ rule: 'unlinked_idle_native', action: 'escalate' });
  });
  it('never acts on a stale unobserved census but stops a stale terminal binding', () => {
    expect(decide(features({ linked: false, nativePhase: 'unknown' }))).toMatchObject({ rule: 'observation_unknown', action: 'observe' });
    expect(decide(features({ linked: false, nativePhase: 'exited' }))).toMatchObject({ rule: 'stale_binding_stop', action: 'stop' });
  });
  it('stops a finished session whose intake closed, even with dirty files (tree stays)', () => {
    const d = decide(features({ intake: 't1', intake_state: 'done', git: { dirty: true, dirty_count: 2, unpushed: 0, checked_at: iso(NOW) } }));
    expect(d).toMatchObject({ rule: 'finished_intake_closed', action: 'stop' });
  });
  it('stops a committed-and-pushed session with no intake after two idle windows', () => {
    const clean = { dirty: false, dirty_count: 0, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ idle_s: 2 * T, git: clean })))
      .toMatchObject({ rule: 'finished_committed_idle', action: 'stop' });
    // One idle window alone never stops a clean session.
    expect(decide(features({ idle_s: T, git: clean, screen_tail: 'working', screen_age_s: 60 })).action)
      .not.toBe('stop');
  });
  it('waits for a pending stop instead of double-stopping', () => {
    expect(decide(features({ prior: { action: 'stop', outcome: null } }))).toMatchObject({ rule: 'stop_pending', action: 'observe' });
  });
  it('sends the matching keystroke for each waiting-for-input pattern', () => {
    expect(matchInputPrompt('Proceed? [y/n]')).toMatchObject({ rule: 'yes_no_prompt', send: 'y' });
    expect(matchInputPrompt('Do you want to allow this?')).toMatchObject({ rule: 'approval_prompt', send: 'y' });
    expect(matchInputPrompt('Update is available. Install?')).toMatchObject({ rule: 'update_prompt', send: 'y' });
    expect(matchInputPrompt('Press enter to continue')).toMatchObject({ rule: 'press_enter', send: 'continue' });
    expect(matchInputPrompt('nothing here')).toBe(null);
    const d = decide(features({ screen_tail: '1 + 1? Press enter to continue', screen_age_s: 60, git: { dirty: true, dirty_count: 1, unpushed: 0, checked_at: iso(NOW) } }));
    expect(d).toMatchObject({ rule: 'waiting_for_input:press_enter', action: 'keystroke', send_text: 'continue' });
    expect(decide(features({ screen_tail: 'Proceed? [y/n]', screen_age_s: 60 })).rule)
      .toBe('waiting_for_input:yes_no_prompt');
  });
  it('escalates after one unanswered nudge and stops after two (dead)', () => {
    const dirty = { dirty: true, dirty_count: 3, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ git: dirty, nudges: 1 }))).toMatchObject({ rule: 'stuck_escalate', action: 'escalate' });
    expect(decide(features({ git: dirty, nudges: 2 }))).toMatchObject({ rule: 'dead_after_nudge', action: 'stop' });
    expect(decide(features({ git: dirty, nudges: 0 }))).toMatchObject({ rule: 'stuck_uncommitted', action: 'nudge', send_text: 'continue, commit, report' });
  });
  it('captures when no fresh screen exists and waits while a capture resolves', () => {
    expect(decide(features({ screen_tail: null }))).toMatchObject({ rule: 'missing_screen', action: 'capture' });
    expect(decide(features({ screen_tail: 'old screen', screen_age_s: 3 * T }))).toMatchObject({ rule: 'missing_screen', action: 'capture' });
    expect(decide(features({ screen_tail: null, prior: { action: 'capture', outcome: null } }))).toMatchObject({ rule: 'capture_pending', action: 'observe' });
  });
  it('observes an idle session with fresh evidence and no matching pattern', () => {
    expect(decide(features({ screen_tail: 'agent working output', screen_age_s: 60 }))).toMatchObject({ rule: 'observe_idle', action: 'observe' });
  });
});

describe('idle audit service (AC1/AC3/AC4)', () => {
  async function fixture(fn: (h: any, ctx: Ctx) => Promise<any>) {
    return inDO(stub(), async (h: any) => {
      for (const table of ['session_audit_runs', 'session_audit_decisions', 'session_audit_meta', 'session_control_sessions',
        'session_control_commands', 'session_control_protections', 'fleet_sessions', 'fleet_catalog_seen',
        'machines', 'intake_worktrees', 'intake_task_projection']) {
        try { h.sql.exec('DELETE FROM ' + table); } catch { /* table may not exist in this fixture */ }
      }
      let now = NOW;
      const calls: any[] = [];
      const alerts: any[] = [];
      const svc = new SessionAuditService(h.sql, {
        request: async (path: string, init: any) => {
          calls.push({ path, ...JSON.parse(init.body) });
          return { status: 200, body: { command_id: 'cmd-' + calls.length } };
        },
        notify: async (kind: string, subject: string, detail: string | null) => { alerts.push({ kind, subject, detail }); },
      }, () => now, makeHeuristicDecider, undefined);
      svc.init();
      const ctx = {
        svc, calls, alerts,
        get now() { return now; },
        advance(ms: number) { now += ms; },
      };
      return fn(h, ctx);
    });
  }
  type Ctx = {
    svc: SessionAuditService; calls: any[]; alerts: any[];
    readonly now: number; advance(ms: number): void;
  };
  function machine(h: any, id = 'm1') {
    h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at) VALUES (?,?,?,?)', id, id, id + '.host', iso(NOW));
  }
  function native(h: any, opts: any = {}) {
    const machineId = opts.machine ?? 'm1', nativeId = opts.native ?? 'n1';
    h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,progress_json)
      VALUES (?,?,?,?,?,?,?,?)`, machineId, nativeId, opts.tag ?? 'worker', opts.engine ?? 'codex', opts.phase ?? 'running',
      iso((ctxNow(opts.now) ?? NOW)), opts.workspace ?? '/home/alexey/git/demo-wt-a',
      JSON.stringify({ last_progress_at: iso(opts.progress ?? NOW - HOUR) }));
    h.sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES (?,?)', machineId, iso(ctxNow(opts.now) ?? NOW));
  }
  const ctxNow = (v: any) => v;
  function session(h: any, opts: any = {}) {
    const id = opts.id ?? 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa';
    h.sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,machine_id,binding_json,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?)`, id, opts.actor ?? 'fixture', id,
      JSON.stringify({ intake_task_id: opts.intake ?? null, workspace: opts.workspace ?? '/home/alexey/git/demo-wt-a' }),
      opts.engine ?? 'codex', opts.mode ?? 'headless', opts.state ?? 'running', opts.machine ?? 'm1',
      opts.bound === false ? null : JSON.stringify({ native_id: opts.native ?? 'n1', tag: 'worker', workspace: opts.workspace ?? '/home/alexey/git/demo-wt-a', native_created_at_ms: NOW - 10 * HOUR }),
      iso(NOW - 10 * HOUR), iso(NOW - HOUR));
    return id;
  }
  function worktree(h: any, opts: any = {}) {
    h.sql.exec(`INSERT INTO intake_worktrees(id,task_id,machine,path,branch,created_at,updated_at,session_ids,origin,state,status_json,status_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, opts.id ?? 'wt1', opts.task ?? 't1', opts.machine ?? 'm1',
      opts.path ?? '/home/alexey/git/demo-wt-a', 'agent-hub-wt-a', NOW - 20 * HOUR, NOW - HOUR, '[]', 'hub_created', 'active',
      JSON.stringify({ dirty: opts.dirty ?? true, dirty_count: opts.dirty_count ?? 3, unpushed: opts.unpushed ?? 0, present: true, merged: false }),
      NOW - HOUR);
  }
  function intake(h: any, id: string, status: string) {
    h.sql.exec('INSERT INTO intake_task_projection(id,data,links) VALUES (?,?,?)', id, JSON.stringify({ id, status }), '[]');
  }
  function captureAck(h: any, sessionId: string, text: string, at = NOW - 60 * 1000) {
    h.sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,ack_json,created_at,updated_at)
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, 'cap-1', sessionId, 'capture', 'someone', 'cap-key', '{}', 'acked', 1, 'corr', JSON.stringify({ outcome: 'delivered', capture: text }), iso(at), iso(at));
  }

  it('runs on the 30-minute gate; manual runs bypass it', async () => fixture(async (h, ctx) => {
    machine(h);
    const first = await ctx.svc.tick('scheduled');
    expect(first.state).toBe('audited');
    expect(await ctx.svc.tick('scheduled')).toMatchObject({ state: 'already_audited' });
    ctx.advance(AUDIT_INTERVAL + 1);
    expect(await ctx.svc.tick('scheduled')).toMatchObject({ state: 'audited' });
    expect((await ctx.svc.tick('manual')).state).toBe('audited');
  }));

  it('nudges a stuck uncommitted session once, escalates, then stops it dead — verified next run', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    const sid = session(h);
    native(h);
    const run1 = await ctx.svc.tick('manual');
    expect(run1.counts).toMatchObject({ nudge: 1 });
    expect(ctx.calls[0]).toMatchObject({ path: `/api/sessions/${sid}/send`, body: 'continue, commit, report' });

    ctx.advance(40 * 60 * 1000);
    const run2 = await ctx.svc.tick('manual');
    expect(run2.counts).toMatchObject({ escalate: 1 });
    expect(ctx.alerts.some(a => a.kind === 'session_audit')).toBe(true);

    ctx.advance(40 * 60 * 1000);
    const run3 = await ctx.svc.tick('manual');
    expect(run3.counts).toMatchObject({ stop: 1 });
    expect(ctx.calls.at(-1).path).toBe(`/api/sessions/${sid}/stop`);

    ctx.advance(40 * 60 * 1000);
    const stopRow = ctx.svc.view(10).decisions.filter((r: any) => r.action === 'stop').at(-1);
    h.sql.exec("UPDATE session_control_commands SET state='acked', ack_json=? WHERE id=?", JSON.stringify({ outcome: 'delivered', confirmed_exit: true }), stopRow.action_key);
    h.sql.exec("UPDATE fleet_sessions SET phase='exited', progress_json=? WHERE native_id='n1'", JSON.stringify({ last_progress_at: iso(ctx.now) }));
    h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sid);
    h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES ('m1', ?)", iso(ctx.now));
    const run4 = await ctx.svc.tick('manual');
    expect(run4.counts.stop ?? 0).toBe(0);
    const resolved = ctx.svc.view(10).decisions.filter((r: any) => r.action === 'stop').at(-1);
    expect(resolved.outcome).toBe('stopped_verified');
  }));

  it('keystrokes a waiting-for-input prompt found in the newest acked screen capture', async () => fixture(async (h, ctx) => {
    machine(h);
    const sid = session(h, { intake: null });
    native(h, { progress: NOW - 2 * HOUR });
    captureAck(h, sid, 'Deploy to production?\nDo you want to allow this? [y/n]');
    const run = await ctx.svc.tick('manual');
    expect(run.counts).toMatchObject({ keystroke: 1 });
    expect(ctx.calls[0]).toMatchObject({ path: `/api/sessions/${sid}/send`, body: 'y' });
  }));

  it('never kills a session whose intake is blocked on Alexey', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'blocked');
    session(h, { intake: 't1' });
    native(h, { progress: NOW - 5 * HOUR });
    const run = await ctx.svc.tick('manual');
    expect(run.counts).toMatchObject({ observed: 1, nudge: 0, stop: 0 });
    const rows = ctx.svc.view(5).decisions;
    expect(rows[0]).toMatchObject({ rule: 'human_blocked_intake', action: 'observe' });
  }));

  it('escalates unlinked live aplexer natives instead of inventing a control channel', async () => fixture(async (h, ctx) => {
    machine(h);
    native(h, { native: 'orphan-1', tag: 'manual-goal', progress: NOW - 3 * HOUR });
    const run = await ctx.svc.tick('manual');
    expect(run.counts.escalate).toBe(1);
    expect(ctx.calls).toHaveLength(0);
    expect(ctx.alerts.some(a => a.detail.includes('orphan-1'))).toBe(true);
  }));

  it('bounds captures and actions per run', async () => fixture(async (h, ctx) => {
    machine(h);
    for (let i = 0; i < 15; i++) {
      session(h, { id: `bbbbbbbb-0000-0000-0000-${String(i).padStart(12, '0')}`, native: `n${i}`, machine: 'm1' });
      native(h, { native: `n${i}`, progress: NOW - 2 * HOUR });
    }
    const run = await ctx.svc.tick('manual');
    expect(run.counts.capture).toBe(12);
    expect(run.counts.skipped).toBe(3);
    expect(ctx.calls).toHaveLength(12);
  }));

  it('records the dataset row features (idle, cpu, git, intake, screen) for export', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    const sid = session(h, { intake: 't1' });
    native(h);
    h.sql.exec("UPDATE machines SET last_heartbeat=? WHERE id='m1'", JSON.stringify({ cpu_usage: { busy_percent: 12, busy_cores: 0.96, cores: 8, psi_full_avg10: 3, observed_at: iso(NOW), interval_s: 10 } }));
    await ctx.svc.tick('manual');
    const data = ctx.svc.exportRows(100);
    const row = data.decisions[0];
    expect(row.session_id).toBe(sid);
    expect(row.features.git).toMatchObject({ dirty: true, dirty_count: 3 });
    expect(row.features.host_cpu).toMatchObject({ busy_percent: 12 });
    expect(row.features.intake_state).toBe('open');
    expect(row.idle_s).toBeGreaterThanOrEqual(3599);
  }));

  it('protects operator-protected sessions and skips their evidence gathering', async () => fixture(async (h, ctx) => {
    machine(h);
    const sid = session(h);
    native(h, { progress: NOW - 5 * HOUR });
    h.sql.exec("INSERT INTO session_control_protections(session_id,actor,request_key,created_at) VALUES (?,'op','k',?)", sid, iso(NOW));
    const run = await ctx.svc.tick('manual');
    // Count buckets are per action; the protected rule observes without acting.
    expect(run.counts).toMatchObject({ observed: 1, capture: 0, nudge: 0, stop: 0 });
    expect(ctx.svc.view(5).decisions[0]).toMatchObject({ rule: 'protected_session', action: 'observe' });
    expect(ctx.calls).toHaveLength(0);
  }));

  it('prunes dataset rows older than retention', async () => fixture(async (h, ctx) => {
    machine(h);
    h.sql.exec(`INSERT INTO session_audit_runs(at,source,scanned,counts_json) VALUES (?,?,?,?)`, iso(NOW - 8 * 24 * HOUR), 'scheduled', 0, '{}');
    const oldRun = h.sql.exec('SELECT id FROM session_audit_runs ORDER BY id DESC LIMIT 1').toArray()[0].id;
    h.sql.exec(`INSERT INTO session_audit_decisions(run_id,at,rule,action,features_json) VALUES (?,?,?,?,?)`, oldRun, iso(NOW - 8 * 24 * HOUR), 'recent_progress', 'observe', '{}');
    await ctx.svc.tick('manual');
    const cutoff = iso(NOW - 7 * 24 * HOUR);
    expect(Number(h.sql.exec('SELECT COUNT(*) AS n FROM session_audit_runs WHERE at < ?', cutoff).one().n)).toBe(0);
    expect(Number(h.sql.exec('SELECT COUNT(*) AS n FROM session_audit_decisions WHERE at < ?', cutoff).one().n)).toBe(0);
  }));
});

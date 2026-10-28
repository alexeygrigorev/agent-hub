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
    bound: true, linked: true, census_stale: false, send_supported: true, nativePhase: 'running',
    protected_session: false, runtime_s: 10 * HOUR / 1000, idle_s: 2 * T, host_cpu: null, git: null,
    intake_state: null, screen_tail: null, screen_age_s: null, nudges: 0, escalations: 0,
    send_broken: false, pending_stop: false, prior: null,
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
  it('escalates unbound running sessions once per idle episode, then stays quiet', () => {
    expect(decide(features({ bound: false, linked: false, escalations: 0 }))).toMatchObject({ rule: 'unlinked_idle_native', action: 'escalate' });
    expect(decide(features({ bound: false, linked: false, escalations: 1 }))).toMatchObject({ rule: 'unlinked_idle_known', action: 'observe' });
  });
  it('observes non-running intents instead of auditing them for action', () => {
    expect(decide(features({ state: 'held', linked: false }))).toMatchObject({ rule: 'intent_not_running', action: 'observe' });
  });
  it('never acts while the host census for the machine is stale (finding 4)', () => {
    const d = decide(features({ census_stale: true, git: { dirty: true, dirty_count: 9, unpushed: 0, checked_at: iso(NOW) }, nudges: 5, nativePhase: 'running' }));
    expect(d).toMatchObject({ rule: 'evidence_stale', action: 'observe' });
  });
  it('never acts on a stale unobserved census but stops a stale terminal binding', () => {
    expect(decide(features({ linked: false, nativePhase: 'unknown' }))).toMatchObject({ rule: 'observation_unknown', action: 'observe' });
    expect(decide(features({ linked: false, nativePhase: 'exited' }))).toMatchObject({ rule: 'stale_binding_stop', action: 'stop' });
  });
  it('stops a finished session whose intake closed, even with dirty files (tree stays)', () => {
    const d = decide(features({ intake: 't1', intake_state: 'done', git: { dirty: true, dirty_count: 2, unpushed: 0, checked_at: iso(NOW) } }));
    expect(d).toMatchObject({ rule: 'finished_intake_closed', action: 'stop' });
    expect(d.detail).toContain('files stay on disk');
  });
  it('stops a committed-and-pushed session with no intake after two idle windows', () => {
    const clean = { dirty: false, dirty_count: 0, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ idle_s: 2 * T, git: clean })))
      .toMatchObject({ rule: 'finished_committed_idle', action: 'stop' });
    // One idle window alone never stops a clean session.
    expect(decide(features({ idle_s: T, git: clean, screen_tail: 'working', screen_age_s: 60 })).action)
      .not.toBe('stop');
  });
  it('waits for a pending stop ahead of every stop-producing branch (finding 4)', () => {
    expect(decide(features({ pending_stop: true, prior: { action: 'stop', outcome: null } }))).toMatchObject({ rule: 'stop_pending', action: 'observe' });
    // The guard now precedes stale_binding and both finished stops.
    expect(decide(features({ pending_stop: true, linked: false, nativePhase: 'exited' }))).toMatchObject({ rule: 'stop_pending', action: 'observe' });
    expect(decide(features({ pending_stop: true, intake: 't1', intake_state: 'done' }))).toMatchObject({ rule: 'stop_pending', action: 'observe' });
    expect(decide(features({ pending_stop: true, git: { dirty: false, dirty_count: 0, unpushed: 0, checked_at: iso(NOW) }, idle_s: 2 * T }))).toMatchObject({ rule: 'stop_pending', action: 'observe' });
  });
  it('matches prompts only at the visible end of the screen, not in scrollback prose (finding 5)', () => {
    expect(matchInputPrompt('Proceed? [y/n]')).toMatchObject({ rule: 'yes_no_prompt', send: 'y' });
    expect(matchInputPrompt('Do you want to allow this?')).toMatchObject({ rule: 'approval_prompt' });
    expect(matchInputPrompt('Update is available. Install?')).toMatchObject({ rule: 'update_prompt' });
    expect(matchInputPrompt('Press enter to continue')).toMatchObject({ rule: 'press_enter' });
    expect(matchInputPrompt('Task paused. Continue?')).toMatchObject({ rule: 'continue_prompt', keystroke_only: false });
    // Prose about trust/allow in the middle of the transcript is not a live gate.
    expect(matchInputPrompt('Earlier we discussed why we trust this module.\nNow doing useful work.')).toBe(null);
    expect(matchInputPrompt('the reviewer asked us to allow retries last week')).toBe(null);
    // A real gate older than the prompt window is buried beyond the tail slice.
    expect(matchInputPrompt('Proceed? [y/n]' + 'x'.repeat(500))).toBe(null);
    expect(matchInputPrompt('nothing here')).toBe(null);
  });
  it('alerts instead of answering keystroke-only gates; sends only supported continuations (finding 1)', () => {
    const screen = { screen_tail: 'Deploy to production?\nDo you want to allow this? [y/n]', screen_age_s: 60 };
    const d = decide(features({ ...screen, git: { dirty: true, dirty_count: 1, unpushed: 0, checked_at: iso(NOW) } }));
    expect(d).toMatchObject({ rule: 'waiting_for_input:yes_no_prompt', action: 'observe' });
    expect(d.detail).toContain('no hub transport');
    // A gate keeps the screen unchanged, so an older capture is still evidence
    // of a live gate (progress would have reset the audit instead).
    expect(decide(features({ screen_tail: 'Proceed? [y/n]', screen_age_s: 40 * 60 })).rule).toBe('waiting_for_input:yes_no_prompt');
    // Past the screen freshness window the evidence is too old: recapture.
    expect(decide(features({ screen_tail: 'Proceed? [y/n]', screen_age_s: 3 * T })).rule).toBe('missing_screen');
    // continue_prompt is an agent-level continuation: inbox works when supported.
    const cont = { screen_tail: 'Task paused. Continue?', screen_age_s: 60 };
    expect(decide(features({ ...cont }))).toMatchObject({ rule: 'waiting_for_input:continue_prompt', action: 'keystroke', send_text: 'continue' });
    expect(decide(features({ ...cont, send_supported: false })).action).toBe('observe');
  });
  it('ladder: nudge once, escalate once, and stop only after BOTH landed (finding 1)', () => {
    const dirty = { dirty: true, dirty_count: 3, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ git: dirty, nudges: 0, escalations: 0 }))).toMatchObject({ rule: 'stuck_uncommitted', action: 'nudge', send_text: 'continue, commit, report' });
    expect(decide(features({ git: dirty, nudges: 1, escalations: 0 }))).toMatchObject({ rule: 'stuck_escalate', action: 'escalate' });
    expect(decide(features({ git: dirty, nudges: 1, escalations: 1 }))).toMatchObject({ rule: 'dead_after_nudge', action: 'stop' });
    // Alerts alone never stop a session: no dispatched continuation, no stop.
    expect(decide(features({ git: dirty, nudges: 0, escalations: 1 }))).toMatchObject({ rule: 'stuck_uncommitted', action: 'nudge' });
    expect(decide(features({ git: dirty, nudges: 0, escalations: 5 }))).toMatchObject({ rule: 'stuck_uncommitted', action: 'nudge' });
  });
  it('unsupported headless targets escalate once per episode and are never stopped (finding 1)', () => {
    const dirty = { dirty: true, dirty_count: 3, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ git: dirty, send_supported: false, nudges: 0, escalations: 0 }))).toMatchObject({ rule: 'stuck_nudge_unsupported', action: 'escalate' });
    expect(decide(features({ git: dirty, send_supported: false, nudges: 0, escalations: 1 }))).toMatchObject({ rule: 'unsupported_target', action: 'observe' });
    expect(decide(features({ git: dirty, send_supported: false, nudges: 0, escalations: 9 }))).toMatchObject({ rule: 'unsupported_target', action: 'observe' });
    expect(decide(features({ git: dirty, send_supported: false, nudges: 9, escalations: 9 })).action).toBe('observe');
  });
  it('a send endpoint that refuses continuations escalates once, then holds (finding 1)', () => {
    const dirty = { dirty: true, dirty_count: 3, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ git: dirty, send_broken: true, nudges: 0, escalations: 0 }))).toMatchObject({ rule: 'stuck_send_broken', action: 'escalate' });
    expect(decide(features({ git: dirty, send_broken: true, nudges: 0, escalations: 1 }))).toMatchObject({ rule: 'send_broken_target', action: 'observe' });
  });
  it('never kills on idle alone while the host CPU says the native may be computing (AC1 CPU input)', () => {
    const busy = { busy_percent: 68, busy_cores: 5.4, cores: 8, observed_at: iso(NOW) };
    const dirty = { dirty: true, dirty_count: 1, unpushed: 0, checked_at: iso(NOW) };
    expect(decide(features({ git: dirty, nudges: 1, escalations: 1, host_cpu: busy }))).toMatchObject({ rule: 'cpu_active', action: 'observe' });
    expect(decide(features({ git: dirty, nudges: 1, escalations: 1, host_cpu: { ...busy, busy_percent: 12 } }))).toMatchObject({ rule: 'dead_after_nudge', action: 'stop' });
    expect(decide(features({ git: dirty, nudges: 1, escalations: 1, host_cpu: null }))).toMatchObject({ rule: 'dead_after_nudge', action: 'stop' });
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
          const id = 'cmd-' + calls.length;
          const m = path.match(/^\/api\/sessions\/([^/]+)\/(send|capture|stop)$/);
          h.sql.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,ack_json,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, id, m?.[1] ?? null, m?.[2] ?? 'send', 'audit', init.body.slice(0, 40), '{}', 'sent', 1, id, null, iso(now), iso(now));
          return { status: 200, body: { command_id: id } };
        },
        notify: async (kind: string, subject: string, detail: string | null) => { alerts.push({ kind, subject, detail }); },
      }, () => now, makeHeuristicDecider, undefined);
      svc.init();
      const ctx = {
        svc, calls, alerts,
        get now() { return now; },
        advance(ms: number) { now += ms; },
        refreshCatalog(machine = 'm1') { h.sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES (?,?)', machine, iso(now)); },
      };
      return fn(h, ctx);
    });
  }
  type Ctx = {
    svc: SessionAuditService; calls: any[]; alerts: any[];
    readonly now: number; advance(ms: number): void; refreshCatalog(machine?: string): void;
  };
  function machine(h: any, id = 'm1') {
    h.sql.exec('INSERT INTO machines(id,name,hostname,registered_at) VALUES (?,?,?,?)', id, id, id + '.host', iso(NOW));
  }
  function native(h: any, opts: any = {}) {
    const machineId = opts.machine ?? 'm1', nativeId = opts.native ?? 'n1';
    h.sql.exec(`INSERT INTO fleet_sessions(machine_id,native_id,tag,engine,phase,observed_at,workspace,progress_json)
      VALUES (?,?,?,?,?,?,?,?)`, machineId, nativeId, opts.tag ?? 'worker', opts.engine ?? 'codex', opts.phase ?? 'running',
      iso(opts.observed ?? NOW), opts.workspace ?? '/home/alexey/git/demo-wt-a',
      JSON.stringify({ last_progress_at: iso(opts.progress ?? NOW - HOUR) }));
    h.sql.exec('INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES (?,?)', machineId, iso(opts.observed ?? NOW));
  }
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
      VALUES (?,?,?,?,?,?,?,?,?,?,?,?)`, 'cap-' + Math.random().toString(36).slice(2), sessionId, 'capture', 'someone', 'cap-key', '{}', 'acked', 1, 'corr', JSON.stringify({ outcome: 'delivered', capture: text }), iso(at), iso(at));
  }
  const decisions = (ctx: Ctx) => ctx.svc.view(50).decisions;

  it('runs on the 30-minute gate; manual runs bypass it', async () => fixture(async (h, ctx) => {
    machine(h);
    const first = await ctx.svc.tick('scheduled');
    expect(first.state).toBe('audited');
    expect(await ctx.svc.tick('scheduled')).toMatchObject({ state: 'already_audited' });
    ctx.advance(AUDIT_INTERVAL + 1);
    expect(await ctx.svc.tick('scheduled')).toMatchObject({ state: 'audited' });
    expect((await ctx.svc.tick('manual')).state).toBe('audited');
  }));

  it('nudges a stuck uncommitted session once, escalates, then stops it dead — verified by census, never a bare rc', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    const sid = session(h, { engine: 'claude' }); // headless claude accepts inbox sends
    native(h);
    const run1 = await ctx.svc.tick('manual');
    expect(run1.counts).toMatchObject({ nudge: 1 });
    expect(ctx.calls[0]).toMatchObject({ path: `/api/sessions/${sid}/send`, body: 'continue, commit, report' });
    const sentRow = decisions(ctx).at(-1);
    expect(sentRow.dispatched).toBe(true);
    expect(sentRow.outcome).toBe(null);

    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run2 = await ctx.svc.tick('manual');
    expect(run2.counts).toMatchObject({ escalate: 1 });
    expect(ctx.alerts.some(a => a.kind === 'session_audit')).toBe(true);
    // The alert actually landed: the escalation row records it as dispatched.
    const escalateRow = decisions(ctx).filter((r: any) => r.action === 'escalate').at(-1);
    expect(escalateRow).toMatchObject({ rule: 'stuck_escalate', dispatched: true, outcome: 'alerted' });

    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run3 = await ctx.svc.tick('manual');
    expect(run3.counts).toMatchObject({ stop: 1 });
    const stopCall = ctx.calls.at(-1);
    expect(stopCall.path).toBe(`/api/sessions/${sid}/stop`);
    // AC2 uncommitted-work guard: the stop command never carries a cleanup flag.
    expect(stopCall).not.toHaveProperty('body');

    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const stopRow = decisions(ctx).filter((r: any) => r.action === 'stop').at(-1);
    h.sql.exec("UPDATE session_control_commands SET state='acked', ack_json=? WHERE id=?", JSON.stringify({ outcome: 'delivered', confirmed_exit: true }), stopRow.action_key);
    // Census still says the native is alive: the stop stays unverified and alerting.
    const run4 = await ctx.svc.tick('manual');
    expect(run4.counts.stop ?? 0).toBe(0);
    const stillPending = decisions(ctx).filter((r: any) => r.action === 'stop').at(-1);
    expect(stillPending.outcome).toBe(null);
    expect(ctx.alerts.some(a => a.subject === 'stop-unverified')).toBe(true);
    expect(decisions(ctx).filter((r: any) => r.rule === 'stop_pending')).toHaveLength(1);

    // Only a fresh census showing the exit verifies it (AC2 "verified gone");
    // the host also marks the session stopped once the native is gone.
    h.sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id='n1'");
    h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sid);
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    await ctx.svc.tick('manual');
    const resolved = decisions(ctx).filter((r: any) => r.action === 'stop').at(-1);
    expect(resolved.outcome).toBe('stopped_verified');
  }));

  it('alerts on a keystroke-only gate instead of pretending to answer it', async () => fixture(async (h, ctx) => {
    machine(h);
    session(h, { intake: null });
    native(h, { progress: NOW - 2 * HOUR });
    captureAck(h, 'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa', 'Deploy to production?\nDo you want to allow this? [y/n]');
    const run = await ctx.svc.tick('manual');
    expect(run.counts).toMatchObject({ keystroke: 0, prompt_alert: 1, observed: 1 });
    expect(ctx.calls).toHaveLength(0);
    expect(ctx.alerts.some(a => a.detail.includes('yes_no_prompt'))).toBe(true);
  }));

  it('sends an inbox continuation for a supported interactive continue_prompt', async () => fixture(async (h, ctx) => {
    machine(h);
    const sid = session(h, { intake: null, mode: 'interactive' });
    native(h, { progress: NOW - 2 * HOUR });
    captureAck(h, sid, 'Checkpoint reached. Continue?');
    const run = await ctx.svc.tick('manual');
    expect(run.counts).toMatchObject({ keystroke: 1 });
    expect(ctx.calls[0]).toMatchObject({ path: `/api/sessions/${sid}/send`, body: 'continue' });
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

  it('escalates unlinked live aplexer natives once per idle episode, then observes', async () => fixture(async (h, ctx) => {
    machine(h);
    native(h, { native: 'orphan-1', tag: 'manual-goal', progress: NOW - 3 * HOUR });
    const run = await ctx.svc.tick('manual');
    expect(run.counts.escalate).toBe(1);
    expect(ctx.calls).toHaveLength(0);
    expect(ctx.alerts.some(a => a.detail.includes('orphan-1'))).toBe(true);
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run2 = await ctx.svc.tick('manual');
    expect(run2.counts).toMatchObject({ escalate: 0, observed: 1 });
    expect(decisions(ctx).at(-1)).toMatchObject({ rule: 'unlinked_idle_known', action: 'observe' });
  }));

  it('never acts on a bound session while its host census is stale', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    session(h, { engine: 'claude' });
    native(h, { progress: NOW - 5 * HOUR, observed: NOW - 2 * HOUR });
    h.sql.exec("UPDATE fleet_catalog_seen SET observed_at=? WHERE machine_id='m1'", iso(NOW - 2 * HOUR));
    const run = await ctx.svc.tick('manual');
    expect(run.counts).toMatchObject({ observed: 1, nudge: 0, stop: 0, escalate: 0 });
    expect(decisions(ctx)[0]).toMatchObject({ rule: 'evidence_stale', action: 'observe' });
  }));

  it('progress after a nudge resets the episode: the recovered session is nudged again, not stopped', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    session(h, { engine: 'claude' });
    native(h);
    const first = await ctx.svc.tick('manual'); // nudge at NOW
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    // The session resumes: native progress lands 10 minutes after the nudge.
    h.sql.exec("UPDATE fleet_sessions SET progress_json=? WHERE native_id='n1'", JSON.stringify({ last_progress_at: iso(NOW + 10 * 60 * 1000) }));
    ctx.advance(20 * 60 * 1000); // idle again (30 min after progress), threshold met
    ctx.refreshCatalog();
    const resumed = await ctx.svc.tick('manual');
    expect(resumed.counts).toMatchObject({ nudge: 1, escalate: 0, stop: 0 });
    const prior = decisions(ctx).find((r: any) => r.action === 'nudge' && r.run_id === first.run_id);
    expect(prior.outcome).toBe('progressed');
  }));

  it('a refused nudge is recorded honestly and still advances the escalation ladder', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    session(h, { engine: 'claude' });
    native(h);
    const failing = new SessionAuditService(h.sql, {
      request: async () => ({ status: 400, body: { error: 'send_unsupported_headless' } }),
      notify: async () => {},
    }, () => ctx.now, makeHeuristicDecider, undefined);
    const run1 = await failing.tick('manual');
    expect(run1.counts).toMatchObject({ nudge: 1, enqueue_failed: 1 });
    const row = decisions(ctx).at(-1);
    expect(row).toMatchObject({ action: 'nudge', dispatched: false, outcome: 'enqueue_failed' });
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run2 = await ctx.svc.tick('manual');
    // The refused continuation escalates as a broken send (never re-nudged, never counted as delivered).
    expect(run2.counts).toMatchObject({ escalate: 1, nudge: 0 });
    expect(decisions(ctx).filter((r: any) => r.action === 'escalate').at(-1)).toMatchObject({ rule: 'stuck_send_broken' });
  }));

  it('records only alerts that actually landed; skipped and failed alerts retry, never advance the ladder', async () => fixture(async (h, ctx) => {
    machine(h);
    for (let i = 0; i < 15; i++) {
      native(h, { native: `orphan-${i}`, tag: `goal-${i}`, progress: NOW - 3 * HOUR });
    }
    const run1 = await ctx.svc.tick('manual');
    expect(run1.counts).toMatchObject({ escalate: 15, alert_skipped: 5, alert_failed: 0 });
    const escalated = decisions(ctx).filter((r: any) => r.action === 'escalate');
    expect(escalated.filter((r: any) => r.outcome === 'alerted' && r.dispatched)).toHaveLength(10);
    expect(escalated.filter((r: any) => r.outcome === 'alert_skipped' && !r.dispatched)).toHaveLength(5);
    // The 5 omitted from the notification are not "already escalated": they retry next run.
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run2 = await ctx.svc.tick('manual');
    expect(run2.counts).toMatchObject({ escalate: 5, alert_skipped: 0 });
    expect(decisions(ctx).filter((r: any) => r.run_id === run2.run_id && r.action === 'observe' && r.rule === 'unlinked_idle_known')).toHaveLength(10);
  }));

  it('a failing notification sink is recorded as alert_failed and retried next run', async () => fixture(async (h, ctx) => {
    machine(h);
    native(h, { native: 'orphan-a', tag: 'goal-a', progress: NOW - 3 * HOUR });
    let fail = true;
    const flaky = new SessionAuditService(h.sql, {
      notify: async () => { if (fail) throw new Error('sink down'); },
    }, () => ctx.now, makeHeuristicDecider, undefined);
    const run1 = await flaky.tick('manual');
    expect(run1.counts).toMatchObject({ escalate: 1, alert_failed: 1 });
    expect(decisions(ctx).filter((r: any) => r.action === 'escalate').at(-1)).toMatchObject({ dispatched: false, outcome: 'alert_failed' });
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    fail = false;
    const run2 = await ctx.svc.tick('manual');
    // The failed alert never counted as an escalation: the retry escalates again and lands.
    expect(run2.counts).toMatchObject({ escalate: 1, alert_failed: 0 });
    expect(decisions(ctx).filter((r: any) => r.action === 'escalate').at(-1)).toMatchObject({ dispatched: true, outcome: 'alerted' });
  }));

  it('keeps verifying a timed-out stop until a census proves the exit (finding 3)', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    const sid = session(h, { engine: 'claude' });
    native(h, { progress: NOW - 5 * HOUR });
    await ctx.svc.tick('manual'); // nudge
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    await ctx.svc.tick('manual'); // escalate
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    await ctx.svc.tick('manual'); // stop issued
    // No acknowledgement ever arrives; past the timeout window the stop is
    // marked stop_timeout but STAYS in the verification lifecycle.
    ctx.advance(80 * 60 * 1000); ctx.refreshCatalog();
    const run4 = await ctx.svc.tick('manual');
    expect(run4.counts.stop ?? 0).toBe(0);
    const stopRow = decisions(ctx).filter((r: any) => r.action === 'stop').at(-1);
    expect(stopRow.outcome).toBe('stop_timeout');
    expect(decisions(ctx).filter((r: any) => r.rule === 'stop_pending')).toHaveLength(1);
    expect(ctx.alerts.filter(a => a.subject === 'stop-unverified').length).toBeGreaterThanOrEqual(1);
    // Later audits keep tracking the timed-out stop (finding 3) ...
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    await ctx.svc.tick('manual');
    expect(ctx.alerts.filter(a => a.subject === 'stop-unverified').length).toBeGreaterThanOrEqual(2);
    expect(h.sql.exec("SELECT COUNT(*) AS n FROM session_audit_decisions WHERE action='stop' AND outcome='stop_timeout'").one().n).toBe(1);
    // ... until a fresh census proves the exit (AC2 verified gone).
    h.sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id='n1'");
    h.sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", sid);
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    await ctx.svc.tick('manual');
    expect(decisions(ctx).filter((r: any) => r.action === 'stop').at(-1).outcome).toBe('stopped_verified');
  }));

  it('budget-skipped actions are recorded as skipped and never advance the ladder', async () => fixture(async (h, ctx) => {
    machine(h);
    for (let i = 0; i < 12; i++) {
      const sid = `bbbbbbbb-0000-0000-0000-${String(i).padStart(12, '0')}`;
      const wtPath = `/home/alexey/git/demo-wt-${i}`;
      session(h, { id: sid, native: `n${i}`, engine: 'claude', workspace: wtPath });
      native(h, { native: `n${i}`, progress: NOW - 2 * HOUR, workspace: wtPath });
      worktree(h, { id: `wt${i}`, task: `t${i}`, path: wtPath });
    }
    const run1 = await ctx.svc.tick('manual');
    expect(run1.counts).toMatchObject({ nudge: 8, skipped: 4 });
    ctx.advance(40 * 60 * 1000); ctx.refreshCatalog();
    const run2 = await ctx.svc.tick('manual');
    // The 4 skipped sessions were never nudged: they nudge now instead of escalating.
    expect(run2.counts).toMatchObject({ escalate: 8, nudge: 4 });
    const skippedRow = decisions(ctx).find((r: any) => r.outcome === 'skipped');
    expect(skippedRow.dispatched).toBe(false);
  }));

  it('bounds captures per run', async () => fixture(async (h, ctx) => {
    machine(h);
    for (let i = 0; i < 15; i++) {
      session(h, { id: `cccccccc-0000-0000-0000-${String(i).padStart(12, '0')}`, native: `n${i}`, machine: 'm1' });
      native(h, { native: `n${i}`, progress: NOW - 2 * HOUR });
    }
    const run = await ctx.svc.tick('manual');
    expect(run.counts.capture).toBe(12);
    expect(run.counts.skipped).toBe(3);
    expect(ctx.calls).toHaveLength(12);
  }));

  it('records the dataset row features (idle, cpu, git, intake, screen, dispatch) for export', async () => fixture(async (h, ctx) => {
    machine(h); worktree(h); intake(h, 't1', 'open');
    const sid = session(h, { intake: 't1', engine: 'claude' });
    native(h);
    h.sql.exec("UPDATE machines SET last_heartbeat=? WHERE id='m1'", JSON.stringify({ cpu_usage: { busy_percent: 12, busy_cores: 0.96, cores: 8, psi_full_avg10: 3, observed_at: iso(NOW), interval_s: 10 } }));
    await ctx.svc.tick('manual');
    const data = ctx.svc.exportRows(100);
    const row = data.decisions[0];
    expect(row.session_id).toBe(sid);
    expect(row.dispatched).toBe(true);
    expect(row.features.git).toMatchObject({ dirty: true, dirty_count: 3 });
    expect(row.features.host_cpu).toMatchObject({ busy_percent: 12 });
    expect(row.features.intake_state).toBe('open');
    expect(row.features.census_stale).toBe(false);
    expect(row.features.send_supported).toBe(true);
    expect(row.idle_s).toBeGreaterThanOrEqual(3599);
    expect(typeof data.decisions[0].features).toBe('object');
    expect(typeof data.decisions[0].screen_tail).toBe('object'); // null included, key present
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

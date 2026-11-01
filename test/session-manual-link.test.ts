/** Intake 27505f04 AC8: operator manual linkage of externally started sessions
 * to Hub tasks, while running and after they ended. Honest provenance from the
 * retained canonical record (no live process or host heartbeat required for
 * historical linkage), idempotent retries, auditable conflict refusals,
 * persistence across census reconciliation, and live-vs-historical semantics:
 * live linked work keeps a task assigned, ended sessions never block new work. */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { SessionControlService, adoptObservedNativeSessions } from '../src/session_control';
import { activeAssignmentEventIds, IntakeService, recordIntakeSessionLink } from '../src/intake';
import { insertSessionObservation } from '../src/session-observation';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const AT = Date.parse('2026-11-01T10:00:00Z');
const operator: any = { caller: { isAdmin: true, kind: 'human', actor: 'operator' }, scopes: ['admin'] };
const agent: any = { caller: { isAdmin: false, kind: 'agent', actor: 'demo:worker', tokenId: 'cred-1', tokenHash: 'hash' }, scopes: [] };
const REFUSED = { status: 409, value: { error: 'resource_limit', rejected: [{ machine: 'host', reason: 'resource_limit' }] } };
const linkReq = (id: string, intake: string) => new Request('https://hub/api/sessions/' + id + '/link',
  { method: 'POST', body: JSON.stringify({ key: 'manual-' + intake.slice(0, 8), intake_task_id: intake }) });

async function fixture(fn: (f: any) => Promise<void>) {
  const b = env as unknown as Env;
  await runInDurableObject(b.HUB.get(b.HUB.idFromName('manual-link-' + crypto.randomUUID())),
    async (hub: Hub, state: DurableObjectState) => {
      const sql = hub.sql, clock = { now: AT }, tx = <T>(f: () => T) => state.storage.transactionSync(f);
      const sc = new SessionControlService(sql, tx, () => clock.now, { reserve: () => REFUSED });
      sc.init();
      const iso = new Date(AT).toISOString();
      sql.exec("INSERT OR IGNORE INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('host','host','host',?,?)", iso, iso);
      sql.exec('INSERT OR IGNORE INTO fleet_catalog_seen(machine_id,observed_at) VALUES(?,?)', 'host', iso);
      const task = (id: string) => {
        sql.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)', id, JSON.stringify({
          id, project: 'agent-hub', status: 'open', version: 1, acceptance_criteria: 'AC8 manual linkage holds end to end.',
          history: [], created_at: AT, updated_at: AT }));
        return id;
      };
      // Synthetic placement only: the canonical assign flow records the link itself.
      const started: string[] = [];
      const startSession = async (input: any) => {
        const sid = crypto.randomUUID();
        const at2 = new Date(clock.now).toISOString();
        sql.exec(`INSERT INTO session_control_sessions (id,actor,request_key,request_json,machine_id,engine,mode,state,binding_json,created_at,updated_at)
          VALUES (?,'operator',?,?,'host','codex','headless','held',NULL,?,?)`, sid, input.key,
          JSON.stringify({ intake_task_id: input.intake_task_id, workspace: input.workspace ?? '/ws', provenance: 'hub_launch_verified' }), at2, at2);
        recordIntakeSessionLink(sql, sql.exec('SELECT * FROM session_control_sessions WHERE id=?', sid).toArray()[0],
          clock.now, REFUSED.value);
        started.push(sid);
        return { status: 201, body: { id: sid, state: 'held', hold: REFUSED.value } };
      };
      const intake = new IntakeService(sql, tx, { startSession });
      const auth = async () => operator;
      const version = (id: string) => taskOf(id).version;
      const assign = async (taskId: string, key: string) => await intake.handle(
        new Request('https://hub/api/intake/tasks/' + taskId + '/assign', { method: 'POST',
          body: JSON.stringify({ key, expected_version: version(taskId),
            session: { workspace: 'demo', brief: 'Synthetic assigned work' }, provenance: { type: 'chat', reference: 'fixture' } }) }), auth);
      const taskOf = (id: string): any => JSON.parse(String(sql.exec('SELECT data FROM intake_tasks WHERE id=?', id).toArray()[0].data));
      const rowOf = (id: string) => sql.exec('SELECT * FROM session_control_sessions WHERE id=?', id).toArray()[0];
      const observed = (state: string, over: any = {}) => {
        const native = { native_id: crypto.randomUUID(), tag: 'ext-' + Math.random().toString(16).slice(2, 8),
          workspace: '/ext', engine: 'zcodex', created_at_ms: AT - 60000, ...over };
        insertSessionObservation(sql, 'host', native, state, 'host-family', AT);
        return native;
      };
      const addLaunch = (launchId: string, nativeId: string | null) => {
        const at2 = new Date(AT).toISOString(), sid = crypto.randomUUID();
        sql.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,native_id,created_at,updated_at)
          VALUES(?,'operator','human','work',?,'{}','host','codex','account','headless','repo','work','running',?,?,?)`,
          launchId, 'launch-' + launchId, nativeId, at2, at2);
        sql.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,created_at,updated_at)
          VALUES(?,'operator',?,'{}',?,'host','codex','headless','starting',?,?)`, sid, 'session-' + launchId, launchId, at2, at2);
        return sid;
      };
      const links = (taskId: string) => taskOf(taskId).history.filter((e: any) => e.kind === 'link');
      await fn({ sql, clock, sc, intake, task, assign, taskOf, rowOf, observed, links, started, version, addLaunch });
    });
}

describe('manual session-to-task linkage (27505f04 AC8)', () => {
  it('exports a pure active-link event selector for intake history compaction', () => {
    const running = crypto.randomUUID(), ended = crypto.randomUUID();
    const task = { _active_assignment_session_ids: [running], history: [
      { id: 'live-link', kind: 'link', hub_session_id: running },
      { id: 'later-ended-link', kind: 'link', hub_session_id: ended },
    ] };
    expect([...activeAssignmentEventIds(task)]).toEqual(['live-link']);
    expect([...activeAssignmentEventIds({ ...task, _active_assignment_session_ids: [] })]).toEqual([]);
    expect(task.history).toHaveLength(2); // selector does not mutate the ledger
  });

  it('links an already ended session from its retained record with honest manual provenance', async () => fixture(async ({ sc, task, rowOf, observed, taskOf }: any) => {
    const taskId = task('task-ended-' + crypto.randomUUID().slice(0, 8));
    const native = observed('stopped'); // ended before any census heartbeat: identity is the retained row
    const sid = native.native_id;
    const res = await sc.handle(linkReq(sid, taskId), operator);
    expect(res.status).toBe(200);
    const body = await res.json();
    expect(body.already_linked).toBe(false);
    expect(body.intake_id).toBe(taskId);
    expect(JSON.parse(rowOf(sid).request_json).intake_task_id).toBe(taskId);
    const event = taskOf(taskId).history.at(-1);
    expect(event).toMatchObject({
      kind: 'link', hub_session_id: sid, native_id: native.native_id, engine: 'zcodex', host: 'host',
      link_source: 'manual', linked_by: 'operator',
      session_state_at_link: 'stopped', evidence: 'observed_native', authority: 'untrusted_task_data',
    });
    expect(typeof event.session_updated_at).toBe('string');
    // Historical linkage never claims live verification.
    expect(event.evidence).not.toBe('external_live_verified');
    expect(event.link_liveness).toBeUndefined();
  }));

  it('repeat linkage is idempotent: no duplicate event, no version advance', async () => fixture(async ({ sc, task, observed, links, version }: any) => {
    const taskId = task('task-repeat-' + crypto.randomUUID().slice(0, 8));
    const sid = observed('running').native_id;
    expect((await sc.handle(linkReq(sid, taskId), operator)).status).toBe(200);
    const before = { events: links(taskId).length, version: version(taskId) };
    const res = await sc.handle(linkReq(sid, taskId), operator);
    expect(res.status).toBe(200);
    expect((await res.json()).already_linked).toBe(true);
    expect(links(taskId)).toHaveLength(before.events);
    expect(version(taskId)).toBe(before.version);
  }));

  it('refuses conflicting assignments auditable-style from the start intent and from an earlier ledger link', async () => fixture(async ({ sc, sql, task, observed, taskOf, links }: any) => {
    const first = task('task-conflict-a-' + crypto.randomUUID().slice(0, 8));
    const second = task('task-conflict-b-' + crypto.randomUUID().slice(0, 8));
    const sid = observed('running').native_id;
    expect((await sc.handle(linkReq(sid, first), operator)).status).toBe(200);
    const conflict = await sc.handle(linkReq(sid, second), operator);
    expect(conflict.status).toBe(409);
    expect((await conflict.json()).error).toBe('intake_link_conflict');
    expect(JSON.parse(sql.exec('SELECT request_json FROM session_control_sessions WHERE id=?', sid).toArray()[0].request_json).intake_task_id).toBe(first);
    expect(links(second)).toHaveLength(0);
    // Ledger-only conflict: the start intent is empty but an earlier link event names this session.
    const ledger = observed('stopped');
    sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify({ ...taskOf(first),
      history: [...taskOf(first).history, { id: crypto.randomUUID(), kind: 'link', hub_session_id: null,
        native_id: ledger.native_id, host: 'host', provenance: { type: 'chat' }, evidence: 'external_unverified_not_launch_proof' }] }), first);
    const refused = await sc.handle(linkReq(ledger.native_id, second), operator);
    expect(refused.status).toBe(409);
    expect((await refused.json()).error).toBe('intake_link_conflict');
    // The exact same task replays idempotently even without a start intent.
    const firstLinks = links(first).length;
    const same = await sc.handle(linkReq(ledger.native_id, first), operator);
    expect(same.status).toBe(200);
    expect((await same.json()).already_linked).toBe(true);
    expect(links(first)).toHaveLength(firstLinks);
  }));

  it('is operator-only and renders missing sessions and tasks without an oracle', async () => fixture(async ({ sc, task, observed }: any) => {
    const taskId = task('task-auth-' + crypto.randomUUID().slice(0, 8));
    const sid = observed('running').native_id;
    const forbidden = await sc.handle(linkReq(sid, taskId), agent);
    expect(forbidden.status).toBe(403);
    expect((await forbidden.json()).error).toBe('forbidden');
    expect((await sc.handle(linkReq(crypto.randomUUID(), taskId), operator)).status).toBe(404);
    expect((await sc.handle(linkReq(sid, 'no-such-task-000000'), operator)).status).toBe(409);
    const a = task('task-amb-' + crypto.randomUUID().slice(0, 8));
    const b = task('task-amb-' + crypto.randomUUID().slice(0, 8));
    const ambiguous = await sc.handle(linkReq(sid, 'task-amb'), operator);
    expect(ambiguous.status).toBe(409);
    expect((await ambiguous.json()).error).toBe('ambiguous_intake');
    expect(a && b).toBeTruthy();
  }));

  it('associates observed natives with Fleet launches using exact tag or recorded native id evidence', async () => fixture(async ({ sql, addLaunch }: any) => {
    for (const evidence of ['tag', 'native_id']) {
      const launchId = crypto.randomUUID(), nativeId = crypto.randomUUID(), tag = 'task-hub-' + launchId;
      const sid = addLaunch(launchId, evidence === 'native_id' ? nativeId : null);
      const native = { native_id: nativeId, tag: evidence === 'tag' ? tag : 'custom-tag',
        workspace: 'C:\\repos\\agent-hub', engine: 'codex', phase: 'running', created_at_ms: AT - 1000 };
      if (evidence === 'tag') insertSessionObservation(sql, 'host', native, 'running', 'host-family', AT);
      adoptObservedNativeSessions(sql, 'host', [native], AT, 'host-family');
      const row: any = sql.exec('SELECT fleet_launch_id,binding_json FROM session_control_sessions WHERE id=?', sid).toArray()[0];
      expect(row.fleet_launch_id).toBe(launchId);
      expect(JSON.parse(row.binding_json)).toMatchObject({ native_id: nativeId, tag: native.tag,
        provenance: 'fleet_launch', evidence: { source: 'fleet_launch', evidence } });
      if (evidence === 'tag') {
        const observedRow: any = sql.exec('SELECT fleet_launch_id,binding_json FROM session_control_sessions WHERE id=?', nativeId).toArray()[0];
        expect(observedRow.fleet_launch_id).toBe(launchId);
        expect(JSON.parse(observedRow.binding_json)).toMatchObject({ provenance: 'fleet_launch', evidence: { source: 'fleet_launch', evidence: 'tag' } });
      } else expect(sql.exec('SELECT id FROM session_control_sessions WHERE id=?', nativeId).toArray()).toHaveLength(0);
    }
    const unmatched = { native_id: crypto.randomUUID(), tag: 'task-hub-' + crypto.randomUUID(),
      workspace: 'C:\\repos\\agent-hub', engine: 'codex', phase: 'running', created_at_ms: AT - 1000 };
    adoptObservedNativeSessions(sql, 'host', [unmatched], AT, 'host-family');
    const observed: any = sql.exec('SELECT fleet_launch_id,binding_json FROM session_control_sessions WHERE id=?', unmatched.native_id).toArray()[0];
    expect(observed.fleet_launch_id).toBeNull();
    expect(JSON.parse(observed.binding_json).provenance).toBe('observed_native');
  }));

  it('links a finished observed native from its retained identity when its catalog row is gone', async () => fixture(async ({ sc, task, observed, sql }: any) => {
    const taskId = task('task-retired-' + crypto.randomUUID().slice(0, 8));
    const native = observed('stopped');
    sql.exec('DELETE FROM fleet_sessions WHERE machine_id=? AND native_id=?', 'host', native.native_id);
    const res = await sc.handle(linkReq(native.native_id, taskId), operator);
    expect(res.status).toBe(200);
    expect((await res.json()).intake_id).toBe(taskId);
  }));

  it('keeps a live linked session assigned through a later historical link, and frees the task once live work ends', async () => fixture(async ({ sc, sql, intake, task, assign, observed, rowOf }: any) => {
    const taskId = task('task-assign-' + crypto.randomUUID().slice(0, 8));
    const first = await assign(taskId, 'assign-live-1');
    expect(first.status).toBe(200);
    const live = ((await first.json()) as any).assigned_session.hub_session_id as string;
    // A live assignment blocks duplicate starts.
    expect((await assign(taskId, 'assign-live-2')).status).toBe(409);
    // Appending a historical link of an ended session must not free the task.
    const ended = observed('stopped');
    expect((await sc.handle(linkReq(ended.native_id, taskId), operator)).status).toBe(200);
    expect((await assign(taskId, 'assign-live-3')).status).toBe(409);
    // Once the live session ends, the same historical links do not block new work.
    sql.exec("UPDATE session_control_sessions SET state='stopped' WHERE id=?", live);
    expect(rowOf(live).state).toBe('stopped');
    const second = await assign(taskId, 'assign-after-end');
    expect(second.status).toBe(200);
    expect(((await second.json()) as any).assigned_session.hub_session_id).not.toBe(live);
  }));

  it('never cancels held work because an ended session was linked later, but a live successor still supersedes', async () => fixture(async ({ sc, sql, intake, task, assign, observed, started, rowOf }: any) => {
    const taskId = task('task-held-' + crypto.randomUUID().slice(0, 8));
    const held = await assign(taskId, 'assign-held-1');
    expect(held.status).toBe(200);
    const heldId = started[started.length - 1];
    // Historical link of a long-ended session lands after the held session's own link event.
    const ended = observed('stopped');
    expect((await sc.handle(linkReq(ended.native_id, taskId), operator)).status).toBe(200);
    sc.reconcileHeldSessions(5, heldId);
    expect(rowOf(heldId).state).toBe('held');
    // A live successor session for the same task does supersede the held intent.
    const successor = observed('running');
    sql.exec('UPDATE session_control_sessions SET request_json=? WHERE id=?',
      JSON.stringify({ intake_task_id: taskId, workspace: '/ext' }), successor.native_id);
    recordIntakeSessionLink(sql, rowOf(successor.native_id), AT + 30000);
    sc.reconcileHeldSessions(5, heldId);
    const after = rowOf(heldId);
    expect(after.state).toBe('cancelled');
    expect(JSON.parse(after.hold_json).superseded_by).toBe(successor.native_id);
  }));

  it('survives census reconciliation: ended identity keeps the link and never duplicates it', async () => fixture(async ({ sc, sql, task, observed, rowOf, links, version }: any) => {
    const taskId = task('task-reconcile-' + crypto.randomUUID().slice(0, 8));
    const native = observed('running');
    const sid = native.native_id;
    expect((await sc.handle(linkReq(sid, taskId), operator)).status).toBe(200);
    // Census confirms the same incarnation exited, then reports it again.
    adoptObservedNativeSessions(sql, 'host', [{ ...native, phase: 'exited', confirmed_exit: true }], AT + 60000);
    expect(rowOf(sid).state).toBe('stopped');
    expect(JSON.parse(rowOf(sid).request_json).intake_task_id).toBe(taskId);
    expect(links(taskId)).toHaveLength(1);
    adoptObservedNativeSessions(sql, 'host', [{ ...native, phase: 'exited', confirmed_exit: true }], AT + 120000);
    expect(links(taskId)).toHaveLength(1);
    const view = await (await sc.handle(new Request('https://hub/api/sessions/' + sid), operator)).json();
    expect(view.intake_id).toBe(taskId);
  }));
});

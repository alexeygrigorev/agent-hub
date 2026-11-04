import { advanceFixtureBudgetDay, fixtureSql } from './helpers/fixture-budget';
import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { AskLedger, splitAsks, acLines, matchAcLine, noteMarks, ASK_GRACE_MS } from '../src/asks';
import { toolRequest } from '../src/mcp-tools';
const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const admin = { caller: { isAdmin: true, actor: 'human:operator', kind: 'human' }, scopes: ['admin'] } as any;
const op = { admin: true, actor: 'human:operator' };
const source = { type: 'chat', reference: null, timestamp: null };
let seq = 0;
async function call(h: any, path: string, payload?: any) {
  const r = await h.intakeService.handle(new Request('https://hub.test/api/intake' + path,
    payload ? { method: 'POST', body: JSON.stringify(payload) } : {}), async () => admin);
  return { status: r.status, body: await r.json() as any };
}
async function intake(h: any, ac: string, text = 'Synthetic request', ref: string | null = null) {
  const r = await call(h, '/tasks', { key: 'a-create-' + ++seq, project: 'demo', title: 'Ask ledger fixture ' + seq, text,
    source: { ...source, reference: ref }, acceptance_criteria: ac });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
  return r.body.task;
}
async function note(h: any, id: string, text: string) {
  const t = (await call(h, '/tasks/' + id)).body.task;
  const r = await call(h, `/tasks/${id}/events`, { key: 'a-note-' + ++seq, expected_version: t.version, note: { text } });
  expect(r.status, JSON.stringify(r.body)).toBe(200);
}
const close = async (h: any, id: string) => {
  const t = (await call(h, '/tasks/' + id)).body.task;
  return call(h, '/tasks/' + id, { key: 'a-close-' + ++seq, expected_version: t.version, status: 'done' });
};
const show = async (h: any, id: string) => (await call(h, '/tasks/' + id)).body.task;
const DONE = (extra: string) => `DONE\nevidence: agent-hub@0123456789abcdef pushed\nlive check: curl https://example.test/health -> 200\nresult: proven\n${extra}`;
async function fixture(fn: (h: any, ledger: AskLedger, alerts: any[], clock: { now: number }) => any) {
  return inDO(stub(), async (h: any, state:any) => {
    await advanceFixtureBudgetDay(h);
    const seedSql=fixtureSql(h,state);
    for (const t of ['tasks', 'receipts']) seedSql.exec('DELETE FROM intake_' + t);
    for (const t of ['ask_messages', 'asks', 'ask_agents', 'ask_gaps', 'ask_sources', 'ask_reconcile_runs', 'liveness_alerts']) seedSql.exec('DELETE FROM ' + t);
    seedSql.exec("UPDATE ask_meta SET gap_scope='agent',last_tick=NULL,last_result=NULL,daily_day=NULL,reconcile_cursor=0,last_reconcile_run=NULL,last_reconcile_at=NULL WHERE id=1");
    const alerts: any[] = [];
    const clock = { now: Date.now() };
    const ledger = new AskLedger(h.sql, h.intakeService, (f: any) => f(), {
      notify: async (kind: string, subject: string, detail: string | null) => { alerts.push({ kind, subject, detail }); return true; },
      hostFresh: () => true,
    }, () => clock.now);
    ledger.init();
    return fn(h, ledger, alerts, clock);
  });
}
const msg = (t_id: string, text: string, extra: any = {}) => ({ t_id, ts: Date.now(), text, ...extra });
const ingest = (l: AskLedger, agent: string, messages: any[], extra: any = {}, caller: any = op) =>
  l.ingest({ source: 'grok-bot-replica', machine_id: 'tabo', observed_at: Date.now(), agents: [{ agent_id: agent, agent_name: 'Developer', messages, ...extra }] }, caller);
const asks = (h: any) => h.sql.exec('SELECT a.*,m.t_id FROM asks a JOIN ask_messages m ON m.id=a.message_id ORDER BY m.seq,a.part').toArray();

describe('ask splitting and AC matching', () => {
  it('splits lists into verbatim items, keeps a plain message whole', () => {
    expect(splitAsks('Сделай вот что:\n1. дашборд usage\n2. per-intake stats\n3. schedules')).toEqual(['1. дашборд usage', '2. per-intake stats', '3. schedules']);
    expect(splitAsks('Fix the login page')).toEqual(['Fix the login page']);
    expect(splitAsks('First, add the export endpoint.\n\nSecond, show it on the dashboard.')).toHaveLength(2);
  });
  it('parses numbered AC lines and matches by ref or word overlap', () => {
    const ac = '1. Hub ask ledger table stores raw messages\n2. Gap detector alerts missing refs t1985u\n3. Dashboard shows usage over time';
    expect(acLines(ac).map(l => l.n)).toEqual([1, 2, 3]);
    expect(matchAcLine('anything', 't1985u', ac)?.n).toBe(2);
    expect(matchAcLine('dashboard usage over time please', 't1u', ac)?.n).toBe(3);
    expect(matchAcLine('совсем другое', 't1u', ac)).toBeNull();
  });
  it('reads explicit met/not-met marks; not-met wins', () => {
    const m = noteMarks('AC1: met — sha abc\nAC2 not met: missing UI\nt1984u: met\nAC3 met, AC3 not met');
    expect(m.ac.get(1)).toBe(true);
    expect(m.ac.get(2)).toBe(false);
    expect(m.ac.get(3)).toBe(false);
    expect(m.refs.get('t1984u')).toBe(true);
  });
});

describe('ingest contract (290b935a watcher)', () => {
  it('stores verbatim, is idempotent on (agent,t_id), records conflicts', () => fixture(async (h, l) => {
    const text = '  Сделай дашборд\n с пробелами  ';
    const r1 = ingest(l, 'dev', [msg('t100u', text)]);
    expect(r1).toMatchObject({ ok: true, accepted: 1, duplicates: 0 });
    expect(ingest(l, 'dev', [msg('t100u', text)])).toMatchObject({ accepted: 0, duplicates: 1 });
    const r3 = ingest(l, 'dev', [msg('t100u', 'edited')]);
    expect(r3.conflicts).toEqual([{ agent_id: 'dev', msg_id: 't100u' }]);
    const row = h.sql.exec('SELECT text,conflict_count FROM ask_messages').toArray();
    expect(row).toEqual([{ text, conflict_count: 1 }]);
  }));
  it('rejects bad items per item, never truncates', () => fixture(async (h, l) => {
    const r = ingest(l, 'dev', [msg('t1u', 'ok fine'), msg('x1', 'bad'), msg('t2u', 'y'.repeat(40000)), msg('t3u', 'z', { seq: 9 })]);
    expect(r.accepted).toBe(2); // 40 KB is fine (limit 128 KiB)
    expect(ingest(l, 'dev', [msg('t4u', 'y'.repeat(140000))]).rejected[0].error).toBe('text_too_long');
    expect(r.rejected.map((x: any) => x.error)).toEqual(['invalid_msg_id', 'seq_mismatch']);
  }));
  it('host callers are bound to their own machine and cannot link intakes', () => fixture(async (h, l) => {
    const host = { admin: false, actor: 'host:tabo', machineId: 'tabo' };
    expect(() => l.ingest({ source: 'grok-bot-replica', machine_id: 'other', agents: [] }, host)).toThrow('machine_mismatch');
    const r = l.ingest({ source: 'grok-bot-replica', machine_id: 'tabo', agents: [{ agent_id: 'dev', messages: [msg('t5u', 'a', { intake_id: 'x' }), msg('t6u', 'b')] }] }, host);
    expect(r.accepted).toBe(1);
    expect(r.rejected[0].error).toBe('intake_link_requires_operator');
    expect(h.sql.exec('SELECT machine_id FROM ask_sources').toArray()).toEqual([{ machine_id: 'tabo' }]);
  }));
  it('REST: hosts may ingest and read the cursor only; reads need the operator', () => fixture(async (h, l) => {
    const host = { admin: false, actor: 'host:tabo', machineId: 'tabo' };
    const post = (path: string, body: any, c: any) => l.handle(new Request('https://hub.test' + path, { method: 'POST', body: JSON.stringify(body) }), c);
    const r = await post('/api/asks/ingest', { source: 'grok-bot-replica', machine_id: 'tabo', agents: [{ agent_id: 'dev', messages: [msg('t7u', 'hello world task')] }] }, host);
    expect(r.status).toBe(200);
    const cur = await (await l.handle(new Request('https://hub.test/api/asks/cursor?agent_id=dev'), host)).json() as any;
    expect(cur).toMatchObject({ last_t_id: 't7u', count: 1 });
    expect((await l.handle(new Request('https://hub.test/api/asks'), host)).status).toBe(403);
    expect((await l.handle(new Request('https://hub.test/api/asks'), null)).status).toBe(401);
    expect((await l.handle(new Request('https://hub.test/api/asks'), op)).status).toBe(200);
  }));
  it('MCP asks_read/asks_write reach every ask route through the view/op slot', () => fixture(async (h, l) => {
    const mcp = async (name: string, args: any) => {
      const r = await l.handle(toolRequest('https://hub.test', name, args, 'Bearer x'), op);
      return { status: r.status, body: await r.json() as any };
    };
    const t = await intake(h, '1. Export the usage report as CSV');
    const w = await mcp('asks_write', { op: 'ingest', payload: { source: 'bot', agents: [{ agent_id: 'dev', agent_name: 'Developer',
      messages: [{ msg_id: 't70u', role: 'user', ts: Date.now(), text: 'Export the usage report as CSV' }, { msg_id: 't71u', role: 'user', ts: Date.now(), text: 'Rename the tab' }] }] } });
    expect(w.body).toMatchObject({ accepted: 2 });
    expect((await mcp('asks_write', { op: 'check' })).status).toBe(200);
    const list = await mcp('asks_read', { view: 'list', query: { agent_id: 'dev' } });
    expect(list.status).toBe(200);
    expect(list.body.asks).toHaveLength(2);
    const [a, b] = list.body.asks.sort((x: any, y: any) => x.t_id.localeCompare(y.t_id));
    expect((await mcp('asks_write', { op: 'map', payload: { intake_id: t.id } })).body.error).toBe('ask_id_required');
    expect((await mcp('asks_write', { op: 'map', payload: { ask_id: a.id, intake_id: t.id, ac_line: 1 } })).body.ask).toMatchObject({ state: 'mapped', ac_line: 1 });
    expect((await mcp('asks_write', { op: 'verify', payload: { ask_id: a.id, evidence: 'csv endpoint live' } })).body.ask.state).toBe('verified');
    expect((await mcp('asks_write', { op: 'dismiss', payload: { ask_id: b.id, reason: 'duplicate' } })).body.ask.state).toBe('not_an_ask');
    expect((await mcp('asks_read', { view: 'messages', query: { agent_id: 'dev' } })).status).toBe(200);
    expect((await mcp('asks_read', { view: 'summary' })).status).toBe(200);
    expect((await mcp('asks_write', { op: 'settings', payload: { gap_scope: 'window' } })).status).toBe(200);
    expect(() => toolRequest('https://hub.test', 'asks_write', { op: 'x/map' }, 'Bearer x')).toThrow('invalid_path_argument');
  }));
});

describe('contract v2: both sides, JSONL, reconciler (290b935a A10-A13)', () => {
  it('stores assistant/system rows as record only; only user text becomes asks', () => fixture(async (h, l) => {
    const r = ingest(l, 'dev', [
      { msg_id: 't900u', t_id: 't900', seq: 900, role: 'user', kind: 'message', ts: Date.UTC(2026, 9, 29, 11, 5), text: 'Build the asks page', reply_to: null,
        attachments: [{ name: 'shot.png', mime: 'image/png', size: 10, ref: 'blob:1' }] },
      { msg_id: 't900s1', seq: 900, role: 'assistant', kind: 'send-message', ts: null, text: 'On it', reply_to: 't900u' },
      { msg_id: 't901u', seq: 901, role: 'user', kind: 'user-attachment', ts: Date.now(), text: null },
      { msg_id: 't902u', seq: 902, role: 'user', ts: Date.now(), text: 'x', attachments: [{ name: 'a', content: 'secret' }] },
    ], { gap: { after_msg_id: 't880u', after_seq: 880, first_seq_in_window: 895 } });
    expect(r).toMatchObject({ accepted: 3, accepted_user: 1, rejected: [{ msg_id: 't902u', error: 'invalid_attachments' }] });
    expect(r.gaps).toEqual([expect.objectContaining({ kind: 'window_overflow', reported_by: 'watcher', after_t_id: 't880u' })]);
    await l.tick();
    expect(asks(h).map((a: any) => a.t_id)).toEqual(['t900u']);
    const lines = l.jsonl(new URLSearchParams()).trim().split('\n').map(x => JSON.parse(x));
    expect(lines.map(x => [x.msg_id, x.role, x.reply_to])).toEqual([['t900u', 'user', null], ['t900s1', 'assistant', 't900u'], ['t901u', 'user', null]]);
    expect(lines[0]).toMatchObject({ t_id: 't900', ts_utc: '2026-10-29T11:05:00.000Z', attachments: [{ name: 'shot.png' }] });
    expect(lines[0].ts_berlin).toContain('2026-10-29 12:05:00');
    expect(l.jsonl(new URLSearchParams({ role: 'assistant' })).split('\n').filter(Boolean)).toHaveLength(1);
    expect(l.jsonl(new URLSearchParams({ after: String(lines[1].cursor) })).split('\n').filter(Boolean)).toHaveLength(1);
  }));
  it('starts one reconciler 2 min after the last user batch, never two at once', () => fixture(async (h, l, alerts, clock) => {
    const starts: any[] = [];
    let state = 'running';
    const rl = new AskLedger(h.sql, h.intakeService, (f: any) => f(), {
      start: async (input: any) => { starts.push(input); return { status: 202, body: { id: 'rec-' + starts.length, state: 'queued' } }; },
      sessionState: () => state,
    }, () => clock.now);
    expect((await rl.reconcileDue()).state).toBe('not_configured');
    const t = await intake(h, '1. Track asks');
    rl.settings({ reconcile_intake: t.id.slice(0, 8) });
    ingest(rl, 'dev', [msg('t950u', 'please add per-intake stats')]);
    await rl.tick();
    expect((await rl.reconcileDue()).state).toBe('debouncing');
    clock.now += 2 * 60000 + 1;
    const r = await rl.reconcileDue();
    expect(r).toMatchObject({ state: 'started', session_id: 'rec-1', asks: 1 });
    expect(starts[0]).toMatchObject({ intake_task_id: t.id, mode: 'headless', workspace: 'agent-hub' });
    expect(starts[0].engine).toBeUndefined();
    expect(starts[0].brief).toContain('hub asks map');
    ingest(rl, 'dev', [msg('t951u', 'and the schedules page')]);
    clock.now += 3 * 60000;
    expect((await rl.reconcileDue()).state).toBe('running');
    state = 'stopped';
    await rl.tick();
    expect((await rl.reconcileDue()).state).toBe('started');
    state = 'running'; // the fake reports the new session rec-2 as live
    expect((await rl.reconcileDue()).state).toBe('running');
  }));
});

describe('mapping, unmapped alert and clearing (AC2, AC3, AC7)', () => {
  it('unmapped ask alerts once after 30 min; mapping it clears the alert', () => fixture(async (h, l, alerts, clock) => {
    ingest(l, 'dev', [msg('t200u', 'Please build the per-intake stats page')]);
    let r = await l.tick();
    expect(asks(h)[0].state).toBe('pending');
    expect(r.unmapped_alerts).toBe(0);
    clock.now += ASK_GRACE_MS + 1000;
    r = await l.tick();
    expect(asks(h)[0].state).toBe('unmapped');
    expect(r.unmapped_alerts).toBe(1);
    const alert = alerts.find(a => a.kind === 'asks_unmapped');
    expect(alert.detail).toContain('t200u');
    clock.now += 60000;
    expect((await l.tick()).unmapped_alerts).toBe(0); // once per ask
    const t = await intake(h, '1. Per-intake stats page');
    l.map(asks(h)[0].id.slice(0, 8), { intake_id: t.id.slice(0, 8) }, 'human:operator');
    expect(asks(h)[0]).toMatchObject({ state: 'mapped', intake_id: t.id, ac_line: 1, match: 'manual' });
    await l.tick();
    expect(alerts.filter(a => a.subject === alert.subject).map(a => a.detail === null)).toEqual([false, true]);
    await l.tick(); await l.tick(); // a resolved batch is never re-resolved (review finding 2)
    expect(alerts.filter(a => a.kind === 'asks_unmapped')).toHaveLength(2);
  }));
  it('auto-maps an ask whose ref is cited in an intake history, to the matching AC line', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Ledger table\n2. Usage over time dashboard with daily points', 'Alexey t300u: дашборд', 't300u');
    ingest(l, 'dev', [msg('t300u', 'Usage over time dashboard please')]);
    await l.tick();
    expect(asks(h)[0]).toMatchObject({ state: 'mapped', intake_id: t.id, ac_line: 2, match: 'ref' });
  }));
  it('explicit intake link from the fallback CLI maps immediately', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Anything');
    l.ingest({ source: 'cli', agents: [{ agent_id: 'dev', messages: [msg('t310u', 'some ask', { intake_id: t.id })] }] }, op);
    await l.tick();
    expect(asks(h)[0]).toMatchObject({ state: 'mapped', intake_id: t.id, match: 'explicit' });
  }));
  it('bare acknowledgements are not asks; dismiss needs a reason', () => fixture(async (h, l) => {
    ingest(l, 'dev', [msg('t320u', 'ок'), msg('t321u', 'how is it going')]);
    await l.tick();
    const [ack, q] = asks(h);
    expect(ack.state).toBe('not_an_ask');
    expect(() => l.dismiss(q.id, {}, 'op')).toThrow('reason_required');
    expect(l.dismiss(q.id, { reason: 'status question, answered' }, 'op').ask.state).toBe('not_an_ask');
  }));
});

describe('drop-after-done gate (AC5, beside done-gate f0b21a52)', () => {
  it('refuses done while a mapped ask is not marked met; marking it lets the close through', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Ledger table\n2. Usage dashboard', 'Alexey t400u', 't400u');
    ingest(l, 'dev', [msg('t400u', 'Usage dashboard')]);
    await l.tick();
    await note(h, t.id, DONE('AC1: met'));
    const refused = await close(h, t.id);
    expect(refused.status).toBe(409);
    expect(refused.body.error).toBe('asks_unverified');
    expect(refused.body.asks[0].ref).toBe('t400u');
    await note(h, t.id, DONE('AC1: met\nAC2: met — /usage live'));
    expect((await close(h, t.id)).status).toBe(200);
    await l.tick();
    expect(asks(h)[0].state).toBe('verified');
  }));
  it('the Hub verifier PASS note proves asks on AC lines', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Usage dashboard', 'Alexey t410u', 't410u');
    ingest(l, 'dev', [msg('t410u', 'Usage dashboard')]);
    await l.tick();
    await note(h, t.id, 'DONE — VERIFIED by Hub verifier\nevidence: agent-hub@0123456789abcdef x\nlive check: curl /usage -> 200\nresult: AC1 proven');
    expect((await close(h, t.id)).status).toBe(200);
  }));
  it('an ask mapped to an already-done intake is dropped and the intake reopens; backfill only flags', () => fixture(async (h, l, alerts) => {
    const live = await intake(h, '1. Something else');
    await note(h, live.id, DONE('AC1: met'));
    expect((await close(h, live.id)).status).toBe(200);
    const old = await intake(h, '1. Something old');
    await note(h, old.id, DONE('AC1: met'));
    expect((await close(h, old.id)).status).toBe(200);
    ingest(l, 'dev', [msg('t500u', 'the jev decider must ship', { intake_id: live.id })]);
    l.ingest({ source: 'backfill', agents: [{ agent_id: 'dev', messages: [msg('t420u', 'jev спека осталась только в памяти', { intake_id: old.id })] }] }, op);
    const r = await l.tick();
    expect(r.reopened).toBe(1);
    expect((await show(h, live.id)).status).toBe('open');
    expect((await show(h, old.id)).status).toBe('done');
    const byRef = Object.fromEntries(asks(h).map((a: any) => [a.t_id, a.state]));
    expect(byRef).toEqual({ t420u: 'dropped_after_done', t500u: 'mapped' });
    const dropAlert = alerts.find(a => a.kind === 'asks_dropped').detail;
    expect(dropAlert).toContain('t500u');
    expect(dropAlert).not.toContain('t420u'); // history is flagged, never paged
  }));
  it('operator closed_unverified is not refused; its asks are marked, not reopened', () => fixture(async (h, l) => {
    const t = await intake(h, '1. X', 'Alexey t430u', 't430u');
    ingest(l, 'dev', [msg('t430u', 'X')]);
    await l.tick();
    const v = (await show(h, t.id)).version;
    const r = await call(h, '/tasks/' + t.id, { key: 'cu-' + ++seq, expected_version: v, status: 'done', outcome: 'closed_unverified', reason: 'Alexey dropped it' });
    expect(r.status, JSON.stringify(r.body)).toBe(200);
    await l.tick();
    expect(asks(h)[0].state).toBe('closed_unverified');
  }));
  it('manual verify needs evidence and a mapped ask', () => fixture(async (h, l) => {
    ingest(l, 'dev', [msg('t440u', 'Ship it')]);
    await l.tick();
    const a = asks(h)[0];
    expect(() => l.verify(a.id, { evidence: 'x' }, 'op')).toThrow('ask_not_mapped');
    const t = await intake(h, '1. Ship it');
    l.map(a.id, { intake_id: t.id }, 'op');
    expect(() => l.verify(a.id, {}, 'op')).toThrow('evidence_required');
    expect(l.verify(a.id, { evidence: 'live at /x' }, 'op').ask).toMatchObject({ state: 'verified', verified_by: 'manual' });
  }));
});

describe('review regressions', () => {
  it('a verifier PASS proves a ref-mapped ask without an AC line', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Something unrelated', 'Alexey t1100u', 't1100u');
    ingest(l, 'dev', [msg('t1100u', 'совсем другое')]);
    await l.tick();
    expect(asks(h)[0]).toMatchObject({ state: 'mapped', ac_line: null });
    await note(h, t.id, 'DONE — VERIFIED by Hub verifier\nevidence: agent-hub@0123456789abcdef x\nlive check: curl /x -> 200\nresult: AC1 proven');
    expect((await close(h, t.id)).status).toBe(200);
  }));
  it('a ref merely mentioned in a closed intake neither maps nor reopens it', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Old unrelated work', 'see Alexey t1200u for context', 't1200u');
    await note(h, t.id, DONE('AC1: met'));
    expect((await close(h, t.id)).status).toBe(200);
    ingest(l, 'dev', [msg('t1200u', 'Нужен новый экспорт отчётов')]);
    const r = await l.tick();
    expect(asks(h)[0].state).toBe('pending');
    expect(r.reopened).toBe(0);
    expect((await show(h, t.id)).status).toBe('done');
  }));
  it('a dropped-ask alert says what happened and resolves once the ask is proven', () => fixture(async (h, l, alerts) => {
    const t = await intake(h, '1. Something else');
    await note(h, t.id, DONE('AC1: met'));
    expect((await close(h, t.id)).status).toBe(200);
    ingest(l, 'dev', [msg('t1300u', 'the decider must ship', { intake_id: t.id })]);
    await l.tick();
    const raised = alerts.filter(a => a.kind === 'asks_dropped');
    expect(raised).toHaveLength(1);
    expect(raised[0].detail).toContain('reopened');
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_dropped')).toHaveLength(1); // still open: the ask is unproven
    l.verify(asks(h)[0].id, { evidence: 'decider live at /jev' }, 'op');
    await l.tick();
    const last = alerts.filter(a => a.kind === 'asks_dropped');
    expect(last).toHaveLength(2);
    expect(last[1]).toMatchObject({ subject: raised[0].subject, detail: null });
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_dropped')).toHaveLength(2);
  }));
  it('fallback writers are not liveness sources; the cursor count is a counter', () => fixture(async (h, l, alerts, clock) => {
    l.ingest({ source: 'cli', agents: [{ agent_id: 'dev', messages: [msg('t1400u', 'one-off ask')] }] }, { admin: false, actor: 'host:tabo', machineId: 'tabo' });
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM ask_sources').toArray()[0].n).toBe(0);
    clock.now += 3 * 3600000;
    expect((await l.tick()).silent_sources).toBe(0);
    ingest(l, 'dev', [msg('t1401a', 'reply', { role: 'assistant' }), msg('t1402u', 'next')]);
    expect(l.cursor('dev')).toMatchObject({ count: 3, last_t_id: 't1402u' });
  }));
  it('a rejected user message becomes an unrecorded range and clears when it is re-sent valid', () => fixture(async (h, l, alerts) => {
    const r = ingest(l, 'dev', [msg('t1500u', 'x'.repeat(131073)), msg('t1501a', 'y'.repeat(131073), { role: 'assistant' })]);
    expect(r.rejected.map((x: any) => x.error)).toEqual(['text_too_long', 'text_too_long']);
    await l.tick();
    const gap = alerts.find(a => a.kind === 'asks_unrecorded');
    expect(gap.detail).toContain('t1500u (1 ref(s), rejected:text_too_long)');
    expect(gap.detail).not.toContain('t1501');
    ingest(l, 'dev', [msg('t1500u', 'shorter, valid')]);
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_unrecorded').at(-1)).toMatchObject({ subject: gap.subject, detail: null });
    // A bad re-post of the now-recorded message is not a new loss.
    ingest(l, 'dev', [msg('t1500u', 'x'.repeat(131073))]);
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM ask_gaps WHERE resolved_at IS NULL').toArray()[0].n).toBe(0);
    // A later rejection of a ref whose earlier rejection was resolved alerts again.
    ingest(l, 'dev', [msg('t1510u', 'x'.repeat(131073))]);
    h.sql.exec('UPDATE ask_gaps SET resolved_at=1 WHERE from_seq=1510');
    ingest(l, 'dev', [msg('t1510u', 'x'.repeat(131074))]);
    expect(h.sql.exec('SELECT resolved_at,alert_subject FROM ask_gaps WHERE from_seq=1510').toArray()).toEqual([{ resolved_at: null, alert_subject: null }]);
  }));
  it('daily silent-agent alert ignores agents with recorded traffic and resolves when messages arrive', () => fixture(async (h, l, alerts, clock) => {
    ingest(l, 'busy', [msg('t1601a', 'bot talking', { role: 'assistant' })], { window: { first_t_id: 't1600a', last_t_id: 't1601a' } });
    ingest(l, 'mute', [], { window: { first_t_id: 't1u', last_t_id: 't9a' } });
    await l.tick();
    const silent = alerts.filter(a => a.kind === 'asks_agent_silent');
    expect(silent.map(a => a.subject.split('-')[0])).toEqual(['mute']);
    ingest(l, 'mute', [msg('t10u', 'hello')]);
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_agent_silent').at(-1)).toMatchObject({ subject: silent[0].subject, detail: null });
  }));
  it('"nothing done" is not met', () => {
    expect(noteMarks('AC2: nothing done yet').ac.get(2)).toBe(false);
  });
  it('a verdict binds to its own AC/ref token, separators share the next verdict', () => {
    const m = noteMarks('AC1: met (AC4 deferred)\nAC5, AC6: met\nAC7-AC8 passed\nt1u: met, t2u: not met');
    expect(m.ac.get(1)).toBe(true);
    expect(m.ac.has(4)).toBe(false);
    expect([m.ac.get(5), m.ac.get(6), m.ac.get(7), m.ac.get(8)]).toEqual([true, true, true, true]);
    expect([m.refs.get('t1u'), m.refs.get('t2u')]).toEqual([true, false]);
  });
  it('scheduled tick and status reads stay bounded however much history exists', () => fixture(async (h, l) => {
    const t = await intake(h, '1. Done long ago');
    for (let i = 0; i < 1500; i++) {
      const id = 'hist-' + i;
      fixtureSql(h).exec(`INSERT INTO ask_messages(id,agent_id,t_id,seq,text,text_hash,source,received_at,split_state,role) VALUES(?,?,?,?,?,?,?,?,?,?)`,
        id, 'dev', 't' + (10000 + i) + 'u', 10000 + i, 'old ask ' + i, 'h' + i, 'backfill', 1, 'split', 'user');
      fixtureSql(h).exec(`INSERT INTO asks(id,message_id,part,text,state,intake_id,created_at,updated_at,alert_subject) VALUES(?,?,0,?,?,?,1,1,?)`,
        'ask-' + i, id, 'old ask ' + i, i % 2 ? 'verified' : 'closed_unverified', t.id, 'resolved:asks_unmapped-' + i);
    }
    let read = 0;
    const sql = { exec: (...a: any[]) => { const c = h.sql.exec(...a); const toArray = c.toArray.bind(c); c.toArray = () => { const r = toArray(); read += c.rowsRead; return r; }; return c; } };
    const scans: string[] = [];
    const svc = new Proxy(h.intakeService, { get: (o: any, k: string) => { if (k === 'statusAuthorizedTasks') scans.push(k); const v = o[k]; return typeof v === 'function' ? v.bind(o) : v; } });
    const bounded = new AskLedger(sql as any, svc, (f: any) => f(), { notify: async () => true }, () => Date.now());
    expect(bounded.counts()).toMatchObject({ verified: 750, closed_unverified: 750, messages: 1500 });
    expect(read).toBeLessThan(50);
    read = 0;
    await bounded.tick();
    expect(scans).toEqual([]);
    expect(read).toBeLessThan(200);
  }));
  it('hosts cannot post backfill', () => fixture(async (h, l) => {
    expect(() => l.ingest({ source: 'backfill', machine_id: 'tabo', agents: [] }, { admin: false, actor: 'host:tabo', machineId: 'tabo' })).toThrow('backfill_requires_operator');
  }));
});

describe('gap detector (AC4)', () => {
  it('flags holes between recorded refs once, resolves when filled', () => fixture(async (h, l, alerts) => {
    const r = ingest(l, 'dev', [msg('t600u', 'a b c'), msg('t603u', 'd e f')]);
    expect(r.gaps).toEqual([{ agent_id: 'dev', after_t_id: 't600u', before_t_id: 't603u', missing: ['t601u', 't602u'], kind: 'hole' }]);
    expect((await l.tick()).gap_alerts).toBe(1);
    expect(alerts.find(a => a.kind === 'asks_unrecorded').detail).toContain('t601u–t602u');
    expect((await l.tick()).gap_alerts).toBe(0);
    ingest(l, 'dev', [msg('t601u', 'g h i'), msg('t602u', 'j k l')]);
    await l.tick();
    expect(h.sql.exec('SELECT resolved_at FROM ask_gaps').toArray()[0].resolved_at).not.toBeNull();
    expect(alerts.filter(a => a.kind === 'asks_unrecorded').at(-1).detail).toBeNull();
  }));
  it('defaults to window-only gaps (counter also numbers non-user entries)', () => fixture(async (h, l) => {
    expect(h.sql.exec("SELECT dflt_value AS d FROM pragma_table_info('ask_meta') WHERE name='gap_scope'").toArray()[0].d).toBe("'window'");
    h.sql.exec("UPDATE ask_meta SET gap_scope='window' WHERE id=1");
    expect(ingest(l, 'dev', [msg('t650u', 'a'), msg('t660u', 'b')]).gaps).toEqual([]);
    ingest(l, 'dev', [], { window: { first_t_id: 't640a', last_t_id: 't661a', diag: { user_entries: 2, kinds: { message: 2 } } } });
    expect(l.cursor('dev')).toMatchObject({ covered_through: 661, window_diag: { user_entries: 2 } });
  }));
  it('global scope treats one counter across agents; window scope ignores holes', () => fixture(async (h, l) => {
    l.settings({ gap_scope: 'global' });
    ingest(l, 'dev', [msg('t700u', 'a')]);
    ingest(l, 'orin', [msg('t701u', 'b')]);
    expect(ingest(l, 'tofu', [msg('t702u', 'c')]).gaps).toEqual([]);
    l.settings({ gap_scope: 'window' });
    expect(ingest(l, 'dev', [msg('t710u', 'd')]).gaps).toEqual([]);
  }));
  it('a replica window that starts past the covered range is a window_overflow gap', () => fixture(async (h, l) => {
    l.settings({ gap_scope: 'window' });
    ingest(l, 'dev', [], { window: { entry_count: 200, first_t_id: 't800u', last_t_id: 't999a' } });
    expect(ingest(l, 'dev', [], { window: { entry_count: 200, first_t_id: 't1000u', last_t_id: 't1100u' } }).gaps).toEqual([]);
    const r = ingest(l, 'dev', [], { window: { entry_count: 200, first_t_id: 't1300u', last_t_id: 't1500u' } });
    expect(r.gaps[0]).toMatchObject({ kind: 'window_overflow', after_t_id: 't1100', before_t_id: 't1300' });
  }));
  it('silent watcher alerts once while its host is alive and resolves on the next ingest; daily flags a silent active agent', () => fixture(async (h, l, alerts, clock) => {
    ingest(l, 'dev', [], { window: { first_t_id: 't1u', last_t_id: 't5u' } });
    clock.now += 3 * 3600000;
    expect((await l.tick()).silent_sources).toBe(1);
    expect(alerts.filter(a => a.kind === 'asks_source_silent')).toHaveLength(1);
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_source_silent')).toHaveLength(1);
    expect(alerts.some(a => a.kind === 'asks_agent_silent')).toBe(true);
    h.sql.exec('UPDATE ask_sources SET last_ingest_at=?', clock.now);
    await l.tick();
    expect(alerts.filter(a => a.kind === 'asks_source_silent').at(-1).detail).toBeNull();
  }));
});

describe('Hub composition', () => {
  it('watchdog tick runs the ledger, status carries counts, /api/asks is wired', () => inDO(stub(), async (h: any) => {
    for (const t of ['ask_messages', 'asks', 'ask_gaps']) h.sql.exec('DELETE FROM ' + t);
    h.sql.exec('UPDATE intake_watchdog_meta SET last_tick=NULL WHERE id=1');
    const r = await h.intakeWatchdog.tick('test');
    expect(r.asks?.state).toBe('checked');
    const res = await h.fetch(new Request('https://hub.test/api/asks'));
    expect(res.status).toBe(401);
    expect(h.askLedger.counts().messages).toBe(0);
  }));
});

describe('daily incremental ask reconciliation (intake f530f981 AC2-AC4, AC6)', () => {
  it('matches against open/done/archived intakes, auto-creates uncovered asks with verbatim words + Berlin ts + stable key, appends missing words on partial matches, and advances cursor incrementally', () => fixture(async (h, l, _alerts, clock) => {
    const openTask = await intake(h, '1. Build fleet quota timeline chart with daily samples', 'Quota timeline chart');
    const doneTask = await intake(h, '1. Export the usage report as CSV from /api/usage', 'CSV usage report');
    await note(h, doneTask.id, DONE('AC1: met'));
    expect((await close(h, doneTask.id)).status).toBe(200);
    const archTask = await intake(h, '1. Configure host heartbeat liveness threshold', 'Host liveness threshold');
    // Mark archTask as archived in storage + summary projection
    const archData = JSON.parse(h.sql.exec('SELECT data FROM intake_tasks WHERE id=?', archTask.id).toArray()[0].data);
    archData.archived_at = clock.now;
    h.sql.exec('UPDATE intake_tasks SET data=? WHERE id=?', JSON.stringify(archData), archTask.id);
    h.sql.exec('UPDATE intake_status_summary SET archived_at=? WHERE id=?', clock.now, archTask.id);

    const recIntake = await intake(h, '1. Daily ask reconciler tracking intake');
    l.settings({ reconcile_intake: recIntake.id });

    const fixedTs = Date.UTC(2026, 9, 31, 14, 30, 0); // 2026-10-31 15:30:00 CET (Berlin)
    ingest(l, 'dev', [
      { msg_id: 't2001u', role: 'user', ts: fixedTs, text: 'Export the usage report as CSV from /api/usage' },
      { msg_id: 't2002u', role: 'user', ts: fixedTs + 1000, text: 'Configure host heartbeat liveness threshold' },
      { msg_id: 't2003u', role: 'user', ts: fixedTs + 2000, text: 'Build fleet quota timeline chart with daily samples and add dark mode toggle' },
      { msg_id: 't2004u', role: 'user', ts: fixedTs + 3000, text: 'Automatically sync phone voice memos into the daily writer queue' },
      { msg_id: 't2005a', role: 'assistant', ts: fixedTs + 4000, text: 'Working on it now' },
    ]);

    const run1 = await l.runReconcilePass(clock.now);
    expect(run1).toMatchObject({
      state: 'reconciled',
      from_cursor: 0,
      scanned_messages: 4,
      found_asks: 4,
      mapped: 2,
      appended: 1,
      created: 1,
      appended_ids: [openTask.id],
    });
    expect(run1.created_ids).toHaveLength(1);
    expect(run1.to_cursor).toBeGreaterThan(0);

    // Verify newly created intake for uncovered ask t2004u (AC3)
    const createdTask = h.intakeService.getTaskById(run1.created_ids[0]);
    expect(createdTask.title).toContain('[Reconciled t2004u]');
    expect(createdTask.title).toContain('Automatically sync phone voice memos into the daily writer queue');
    expect(createdTask.acceptance_criteria).toContain('Automatically sync phone voice memos into the daily writer queue');
    expect(createdTask.acceptance_criteria).toContain('2026-10-31 15:30:03');
    expect(createdTask.history[0]).toMatchObject({
      kind: 'intake',
      intake_key: 'ask-reconcile-dev-t2004u-p0',
      source: {
        type: 'chat',
        reference: expect.stringContaining('Developer/t2004u (2026-10-31 15:30:03 CET)'),
      },
    });
    expect(createdTask.history[0].text).toContain('Automatically sync phone voice memos into the daily writer queue');

    // Verify partial match appended missing words to openTask (AC3)
    const updatedOpen = h.intakeService.getTaskById(openTask.id);
    const partialNote = updatedOpen.history.find((e: any) => e.intake_key === 'ask-reconcile-dev-t2003u-p0-note');
    expect(partialNote).toBeDefined();
    expect(partialNote.text).toContain('missing words:');
    expect(partialNote.text).toContain('dark');
    expect(partialNote.text).toContain('toggle');
    expect(partialNote.text).toContain('Build fleet quota timeline chart with daily samples and add dark mode toggle');

    // Verify short report written to Hub summary and tracking intake (AC4)
    const sum = l.summary();
    expect(sum.meta.reconcile.cursor).toBe(run1.to_cursor);
    expect(sum.meta.reconcile.last_run.id).toBe(run1.id);
    expect(sum.meta.reconcile.runs).toHaveLength(1);
    expect(sum.meta.reconcile.runs[0].report).toContain('RECONCILE REPORT');
    const updatedRec = h.intakeService.getTaskById(recIntake.id);
    expect(updatedRec.history.some((e: any) => e.kind === 'note' && String(e.text).includes('RECONCILE REPORT'))).toBe(true);

    // Incremental cursor: next run with no new messages does no scan (AC4)
    const run2 = await l.runReconcilePass(clock.now + 60000);
    expect(run2).toMatchObject({
      state: 'no_new_messages',
      from_cursor: run1.to_cursor,
      to_cursor: run1.to_cursor,
      scanned_messages: 0,
      found_asks: 0,
    });

    // Re-running from cursor 0 never duplicates intakes or notes due to stable keys (AC3)
    const taskCountBefore = h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n;
    const openHistoryLenBefore = h.intakeService.getTaskById(openTask.id).history.length;
    const rerun = await l.runReconcilePass(clock.now + 120000, { cursor: 0 });
    expect(rerun).toMatchObject({
      state: 'reconciled',
      scanned_messages: 4,
      created: 0,
      appended: 0,
      mapped: 4,
    });
    expect(h.sql.exec('SELECT COUNT(*) AS n FROM intake_tasks').toArray()[0].n).toBe(taskCountBefore);
    expect(h.intakeService.getTaskById(openTask.id).history.length).toBe(openHistoryLenBefore);

    // Incremental new message scans only the new message after cursor (AC4)
    ingest(l, 'dev', [
      { msg_id: 't2006u', role: 'user', ts: fixedTs + 5000, text: 'Add a one-click button to copy session ID in the sessions table' },
    ]);
    const postRes = await l.handle(new Request('https://hub.test/api/asks/reconcile', {
      method: 'POST',
      body: JSON.stringify({ run_pass: true }),
    }), op);
    expect(postRes.status).toBe(200);
    const run3 = await postRes.json() as any;
    expect(run3).toMatchObject({
      state: 'reconciled',
      from_cursor: run1.to_cursor,
      scanned_messages: 1,
      found_asks: 1,
      created: 1,
    });
  }));
});

/** F7: Hub usable from a phone + intake pages (server + static contract).
 * Browser layout (390x844, both themes) is covered by test/phone-ui.browser.mjs,
 * run from scripts/test-full-local.sh and CI; this file runs in vitest
 * discovery (CI + full-local) and pins the API/static guarantees. */
import { beforeEach, describe, expect, it } from 'vitest';
import { env, SELF, runInDurableObject } from 'cloudflare:test';
import { createSessionCookie } from '../src/auth/session';
import intakeHtml from '../../public/intake.html?raw';
import intakeJs from '../../public/intake.js?raw';
import intakeCss from '../../public/intake.css?raw';
import enginesHtml from '../../public/engines.html?raw';
import sessionsJs from '../../public/sessions.js?raw';
import conversationsJs from '../../public/conversations.js?raw';
import workerSrc from '../src/worker.ts?raw';

const inDO: any = runInDurableObject;
const stub = () => (env as any).HUB.get((env as any).HUB.idFromName('hub'));
const source = { type: 'unknown', reference: null, timestamp: null };
let cookie = '';
let seq = 0;

async function call(path: string, payload?: any) {
  return SELF.fetch('https://hub.test/api/intake' + path, {
    method: payload ? 'POST' : 'GET',
    headers: { Cookie: cookie, 'x-csrf-token': 'csrf' },
    ...(payload ? { body: JSON.stringify(payload) } : {}),
  });
}
async function ok(path: string, payload?: any): Promise<any> {
  const res = await call(path, payload);
  const data = await res.json();
  expect(res.status, JSON.stringify(data)).toBe(200);
  return data;
}
const createTask = (project = 'phone', key = 'phone-' + ++seq, extra: any = {}) =>
  ok('/tasks', { key, project, title: 'Phone task ' + seq, text: 'Request body', source,
    acceptance_criteria: 'AC: list renders this text.', ...extra });

beforeEach(async () => {
  cookie = (await createSessionCookie(env as any, { sub: 'operator', email: 'admin@fake.local', isAdmin: true, csrfToken: 'csrf', exp: Math.floor(Date.now() / 1000) + 3600 })).split(';')[0];
  await inDO(stub(), (h: any) => {
    h.env.HUB_ADMIN_TOKEN_SHA256 = 'a'.repeat(64);
    for (const t of ['tasks', 'grants', 'rules', 'receipts']) h.sql.exec('DELETE FROM intake_' + t);
    h.sql.exec('UPDATE intake_meta SET revision=0');
    h.sql.exec('DELETE FROM credentials');
    h.sql.exec('DELETE FROM agents');
    h.sql.exec("INSERT OR REPLACE INTO users (sub, email, is_admin, first_seen_at, last_seen_at) VALUES ('fixture', 'admin@fake.local', 1, '2026-01-01', '2026-01-01')");
  });
});

function sessionRowCount(): Promise<number> {
  return inDO(stub(), (h: any) => {
    try {
      return Number(h.sql.exec('SELECT COUNT(*) AS n FROM session_control_sessions').toArray()[0].n);
    } catch { return 0; }
  });
}

describe('F7 phone routes and intake pages', () => {
  it('gates clean phone URLs (not 404) and keeps unknown paths denied', async () => {
    for (const path of ['/intake', '/fleet', '/conversations', '/engines', '/sessions', '/metrics', '/']) {
      const anon = await SELF.fetch('https://hub.test' + path, { redirect: 'manual' });
      expect([303, 200], path).toContain(anon.status);
    }
    // Detail URLs resolve to the intake bundle route instead of 404.
    const detail = await SELF.fetch('https://hub.test/intake/621de658-e9c3-425e-9a71-b742e3f3a1e0', { redirect: 'manual' });
    expect([303, 200]).toContain(detail.status);
    const missing = await SELF.fetch('https://hub.test/no-such-page', { redirect: 'manual' });
    expect(missing.status).toBe(404);
  });

  it('serves the intake bundle behind auth and lists with a status filter', async () => {
    const a = await createTask('phone', 'phone-a');
    const b = await createTask('phone', 'phone-b');
    await ok('/tasks/' + b.task.id, { key: 'phone-done', expected_version: 1, status: 'done' });
    const open = await ok('/tasks?status=open&limit=50');
    expect(open.items.map((t: any) => t.id)).toContain(a.task.id);
    expect(open.items.map((t: any) => t.id)).not.toContain(b.task.id);
    const done = await ok('/tasks?status=done&limit=50');
    expect(done.items.map((t: any) => t.id)).toContain(b.task.id);
    for (const t of open.items) {
      expect(t.payload_data.title).toBeTruthy();
      expect(t.control.status).toBeTruthy();
    }
  });

  it('exposes title, status, AC, history and linked sessions on the detail view', async () => {
    const { task } = await createTask();
    const { task: detail } = await ok('/tasks/' + task.id);
    expect(detail.payload_data.title).toContain('Phone task');
    expect(detail.control.status).toBe('open');
    expect(detail.payload_data.acceptance_criteria).toContain('AC: list renders');
    expect(Array.isArray(detail.payload_data.history)).toBe(true);
    expect(Array.isArray(detail.assigned_sessions)).toBe(true);
  });

  it('supports note, status and approve actions', async () => {
    const { task } = await createTask();
    const noted = await ok('/tasks/' + task.id + '/events', { key: 'phone-note', expected_version: 1, note: { text: 'Phone note' } });
    expect(noted.task.control.version).toBe(2);
    const moved = await ok('/tasks/' + task.id, { key: 'phone-status', expected_version: 2, status: 'pending_approval' });
    expect(moved.task.control.status).toBe('pending_approval');
    const flagged = await ok('/tasks/' + task.id + '/events', { key: 'phone-flag', expected_version: 3, event: { kind: 'pending_approval', text: 'Needs operator eyes' } });
    const pending = flagged.task.unresolved.find((e: any) => e.kind === 'pending_approval');
    expect(pending).toBeTruthy();
    const decided = await ok('/tasks/' + task.id + '/events', { key: 'phone-approve', expected_version: 4,
      event: { kind: 'approval_decision', ref: pending.id, decision: 'granted', source: { type: 'unknown' }, text: 'Approved from phone' } });
    expect(decided.task.unresolved.some((e: any) => e.id === pending.id)).toBe(false);
  });

  it('opening a task (list + detail GETs) never starts a session', async () => {
    const { task } = await createTask();
    const before = await sessionRowCount();
    const list = await ok('/tasks?status=open&limit=50');
    expect(list.items.length).toBeGreaterThan(0);
    for (let i = 0; i < 3; i++) {
      const { task: detail } = await ok('/tasks/' + task.id);
      expect(detail.control.version).toBe(1);
      expect(detail.payload_data.history).toHaveLength(1);
      expect(detail.assigned_sessions).toHaveLength(0);
    }
    expect(await sessionRowCount()).toBe(before);
  });

  it('intake client only reads on open: no assign/continue/start paths', () => {
    for (const forbidden of ['/assign', '/continue', 'api/sessions', 'fleet/launches', 'startSession']) {
      expect(intakeJs, forbidden).not.toContain(forbidden);
    }
    expect(intakeJs).toContain('/api/intake/tasks');
  });

  it('delivery is never rendered as accepted in phone clients', () => {
    // Sessions render real command states; the client knows no "accepted" label.
    expect(sessionsJs).not.toContain('accepted');
    // Conversations never consume delivery stages, so delivery cannot be
    // relabelled as acceptance; the only "Accepted" is the authority fact.
    expect(conversationsJs).not.toContain('delivered_at');
    expect(conversationsJs).not.toContain('accepted_at');
    expect(conversationsJs).not.toContain('acked_at');
    // Intake renders linkage/state markers truthfully, never "accepted".
    expect(intakeJs).not.toContain("textContent = 'accepted'");
    expect(intakeJs).not.toContain('textContent = "accepted"');
  });

  it('intake and engines pages are phone-ready viewports without terminal/SSH baggage', () => {
    for (const [name, html] of [['intake', intakeHtml], ['engines', enginesHtml]] as const) {
      expect(html, name).toContain('name="viewport"');
      expect(html, name).toContain('width=device-width');
      expect(html, name).toContain('data-dk-drawer-toggle');
      expect(html, name).toContain('shell.js');
    }
    expect(intakeCss).toMatch(/@media\s*\(max-width:\s*820px\)/);
    for (const [name, body] of [['intake.html', intakeHtml], ['intake.js', intakeJs], ['engines.html', enginesHtml]] as const) {
      for (const token of ['xterm', 'node-pty', 'ssh2', 'pocketshell', 'PTY', 'SSHCredential']) {
        expect(body, `${name} must not contain ${token}`).not.toContain(token);
      }
    }
    // Intake detail renders the F2 scope flag when present, without requiring it.
    expect(intakeJs).toContain('scope_flag');
  });

  it('worker maps every F7 clean URL to its bundle with no terminal/SSH routes', () => {
    for (const route of ["'/intake'", "'/intake.html'", "'/intake.js'", "'/intake.css'", "'/fleet'", "'/conversations'", "'/engines'"]) {
      expect(workerSrc, route).toContain(route);
    }
    expect(workerSrc).toContain("url.pathname = '/intake.html'");
    expect(workerSrc).toContain("url.pathname = '/fleet.html'");
    expect(workerSrc).toContain("url.pathname = '/conversations.html'");
    expect(workerSrc).toContain("url.pathname = '/engines.html'");
    for (const token of ['xterm', 'node-pty', 'ssh2', 'pocketshell', 'attach-ws', 'PTY']) {
      expect(workerSrc, `worker must not contain ${token}`).not.toContain(token);
    }
  });
});

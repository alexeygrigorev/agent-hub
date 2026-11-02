import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { SCHEMA_SQL } from '../src/schema';
import { doFetch } from './m1-focus-setup';

const defaultEnv = env as unknown as Env;
const ADMIN_TOKEN = 'op-token-prompt-schedules-test';
const HEADERS = { Authorization: `Bearer ${ADMIN_TOKEN}`, 'Content-Type': 'application/json' };

async function sha256Hex(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return [...new Uint8Array(digest)].map(b => b.toString(16).padStart(2, '0')).join('');
}

// Prompt library + session schedules (intake 19ec91c2 AC8-AC10).
describe('prompt library and session schedules', () => {
  let stub: any;
  beforeEach(async () => {
    stub = defaultEnv.HUB.get(defaultEnv.HUB.idFromName('hub'));
    await runInDurableObject(stub, async (instance: Hub) => {
      instance.env = { ...instance.env, HUB_ADMIN_TOKEN_SHA256: await sha256Hex(ADMIN_TOKEN) } as Env;
      for (const statement of SCHEMA_SQL.split(';')) {
        const trimmed = statement.trim();
        if (trimmed) { try { instance.sql.exec(trimmed); } catch (e) {} }
      }
      for (const table of ['hub_prompts', 'hub_prompt_versions', 'session_schedules', 'webhook_routes',
        'session_deliveries', 'audit', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
    });
  });
  const admin = (method: string, path: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method, headers: HEADERS, body: body === undefined ? undefined : JSON.stringify(body),
  }));

  it('versions prompts, resolves by slug/id/pin, renders vars, blocks delete while in use', async () => {
    const created: any = await (await admin('POST', '/api/prompts', { slug: 'oncall-triage', body: 'v1 {{var.who}}' })).json();
    expect(created.current_version).toBe(1);
    expect((await admin('POST', '/api/prompts', { slug: 'oncall-triage', body: 'x' })).status).toBe(409);
    const updated: any = await (await admin('PUT', '/api/prompts/oncall-triage', { body: 'v2 {{var.who}}' })).json();
    expect(updated.current_version).toBe(2);
    const bySlug: any = await (await admin('GET', '/api/prompts/oncall-triage')).json();
    expect(bySlug.body).toBe('v2 {{var.who}}');
    const pinned: any = await (await admin('GET', '/api/prompts/oncall-triage@1')).json();
    expect(pinned.body).toBe('v1 {{var.who}}');
    expect(((await (await admin('GET', `/api/prompts/${created.id}`)).json()) as any).slug).toBe('oncall-triage');
    const rendered: any = await (await admin('POST', '/api/prompts/oncall-triage/render', { vars: { who: 'bob' } })).json();
    expect(rendered).toMatchObject({ text: 'v2 bob', version: 2 });
    const versions: any = await (await admin('GET', '/api/prompts/oncall-triage/versions')).json();
    expect(versions.versions.map((v: any) => v.version)).toEqual([2, 1]);

    expect((await admin('POST', '/api/session-schedules', { name: 'uses-it', target_label: 't', cron: '*/5 * * * *', prompt_id: 'oncall-triage' })).status).toBe(201);
    expect((await admin('DELETE', '/api/prompts/oncall-triage')).status).toBe(409);
    await admin('DELETE', '/api/session-schedules/uses-it');
    expect((await admin('DELETE', '/api/prompts/oncall-triage')).status).toBe(200);
    expect((await admin('GET', '/api/prompts/oncall-triage')).status).toBe(404);
  });

  it('rejects non-admin callers', async () => {
    const r = await doFetch(new Request('http://example.com/api/prompts'));
    expect([401, 403]).toContain(r.status);
  });

  it('validates schedule input and manages pause/resume/delete', async () => {
    expect((await admin('POST', '/api/session-schedules', { name: 'a-b', target_label: 't', cron: 'nope', prompt: 'x' })).status).toBe(400);
    expect((await admin('POST', '/api/session-schedules', { name: 'a-b', target_label: 't', cron: '* * * * *' })).status).toBe(400);
    expect((await admin('POST', '/api/session-schedules', { name: 'a-b', target_label: 't', cron: '* * * * *', prompt: 'x', prompt_id: 'y' })).status).toBe(400);
    expect((await admin('POST', '/api/session-schedules', { name: 'a-b', target_label: 't', cron: '* * * * *', prompt_id: 'missing' })).status).toBe(400);
    const made: any = await (await admin('POST', '/api/session-schedules', { name: 'hourly', target_label: 'orchestrator-main', cron: '0 * * * *', prompt: 'check things' })).json();
    expect(made.next_due_at).toBeTruthy();
    expect(JSON.stringify(made)).not.toContain('check things');
    const paused: any = await (await admin('POST', '/api/session-schedules/hourly/pause')).json();
    expect(paused).toMatchObject({ enabled: false, next_due_at: null });
    const resumed: any = await (await admin('POST', '/api/session-schedules/hourly/resume')).json();
    expect(resumed.enabled).toBe(true);
    expect(((await (await admin('GET', '/api/session-schedules')).json()) as any).schedules).toHaveLength(1);
    expect((await admin('DELETE', '/api/session-schedules/hourly')).status).toBe(200);
  });

  it('fires into the shared delivery queue, idempotent per fire time, one catch-up for missed ticks', async () => {
    await admin('POST', '/api/prompts', { slug: 'hourly-check', body: 'Run {{var.what}}' });
    await admin('POST', '/api/session-schedules', { name: 'hourly', target_label: 'no-such-session', cron: '0 * * * *', prompt_id: 'hourly-check', prompt_vars: { what: 'sweep' } });
    const fire = async (key: string) => (await (await admin('POST', '/api/session-schedules/hourly/fire', { delivery_key: key })).json()) as any;
    const first = await fire('manual-1');
    const again = await fire('manual-1');
    expect(first.delivery_id).toBe(again.delivery_id);
    expect(again.replay).toBe(true);
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec('SELECT * FROM session_deliveries WHERE id = ?', first.delivery_id).toArray()[0];
      expect(row.body).toBe('Run sweep');
      expect(row.source).toBe('schedule-test');
      // Tick: simulate Hub down for 5 hours -> exactly one catch-up delivery.
      instance.sql.exec('UPDATE session_schedules SET next_due_ms = ?', Date.now() - 5 * 3600_000);
      const fired = await instance.sessionSchedules.tick();
      expect(fired).toBe(1);
      expect(await instance.sessionSchedules.tick()).toBe(0);
      const rows = instance.sql.exec("SELECT * FROM session_deliveries WHERE source = 'schedule'").toArray();
      expect(rows).toHaveLength(1);
      const audit: any[] = instance.sql.exec("SELECT detail FROM audit WHERE action = 'schedule.fire'").toArray();
      expect(audit.length).toBeGreaterThan(0);
      expect(JSON.stringify(audit)).not.toContain('sweep');
    });
    const deliveries: any = await (await admin('GET', '/api/session-schedules/deliveries')).json();
    expect(deliveries.deliveries.length).toBeGreaterThanOrEqual(2);
  });

  it('binds a webhook route to a library prompt with variables', async () => {
    await admin('POST', '/api/prompts', { slug: 'alert-template', body: '[{{var.team}}] {{payload.subject}}' });
    expect((await admin('POST', '/api/webhooks', { name: 'bad', target_label: 't', prompt_id: 'nope' })).status).toBe(400);
    const route: any = await (await admin('POST', '/api/webhooks', { name: 'dev-alerts', target_label: 'oncall-engineer', prompt_id: 'alert-template', prompt_vars: { team: 'dev' } })).json();
    expect(route.prompt_id).toBe('alert-template');
    const test: any = await (await admin('POST', `/api/webhooks/${route.id}/test`, { payload: { subject: 'disk full' } })).json();
    await runInDurableObject(stub, async (instance: Hub) => {
      const row: any = instance.sql.exec('SELECT body FROM session_deliveries WHERE id = ?', test.delivery_id).toArray()[0];
      expect(row.body).toBe('[dev] disk full');
    });
  });
});

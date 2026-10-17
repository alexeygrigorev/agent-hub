import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const testEnv = env as unknown as Env;

function notePeak(_label: string): void {
  // Scratch/cgroup peaks are measured by the serial test runner around this file.
}

async function cookieFor(
  sub: string,
  email: string,
  csrf: string,
): Promise<string> {
  const header = await createSessionCookie(testEnv, {
    sub,
    email,
    isAdmin: true,
    csrfToken: csrf,
    exp: Math.floor(Date.now() / 1000) + 3600,
  });
  return header.split(';')[0];
}

async function withHub(
  fn: (hub: Hub, state: DurableObjectState) => Promise<void>,
): Promise<void> {
  const stub = testEnv.HUB.get(
    testEnv.HUB.idFromName('links-' + crypto.randomUUID()),
  );
  await runInDurableObject(
    stub,
    async (_base: BaseHub, state: DurableObjectState) => {
      const hub = new Hub(state, testEnv);
      const nowIso = new Date().toISOString();
      hub.sql.exec(
        "INSERT OR IGNORE INTO machines (id, name, hostname, registered_at) VALUES ('machine-A', 'A', 'host-a', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO machines (id, name, hostname, registered_at) VALUES ('machine-B', 'B', 'host-b', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-1', 'projA', 'implementer', 'machine-A', ?)",
        nowIso,
      );
      hub.sql.exec(
        "INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('worker-2', 'projB', 'reviewer', 'machine-B', ?)",
        nowIso,
      );
      await fn(hub, state);
    },
  );
}

function jsonReq(
  path: string,
  opts: {
    method?: string;
    cookie?: string;
    csrf?: string;
    token?: string;
    body?: unknown;
  } = {},
): Request {
  const headers: Record<string, string> = {};
  if (opts.cookie) headers.Cookie = opts.cookie;
  if (opts.csrf) headers['X-CSRF-Token'] = opts.csrf;
  if (opts.token) headers.Authorization = `Bearer ${opts.token}`;
  let body: string | undefined;
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(opts.body);
  }
  return new Request('https://hub.test' + path, {
    method: opts.method ?? (opts.body !== undefined ? 'POST' : 'GET'),
    headers,
    body,
  });
}

async function seedAgentToken(
  sql: any,
  opts: {
    id: string;
    token: string;
    actor: string;
    machineId: string;
    scopes: string[];
    expiresAt?: string;
    revokedAt?: string | null;
  },
): Promise<string> {
  const nowIso = new Date().toISOString();
  sql.exec(
    `INSERT INTO credentials
     (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at, revoked_at)
     VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, 'alice', ?, ?, ?)`,
    opts.id,
    await sha256(opts.token),
    'fam-' + opts.id,
    opts.actor,
    opts.actor,
    opts.machineId,
    JSON.stringify(opts.scopes),
    nowIso,
    opts.expiresAt ?? new Date(Date.now() + 86400000).toISOString(),
    opts.revokedAt ?? null,
  );
  return opts.token;
}

function insertJob(
  sql: any,
  opts: { id: string; actor: string; state?: string; launchId?: string | null },
): void {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT INTO routing_jobs
     (id, actor, request_key, request_json, launch_id, state, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
    opts.id,
    opts.actor,
    'key-' + opts.id,
    JSON.stringify({
      workspace: 'tasks',
      tier: 'daily',
      model: null,
      needs: [],
      tasks: [{ prompt: 'Read only', approved_action: 'Read only' }],
      preferred_runner: null,
      preferred_machine: null,
    }),
    opts.launchId ?? null,
    opts.state ?? 'queued',
    at,
    at,
  );
}

function insertLaunch(
  sql: any,
  opts: { id: string; actor: string; state?: string },
): void {
  const at = new Date().toISOString();
  sql.exec(
    `INSERT INTO fleet_launches
     (id, actor, requester_kind, parent_id, summary, request_key, request_json, machine_id, engine, account, profile, mode, workspace, prompt, state, created_at, updated_at)
     VALUES (?, ?, 'agent', NULL, 'summary', ?, '{}', 'machine-A', 'zcodex', 'acct', NULL, 'headless', 'tasks', 'prompt', ?, ?, ?)`,
    opts.id,
    opts.actor,
    'launch-' + opts.id,
    opts.state ?? 'offered',
    at,
    at,
  );
}

async function createGroup(
  hub: Hub,
  cookie: string,
  csrf: string,
  title: string,
  members: string[],
): Promise<any> {
  const res = await hub.fetch(
    jsonReq('/api/conversations', {
      method: 'POST',
      cookie,
      csrf,
      body: { kind: 'group', title, members },
    }),
  );
  expect(res.status, await res.clone().text()).toBe(201);
  return res.json();
}

describe('M4 conversation links HTTP (Miniflare SQL)', () => {
  it('member attaches, lists, gets, reverses, and removes a routing_job without submitting', async () => {
    await withHub(async hub => {
      const csrf = 'csrf-alice-links';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const conv = await createGroup(hub, cookie, csrf, 'M4 job thread', [
        'projA:worker-1',
      ]);
      const jobId = crypto.randomUUID();
      insertJob(hub.sql, { id: jobId, actor: 'routing-client:x' });

      const beforeJobs = Number(
        hub.sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').toArray()[0].n,
      );
      const attached = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'routing_job',
            ref: jobId,
            idempotency_key: 'link-job-1',
          },
        }),
      );
      expect(attached.status).toBe(201);
      const body = (await attached.json()) as any;
      expect(body.kind).toBe('routing_job');
      expect(body.ref).toBe(jobId);
      expect(body.replay).toBe(false);
      expect(body.target.available).toBe(true);
      expect(body.target.canonical_status).toBe('queued');
      expect(body.target.complete).toBe(false);
      expect(JSON.stringify(body)).not.toContain('Read only');
      expect(JSON.stringify(body)).not.toContain('routing-client:x');
      expect(
        Number(
          hub.sql.exec('SELECT COUNT(*) AS n FROM routing_jobs').toArray()[0].n,
        ),
      ).toBe(beforeJobs);

      const listed = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, { cookie }),
      );
      expect(listed.status).toBe(200);
      const listBody = (await listed.json()) as any;
      expect(listBody.links).toHaveLength(1);
      expect(listBody.links[0].id).toBe(body.id);

      const got = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links/${body.id}`, { cookie }),
      );
      expect(got.status).toBe(200);

      const reverse = await hub.fetch(
        jsonReq(
          `/api/conversations/links?kind=routing_job&ref=${jobId}`,
          { cookie },
        ),
      );
      expect(reverse.status).toBe(200);
      const rev = (await reverse.json()) as any;
      expect(rev.links).toHaveLength(1);
      expect(rev.links[0].conversation_id).toBe(conv.id);
      expect(rev.target.canonical_status).toBe('queued');

      const removed = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links/${body.id}`, {
          method: 'DELETE',
          cookie,
          csrf,
        }),
      );
      expect(removed.status).toBe(200);
      expect(
        (
          await (
            await hub.fetch(
              jsonReq(`/api/conversations/${conv.id}/links`, { cookie }),
            )
          ).json() as any
        ).links,
      ).toHaveLength(0);
    });
    notePeak('attach-list-remove');
  });

  it('derives outcome/review from live job+launch state and rejects client verdicts', async () => {
    await withHub(async hub => {
      const csrf = 'csrf-alice-outcome';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const conv = await createGroup(hub, cookie, csrf, 'Outcome thread', []);
      const launchId = crypto.randomUUID();
      const jobId = crypto.randomUUID();
      insertLaunch(hub.sql, {
        id: launchId,
        actor: 'projA:worker-1',
        state: 'exited',
      });
      insertJob(hub.sql, {
        id: jobId,
        actor: 'projA:worker-1',
        state: 'queued',
        launchId,
      });

      const bad = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'outcome',
            ref: jobId,
            status: 'done',
            verdict: 'accept',
          },
        }),
      );
      expect(bad.status).toBe(400);
      expect(await bad.json()).toMatchObject({ error: 'unknown_fields' });

      const outcome = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'outcome', ref: jobId, idempotency_key: 'out-1' },
        }),
      );
      expect(outcome.status).toBe(201);
      const out = (await outcome.json()) as any;
      expect(out.target.canonical_status).toBe('done');
      expect(out.target.complete).toBe(true);
      expect(out.target.terminal).toBe(true);

      hub.sql.exec(
        "UPDATE fleet_launches SET state = 'failed' WHERE id = ?",
        launchId,
      );
      const again = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links/${out.id}`, { cookie }),
      );
      const live = (await again.json()) as any;
      expect(live.target.canonical_status).toBe('failed');
      expect(live.target.complete).toBe(false);

      const review = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'review', ref: jobId, idempotency_key: 'rev-1' },
        }),
      );
      expect(review.status).toBe(201);

      const launchLink = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'fleet_launch',
            ref: launchId,
            idempotency_key: 'ln-1',
          },
        }),
      );
      expect(launchLink.status).toBe(201);
      const launchBody = (await launchLink.json()) as any;
      expect(launchBody.target.canonical_status).toBe('failed');
      expect(launchBody.target.complete).toBe(false);
    });
    notePeak('outcome-review');
  });
});

describe('M4 membership and target ACL', () => {
  it('non-member admin is 403; unknown/foreign targets are opaque 404', async () => {
    await withHub(async hub => {
      const aliceCsrf = 'csrf-alice-acl';
      const bobCsrf = 'csrf-bob-acl';
      const alice = await cookieFor('alice', 'admin@fake.local', aliceCsrf);
      const bob = await cookieFor('bob', 'another@fake.local', bobCsrf);
      const conv = await createGroup(hub, alice, aliceCsrf, 'Private', [
        'projA:worker-1',
      ]);
      const jobId = crypto.randomUUID();
      insertJob(hub.sql, { id: jobId, actor: 'projA:worker-1' });

      const nonMember = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie: bob,
          csrf: bobCsrf,
          body: { kind: 'routing_job', ref: jobId, idempotency_key: 'bob-1' },
        }),
      );
      expect(nonMember.status).toBe(403);
      expect(await nonMember.json()).toMatchObject({
        error: 'forbidden',
        reason: 'not_a_member',
      });

      const missing = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie: alice,
          csrf: aliceCsrf,
          body: {
            kind: 'routing_job',
            ref: crypto.randomUUID(),
            idempotency_key: 'no-job',
          },
        }),
      );
      expect(missing.status).toBe(404);
      expect(await missing.json()).toEqual({ error: 'not_found' });

      const workerTok = await seedAgentToken(hub.sql, {
        id: 'cred-w1',
        token: 'tok-w1-links',
        actor: 'projA:worker-1',
        machineId: 'machine-A',
        scopes: ['bus:read', 'bus:send', 'fleet:launch'],
      });
      const otherJob = crypto.randomUUID();
      insertJob(hub.sql, { id: otherJob, actor: 'projB:worker-2' });
      const foreign = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          token: workerTok,
          body: {
            kind: 'routing_job',
            ref: otherJob,
            idempotency_key: 'foreign',
          },
        }),
      );
      expect(foreign.status).toBe(404);
      expect(await foreign.json()).toEqual({ error: 'not_found' });

      const own = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          token: workerTok,
          body: {
            kind: 'routing_job',
            ref: jobId,
            idempotency_key: 'own-job',
          },
        }),
      );
      expect(own.status).toBe(201);

      const launchId = crypto.randomUUID();
      insertLaunch(hub.sql, { id: launchId, actor: 'projA:worker-1' });
      const agentLaunch = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          token: workerTok,
          body: {
            kind: 'fleet_launch',
            ref: launchId,
            idempotency_key: 'ln-agent',
          },
        }),
      );
      expect(agentLaunch.status).toBe(404);

      const reverseForeign = await hub.fetch(
        jsonReq(
          `/api/conversations/links?kind=routing_job&ref=${otherJob}`,
          { token: workerTok },
        ),
      );
      expect(reverseForeign.status).toBe(404);

      const reverseOwn = await hub.fetch(
        jsonReq(`/api/conversations/links?kind=routing_job&ref=${jobId}`, {
          token: workerTok,
        }),
      );
      expect(reverseOwn.status).toBe(200);
      const rev = (await reverseOwn.json()) as any;
      expect(rev.links[0].conversation_id).toBe(conv.id);

      const bobReverse = await hub.fetch(
        jsonReq(`/api/conversations/links?kind=routing_job&ref=${jobId}`, {
          cookie: bob,
        }),
      );
      expect(bobReverse.status).toBe(200);
      expect((await bobReverse.json() as any).links).toHaveLength(0);
    });
    notePeak('acl');
  });

  it('revoked membership, expired token, and CSRF fail closed on current checks', async () => {
    await withHub(async hub => {
      const csrf = 'csrf-alice-rev';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const conv = await createGroup(hub, cookie, csrf, 'Revoke', [
        'projA:worker-1',
      ]);
      const jobId = crypto.randomUUID();
      insertJob(hub.sql, { id: jobId, actor: 'projA:worker-1' });
      const workerTok = await seedAgentToken(hub.sql, {
        id: 'cred-w1-rev',
        token: 'tok-w1-rev',
        actor: 'projA:worker-1',
        machineId: 'machine-A',
        scopes: ['bus:read', 'bus:send', 'fleet:launch'],
      });
      const attached = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          token: workerTok,
          body: { kind: 'routing_job', ref: jobId, idempotency_key: 'k1' },
        }),
      );
      expect(attached.status).toBe(201);

      await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/members/remove`, {
          cookie,
          csrf,
          body: { actor: 'projA:worker-1' },
        }),
      );
      const afterRemove = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, { token: workerTok }),
      );
      expect(afterRemove.status).toBe(403);

      const expiredTok = await seedAgentToken(hub.sql, {
        id: 'cred-w2-exp',
        token: 'tok-w2-exp',
        actor: 'projB:worker-2',
        machineId: 'machine-B',
        scopes: ['bus:read', 'bus:send'],
        expiresAt: new Date(Date.now() - 1000).toISOString(),
      });
      const expired = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, { token: expiredTok }),
      );
      expect(expired.status).toBe(401);

      const noCsrf = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          body: { kind: 'routing_job', ref: jobId, idempotency_key: 'csrf' },
        }),
      );
      expect(noCsrf.status).toBe(403);
    });
    notePeak('revocation');
  });
});

describe('M4 idempotency, cap, restart, retention', () => {
  it('replays duplicate refs and keys, enforces cap, and preserves links across restart', async () => {
    await withHub(async (hub, state) => {
      hub.linksService.limits.maxLinksPerConversation = 2;
      const csrf = 'csrf-alice-cap';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const conv = await createGroup(hub, cookie, csrf, 'Cap', []);
      const a = crypto.randomUUID();
      const b = crypto.randomUUID();
      const c = crypto.randomUUID();
      insertJob(hub.sql, { id: a, actor: 'x' });
      insertJob(hub.sql, { id: b, actor: 'x' });
      insertJob(hub.sql, { id: c, actor: 'x' });

      const first = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'routing_job', ref: a, idempotency_key: 'k-a' },
        }),
      );
      expect(first.status).toBe(201);
      const firstBody = (await first.json()) as any;
      const retry = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'routing_job', ref: a, idempotency_key: 'k-a' },
        }),
      );
      expect(retry.status).toBe(200);
      expect((await retry.json() as any).id).toBe(firstBody.id);

      const conflict = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'routing_job', ref: b, idempotency_key: 'k-a' },
        }),
      );
      expect(conflict.status).toBe(409);

      expect(
        (
          await hub.fetch(
            jsonReq(`/api/conversations/${conv.id}/links`, {
              cookie,
              csrf,
              body: { kind: 'routing_job', ref: b, idempotency_key: 'k-b' },
            }),
          )
        ).status,
      ).toBe(201);
      const over = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'routing_job', ref: c, idempotency_key: 'k-c' },
        }),
      );
      expect(over.status).toBe(409);
      expect(await over.json()).toMatchObject({
        error: 'storage_limit_exceeded',
      });

      const restarted = new Hub(state, testEnv);
      const listed = await restarted.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, { cookie }),
      );
      expect(listed.status).toBe(200);
      expect((await listed.json() as any).links).toHaveLength(2);
    });
    notePeak('idempotency-cap-restart');
  });

  it('counts link rows inside archived parent cleanup budget and leaves no orphans', async () => {
    await withHub(async hub => {
      const csrf = 'csrf-alice-ret';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const conv = await createGroup(hub, cookie, csrf, 'Expire me', []);
      const jobId = crypto.randomUUID();
      insertJob(hub.sql, { id: jobId, actor: 'x' });
      expect(
        (
          await hub.fetch(
            jsonReq(`/api/conversations/${conv.id}/links`, {
              cookie,
              csrf,
              body: { kind: 'routing_job', ref: jobId, idempotency_key: 'ret' },
            }),
          )
        ).status,
      ).toBe(201);
      expect(
        (
          await hub.fetch(
            jsonReq(`/api/conversations/${conv.id}/archive`, {
              cookie,
              csrf,
              body: {},
            }),
          )
        ).status,
      ).toBe(200);

      hub.conversationsService.limits.archivedConversationRetentionMs = 1;
      hub.sql.exec(
        "UPDATE conversations SET archived_at = '2000-01-01T00:00:00.000Z' WHERE id = ?",
        conv.id,
      );

      let totalDeleted = 0;
      for (let i = 0; i < 20; i++) {
        const step = hub.conversationsService.retentionCleanup(Date.now(), 1);
        expect(step.rowsDeleted).toBeLessThanOrEqual(1);
        totalDeleted += step.rowsDeleted;
        if (!step.hasMore && step.rowsDeleted === 0) break;
      }
      expect(totalDeleted).toBeGreaterThan(0);
      expect(
        Number(
          hub.sql
            .exec(
              'SELECT COUNT(*) AS n FROM conversation_links WHERE conversation_id = ?',
              conv.id,
            )
            .toArray()[0].n,
        ),
      ).toBe(0);
      expect(
        Number(
          hub.sql
            .exec('SELECT COUNT(*) AS n FROM conversations WHERE id = ?', conv.id)
            .toArray()[0].n,
        ),
      ).toBe(0);
    });
    notePeak('retention');
  });

  it('submits a routing task without a conversation_id', async () => {
    await withHub(async hub => {
      const csrf = 'csrf-alice-submit';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);
      const submitted = await hub.fetch(
        jsonReq('/api/fleet/tasks', {
          cookie,
          csrf,
          body: {
            prompt: 'Check browser state',
            approved_action: 'Read state only',
            needs: ['unavailable'],
            workspace: 'tasks',
            idempotency_key: crypto.randomUUID(),
          },
        }),
      );
      expect(submitted.status).toBe(201);
      const job = (await submitted.json()) as any;
      expect(job.id).toBeTruthy();
      expect(job.status).toBe('queued');
      expect(job).not.toHaveProperty('conversation_id');
    });
    notePeak('routing-submit');
  });
});


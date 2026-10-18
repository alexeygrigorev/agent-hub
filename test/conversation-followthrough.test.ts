import { describe, it, expect } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { Hub, BaseHub } from '../src/conversation-hub';
import type { Env } from '../src/env';
import { createSessionCookie } from '../src/auth/session';
import { sha256 } from '../src/auth/crypto';

const testEnv = env as unknown as Env;

async function cookieFor(
  sub: string,
  email: string = 'admin@fake.local',
  csrf: string = 'csrf',
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
    testEnv.HUB.idFromName('followthrough-' + crypto.randomUUID()),
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
        "INSERT OR IGNORE INTO agents (tag, project, role, machine_id, registered_at) VALUES ('reviewer-1', 'projA', 'reviewer', 'machine-B', ?)",
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
  if (opts.csrf) headers['x-csrf-token'] = opts.csrf;
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
    `INSERT INTO credentials (
      id, kind, token_hash, family_id, subject, actor, scopes, machine_id,
      created_by_sub, created_at, expires_at, revoked_at
    ) VALUES (?, 'agent', ?, ?, ?, ?, ?, ?, 'system', ?, ?, ?)`,
    opts.id,
    await sha256(opts.token),
    opts.id + '-fam',
    opts.actor,
    opts.actor,
    JSON.stringify(opts.scopes),
    opts.machineId,
    nowIso,
    opts.expiresAt ?? new Date(Date.now() + 86400000).toISOString(),
    opts.revokedAt ?? null,
  );
  return opts.token;
}

const validSource = { type: 'chat', reference: 'ref1', timestamp: new Date().toISOString() };

describe('M10 conversation follow-through authority and canonical links', () => {
  it('enforces real IntakeService ACL on intake_task links; conversation membership alone never grants target access', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      // 1. Create intake task
      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'task-key-1',
            project: 'projA',
            title: 'Fix issue',
            text: 'Need to fix bug',
            source: validSource,
          },
        }),
      );
      expect(createResp.status).toBe(200);
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // 2. Create conversation
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: {
            title: 'Task room',
            kind: 'group',
            idempotency_key: 'conv-key-1',
            members: ['projA:worker-1', 'projA:reviewer-1'],
          },
        }),
      );
      expect(convResp.status).toBe(201);
      const conv = (await convResp.json()) as any;

      // 3. Attach intake_task link as admin human
      const attachResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'intake_task',
            ref: taskId,
            idempotency_key: 'link-attach-1',
          },
        }),
      );
      expect(attachResp.status).toBe(201);
      const link = (await attachResp.json()) as any;
      expect(link.kind).toBe('intake_task');
      expect(link.ref).toBe(taskId);
      expect(link.target.available).toBe(true);
      expect(link.follow_through).toBeTruthy();

      // 4. Token for worker-1 without intake grant
      const workerTokenNoGrant = await seedAgentToken(hub.sql, {
        id: 'tok-w1',
        token: 'token-worker-1',
        actor: 'projA:worker-1',
        machineId: 'machine-A',
        scopes: ['bus:read', 'bus:send'],
      });

      // worker-1 reads link: member of conversation, but has NO intake grant -> target must be unavailable (opaque 404, no existence leak)
      const w1ReadResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links/${link.id}`, {
          token: workerTokenNoGrant,
        }),
      );
      expect(w1ReadResp.status).toBe(200);
      const w1Read = (await w1ReadResp.json()) as any;
      expect(w1Read.target.available).toBe(false);
      expect(w1Read.target.canonical_status).toBeNull();
      expect(w1Read.follow_through.facts.owner_acceptance.state).toBe('unknown');

      // 5. Grant intake access to worker-1
      const grantResp = await hub.fetch(
        jsonReq('/api/intake/grants', {
          cookie,
          csrf,
          body: {
            key: 'grant-w1',
            credential_id: 'tok-w1',
            scope: 'project',
            target: 'projA',
            access: 'read',
            expires_at: Date.now() + 3600000,
            source: validSource,
          },
        }),
      );
      expect(grantResp.status).toBe(200);

      // Now worker-1 with valid grant reads link -> target available
      const w1ReadWithGrant = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links/${link.id}`, {
          token: workerTokenNoGrant,
        }),
      );
      expect(w1ReadWithGrant.status).toBe(200);
      const w1View = (await w1ReadWithGrant.json()) as any;
      expect(w1View.target.available).toBe(true);
      expect(w1View.target.canonical_status).toBe('open');
    });
  });

  it('strictly enforces the negative acceptance matrix against heuristic inference', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      // 1. Create intake task with claimed owner in payload
      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'neg-task-1',
            project: 'projA',
            title: 'Fix issue',
            text: 'Need to fix bug',
            source: validSource,
          },
        }),
      );
      expect(createResp.status).toBe(200);
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // Create conversation and link task
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: {
            title: 'Neg matrix room',
            kind: 'group',
            idempotency_key: 'conv-neg-1',
          },
        }),
      );
      const conv = (await convResp.json()) as any;

      const attachResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'intake_task',
            ref: taskId,
            idempotency_key: 'neg-link-1',
          },
        }),
      );
      const link = (await attachResp.json()) as any;

      // Negative Check 1: Task owner text alone is NOT owner acceptance
      expect(link.follow_through.facts.owner_acceptance.state).toBe('unknown');
      expect(link.follow_through.facts.owner_acceptance.source_identity).toBeNull();

      // Negative Check 2: Process exit is NOT outcome acceptance
      expect(link.follow_through.facts.accepted_outcome.state).toBe('unknown');

      // Negative Check 3: Routing job labeled review is NOT independent review
      expect(link.follow_through.facts.independent_review.state).toBe('unknown');

      // Negative Check 4: Reading a message / advancing read cursor is NOT delivery
      const readResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/read`, {
          cookie,
          csrf,
          body: { seq: 0 },
        }),
      );
      expect(readResp.status).toBe(200);

      const ftResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/follow-through`, {
          cookie,
        }),
      );
      const ft = (await ftResp.json()) as any;
      expect(ft.links[0].facts.human_delivery.state).toBe('unknown');
    });
  });

  it('records verified authority events with CAS and enforces reviewer independence & 40-char SHA pin', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      // Create intake task
      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'auth-task-1',
            project: 'projA',
            title: 'Verify task',
            text: 'Detailed task description',
            source: validSource,
          },
        }),
      );
      expect(createResp.status).toBe(200);
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // 1. Append owner_acceptance event via canonical intake endpoint
      const acceptResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-accept-1',
            expected_version: 1,
            event: {
              kind: 'owner_acceptance',
              text: 'Accepted by operator',
              verdict: 'accepted',
              source: validSource,
            },
          },
        }),
      );
      expect(acceptResp.status).toBe(200);

      // 2. Reject independent_review if reviewer is same as task owner or creator
      const badReviewResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-rev-bad',
            expected_version: 2,
            event: {
              kind: 'independent_review',
              reviewer: 'human:alice', // Alice created the task, cannot self-review
              verdict: 'approved',
              source_sha: 'a'.repeat(40),
              source: validSource,
            },
          },
        }),
      );
      expect(badReviewResp.status).toBe(403);

      // 3. Reject independent_review if source_sha is not a 40-char hex string
      const badShaResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-rev-bad-sha',
            expected_version: 2,
            event: {
              kind: 'independent_review',
              reviewer: 'projA:reviewer-1',
              verdict: 'approved',
              source_sha: 'invalid-short-sha',
              source: validSource,
            },
          },
        }),
      );
      expect(badShaResp.status).toBe(400);

      // 4. Accept valid independent_review with exact 40-char SHA pin and independent reviewer
      const revToken = await seedAgentToken(hub.sql, {
        id: 'tok-rev-1',
        token: 'token-reviewer-1',
        actor: 'projA:reviewer-1',
        machineId: 'machine-B',
        scopes: ['bus:read', 'bus:send'],
      });
      await hub.fetch(
        jsonReq('/api/intake/grants', {
          cookie,
          csrf,
          body: {
            key: 'grant-rev-1',
            credential_id: 'tok-rev-1',
            scope: 'project',
            target: 'projA',
            access: 'write',
            expires_at: Date.now() + 3600000,
            source: validSource,
          },
        }),
      );

      const validSha = 'b468b5e1b0e6562b5f87c86b9cd8f1a7bc6f693c';
      const goodReviewResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          token: revToken,
          body: {
            key: 'evt-rev-good',
            expected_version: 2,
            event: {
              kind: 'independent_review',
              reviewer: 'projA:reviewer-1',
              verdict: 'approved',
              source_sha: validSha,
              source: validSource,
            },
          },
        }),
      );
      expect(goodReviewResp.status).toBe(200);
      const reviewTaskResp = await hub.fetch(jsonReq(`/api/intake/tasks/${taskId}`, { cookie }));
      const reviewTaskData = (await reviewTaskResp.json()) as any;
      const history = reviewTaskData.task.payload_data?.history ?? [];
      const reviewEventId = history.find((e: any) => e.kind === 'independent_review')?.id;
      expect(reviewEventId).toBeTruthy();

      // Adversarial check 1: Reject bogus review_ref even if source_sha matches
      const badReviewRefResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-outcome-bad-ref',
            expected_version: 3,
            event: {
              kind: 'accepted_outcome',
              outcome_ref: 'commit-' + validSha,
              source_sha: validSha,
              review_ref: 'does-not-exist',
              verdict: 'accepted',
              source: validSource,
            },
          },
        }),
      );
      expect(badReviewRefResp.status).toBe(409);

      // Adversarial check 2: Contributor admin cannot invent reviewer label
      const contributorReviewResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-rev-invented',
            expected_version: 3,
            event: {
              kind: 'independent_review',
              reviewer: 'agent:invented-independent',
              verdict: 'approved',
              source_sha: validSha,
              source: validSource,
            },
          },
        }),
      );
      expect(contributorReviewResp.status).toBe(403);

      // 5. Append accepted_outcome with structured source_sha and exact review_ref
      const outcomeResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-outcome-1',
            expected_version: 3,
            event: {
              kind: 'accepted_outcome',
              outcome_ref: 'commit-' + validSha,
              source_sha: validSha,
              review_ref: reviewEventId,
              verdict: 'accepted',
              source: validSource,
            },
          },
        }),
      );
      expect(outcomeResp.status).toBe(200);

      // Positive end-to-end first action: genuine Intake work_action event as authenticated Hub work
      const actionResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-action-1',
            expected_version: 4,
            event: {
              kind: 'work_action',
              action_type: 'execution_start',
              text: 'Owner execution started',
              source: validSource,
            },
          },
        }),
      );
      expect(actionResp.status).toBe(200);

      // 6. Append human_delivery bound to exact accepted outcome and source_sha
      const deliveryResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-delivery-1',
            expected_version: 5,
            event: {
              kind: 'human_delivery',
              recipient: 'human:alice',
              outcome_ref: 'commit-' + validSha,
              source_sha: validSha,
              text: 'Delivered',
              source: validSource,
            },
          },
        }),
      );
      expect(deliveryResp.status).toBe(200);

      // Link to conversation and verify all facts are typed, verified, and source-pinned
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: {
            title: 'Auth room',
            kind: 'group',
            idempotency_key: 'conv-auth-1',
          },
        }),
      );
      const conv = (await convResp.json()) as any;

      const linkResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'intake_task',
            ref: taskId,
            idempotency_key: 'auth-link-1',
          },
        }),
      );
      const link = (await linkResp.json()) as any;
      const facts = link.follow_through.facts;

      expect(facts.owner_acceptance.state).toBe('known');
      expect(facts.owner_acceptance.source_identity).toBe('human:alice');

      expect(facts.first_action.state).toBe('known');
      expect(facts.first_action.authority).toBe('canonical_hub_action_receipt');
      expect(facts.first_action.source_identity).toBe('human:alice');
      expect(facts.first_action.observation.hub_action).toBe(true);

      expect(facts.independent_review.state).toBe('known');
      expect(facts.independent_review.source_identity).toBe('projA:reviewer-1');
      expect(facts.independent_review.observation.source_sha).toBe(validSha);

      expect(facts.accepted_outcome.state).toBe('known');
      expect(facts.accepted_outcome.observation.outcome_ref).toBe('commit-' + validSha);

      expect(facts.human_delivery.state).toBe('known');
      expect(facts.human_delivery.source_identity).toBe('human:alice');

      expect(link.follow_through.summary.complete).toBe(true);
    });
  });

  it('regression: changes_requested review plus accepted outcome must not become complete', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'cr-task-1',
            project: 'projA',
            title: 'Changes requested task',
            text: 'Task under review',
            source: validSource,
          },
        }),
      );
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // 1. Owner acceptance
      await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-cr-owner',
            expected_version: 1,
            event: { kind: 'owner_acceptance', text: 'Accepted', verdict: 'accepted', source: validSource },
          },
        }),
      );

      // 2. Review with verdict changes_requested by independent reviewer
      const revToken = await seedAgentToken(hub.sql, {
        id: 'tok-rev-cr',
        token: 'token-reviewer-cr',
        actor: 'projA:reviewer-1',
        machineId: 'machine-B',
        scopes: ['bus:read', 'bus:send'],
      });
      await hub.fetch(
        jsonReq('/api/intake/grants', {
          cookie,
          csrf,
          body: {
            key: 'grant-rev-cr',
            credential_id: 'tok-rev-cr',
            scope: 'project',
            target: 'projA',
            access: 'write',
            expires_at: Date.now() + 3600000,
            source: validSource,
          },
        }),
      );

      const validSha = 'b468b5e1b0e6562b5f87c86b9cd8f1a7bc6f693c';
      await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          token: revToken,
          body: {
            key: 'evt-cr-rev',
            expected_version: 2,
            event: {
              kind: 'independent_review',
              reviewer: 'projA:reviewer-1',
              verdict: 'changes_requested',
              source_sha: validSha,
              source: validSource,
            },
          },
        }),
      );

      // 3. Accepted outcome on an unrelated reference
      await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-cr-outcome',
            expected_version: 3,
            event: {
              kind: 'accepted_outcome',
              outcome_ref: 'commit-unrelated-output',
              verdict: 'accepted',
              source: validSource,
            },
          },
        }),
      );

      // Link to conversation
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: { title: 'CR room', kind: 'group', idempotency_key: 'conv-cr-1' },
        }),
      );
      const conv = (await convResp.json()) as any;

      const linkResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'intake_task', ref: taskId, idempotency_key: 'cr-link-1' },
        }),
      );
      const link = (await linkResp.json()) as any;

      // Proof check: changes_requested must NOT result in complete = true
      expect(link.follow_through.facts.independent_review.state).toBe('refused');
      expect(link.follow_through.summary.complete).toBe(false);
      expect(link.follow_through.summary.blocked).toBe(true);
    });
  });

  it('regression: stale review unpinning when outcome or subsequent commit changes source SHA', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'stale-task-1',
            project: 'projA',
            title: 'Stale review task',
            text: 'Source evolves',
            source: validSource,
          },
        }),
      );
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      const sha1 = '1111111111111111111111111111111111111111';
      const sha2 = '2222222222222222222222222222222222222222';

      // 1. Review pinned to sha1 by independent reviewer
      const revToken = await seedAgentToken(hub.sql, {
        id: 'tok-rev-stale',
        token: 'token-reviewer-stale',
        actor: 'projA:reviewer-1',
        machineId: 'machine-B',
        scopes: ['bus:read', 'bus:send'],
      });
      await hub.fetch(
        jsonReq('/api/intake/grants', {
          cookie,
          csrf,
          body: {
            key: 'grant-rev-stale',
            credential_id: 'tok-rev-stale',
            scope: 'project',
            target: 'projA',
            access: 'write',
            expires_at: Date.now() + 3600000,
            source: validSource,
          },
        }),
      );
      await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          token: revToken,
          body: {
            key: 'evt-stale-rev',
            expected_version: 1,
            event: {
              kind: 'independent_review',
              reviewer: 'projA:reviewer-1',
              verdict: 'approved',
              source_sha: sha1,
              source: validSource,
            },
          },
        }),
      );

      // 2. Later commit or accepted outcome pinned to sha2
      await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-stale-commit',
            expected_version: 2,
            event: {
              kind: 'commit',
              reference: 'commit-' + sha2,
              source: validSource,
            },
          },
        }),
      );

      // Link to conversation
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: { title: 'Stale room', kind: 'group', idempotency_key: 'conv-stale-1' },
        }),
      );
      const conv = (await convResp.json()) as any;

      const linkResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: { kind: 'intake_task', ref: taskId, idempotency_key: 'stale-link-1' },
        }),
      );
      const link = (await linkResp.json()) as any;

      // Prior review on sha1 is unpinned and stale!
      expect(link.follow_through.facts.independent_review.state).toBe('unknown');
      expect(link.follow_through.facts.independent_review.authority).toBe('stale_review_unpinned');
      expect(link.follow_through.summary.complete).toBe(false);
    });
  });

  it('maintains ask-ledger obligations with deadline, reversible default, and late-answer reconciliation', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      // Create intake task
      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'ob-task-1',
            project: 'projA',
            title: 'Obligation task',
            text: 'Need confirmation',
            source: validSource,
          },
        }),
      );
      expect(createResp.status).toBe(200);
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // Create conversation & link
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: {
            title: 'Obligation room',
            kind: 'group',
            idempotency_key: 'conv-ob-1',
          },
        }),
      );
      const conv = (await convResp.json()) as any;
      await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'intake_task',
            ref: taskId,
            idempotency_key: 'ob-link-1',
          },
        }),
      );

      // Append obligation event with deadline 100 seconds in the future
      const now = Date.now();
      const deadline = now + 100_000;
      const obResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-ob-1',
            expected_version: 1,
            event: {
              kind: 'obligation',
              obligation_id: 'ob-1',
              text: 'May we proceed with plan B?',
              default_action: 'proceed',
              deadline,
              source: validSource,
            },
          },
        }),
      );
      expect(obResp.status).toBe(200);

      // Follow-through reflects open obligation
      const ftResp1 = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/follow-through`, { cookie }),
      );
      const ft1 = (await ftResp1.json()) as any;
      expect(ft1.links[0].facts.due_obligation.state).toBe('open');
      expect(ft1.links[0].facts.due_obligation.observation.default_choice).toBe('proceed');
      expect(ft1.links[0].overdue_obligation).toBe(false);

      // Proceed before deadline is rejected!
      const earlyProceedResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-early-proceed',
            expected_version: 2,
            event: {
              kind: 'obligation_response',
              ref: 'ob-1',
              response_type: 'proceed',
              text: 'Too early',
              source: validSource,
            },
          },
        }),
      );
      expect(earlyProceedResp.status).toBe(409);

      // Duplicate obligation ID is rejected!
      const dupObResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-dup-ob',
            expected_version: 2,
            event: {
              kind: 'obligation',
              obligation_id: 'ob-1',
              text: 'Duplicate question',
              default_action: 'proceed',
              deadline: now + 500_000,
              source: validSource,
            },
          },
        }),
      );
      expect(dupObResp.status).toBe(409);

      // Append valid answer
      const respResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-ob-resp-1',
            expected_version: 2,
            event: {
              kind: 'obligation_response',
              ref: 'ob-1',
              response_type: 'answer',
              text: 'Yes, proceed',
              source: validSource,
            },
          },
        }),
      );
      expect(respResp.status).toBe(200);

      // Follow-through reflects answered obligation
      const ftResp2 = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/follow-through`, { cookie }),
      );
      const ft2 = (await ftResp2.json()) as any;
      expect(ft2.links[0].facts.due_obligation.state).toBe('answered');

      // Duplicate answer is rejected!
      const dupResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-dup-resp',
            expected_version: 3,
            event: {
              kind: 'obligation_response',
              ref: 'ob-1',
              response_type: 'answer',
              text: 'Second answer',
              source: validSource,
            },
          },
        }),
      );
      expect(dupResp.status).toBe(409);
    });
  });

  it('evaluates overdue obligations in scheduled backlog with recipient grant revalidation', async () => {
    await withHub(async (hub) => {
      const csrf = 'csrf-alice';
      const cookie = await cookieFor('alice', 'admin@fake.local', csrf);

      // Create intake task
      const createResp = await hub.fetch(
        jsonReq('/api/intake/tasks', {
          cookie,
          csrf,
          body: {
            key: 'overdue-task-1',
            project: 'projA',
            title: 'Overdue task',
            text: 'Time-sensitive inquiry',
            source: validSource,
          },
        }),
      );
      expect(createResp.status).toBe(200);
      const task = (await createResp.json()) as any;
      const taskId = task.task.id;

      // Create conversation & link
      const convResp = await hub.fetch(
        jsonReq('/api/conversations', {
          cookie,
          csrf,
          body: {
            title: 'Overdue room',
            kind: 'group',
            idempotency_key: 'conv-od-1',
          },
        }),
      );
      const conv = (await convResp.json()) as any;
      await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/links`, {
          cookie,
          csrf,
          body: {
            kind: 'intake_task',
            ref: taskId,
            idempotency_key: 'od-link-1',
          },
        }),
      );

      // Append obligation with a deadline in the PAST
      const pastDeadline = Date.now() - 50_000;
      const pastResp = await hub.fetch(
        jsonReq(`/api/intake/tasks/${taskId}/events`, {
          cookie,
          csrf,
          body: {
            key: 'evt-ob-past',
            expected_version: 1,
            event: {
              kind: 'obligation',
              obligation_id: 'ob-past',
              text: 'Past due question',
              default_action: 'proceed',
              deadline: pastDeadline,
              source: validSource,
            },
          },
        }),
      );
      expect(pastResp.status).toBe(200);

      // Check follow-through: overdue_obligation must be true
      const ftResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/follow-through`, { cookie }),
      );
      const ft = (await ftResp.json()) as any;
      expect(ft.links[0].overdue_obligation).toBe(true);
      expect(ft.links[0].facts.due_obligation.state).toBe('overdue');

      // Check countOverdueObligationsForConversation via service without targetActor (direct admin path)
      const countDirect = hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
        conv.id,
        Date.now(),
      );
      expect(countDirect).toBe(1);

      // Signed-cookie admin human: authority succeeds without credentials table row
      const countHumanAdmin = hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
        conv.id,
        Date.now(),
        'human:alice',
        null,
      );
      expect(countHumanAdmin).toBe(1);

      // Unregistered / non-admin human: throws target_authority_unavailable
      expect(() => {
        hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
          conv.id,
          Date.now(),
          'human:unknown-user',
          null,
        );
      }).toThrow('target_authority_unavailable');

      // Seed worker token
      await seedAgentToken(hub.sql, {
        id: 'tok-w1',
        token: 'token-worker-1',
        actor: 'projA:worker-1',
        machineId: 'machine-A',
        scopes: ['bus:read', 'bus:send'],
      });

      // Target agent WITHOUT target_credential_id: throws target_authority_unavailable
      expect(() => {
        hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
          conv.id,
          Date.now(),
          'projA:worker-1',
          null,
        );
      }).toThrow('target_authority_unavailable');

      // Target actor worker-1 with credential but WITHOUT grant to intake task: throws target_authority_unavailable
      expect(() => {
        hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
          conv.id,
          Date.now(),
          'projA:worker-1',
          'tok-w1',
        );
      }).toThrow('target_authority_unavailable');

      // Grant worker-1 access to projA
      const createGrantResp = await hub.fetch(
        jsonReq('/api/intake/grants', {
          cookie,
          csrf,
          body: {
            key: 'grant-w1-od',
            credential_id: 'tok-w1',
            scope: 'project',
            target: 'projA',
            access: 'read',
            expires_at: Date.now() + 3600000,
            source: validSource,
          },
        }),
      );
      expect(createGrantResp.status).toBe(200);
      const intakeGrantData = (await createGrantResp.json()) as any;
      const intakeGrantId = intakeGrantData.grant.id;

      // Now with active grant, worker-1 sees the overdue obligation
      const countWithGrant = hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
        conv.id,
        Date.now(),
        'projA:worker-1',
        'tok-w1',
      );
      expect(countWithGrant).toBe(1);

      // Positive migrated-bare actor + qualified registry_actor fixture:
      // A migrated credential retains bare actor 'worker-1' while registry_actor is qualified 'projA:worker-1'.
      hub.sql.exec("UPDATE credentials SET actor = 'worker-1' WHERE id = 'tok-w1'");
      const countMigrated = hub.conversationsService.linksHandler.followThrough.countOverdueObligationsForConversation(
        conv.id,
        Date.now(),
        'projA:worker-1',
        'tok-w1',
      );
      expect(countMigrated).toBe(1);

      // Add worker-1 as conversation member and create automation grant
      await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/members`, {
          cookie,
          csrf,
          body: {
            actor: 'projA:worker-1',
            role: 'member',
          },
        }),
      );

      const grantResp = await hub.fetch(
        jsonReq(`/api/conversations/${conv.id}/automation`, {
          method: 'PUT',
          cookie,
          csrf,
          body: {
            check: 'followup',
            target_actor: 'projA:worker-1',
            body: 'Please address overdue obligation',
            on: 'fail',
            expires_at: Date.now() + 600000,
          },
        }),
      );
      expect(grantResp.status).toBe(200);
      const grantData = (await grantResp.json()) as any;

      // Evaluate scheduled backlog via ConversationsService: status must be 'fail' due to overdue obligation
      const evalResult = hub.conversationsService.evaluateScheduledBacklog({
        actor: 'projA:worker-1',
        checkName: 'followup',
        maxAgeS: 3600,
        conversationId: conv.id,
      });
      expect(evalResult.status).toBe('fail');
      expect(evalResult.detail).toContain('1 overdue backlog item(s) (1 obligation(s))');

      // Post scheduled follow-up: reserve check slot in hub_check_slots matching the intent
      const followUpIntent = {
        conversationId: conv.id,
        actor: 'projA:worker-1',
        grantId: grantData.grant_id,
        checkName: 'followup',
        idempotencyKey: 'check-slot1',
        body: 'Overdue notice: action required',
        bodyTemplate: 'Please address overdue obligation',
        on: 'fail' as const,
      };
      hub.sql.exec(
        `INSERT INTO hub_check_slots (name, slot_key, next_at, check_json, intent_json, state, lease_until)
         VALUES (?, ?, ?, '{}', ?, 'dispatch', ?)`,
        followUpIntent.checkName,
        'slot1',
        Date.now(),
        JSON.stringify(followUpIntent),
        Date.now() + 60000,
      );

      const followUpMsgId = hub.conversationsService.postScheduledFollowUp(followUpIntent);
      expect(typeof followUpMsgId).toBe('string');

      // Idempotent retry returns identical message ID
      const retryMsgId = hub.conversationsService.postScheduledFollowUp(followUpIntent);
      expect(retryMsgId).toBe(followUpMsgId);

      // Revoking worker-1 intake grant causes evaluateScheduledBacklog to fail closed with error status (target_authority_unavailable)
      const revokeResp = await hub.fetch(
        jsonReq(`/api/intake/grants/${intakeGrantId}/revoke`, {
          cookie,
          csrf,
          body: {
            key: 'revoke-w1-od',
            source: validSource,
          },
        }),
      );
      expect(revokeResp.status).toBe(200);

      const revokedGrantEval = hub.conversationsService.evaluateScheduledBacklog({
        actor: 'projA:worker-1',
        checkName: 'followup',
        maxAgeS: 3600,
        conversationId: conv.id,
      });
      expect(revokedGrantEval.status).toBe('error');
      expect(revokedGrantEval.detail).toBe('target_authority_unavailable');

      // Revoking worker-1 credential causes evaluateScheduledBacklog to fail closed with unauthoried error
      hub.sql.exec('UPDATE credentials SET revoked_at = ? WHERE id = ?', new Date().toISOString(), 'tok-w1');
      expect(() =>
        hub.conversationsService.evaluateScheduledBacklog({
          actor: 'projA:worker-1',
          checkName: 'followup',
          maxAgeS: 3600,
          conversationId: conv.id,
        }),
      ).toThrow();
    });
  });
});

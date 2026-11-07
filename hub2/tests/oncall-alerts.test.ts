import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { mkdir, mkdtemp, rm } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createFixture, restartFixture } from './node-fixture.mjs';

const root = resolve(import.meta.dirname, '../..');
await mkdir(resolve(root, '.local/tmp'), { recursive: true });
const { default: entry, ControlDO, ProjectDO, MachineDO, AccountDO, MailboxDO } =
  await import('../production-worker.ts');
const dataDir = await mkdtemp(resolve(root, '.local/tmp/oncall-alerts-'));
const spec = {
  root,
  entry,
  classes: {
    CONTROL: ControlDO,
    PROJECTS: ProjectDO,
    MACHINES: MachineDO,
    ACCOUNTS: AccountDO,
    MAILBOXES: MailboxDO,
  },
  env: {
    HUB2_RELEASE_MODE: 'bootstrap',
    HUB2_BOOTSTRAP_TOKEN: 'oncall-alerts-bootstrap-token-000000000000000000',
  },
  dataDir,
};
let fixture = await createFixture(spec);
after(async () => {
  await fixture?.dispose();
  await rm(dataDir, { recursive: true, force: true });
});

async function json(response: Response) {
  return response.json() as Promise<Record<string, any>>;
}

test('bootstrap config, idempotent ingest, and leased queue survive a Node restart', async () => {
  const configured = await fixture.fetch('/api/oncall-alerts/config', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${spec.env.HUB2_BOOTSTRAP_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: '{}',
  });
  assert.equal(configured.status, 201);
  const tokens = await json(configured);
  assert.equal(typeof tokens.ingest_token, 'string');
  assert.equal(typeof tokens.poll_token, 'string');

  const duplicateConfig = await fixture.fetch('/api/oncall-alerts/config', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${spec.env.HUB2_BOOTSTRAP_TOKEN}`,
      'Content-Type': 'application/json',
    },
    body: '{}',
  });
  assert.equal(duplicateConfig.status, 409);

  const deliveryKey = 'a'.repeat(64);
  const payload = {
    from: 'alert@example.invalid',
    to: 'dev@dtcdev.click',
    subject: '[TEST] durable oncall alert',
    message_id: '<oncall-alert-1@example.invalid>',
    text: 'Synthetic test alert',
  };
  const ingest = () => fixture.fetch('/webhooks/oncall', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${tokens.ingest_token}`,
      'Content-Type': 'application/json',
      'X-Delivery-Id': deliveryKey,
    },
    body: JSON.stringify(payload),
  });
  const first = await ingest();
  assert.equal(first.status, 202);
  const accepted = await json(first);
  assert.equal(accepted.status, 'pending');
  assert.equal(accepted.replay, false);

  const replay = await ingest();
  assert.equal(replay.status, 202);
  const replayed = await json(replay);
  assert.equal(replayed.delivery_id, accepted.delivery_id);
  assert.equal(replayed.replay, true);

  const conflict = await fixture.fetch('/webhooks/oncall', {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${tokens.ingest_token}`,
      'Content-Type': 'application/json',
      'X-Delivery-Id': deliveryKey,
    },
    body: JSON.stringify({ ...payload, subject: 'different content' }),
  });
  assert.equal(conflict.status, 409);

  fixture = await restartFixture(fixture, spec);
  const offersResponse = await fixture.fetch('/api/oncall-alerts/offers', {
    headers: { Authorization: `Bearer ${tokens.poll_token}` },
  });
  assert.equal(offersResponse.status, 200);
  const offers = await json(offersResponse);
  assert.equal(offers.offers.length, 1);
  assert.equal(offers.offers[0].payload.subject, payload.subject);

  const offer = offers.offers[0];
  const ack = await fixture.fetch(`/api/oncall-alerts/${offer.delivery_id}/ack`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${tokens.poll_token}`,
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ lease_token: offer.lease_token }),
  });
  assert.equal(ack.status, 200);
  assert.equal((await json(ack)).saved, true);

  const replayAfterAck = await ingest();
  assert.equal(replayAfterAck.status, 202);
  const savedReplay = await json(replayAfterAck);
  assert.equal(savedReplay.status, 'saved');
  assert.equal(savedReplay.replay, true);

  const empty = await fixture.fetch('/api/oncall-alerts/offers', {
    headers: { Authorization: `Bearer ${tokens.poll_token}` },
  });
  assert.equal((await json(empty)).offers.length, 0);
});

test('oncall ingest and offers refuse missing bearer credentials', async () => {
  assert.equal((await fixture.fetch('/webhooks/oncall', {
    method: 'POST',
    headers: { 'X-Delivery-Id': 'unauthorized' },
    body: '{}',
  })).status, 401);
  assert.equal((await fixture.fetch('/api/oncall-alerts/offers')).status, 401);
});

import { env, runInDurableObject } from 'cloudflare:test';
import { describe, it, expect, beforeEach } from 'vitest';
import { Env } from '../src/env';
import { Hub } from '../src/hub';
import { BUS_SCHEMA_SQL } from '../src/bus';
import { SCHEMA_SQL } from '../src/schema';
import { sha256 } from '../src/auth/crypto';
import { doFetch } from './m1-focus-setup';
import {
  formatNativeActor,
  isWorkMessageKind,
  parseNativeActor,
  parseWorkTaskString,
  redactClaimData,
  redactForBus,
  taskIdFromWorkKey,
  validateWorkClaimData,
  workKeyToScope,
} from '../src/work_claim';

const defaultEnv = env as unknown as Env;

// Phase 1 work-claim protocol: strict hub.work-claim.v1 validation, secret
// redaction, host-attested native sync, and activity surfacing. No competing
// protocol is invented: native rows reuse the existing claims/messages tables.
describe('work-claim helpers', () => {
  const validClaim = () => ({
    schema: 'hub.work-claim.v1',
    event: 'claim',
    claim_id: 'clm_01K8AN7D4XE9P0T2V5WYH6JM',
    seq: 1,
    work_key: 'task:7142fe0b-d45c-4a3a-b92b-726ecc10575a',
    alias_keys: ['intake:operator:7142fe0b'],
    project: 'agent-hub',
    workspace: '/home/alexey/git/agent-hub',
    worktree: '/home/alexey/git/agent-hub-wt-claim-impl',
    mode: 'edit',
    scopes: ['src/hub.ts', 'host/agent_hub_host'],
    holder: { machine_id: 'rmthz', session_id: 'f46404e5', tag: 'claim-impl', engine: 'opencode' },
    lineage: { task_id: '7142fe0b-d45c-4a3a-b92b-726ecc10575a' },
    lease_ttl_sec: 1800,
    summary: 'Claiming work-claim bridge Phase 1',
  });

  it('accepts a complete valid payload', () => {
    expect(validateWorkClaimData(validClaim())).toBeNull();
  });

  it('accepts a minimal payload without optional fields', () => {
    const minimal: any = { ...validClaim() };
    for (const k of ['alias_keys', 'worktree', 'scopes', 'lineage', 'summary']) delete minimal[k];
    expect(validateWorkClaimData(minimal)).toBeNull();
  });

  it('rejects schema violations with reasons', () => {
    const cases: Array<[string, (c: any) => void]> = [
      ['schema must be hub.work-claim.v1', (c) => { c.schema = 'hub.other.v9'; }],
      ['event invalid', (c) => { c.event = 'start'; }],
      ['claim_id invalid', (c) => { c.claim_id = 'c67d381d-uuid'; }],
      ['claim_id invalid', (c) => { c.claim_id = 'clm_01J8Z9DEIIGN7142'; }], // I is outside the design alphabet
      ['seq invalid', (c) => { c.seq = 0; }],
      ['work_key invalid', (c) => { c.work_key = 'TASK:upper'; }],
      ['alias_keys entry invalid', (c) => { c.alias_keys = ['bogus']; }],
      ['project invalid', (c) => { c.project = ''; }],
      ['workspace invalid', (c) => { c.workspace = ''; }],
      ['mode invalid', (c) => { c.mode = 'write'; }],
      ['scope entry not workspace-relative: ../escape', (c) => { c.scopes = ['../escape']; }],
      ['holder must be an object', (c) => { c.holder = 'native:x'; }],
      ['holder has forbidden field token', (c) => { c.holder = { ...c.holder, token: 'hubt_x' }; }],
      ['lineage.stolen_reason invalid', (c) => { c.lineage = { stolen_reason: 'nope' }; }],
      ['lease_ttl_sec invalid', (c) => { c.lease_ttl_sec = 30; }],
      ['outcome.fix_commit_sha invalid', (c) => { c.outcome = { fix_commit_sha: 'ZZZ' }; }],
      ['summary invalid', (c) => { c.summary = 'x'.repeat(281); }],
      ['forbidden field raw_cli_args', (c) => { c.raw_cli_args = '--token hubt_x'; }],
    ];
    for (const [reason, mutate] of cases) {
      const c: any = validClaim();
      mutate(c);
      expect(validateWorkClaimData(c), JSON.stringify(c)).toBe(reason);
    }
  });

  it('maps work keys into the @work scope namespace', () => {
    expect(workKeyToScope('task:7142fe0b-d45c-4a3a-b92b-726ecc10575a'))
      .toBe('@work/task/7142fe0b-d45c-4a3a-b92b-726ecc10575a');
    expect(workKeyToScope('ci:alexeygrigoriev/agent-hub:wf:ci.yml:branch:main'))
      .toBe('@work/ci/alexeygrigoriev/agent-hub/wf/ci.yml/branch/main');
    expect(workKeyToScope('bogus')).toBeNull();
    expect(workKeyToScope('task:has space')).toBeNull();
    expect(workKeyToScope('task:UPPER')).toBe('@work/task/UPPER'); // case-sensitive names allowed
  });

  it('extracts task ids for intake linking', () => {
    expect(taskIdFromWorkKey('task:7142fe0b-d45c-4a3a-b92b-726ecc10575a'))
      .toBe('7142fe0b-d45c-4a3a-b92b-726ecc10575a');
    expect(taskIdFromWorkKey('ci:owner/repo:run:123')).toBeNull();
    expect(taskIdFromWorkKey(42)).toBeNull();
  });

  it('parses a work join task strings per the 9.1 convention', () => {
    const p = parseWorkTaskString('task:d8d475bb-1b51-44e6-afc1-81faf633a4f4 [claim=clm_01K8AN7D4XE9P0T2V5WYH6JM] [ttl=1800s] | Surface messaging');
    expect(p.workKey).toBe('task:d8d475bb-1b51-44e6-afc1-81faf633a4f4');
    expect(p.claimId).toBe('clm_01K8AN7D4XE9P0T2V5WYH6JM');
    expect(p.ttlSec).toBe(1800);
    expect(p.summary).toBe('Surface messaging');
    const free = parseWorkTaskString('Urgent owned host recovery');
    expect(free.workKey).toBeNull();
    expect(free.summary).toBe('');
  });

  it('formats and parses host-attested synthetic actors', () => {
    expect(formatNativeActor('rmthz', 'claim-impl', 'f46404e5-8b9b-459f-bf74-6eb5a7bd3e4d'))
      .toBe('native:rmthz:claim-impl[f46404e5]');
    expect(parseNativeActor('native:rmthz:claim-impl[f46404e5]')).toEqual({
      machineId: 'rmthz', tag: 'claim-impl', sessionShort: 'f46404e5',
    });
    expect(parseNativeActor('agent-hub:worker-1')).toBeNull();
    expect(parseNativeActor('native:rmthz:')).toBeNull();
  });

  it('recognizes only the six work.* lifecycle kinds', () => {
    for (const k of ['work.claim', 'work.heartbeat', 'work.complete', 'work.abandon', 'work.steal', 'work.handoff']) {
      expect(isWorkMessageKind(k)).toBe(true);
    }
    expect(isWorkMessageKind('note')).toBe(false);
    expect(isWorkMessageKind('work.bogus')).toBe(false);
  });

  it('redacts secrets while preserving paths and identifiers', () => {
    expect(redactForBus('token=hubt_secret123 body')).toBe('token=[REDACTED] body');
    expect(redactForBus('key ghp_abcdefghijklmnopqrstuvwxyz012345 end')).toBe('key [REDACTED_TOKEN] end');
    expect(redactForBus('Authorization: Bearer abcdef0123456789 rest')).toBe('Authorization: Bearer [REDACTED] rest');
    expect(redactForBus('AKIAIOSFODNN7EXAMPLE here')).toBe('[REDACTED_AWS_KEY] here');
    expect(redactForBus('-----BEGIN RSA PRIVATE KEY-----\nabc\n-----END RSA PRIVATE KEY-----'))
      .toBe('[REDACTED_PRIVATE_KEY]');
    expect(redactForBus('see https://user:pass@example.com/hook?token=abc123&x=1 done'))
      .toBe('see https://example.com/hook?token=[REDACTED]&x=1 done');
    expect(redactForBus('blob ' + 'a'.repeat(48) + ' tail')).toBe('blob [REDACTED_HIGH_ENTROPY] tail');
    // Non-secrets survive: paths, task keys, UUIDs, short SHAs, counts.
    expect(redactForBus('COMPLETE task:7142fe0b report=/home/alexey/tmp/agent-reports/hub-work-claim-protocol.md'))
      .toBe('COMPLETE task:7142fe0b report=/home/alexey/tmp/agent-reports/hub-work-claim-protocol.md');
    expect(redactForBus('session b3c55563-5238-43b2-a02c-d8fdbc55d1e7 c87719b 280')).toBe(
      'session b3c55563-5238-43b2-a02c-d8fdbc55d1e7 c87719b 280');
    expect(redactForBus('sha ' + 'a'.repeat(40) + ' kept')).toBe('sha ' + 'a'.repeat(40) + ' kept');
    expect(redactForBus(42)).toBe(42);
  });

  it('deep-redacts claim payloads', () => {
    const clean: any = redactClaimData({
      ...validClaim(),
      summary: 'fixing with token=hubt_abc now',
      holder: { machine_id: 'rmthz', session_id: 's', tag: 't' },
    });
    expect(clean.summary).toBe('fixing with token=[REDACTED] now');
    expect(clean.holder.tag).toBe('t');
  });
});

describe('POST /api/bus/native-sync (host-attested bridge)', () => {
  let stub: any;

  async function seedCred(sql: any, id: string, token: string, kind: string, machineId: string, actor: string | null, scopes: string[]) {
    const now = new Date().toISOString();
    const future = new Date(Date.now() + 86400000).toISOString();
    sql.exec(
      "INSERT INTO credentials (id, kind, token_hash, family_id, subject, actor, machine_id, scopes, created_by_sub, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
      id, kind, await sha256(token), `fam_${id}`, actor || machineId, actor, machineId, JSON.stringify(scopes), 'admin-1', now, future
    );
  }

  const claimData = (over: Record<string, any> = {}) => ({
    schema: 'hub.work-claim.v1',
    event: 'claim',
    claim_id: 'clm_01K8AN7D4XE9P0T2V5WYH6JM',
    seq: 1,
    work_key: 'task:7142fe0b-d45c-4a3a-b92b-726ecc10575a',
    project: 'projA',
    workspace: '/repo',
    mode: 'edit',
    scopes: ['src/worker'],
    holder: { machine_id: 'mach-A', session_id: 'sess-1', tag: 'fixer', engine: 'codex' },
    lease_ttl_sec: 1800,
    summary: 'Claiming the CI fix',
    ...over,
  });

  beforeEach(async () => {
    const id = defaultEnv.HUB.idFromName('hub');
    stub = defaultEnv.HUB.get(id);
    await runInDurableObject(stub, async (instance: Hub) => {
      for (const schema of [SCHEMA_SQL, BUS_SCHEMA_SQL]) {
        for (const statement of schema.split(';')) {
          const trimmed = statement.trim();
          if (trimmed) {
            try { instance.sql.exec(trimmed); } catch (e) {}
          }
        }
      }
      for (const table of ['messages', 'claims', 'agents', 'machines', 'credentials']) {
        try { instance.sql.exec(`DELETE FROM ${table}`); } catch (e) {}
      }
      try { instance.sql.exec('DELETE FROM fleet_claim_policy'); } catch (e) {}
      try {
        for (const col of ['work_key', 'claim_mode', 'claim_source', 'worktree', 'session_state', 'native_claim_id']) {
          const has = (instance.sql.exec('PRAGMA table_info(claims)').toArray() as any[]).some((c) => c.name === col);
          if (!has) instance.sql.exec(`ALTER TABLE claims ADD COLUMN ${col} TEXT`);
        }
      } catch (e) {}
      const now = new Date().toISOString();
      for (const m of ['mach-A', 'mach-B']) {
        instance.sql.exec('INSERT INTO machines (id, name, hostname, registered_at) VALUES (?, ?, ?, ?)', m, m, m, now);
      }
      instance.sql.exec("INSERT INTO agents (tag, project, role, machine_id) VALUES ('worker-1', 'projA', 'worker', 'mach-A')");
      await seedCred(instance.sql, 'c_host_a', 'tok_host_a', 'host', 'mach-A', null, ['host', 'bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_host_b', 'tok_host_b', 'host', 'mach-B', null, ['host', 'bus:send', 'bus:read']);
      await seedCred(instance.sql, 'c_w1', 'tok_w1', 'agent', 'mach-A', 'worker-1', ['bus:send', 'bus:read']);
    });
  });

  const http = (method: string, path: string, token?: string, body?: any) => doFetch(new Request(`http://example.com${path}`, {
    method,
    headers: { ...(token ? { 'Authorization': `Bearer ${token}` } : {}), 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }));

  it('requires host or admin authority with bus:send', async () => {
    expect((await http('POST', '/api/bus/native-sync', undefined, { claims: [], messages: [] })).status).toBe(401);
    expect((await http('POST', '/api/bus/native-sync', 'tok_w1', { claims: [], messages: [] })).status).toBe(403);
  });

  it('syncs a native edit declaration and surfaces it in activity', async () => {
    const res = await http('POST', '/api/bus/native-sync', 'tok_host_a', {
      claims: [{ data: claimData(), remaining_ttl_ms: 1800000, session_state: 'running' }],
      messages: [],
    });
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ status: 'ok', synced_claims: 1 });

    const activity = await (await http('GET', '/api/bus/activity?project=projA', 'tok_w1')).json() as any;
    expect(activity.claims).toHaveLength(1);
    expect(activity.claims[0]).toMatchObject({
      project: 'projA',
      scope: 'src/worker',
      holder: 'native:mach-A:fixer[sess1]',
      source: 'native',
      mode: 'edit',
      work_key: 'task:7142fe0b-d45c-4a3a-b92b-726ecc10575a',
      task_id: '7142fe0b-d45c-4a3a-b92b-726ecc10575a',
      native_claim_id: 'clm_01K8AN7D4XE9P0T2V5WYH6JM',
    });
    expect(activity.notice).toContain('someone may already be on this');
  });

  it('makes Hub claims conflict with synced native edit scopes', async () => {
    await http('POST', '/api/bus/native-sync', 'tok_host_a', { claims: [{ data: claimData() }], messages: [] });
    const dup = await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    });
    expect(dup.status).toBe(409);
  });

  it('treats synced read declarations as non-exclusive observations', async () => {
    await http('POST', '/api/bus/native-sync', 'tok_host_a', {
      claims: [{ data: claimData({ mode: 'read' }) }], messages: [],
    });
    const ok = await http('POST', '/api/bus/claims', 'tok_w1', {
      project: 'projA', scope: 'src/worker', holder: 'projA:worker-1', ttl_ms: 3600000,
    });
    expect(ok.status).toBe(200);
    const activity = await (await http('GET', '/api/bus/activity?project=projA', 'tok_w1')).json() as any;
    expect(activity.claims.some((c: any) => c.mode === 'read' && c.source === 'native')).toBe(true);
  });

  it('releases synced rows on terminal work events', async () => {
    await http('POST', '/api/bus/native-sync', 'tok_host_a', { claims: [{ data: claimData() }], messages: [] });
    const done = await http('POST', '/api/bus/native-sync', 'tok_host_a', {
      claims: [{ data: claimData({ event: 'complete', seq: 2 }) }], messages: [],
    });
    expect(await done.json()).toMatchObject({ released_claims: 1 });
    const activity = await (await http('GET', '/api/bus/activity?project=projA', 'tok_w1')).json() as any;
    expect(activity.claims).toEqual([]);
  });

  it('mirrors work.* envelopes as redacted project messages, idempotently', async () => {
    const payload = {
      claims: [],
      messages: [{
        native_id: '01a15cc1-71d4-7d43-80cb-6642f9b52718',
        kind: 'work.claim',
        body: 'CLAIM task:7142fe0b by fixer token=hubt_leaked',
        project: 'projA',
        from: { machine_id: 'mach-A', session_id: 'sess-1', tag: 'fixer' },
      }],
    };
    const first = await http('POST', '/api/bus/native-sync', 'tok_host_a', payload);
    expect(await first.json()).toMatchObject({ synced_messages: 1 });
    const second = await http('POST', '/api/bus/native-sync', 'tok_host_a', payload);
    expect(await second.json()).toMatchObject({ synced_messages: 0, deduped_messages: 1 });

    const activity = await (await http('GET', '/api/bus/activity?project=projA', 'tok_w1')).json() as any;
    const mirrored = activity.recent_messages.find((m: any) => m.kind === 'work.claim');
    expect(mirrored).toMatchObject({ from_actor: 'native:mach-A:fixer[sess1]', to_actor: 'project:projA' });
    expect(mirrored.body).toContain('token=[REDACTED]');
    expect(mirrored.body).not.toContain('hubt_leaked');
  });

  it('rejects cross-machine attestation and invalid rows with reasons', async () => {
    const res = await http('POST', '/api/bus/native-sync', 'tok_host_b', {
      claims: [
        { data: claimData() }, // holder mach-A, caller mach-B
        { data: claimData({ claim_id: 'bogus', work_key: 'nope' }) },
      ],
      messages: [{ native_id: 'x', kind: 'note', body: 'b', project: 'projA', from: { machine_id: 'mach-B', session_id: 's', tag: 't' } }],
    });
    expect(res.status).toBe(200);
    const data = await res.json() as any;
    expect(data.synced_claims).toBe(0);
    expect(data.synced_messages).toBe(0);
    expect(data.errors).toHaveLength(3);
  });

  it('resolves seeded workspace mappings for activity without operator setup', async () => {
    await runInDurableObject(stub, async (instance: Hub) => {
      try { instance.sql.exec('DELETE FROM fleet_claim_policy'); } catch (e) {}
      (instance as any).fleetService?.init?.();
      try {
        const admission = (instance as any).fleetService?.admission;
        admission?.seedWorkClaimPolicy?.();
      } catch (e) {}
    });
    const policy = await runInDurableObject(stub, async (instance: Hub) => {
      return (instance.sql.exec('SELECT workspace, project, require_claim FROM fleet_claim_policy ORDER BY workspace').toArray() as any[]);
    });
    const byWs = new Map(policy.map((r: any) => [r.workspace, r]));
    expect(byWs.get('/home/alexey/git/agent-hub')).toMatchObject({ project: 'agent-hub', require_claim: 0 });
    expect(byWs.get('/home/alexey/git/aplexer')).toMatchObject({ project: 'aplexer', require_claim: 0 });
  });
});

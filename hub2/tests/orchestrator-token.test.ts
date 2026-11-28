// End-to-end regression for intake 6b3b340c:
// - Orchestrator sessions (--purpose orchestrate, --interactive, --with-goal) receive an
//   orchestrator-scoped token: quota:read, repos list/add, sessions list/start/show/send/stop,
//   and intake show/append for the linked parent intake and its child intakes (full and short id).
// - Unrelated intakes still 403 even for orchestrators.
// - Normal implement / headless workers stay least-privilege (single-intake) and 403 on
//   sessions list/start/show/stop, quota, repos list/add, and unrelated intakes.
// - Child sessions started by an orchestrator cannot escalate to orchestrator tokens.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { DatabaseSync } from 'node:sqlite';
import { ControlDO } from '../src/control-do';
import { ProjectDO } from '../src/project-do';
import { MachineDO } from '../src/machine-do';
import { AccountDO } from '../src/account-do';

const PROJECT = 'org/a';
const MACHINE = 'hetzner-fixture';
const ACCOUNT = 'codex';
const CREDENTIAL_KEY = '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef';
const ADMIN_TOKEN = 'operator-admin-token-' + 'a'.repeat(32);
const admin = { id: 'human:admin', kind: 'operator', scopes: ['operator:admin'] };

function storage(sql: DatabaseSync, id = 'synthetic', sockets: any[] = []): any {
  return {
    id: { toString: () => id },
    getWebSockets: () => sockets,
    storage: {
      sql: {
        exec(q: string, ...a: any[]) {
          const s = sql.prepare(q);
          const rows = s.columns().length ? s.all(...a) : (s.run(...a), []);
          return { toArray: () => rows };
        },
      },
      transactionSync(fn: () => unknown) {
        sql.exec('BEGIN');
        try {
          const v = fn();
          sql.exec('COMMIT');
          return v;
        } catch (e) {
          sql.exec('ROLLBACK');
          throw e;
        }
      },
      async getAlarm() { return null; },
      async setAlarm() {},
      async deleteAlarm() {},
      async get() { return undefined; },
      async put() {},
      async delete() { return false; },
    },
    blockConcurrencyWhile: (fn: () => unknown) => Promise.resolve(fn()),
  };
}

function fixture() {
  const controlSql = new DatabaseSync(':memory:');
  const machineSql = new DatabaseSync(':memory:');
  const accountSql = new DatabaseSync(':memory:');
  const projectSqls = new Map<string, DatabaseSync>();
  const projects = new Map<string, ProjectDO>();
  let control: ControlDO;
  let machine: MachineDO;
  let account: AccountDO;
  const req = (i: string | Request, init?: RequestInit) => (i instanceof Request ? i : new Request(i, init));
  const stub = { idFromName: (id: string) => id, get: () => ({ fetch: async () => Response.json({ ok: true }) }) };
  const env: any = {
    HUB2_CREDENTIAL_KEY: CREDENTIAL_KEY,
    HUB2_SCHEMA_MIGRATIONS: 'apply',
    CONTROL: {
      idFromName: (id: string) => id,
      get: () => ({
        fetch: async (i: string | Request, init?: RequestInit) => {
          const r = req(i, init);
          if (new URL(r.url).pathname === '/_verify') {
            const { token } = (await r.clone().json()) as { token: string };
            if (token === ADMIN_TOKEN) return Response.json({ actor: admin });
          }
          return control.fetch(r);
        },
      }),
    },
    PROJECTS: {
      idFromName: (id: string) => id,
      get: (id: string) => ({ fetch: (i: string | Request, init?: RequestInit) => project(id).fetch(req(i, init)) }),
    },
    MACHINES: {
      idFromName: (id: string) => id,
      get: () => ({ fetch: (i: string | Request, init?: RequestInit) => machine.fetch(req(i, init)) }),
    },
    ACCOUNTS: {
      idFromName: (id: string) => id,
      get: () => ({ fetch: (i: string | Request, init?: RequestInit) => account.fetch(req(i, init)) }),
    },
    MAILBOXES: stub,
  };
  const project = (id: string) => {
    let p = projects.get(id);
    if (!p) {
      const sql = new DatabaseSync(':memory:');
      projectSqls.set(id, sql);
      p = new ProjectDO(storage(sql, 'p/' + id), env);
      projects.set(id, p);
    }
    return p;
  };
  let hostAttachment: any = { machine_id: MACHINE, stop_only: false };
  const sockets = [{
    readyState: 1,
    send() {},
    close() {},
    deserializeAttachment: () => hostAttachment,
  }];
  control = new ControlDO(storage(controlSql, 'control/global'), env);
  machine = new MachineDO(storage(machineSql, 'machine/' + MACHINE, sockets), env);
  account = new AccountDO(storage(accountSql, 'account/' + ACCOUNT), env);
  return {
    env,
    control,
    controlSql,
    machine,
    machineSql,
    account,
    project,
    setHostAttachment: (a: any) => { hostAttachment = a; },
    close: () => {
      controlSql.close();
      machineSql.close();
      accountSql.close();
      for (const s of projectSqls.values()) s.close();
    },
  };
}

const data = async (r: Response) => (await r.json()) as any;

async function setupFleet(f: ReturnType<typeof fixture>, worker: { fetch: (r: Request, e: any) => Promise<Response> }) {
  const api = (path: string, method = 'GET', body?: unknown, token = ADMIN_TOKEN) =>
    worker.fetch(
      new Request('http://127.0.0.1' + path, {
        method,
        headers: {
          ...(token ? { Authorization: 'Bearer ' + token } : {}),
          'Content-Type': 'application/json',
        },
        ...(body === undefined ? {} : { body: JSON.stringify(body) }),
      }),
      f.env,
    );

  // Register repository
  const repoRes = await api('/api/repos/' + encodeURIComponent(PROJECT), 'PATCH', {
    canonical_url: 'https://github.com/' + PROJECT,
    purpose: 'Fixture repo',
    aliases: [],
  });
  assert.equal(repoRes.status, 200, 'register repo');

  // Enroll machine
  const pairRes = await api('/api/enrollments/pair', 'POST', { machine_id: MACHINE, alias: 'fixture' });
  assert.equal(pairRes.status, 201, 'pair machine');
  const pair = await data(pairRes);
  const enrollRes = await api('/api/enrollments/complete', 'POST', { code: pair.pairing_code }, '');
  assert.equal(enrollRes.status, 201, 'enroll machine');
  const hostToken: string = (await data(enrollRes)).token;
  const hostCred = f.controlSql.prepare("SELECT actor_id, generation FROM credentials WHERE kind='machine' AND resource_id=?").get(MACHINE) as any;
  f.setHostAttachment({ machine_id: MACHINE, actor_id: hostCred.actor_id, generation: hostCred.generation, stop_only: false });

  // Heartbeat with headless, interactive, goal capabilities
  const hb = await api('/api/heartbeat', 'POST', {
    os: 'linux',
    arch: 'x64',
    engines: ['codex'],
    capabilities: ['headless', 'interactive', 'goal', 'goal:codex', 'modes:v1:codex:hi'],
  }, hostToken);
  assert.equal(hb.status, 200, 'heartbeat');

  // Configure account + quota + approved host
  const acctCfg = await f.account.fetch(new Request('https://account.internal/api/account', {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(admin) },
    body: JSON.stringify({ provider: 'codex', enabled: true, min_remaining: 5 }),
  }));
  assert.equal(acctCfg.status, 200, 'account config');
  const acctHosts = await f.account.fetch(new Request('https://account.internal/api/account/hosts', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(admin) },
    body: JSON.stringify({ hosts: [MACHINE] }),
  }));
  assert.equal(acctHosts.status, 200, 'account hosts');
  const acctQuota = await f.account.fetch(new Request('https://account.internal/api/account/quota', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json', 'x-hub2-actor': JSON.stringify(admin) },
    body: JSON.stringify({
      remaining: 80,
      captured_at: Date.now(),
      valid_until: Date.now() + 3600_000,
      source: 'fixture',
    }),
  }));
  assert.equal(acctQuota.status, 200, 'account quota');

  // Model policy with codex candidate
  const policyRes = await api('/api/model-policy', 'PUT', {
    candidates: [{
      id: 'codex-fixture',
      engine: 'codex',
      model: 'gpt-6.1-sol',
      effort: 'medium',
      account_id: ACCOUNT,
      weight: 10,
      capabilities: ['headless', 'interactive', 'goal'],
      enabled: true,
    }],
  });
  assert.equal(policyRes.status, 200, 'model policy');

  async function issueTokenForSession(sessionId: string) {
    // Transition offered -> claimed and record admission directly so /credentials issues a real sealed token.
    const row = f.machineSql.prepare('SELECT id, operation_id, epoch FROM sessions WHERE id=?').get(sessionId) as any;
    assert.ok(row, 'session row exists on machine');
    f.machineSql.prepare("UPDATE sessions SET state='claimed', offer_expires_at=NULL WHERE id=?").run(sessionId);
    f.machineSql.prepare('INSERT OR REPLACE INTO launch_admissions(operation_id,session_id,epoch,observed_at) VALUES(?,?,?,?)')
      .run(row.operation_id, row.id, row.epoch, Date.now());
    const credRes = await api(`/api/sessions/${sessionId}/credentials`, 'POST', {
      operation_id: row.operation_id,
      epoch: row.epoch,
    }, hostToken);
    assert.equal(credRes.status, 200, 'issue credentials for ' + sessionId);
    return await data(credRes);
  }

  return { api, hostToken, issueTokenForSession };
}

test('orchestrator session (--purpose orchestrate and --with-goal) gets orchestrator-scoped token while unrelated intakes 403', async t => {
  const f = fixture();
  t.after(f.close);
  const { default: worker } = await import('../src/worker');
  const { api, issueTokenForSession } = await setupFleet(f, worker);

  const createIntake = async (key: string, parentId?: string) => {
    const r = await api('/api/intakes?project=' + encodeURIComponent(PROJECT), 'POST', {
      key,
      title: 'Task ' + key,
      text: 'Do ' + key,
      acceptance_criteria: ['Done ' + key],
      ...(parentId ? { parent_intake_id: parentId } : {}),
    });
    assert.equal(r.status, 201, 'create ' + key);
    return (await data(r)).intake;
  };

  const parent = await createIntake('orch-parent');
  const child = await createIntake('orch-child', parent.id);
  const unrelated = await createIntake('orch-unrelated');

  // Start a headless orchestrator session via --purpose orchestrate
  const startRes = await api('/api/sessions', 'POST', {
    key: 'start-orch-1',
    intake_task_id: parent.id,
    repo: PROJECT,
    workspace: '/synthetic/orch',
    purpose: 'orchestrate',
    mode: 'headless',
    brief: 'Coordinate child workers',
  });
  const startBody = await data(startRes);
  assert.equal(startRes.status, 201, 'start orchestrator session: ' + JSON.stringify(startBody));
  const orchSession = startBody;
  const orchGrant = await issueTokenForSession(orchSession.id);
  assert.equal(orchGrant.role, 'orchestrator');
  const orchToken: string = orchGrant.token;

  // 1. hub quota -> 200
  const quotaRes = await api('/api/quota?view=fleet', 'GET', undefined, orchToken);
  assert.equal(quotaRes.status, 200, 'orchestrator hub quota');

  // 2. hub repos list -> 200
  const reposListRes = await api('/api/repos', 'GET', undefined, orchToken);
  assert.equal(reposListRes.status, 200, 'orchestrator hub repos list');
  assert.ok((await data(reposListRes)).items.some((r: any) => r.canonical_name === PROJECT));

  // 3. hub repos add --file -> 200
  const repoAddRes = await api('/api/repos/' + encodeURIComponent('org/added-by-orch'), 'PATCH', {
    canonical_url: 'https://github.com/org/added-by-orch',
    purpose: 'Registered by orchestrator',
    aliases: [],
  }, orchToken);
  assert.equal(repoAddRes.status, 200, 'orchestrator hub repos add');

  // 4. hub sessions list -> 200
  const sessionsListRes = await api('/api/sessions', 'GET', undefined, orchToken);
  assert.equal(sessionsListRes.status, 200, 'orchestrator hub sessions list');
  assert.ok((await data(sessionsListRes)).sessions.some((s: any) => s.id === orchSession.id));

  // 5. hub intake show & append on parent AND child (full UUID and 8-char prefix) -> 200/201
  for (const ref of [parent.id, child.id, child.id.slice(0, 8)]) {
    const showRes = await api('/api/intakes/' + ref, 'GET', undefined, orchToken);
    assert.equal(showRes.status, 200, 'orchestrator intake show ' + ref);
    const appendRes = await api('/api/intakes/' + ref + '/append', 'POST', {
      key: 'orch-note-' + ref,
      note: 'Orchestrator note on ' + ref,
    }, orchToken);
    assert.equal(appendRes.status, 201, 'orchestrator intake append ' + ref);
  }

  // 6. Unrelated intake still returns 403
  assert.equal((await api('/api/intakes/' + unrelated.id, 'GET', undefined, orchToken)).status, 403, 'unrelated intake full 403');
  assert.equal((await api('/api/intakes/' + unrelated.id.slice(0, 8), 'GET', undefined, orchToken)).status, 403, 'unrelated intake prefix 403');

  // 7. hub sessions start on child intake (using 8-char prefix!) -> 201
  const childStartRes = await api('/api/sessions', 'POST', {
    key: 'start-child-from-orch',
    intake_task_id: child.id.slice(0, 8),
    workspace: '/synthetic/child',
    mode: 'interactive',
    brief: 'Run child task',
  }, orchToken);
  assert.equal(childStartRes.status, 201, 'orchestrator starts child session');
  const childSession = await data(childStartRes);
  assert.equal(childSession.intake_id, child.id);

  // Child session started by orchestrator MUST NOT escalate to orchestrator token (even with interactive: true)
  const childGrant = await issueTokenForSession(childSession.id);
  assert.equal(childGrant.role, 'worker', 'child started by orchestrator stays least-privilege worker');

  // 8. hub sessions show / send / stop on child session -> 200/201
  const showChildRes = await api('/api/sessions/' + childSession.id.slice(0, 8), 'GET', undefined, orchToken);
  assert.equal(showChildRes.status, 200, 'orchestrator sessions show child');

  // Mark child running so send is accepted
  f.machineSql.prepare("UPDATE sessions SET state='running', native_id='11111111-2222-4333-8444-555555555555' WHERE id=?").run(childSession.id);
  const sendChildRes = await api('/api/sessions/' + childSession.id + '/send', 'POST', {
    key: 'orch-send-child',
    body: 'Check status',
  }, orchToken);
  assert.equal(sendChildRes.status, 201, 'orchestrator sessions send child');

  const stopChildRes = await api('/api/sessions/' + childSession.id + '/stop', 'POST', {
    key: 'orch-stop-child',
  }, orchToken);
  assert.ok([200, 201].includes(stopChildRes.status), 'orchestrator sessions stop child: ' + stopChildRes.status);

  // Also verify --with-goal triggers orchestrator role
  const goalParent = await createIntake('goal-parent');
  const goalStart = await api('/api/sessions', 'POST', {
    key: 'start-goal-orch',
    intake_task_id: goalParent.id,
    repo: PROJECT,
    workspace: '/synthetic/goal',
    mode: 'interactive',
    with_goal: true,
    brief: 'Goal orchestrator',
  });
  const goalBody = await data(goalStart);
  assert.equal(goalStart.status, 201, 'start --with-goal orchestrator: ' + JSON.stringify(goalBody));
  const goalGrant = await issueTokenForSession(goalBody.id);
  assert.equal(goalGrant.role, 'orchestrator', '--with-goal issues orchestrator role');
});

test('AC3 regression: normal implement / headless worker stays least-privilege and gets 403 on list/start/quota/repos/unrelated intakes', async t => {
  const f = fixture();
  t.after(f.close);
  const { default: worker } = await import('../src/worker');
  const { api, issueTokenForSession } = await setupFleet(f, worker);

  const createIntake = async (key: string, parentId?: string) => {
    const r = await api('/api/intakes?project=' + encodeURIComponent(PROJECT), 'POST', {
      key,
      title: 'Task ' + key,
      text: 'Do ' + key,
      acceptance_criteria: ['Done ' + key],
      ...(parentId ? { parent_intake_id: parentId } : {}),
    });
    assert.equal(r.status, 201, 'create ' + key);
    return (await data(r)).intake;
  };

  const ownIntake = await createIntake('worker-own');
  const unrelatedIntake = await createIntake('worker-unrelated');
  const otherIntake = await createIntake('worker-other');

  // Start a normal implement / headless worker
  const startRes = await api('/api/sessions', 'POST', {
    key: 'start-normal-worker',
    intake_task_id: ownIntake.id,
    repo: PROJECT,
    workspace: '/synthetic/worker',
    purpose: 'implement',
    mode: 'headless',
    brief: 'Implement a feature',
  });
  const startBody = await data(startRes);
  assert.equal(startRes.status, 201, 'start normal worker: ' + JSON.stringify(startBody));
  const workerSession = startBody;
  const workerGrant = await issueTokenForSession(workerSession.id);
  assert.equal(workerGrant.role, 'worker');
  const workerToken: string = workerGrant.token;

  // Start another session for cross-session 403 checks
  const otherStartRes = await api('/api/sessions', 'POST', {
    key: 'start-other-session',
    intake_task_id: otherIntake.id,
    repo: PROJECT,
    workspace: '/synthetic/other',
    purpose: 'implement',
    mode: 'headless',
    brief: 'Other worker',
  });
  assert.equal(otherStartRes.status, 201);
  const otherSession = await data(otherStartRes);

  // Own intake read/append still works
  assert.equal((await api('/api/intakes/' + ownIntake.id, 'GET', undefined, workerToken)).status, 200, 'worker reads own intake');
  assert.equal((await api('/api/intakes/' + ownIntake.id + '/append', 'POST', { key: 'w-note', note: 'progress' }, workerToken)).status, 201, 'worker appends own intake');

  // Least-privilege 403 assertions (AC3)
  assert.equal((await api('/api/sessions', 'GET', undefined, workerToken)).status, 403, 'worker 403 on sessions list');
  assert.equal((await api('/api/sessions', 'POST', {
    key: 'worker-try-start',
    repo: PROJECT,
    workspace: '/synthetic/sub',
    brief: 'Try to start a child',
  }, workerToken)).status, 403, 'worker 403 on sessions start');
  assert.equal((await api('/api/intakes/' + unrelatedIntake.id + '/start?project=' + encodeURIComponent(PROJECT), 'POST', {
    key: 'worker-try-intake-start',
    workspace: '/synthetic/sub',
  }, workerToken)).status, 403, 'worker 403 on intake start');
  assert.equal((await api('/api/sessions/' + otherSession.id, 'GET', undefined, workerToken)).status, 403, 'worker 403 on other session show');
  assert.equal((await api('/api/sessions/' + otherSession.id + '/stop', 'POST', { key: 'w-stop' }, workerToken)).status, 403, 'worker 403 on other session stop');
  assert.equal((await api('/api/quota?view=fleet', 'GET', undefined, workerToken)).status, 403, 'worker 403 on quota');
  assert.equal((await api('/api/repos', 'GET', undefined, workerToken)).status, 403, 'worker 403 on repos list');
  assert.equal((await api('/api/repos/' + encodeURIComponent('org/forbidden-repo'), 'PATCH', {
    canonical_url: 'https://github.com/org/forbidden-repo',
    purpose: 'Forbidden',
    aliases: [],
  }, workerToken)).status, 403, 'worker 403 on repos add');
  assert.equal((await api('/api/intakes/' + unrelatedIntake.id, 'GET', undefined, workerToken)).status, 403, 'worker 403 on unrelated intake');
  assert.equal((await api('/api/intakes/' + unrelatedIntake.id.slice(0, 8), 'GET', undefined, workerToken)).status, 403, 'worker 403 on unrelated intake prefix');
});

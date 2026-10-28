// Real workerd HTTP + SQLite + browser + Python CLI. No API request mocks.
import assert from 'node:assert/strict';
import { launchBrowser } from './playwright.mjs';
import { randomBytes, createHash } from 'node:crypto';
import { mkdir, readFile, writeFile, chmod, access } from 'node:fs/promises';
import { resolve, relative, extname } from 'node:path';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { build } from 'esbuild';
import { Miniflare, Log, LogLevel, convertV4MiniflareOptions } from 'miniflare';

const exec = promisify(execFile);
const root = resolve(import.meta.dirname, '..');
const scratch = resolve(root, '.local/path');
await mkdir(scratch, { recursive: true, mode: 0o700 });
await chmod(scratch, 0o700);
assert.ok(process.env.TMPDIR && !relative(resolve(root, '.local/tmp'), resolve(process.env.TMPDIR)).startsWith('..'), 'Use task-local TMPDIR under .local/tmp');
const nonce = randomBytes(8).toString('hex');
const key = randomBytes(32).toString('hex');
const secret = randomBytes(32).toString('hex');
const native = process.argv.includes('--native');
const projectArg = process.argv.find(arg => arg.startsWith('--project='));
const project = projectArg ? projectArg.slice('--project='.length) : 'cint-local';
assert.match(project, /^cint-local(?:-[a-z0-9]+)?$/);
const nativeWorkspace = process.env.INTEGRATION_NATIVE_WORKSPACE || '/home/alexey/git/agent-hub-wt-platform-conv-final';
let own;
if (native) {
  own = JSON.parse((await exec('a', ['--json', 'whoami'], { cwd: nativeWorkspace })).stdout);
  assert.equal(own.workspace, nativeWorkspace);
  assert.equal(own.tag, 'platform-conv-final');
  assert.equal(own.id, 'd19b72bc-5019-4e35-9c05-58e6434d49de');
  assert.equal(own.engine, 'codex');
  const journalName = 'conversation-bridge-' + createHash('sha256').update(JSON.stringify([project + ':' + own.tag, project])).digest('hex').slice(0, 24) + '.json';
  const journalExists = await access(resolve(nativeWorkspace, '.local', journalName)).then(() => true, () => false);
  assert.equal(journalExists, false, 'Retained native journal requires explicit reconciliation; never reset it to rerun this ephemeral fixture');
}
const tag = own?.tag || 'cint-agent';
const actor = project + ':' + tag;
const machineId = 'cint-machine';
const testConfig = JSON.parse(await readFile(resolve(root, 'test/wrangler.conversation-platform.jsonc'), 'utf8'));
assert.equal(testConfig.durable_objects.bindings.length, 1);
assert.equal(testConfig.durable_objects.bindings[0].name, 'HUB');
assert.equal(testConfig.durable_objects.bindings[0].class_name, 'Hub');
const bundled = await build({ entryPoints: [resolve(root, 'test', testConfig.main)], bundle: true, write: false, format: 'esm', platform: 'browser', external: ['cloudflare:workers'] });
const mime = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const mf = new Miniflare(convertV4MiniflareOptions({
  host: '127.0.0.1', port: 0,
  log: new Log(LogLevel.ERROR),
  workers: [{ name: testConfig.name,
  script: bundled.outputFiles[0].text, modules: true,
  compatibilityDate: testConfig.compatibility_date, compatibilityFlags: testConfig.compatibility_flags,
  durableObjects: { HUB: { className: 'Hub', useSQLite: true } },
  bindings: { HUB_ADMINS: 'cint@fake.local', SESSION_SECRET: secret, LOCAL_FIXTURE_KEY: key },
  serviceBindings: { ASSETS: async request => {
    const path = new URL(request.url).pathname;
    const file = resolve(root, 'public', '.' + path);
    assert.ok(file.startsWith(resolve(root, 'public') + '/'));
    try { return new Response(await readFile(file), { headers: { 'Content-Type': mime[extname(file)] || 'application/octet-stream' } }); }
    catch { return new Response('Not found', { status: 404 }); }
  } },
  }],
}));
let browser;
const evidence = { frozenInput: 'combined-provisional', pins: { transport: 'd9b4cde07e3718d77b2db9308cd323a5c8e003ed', api: '8dc9235ce246ff8316048fa837b34b53ae7a0cdd', f7: 'ddaf80d82022736da82ff11d521c6a9f92ff593e', p3: '0896ebe157cc732342b17fb40a6cf6e096ef93a2', pairing: 'eee4d2f88305a6e64545c8e7b68f329a162811d1', m2b: 'd80dfebd806ae320af699ab149b6c183e376ed07' }, head: (await exec('git', ['rev-parse', 'HEAD'])).stdout.trim(), bundleSha256: createHash('sha256').update(bundled.outputFiles[0].text).digest('hex'), checks: [], native: native ? 'pending' : 'not requested' };
const check = name => { evidence.checks.push(name); console.log('PASS ' + name); };
const deadline = setTimeout(async () => {
  console.error('Transport deadline exceeded');
  try { if (browser) await browser.close(); } finally { await mf.dispose(); process.exit(2); }
}, 180000);
try {
  const base = (await mf.ready).origin;
  const fixtureResponse = await fetch(base + '/__local_fixture', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify({ tag, project }) });
  assert.equal(fixtureResponse.status, 200);
  const fixture = await fixtureResponse.json();
  assert.equal(fixture.machine_id, machineId);
  const headers = { Cookie: fixture.cookie, 'X-CSRF-Token': fixture.csrf, 'Content-Type': 'application/json' };
  const api = async (path, method = 'GET', body) => {
    const res = await fetch(base + path, { method, headers, ...(body === undefined ? {} : { body: JSON.stringify(body) }) });
    assert.ok(res.ok, path + ' HTTP ' + res.status);
    return res.json();
  };
  assert.equal((await fetch(base + '/api/device/code', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ client_kind: 'agent', name: actor, machine_id: machineId, requested_scopes: ['bus:read', 'bus:send'] }) })).status, 400);
  for (const path of ['/', '/conversations.js', '/fleet.js', '/fleet.css', '/fleet.html']) {
    const res = await fetch(base + path, { redirect: 'manual' });
    assert.equal(res.status, 303);
    assert.equal(res.headers.get('location'), '/auth/login');
  }
  assert.equal((await fetch(base + '/.dev.vars')).status, 404);
  assert.equal((await fetch(base + '/api/conversations')).status, 401);
  const missingCsrf = await fetch(base + '/api/conversations', { method: 'POST', headers: { Cookie: fixture.cookie }, body: '{}' });
  assert.equal(missingCsrf.status, 403);
  assert.equal((await api('/healthz')).component, 'hub-do');
  check('real Worker auth, CSRF, inherited health/registry and private asset gate');
  const configFile = resolve(scratch, 'host-' + nonce + '.json');
  const cliEnv = { ...process.env, PYTHONPATH: resolve(root, 'host'), PYTHONDONTWRITEBYTECODE: '1', HUB_URL: '', HUB_BASE_URL: '', HUB_TOKEN: '', HUB_JOB_TOKEN: '', HUB_CONFIG_FILE: configFile };
  const loginChild = execFile('python3', ['-u', '-m', 'agent_hub_host.cli', 'login', base, '--kind', 'agent', '--name', actor], { cwd: native ? nativeWorkspace : root, env: cliEnv, timeout: 45000 });
  let loginOutput = '', loginError = '';
  loginChild.stderr.on('data', d => loginError += d);
  const loginDone = new Promise((ok, fail) => { loginChild.on('error', fail); loginChild.on('close', code => code === 0 ? ok() : fail(new Error('public CLI login failed: ' + code))); });
  const verification = await new Promise((ok, fail) => {
    const timer = setTimeout(() => fail(new Error('CLI verification prompt missing')), 15000);
    loginChild.stdout.on('data', d => { loginOutput += d; const m = loginOutput.match(/Open in a browser:\s+(http[^\s]+)/); if (m) { clearTimeout(timer); ok(m[1]); } });
    loginChild.on('close', () => { clearTimeout(timer); fail(new Error('CLI exited before prompt')); });
  });
  const userCode = new URL(verification).searchParams.get('code');
  const approval = await fetch(base + '/api/device/approve/' + userCode, { method:'POST', headers, body:JSON.stringify({expected_actor:actor,machine_id:machineId,granted_scopes:['bus:read','bus:send']}) });
  assert.equal(approval.status,200); assert.equal((await approval.json()).machine_id,machineId);
  await loginDone;
  assert.ok(loginOutput.includes('Login successful.'));
  // Read only this fresh fixture config; no manual machine/token writes or overrides.
  const saved = JSON.parse(await readFile(configFile,'utf8'));
  assert.equal(saved.machine_id,machineId); assert.equal(saved.client_name,actor); assert.equal(saved.client_kind,'agent');
  const token = saved.access_token;
  assert.equal(typeof token,'string'); assert.ok(token.length > 16);
  assert.equal((await api('/api/agents')).agents.length, 1);
  check('actual public CLI device login + signed local human/current CSRF approval automatically persists authoritative machine binding');
  let failedCliCount = 0;
  const cli = async (args, input) => {
    // Message text enters stdin, credentials remain in private env/config.
    const child = execFile('python3', ['-m', 'agent_hub_host.cli', ...(args[0] === 'bridge-resume' ? args : ['conversations', ...args])], { cwd: native ? nativeWorkspace : root, env: cliEnv, timeout: 30000, maxBuffer: 1024 * 1024 });
    if (input !== undefined) child.stdin.end(input);
    const result = await new Promise((resolvePromise, reject) => {
      let stdout = '', stderr = '';
      child.stdout.on('data', data => { stdout += data; });
      child.stderr.on('data', data => { stderr += data; });
      child.on('error', reject);
      child.on('close', async code => {
        if (code === 0) { resolvePromise({stdout,stderr}); return; }
        await writeFile(resolve(scratch,'cli-failure-'+(++failedCliCount)+'.json'),JSON.stringify({args,code,stdout,stderr}),{mode:0o600});
        reject(new Error('CLI failed ('+code+'): '+stderr));
      });
    });
    return JSON.parse(result.stdout);
  };
  const participants = await cli(['participants']);
  assert.equal(participants.self.actor, actor);
  check('actual Python CLI discovers authenticated registered actor');
  browser = await launchBrowser({ args: ['--single-process', '--in-process-gpu', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--js-flags=--thread-pool-size=1', '--num-raster-threads=1', '--disable-threaded-animation', '--disable-threaded-scrolling'] });
  const context = await browser.newContext();
  const [name, value] = fixture.cookie.split('=');
  await context.addCookies([{ name, value, url: base, httpOnly: true, sameSite: 'Lax' }]);
  const page = await context.newPage();
  page.setDefaultTimeout(15000);
  const requests = [];
  page.on('request', req => { if (new URL(req.url()).pathname.startsWith('/api/conversations')) requests.push({ path: new URL(req.url()).pathname, method: req.method() }); });
  await page.goto(base + '/');
  await page.getByRole('link', { name: 'Open conversations', exact: true }).click();
  await page.locator('#new-conversation').click();
  await page.locator('input[name="conv-kind"][value="group"]').check();
  await page.locator('#group-title').fill('Local transport ' + nonce);
  await page.locator('#identity-list label').filter({ hasText: actor }).locator('input').check();
  const created = page.waitForResponse(res => new URL(res.url()).pathname === '/api/conversations' && res.request().method() === 'POST');
  await page.locator('#new-conversation-create').click();
  const createRes = await created;
  assert.equal(createRes.status(), 201);
  const cid = (await createRes.json()).conversation.id;
  const humanText = 'browser nonce ' + nonce;
  await page.locator('#composer-field').fill(humanText);
  await page.locator('#composer button[type=submit]').click();
  await page.locator('#thread-scroll').getByText(humanText, { exact: true }).waitFor();
  let history = await cli(['history', cid, '--after-seq', '0']);
  assert.equal(history.messages.length, 1);
  assert.equal(history.messages[0].body, humanText);
  const original = history.messages[0];
  await cli(['read', cid, '--seq', '1']);
  check('human browser creates and sends; actual CLI receives and advances read watermark');
  const replyText = 'CLI reply ' + nonce;
  const browserRead = page.waitForResponse(res => new URL(res.url()).pathname === '/api/conversations/' + cid + '/read' && res.request().method() === 'POST' && res.request().postDataJSON()?.seq === 2);
  const sent = await cli(['send', cid, '--body-file', '-', '--key', 'path-reply-' + nonce, '--kind', 'reply', '--reply-to', original.id], replyText);
  assert.equal(sent.message.seq, 2);
  const replay = await cli(['send', cid, '--body-file', '-', '--key', 'path-reply-' + nonce, '--kind', 'reply', '--reply-to', original.id], replyText);
  assert.equal(replay.message.id, sent.message.id);
  await page.locator('#thread-scroll').getByText(replyText, { exact: true }).waitFor();
  assert.equal((await browserRead).status(), 200);
  let detail = await cli(['get', cid]);
  assert.equal(detail.members.find(m => m.actor === 'human:cint-human').last_read_seq, 2);
  check('CLI reply, idempotency, browser ordered response and stored human read watermark');
  await context.setOffline(true);
  const catchup = 'catch-up ' + nonce;
  await cli(['send', cid, '--body-file', '-', '--key', 'path-catchup-' + nonce], catchup);
  await context.setOffline(false);
  await page.evaluate(() => window.dispatchEvent(new Event('online')));
  await page.locator('#thread-scroll').getByText(catchup, { exact: true }).waitFor();
  await page.reload();
  await page.locator('#thread-scroll').getByText(catchup, { exact: true }).waitFor();
  for (const body of [humanText, replyText, catchup]) {
    await page.locator('#thread-scroll').getByText(body, { exact: true }).waitFor();
    assert.equal(await page.locator('#thread-scroll').getByText(body, { exact: true }).count(), 1);
  }
  assert.deepEqual(await page.locator('#thread-scroll .msg__body').allTextContents(), [humanText, replyText, catchup]);
  history = await cli(['history', cid, '--after-seq', '0']);
  assert.deepEqual(history.messages.map(m => m.seq), [1, 2, 3]);
  assert.equal((await cli(['history', cid, '--after-seq', '3'])).messages.length, 0);
  check('offline reconnect, reload and sequence catch-up without duplicates');
  // Observe retention honestly through actual Worker HTTP + Python history.
  // An interior gap must be explicit in the accepted API contract; P3 holds by default.
  const gapConv = (await api('/api/conversations', 'POST', { kind: 'group', title: 'Gap probe', members: [actor], idempotency_key: 'gap-conv-' + nonce })).conversation.id;
  for (let seq = 1; seq <= 3; seq++) await cli(['send',gapConv,'--body-file','-','--key','gap-'+nonce+'-'+seq], 'gap probe '+seq);
  const lowerRead = await cli(['read', gapConv, '--seq', '1']);
  assert.equal(lowerRead.read_seq,3,'self sends advanced server read; explicit lower read is monotonic');
  const expired = await fetch(base + '/__local_fixture/expire', { method: 'POST', headers: { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }, body: JSON.stringify({ cid: gapConv, seq: 2 }) });
  assert.equal(expired.status, 200);
  const gapPage = await cli(['history', gapConv, '--after-seq', '1']);
  assert.deepEqual(gapPage.messages.map(m => m.seq), [3]);
  const gapDetail = await cli(['get', gapConv]);
  assert.equal(gapPage.my_read_seq,3); assert.equal(gapDetail.conversation.my_read_seq,3);
  assert.equal(gapDetail.conversation.unread_count,0);
  evidence.retentionGap = { cursor: 1, retainedSequences: gapPage.messages.map(m => m.seq), unreadReported: gapDetail.conversation.unread_count, retainedAfterLocalCursor: gapPage.messages.length, serverReadSeq: gapPage.my_read_seq, nativeRuntimeTested: false, bridgeSourceOutcome: 'History is not contiguous/canonical; HOLD before native delivery/read' };
  assert.ok(gapPage.retention_gap || gapPage.gaps?.length || gapPage.oldest_seq === 3);
  check('actual Worker/CLI retention metadata exposes the gap; P3 strict handling is verified separately');
  if (native) {
    const journalName = 'conversation-bridge-' + createHash('sha256').update(JSON.stringify([actor,project])).digest('hex').slice(0,24) + '.json';
    const journalPath = resolve(nativeWorkspace,'.local',journalName);
    assert.equal(await access(journalPath).then(()=>true,()=>false),false,'fresh gap-only journal, never reuse another origin');
    const stateBefore = {deliveries:{},replies:{}};
    const mapping = ['--enable-native','--actor',actor,'--project',project,'--workspace',nativeWorkspace,'--machine-id',machineId,'--conversation',gapConv];
    // All gap-probe messages are self-authored: this cannot add native mail.
    await assert.rejects(cli(['bridge','--once',...mapping]), /Conversation bridge held/);
    const held = JSON.parse(await readFile(journalPath,'utf8'));
    assert.equal(held.cursors[gapConv],1,'default HOLD cannot skip interior missing seq2');
    assert.deepEqual(held.deliveries,stateBefore.deliveries); assert.deepEqual(held.replies,stateBefore.replies);
    const heldBytes = await readFile(journalPath);
    await assert.rejects(cli(['bridge-resume',...mapping,'--resume-after-seq','3','--acknowledge-gap']), /Bridge recovery HOLD/);
    assert.deepEqual(await readFile(journalPath),heldBytes);
    const readBefore = (await cli(['get',gapConv])).conversation.my_read_seq;
    assert.equal(readBefore,3);
    assert.ok(held.cursors[gapConv] < readBefore,'LOCAL HOLD remains independent of advanced server read');
    const resumed = await cli(['bridge-resume',...mapping,'--resume-after-seq','2','--acknowledge-gap']);
    assert.equal(resumed.status,'gap_recorded'); assert.equal(resumed.server_read_advanced,false);
    assert.equal((await cli(['get',gapConv])).conversation.my_read_seq,readBefore);
    const auditBytes = await readFile(journalPath);
    await assert.rejects(cli(['bridge-resume',...mapping,'--resume-after-seq','2','--acknowledge-gap']), /Bridge recovery HOLD/);
    assert.deepEqual(await readFile(journalPath),auditBytes);
    await cli(['bridge','--once',...mapping]); await cli(['bridge','--once',...mapping]);
    const finalState = JSON.parse(await readFile(journalPath,'utf8'));
    assert.equal(finalState.cursors[gapConv],3);
    assert.equal(Object.keys(finalState.deliveries).length,0); assert.equal(Object.keys(finalState.replies).length,0);
    assert.equal((await cli(['get',gapConv])).conversation.my_read_seq,readBefore);
    assert.equal(finalState.gaps.length,1); assert.equal(finalState.gaps[0].missing_from_seq,2); assert.equal(finalState.gaps[0].missing_through_seq,2);
    assert.deepEqual(finalState.deliveries,stateBefore.deliveries); assert.deepEqual(finalState.replies,stateBefore.replies);
    assert.ok(Object.values(finalState.deliveries).every(d=>d.attempted && d.native_id));
    assert.ok(Object.values(finalState.replies).every(r=>r.done));
    evidence.retentionGap.nativeRuntimeTested = true;
    evidence.nativeJournal = {basename:journalName,cursors:finalState.cursors,deliveries:Object.keys(finalState.deliveries).length,replies:Object.keys(finalState.replies).length,gaps:finalState.gaps.length,unresolved:0};
    check('final Worker/CLI/native-bound P3 default HOLD, retained-row refusal, explicit missing-range audit, replay refusal and reconnect without extra native effects');
  }

  if (native) {
    const bridgeArgs = ['bridge', '--enable-native', '--once', '--actor', actor, '--project', project, '--workspace', nativeWorkspace, '--machine-id', machineId, '--conversation', cid];
    await cli(bridgeArgs);
    const log = JSON.parse((await exec('a', ['--json', 'message', 'log', '--workspace', nativeWorkspace])).stdout);
    const deliveries = log.filter(m => m.kind === 'hub-conversation' && m.from?.session_id === own.id && m.to?.session_id === own.id && m.body.includes(nonce));
    assert.equal(deliveries.length, 1);
    const envelope = deliveries[0];
    assert.equal(envelope.delivery, 'inbox');
    assert.equal(envelope.from.workspace, nativeWorkspace);
    const nativeText = 'native reply ' + nonce;
    const nativeReply = JSON.parse((await exec('a', ['--json', 'message', 'reply', envelope.id, nativeText])).stdout);
    await writeFile(resolve(scratch, 'native-receipts.json'), JSON.stringify({ own: { id: own.id, tag: own.tag, workspace: nativeWorkspace }, envelope, nativeReply }), { mode: 0o600 });
    await cli(bridgeArgs);
    await cli(bridgeArgs);
    await page.locator('#thread-scroll').getByText(nativeText, { exact: true }).waitFor();
    history = await cli(['history', cid, '--after-seq', '0']);
    assert.equal(history.messages.filter(m => m.body === nativeText).length, 1);
    const nativeMsg = history.messages.find(m => m.body === nativeText);
    assert.equal(nativeMsg.sender_actor, actor);
    assert.equal(nativeMsg.reply_to, original.id);
    const finalLog = JSON.parse((await exec('a', ['--json', 'message', 'log', '--workspace', nativeWorkspace])).stdout);
    assert.equal(finalLog.filter(m => m.kind === 'hub-conversation' && m.body === envelope.body).length, 1);
    const journalName = 'conversation-bridge-' + createHash('sha256').update(JSON.stringify([actor,project])).digest('hex').slice(0,24) + '.json';
    const settled = JSON.parse(await readFile(resolve(nativeWorkspace,'.local',journalName),'utf8'));
    assert.equal(settled.cursors[cid],4); assert.equal(settled.cursors[gapConv],3);
    assert.equal(Object.keys(settled.deliveries).length,1); assert.equal(Object.keys(settled.replies).length,1); assert.equal(settled.gaps.length,1);
    assert.ok(Object.values(settled.deliveries).every(d=>d.attempted && d.native_id)); assert.ok(Object.values(settled.replies).every(r=>r.done));
    evidence.nativeJournal = {basename:journalName,cursors:settled.cursors,deliveries:1,replies:1,gaps:1,unresolved:0};
    evidence.native = 'genuine own-session inbox/reply and restart reconciliation passed; second physical host untested';
    check('genuine native self-inbox, mapped reply, restart reconciliation and loop suppression');
  }
  await api('/api/conversations/' + cid + '/members/remove', 'POST', { actor });
  for (const [method, suffix, body] of [['GET', '/messages'], ['POST', '/messages', { body: 'stale denied', idempotency_key: 'stale-' + nonce }], ['POST', '/read', { seq: 1 }]]) {
    const response = await fetch(base + '/api/conversations/' + cid + suffix, { method, headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' }, ...(body ? { body: JSON.stringify(body) } : {}) });
    assert.equal(response.status, 403);
  }
  await assert.rejects(cli(['history', cid]));
  await assert.rejects(cli(['send', cid, '--body-file', '-', '--key', 'stale-cli-' + nonce], 'stale denied'));
  check('removal immediately denies stale agent history/send/read and actual CLI requests');
  evidence.httpConversationRequests = requests.length;
  evidence.messageCount = history.messages.length;
  await page.screenshot({ path: resolve(scratch, 'browser.png') });
  await writeFile(resolve(scratch, native ? 'native-evidence.json' : 'evidence.json'), JSON.stringify(evidence, null, 2), { mode: 0o600 });
  console.log(JSON.stringify(evidence));
} finally {
  clearTimeout(deadline);
  if (browser) await browser.close();
  await mf.dispose();
}

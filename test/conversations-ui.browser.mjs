/* Browser behaviour tests for the conversations UI, using the existing
   Playwright installation (no package installs). Runs standalone:
      node test/conversations-ui.browser.mjs
   Serves public/ from a local static server and mocks the REAL M1
   conversations contract (docs contract M1-UI-REPAIR.md): strict paths,
   strict payload keys, CSRF enforcement, seq cursors, idempotency replays
   and 200-JSON read cursors. Wrong paths or payload keys are rejected with
   the contract's error codes, so a guessed frontend cannot pass by
   agreeing with the mock. The production page uses the real hub API; these
   are test-only protocol fixtures (design proof, not end-to-end).
   Offline isolation: every scenario resets network state in a finally block
   before the next test, so a stuck offline mode cannot poison later
   screenshots (the ERR_INTERNET_DISCONNECTED failure mode).
   Host budget: single-process chromium is the only mode that fits the
   cgroup and survives one context; the suite runs in ONE browser with ONE
   reused page, cleared between tests, and retries once if the browser dies. */
import { createRequire } from 'node:module';
import { createServer } from 'node:http';
import { readFileSync, existsSync, mkdirSync } from 'node:fs';
import { dirname, join, extname } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const root = join(dirname(fileURLToPath(import.meta.url)), '..');
const outDir = join(root, '.local', 'tmp', 'conversations-ui');
const requireDataops = createRequire('/home/alexey/git/dataops/package.json');
const { chromium } = requireDataops('playwright');
const LAUNCH_ARGS = ['--single-process', '--in-process-gpu', '--disable-gpu', '--disable-dev-shm-usage', '--no-sandbox', '--js-flags=--thread-pool-size=1', '--num-raster-threads=1', '--disable-threaded-animation', '--disable-threaded-scrolling'];

const MIME = { '.html': 'text/html', '.js': 'text/javascript', '.css': 'text/css', '.woff2': 'font/woff2' };
const server = createServer((req, res) => {
  const path = req.url.split('?')[0];
  const file = join(root, 'public', path === '/' ? 'conversations.html' : path);
  try {
    const body = readFileSync(file);
    res.writeHead(200, { 'Content-Type': MIME[extname(file)] || 'application/octet-stream' });
    res.end(body);
  } catch (e) {
    res.writeHead(404); res.end('not found');
  }
});
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
const base = 'http://127.0.0.1:' + server.address().port;

/* ---- strict M1 contract fixtures ---- */

const ME = { authenticated: true, email: 'grace@fake.local', name: 'Grace Hopper', csrf_token: 'test-csrf' };
const SELF = 'human:user-123';
const IDKEY = /^[A-Za-z0-9._:-]{1,128}$/;
const NOW = new Date().toISOString();

let state;          // mutable fixture store, reset per test
let sendLog;        // POST /:id/messages: { conversationId, payload, csrf }
let createLog;      // POST /api/conversations: { payload, csrf }
let readLog;        // POST /:id/read: { conversationId, payload, csrf }
let messagesLog;    // GET /:id/messages: { conversationId, query }
let apiRequestCount;// every /api request (loop detection)

function participant(actor, kind, displayName, extra) {
  return Object.assign({ actor, kind, display_name: displayName }, extra || {});
}
const PARTICIPANTS = {
  self: participant(SELF, 'human', 'grace@fake.local'),
  participants: [
    participant(SELF, 'human', 'grace@fake.local'),
    participant('projA:worker-1', 'agent', 'projA:worker-1', { project: 'projA', role: 'worker', machine_id: 'machine-A' }),
    participant('platform-ui', 'agent', 'platform-ui')
  ]
};

function member(actor, kind, role, readSeq) {
  return { actor, kind, role, added_at: NOW, added_by: SELF, removed_at: null, removed_by: null, last_read_seq: readSeq || 0, last_read_at: readSeq ? NOW : null };
}
function detailShape(id, kind, title, lastSeq, members, unread) {
  const fields = {
    id, kind, title, name: title,
    created_by: SELF, created_at: NOW, updated_at: NOW,
    archived_at: null, archived_by: null,
    last_seq: lastSeq, last_message_at: lastSeq ? NOW : null,
    last_message_preview: lastSeq ? 'Latest message in ' + title : null,
    my_role: 'owner', my_read_seq: lastSeq - (unread || 0),
    unread_count: unread || 0, member_count: members.length, members
  };
  return Object.assign({}, fields, { conversation: Object.assign({}, fields) });
}
function summaryShape(id, kind, title, lastSeq, preview, readSeq, unread, memberCount) {
  return {
    id, kind, title, name: title,
    created_by: SELF, created_at: NOW, updated_at: NOW,
    archived_at: null, archived_by: null,
    last_seq: lastSeq, last_message_at: lastSeq ? NOW : null,
    last_message_preview: preview,
    my_role: 'owner', my_read_seq: readSeq,
    unread_count: unread, member_count: memberCount
  };
}
function message(id, conversationId, seq, senderActor, senderKind, body) {
  return {
    id, conversation_id: conversationId, seq,
    sender_actor: senderActor, sender_kind: senderKind,
    kind: 'message', body,
    idempotency_key: 'seed-' + conversationId + '-' + seq,
    reply_to: null, reply_to_seq: null, bridge_ref: null,
    created_at: new Date(Date.parse(NOW) - (100 - seq) * 60000).toISOString()
  };
}
const HOSTILE = 'Drafting the domain service now. <img src=x onerror="window.__pwned=1">';
const C1_MESSAGES = [
  message('m1', 'c1', 1, SELF, 'human', 'Morning! Status on the conversation API?'),
  message('m2', 'c1', 2, 'projA:worker-1', 'agent', HOSTILE)
];

function resetFixtures() {
  state = {
    me: { ...ME }, actor: SELF,
    denied: new Set(['c-denied']),
    participantsGate: null, sendGate: null, createGate: null, historyGate: null,
    historySnapshotGate: null, sendResponseGate: null, events: [],
    failNextCreates: 0, createStore: new Map(), createCount: 0,
    csrfFailures: 0,
    me401: false,
    failNextSends: 0,
    nextMessageSeq: { c1: 3, 'c-new': 1 },
    messageStore: { c1: C1_MESSAGES.slice(), c2: c2History(), 'c-empty': [], 'c-new': [] },
    idempotent: {}       // conversationId -> key -> message (replay)
  };
  sendLog = []; createLog = []; readLog = []; messagesLog = []; apiRequestCount = 0;
}
function c2History() {
  const history = [];
  for (let seq = 1; seq <= 7; seq++) history.push(message('m2-old-' + seq, 'c2', seq, seq % 2 ? 'projA:worker-1' : SELF, seq % 2 ? 'agent' : 'human', 'History message ' + seq));
  history.push(message('m8', 'c2', 8, 'projA:worker-1', 'agent', 'Closer to the milestone.'));
  history.push(message('m9', 'c2', 9, 'platform-ui', 'agent', 'Layout work started.'));
  return history;
}

function listPayload() {
  return {
    conversations: [
      summaryShape('c1', 'direct', 'projA:worker-1', 2, 'M1 domain service drafted.', 1, 1, 2),
      summaryShape('c2', 'group', 'Milestone M2', 9, 'Layout work started.', 7, 2, 3),
      summaryShape('c-empty', 'group', 'Empty room', 0, null, 0, 0, 2),
      summaryShape('c-denied', 'direct', 'Removed member', 3, 'Earlier message', 1, 2, 2)
    ],
    next_cursor: null
  };
}
function detailsPayload() {
  return {
    c1: detailShape('c1', 'direct', 'projA:worker-1', 2, [member(SELF, 'human', 'owner', 2), member('projA:worker-1', 'agent', 'member', 2)]),
    c2: detailShape('c2', 'group', 'Milestone M2', 9, [member(SELF, 'human', 'owner', 7), member('projA:worker-1', 'agent', 'member', 9), member('platform-ui', 'agent', 'member', 9)], 2),
    'c-empty': detailShape('c-empty', 'group', 'Empty room', 0, [member(SELF, 'human', 'owner', 0), member('platform-ui', 'agent', 'member', 0)]),
    'c-new': detailShape('c-new', 'direct', 'projA:worker-1', 0, [member(SELF, 'human', 'owner', 0), member('projA:worker-1', 'agent', 'member', 0)])
  };
}
function messagesPage(conversationId, messages, extra) {
  const seqs = messages.map((m) => m.seq);
  const oldest = seqs.length ? Math.min.apply(null, seqs) : null;
  const newest = seqs.length ? Math.max.apply(null, seqs) : null;
  return Object.assign({
    conversation_id: conversationId,
    self_actor: SELF,
    messages,
    last_seq: state.nextMessageSeq[conversationId] !== undefined && conversationId !== 'c1' && conversationId !== 'c2' ? state.nextMessageSeq[conversationId] - 1 : (conversationId === 'c1' ? 2 : 9),
    my_read_seq: conversationId === 'c1' ? 1 : (conversationId === 'c2' ? 7 : 0),
    unread_count: conversationId === 'c1' ? 1 : (conversationId === 'c2' ? 2 : 0),
    oldest_seq: oldest,
    newest_seq: newest,
    has_more_before: false,
    has_more_after: false,
    next_after_seq: newest,
    prev_before_seq: oldest
  }, extra || {});
}
function c2OlderPage() {
  return (state.messageStore.c2 || []).filter((m) => m.seq < 8);
}

/* Contract errors, exactly like the real service answers. */
const json = (route, status, payload) => route.fulfill({ status, contentType: 'application/json; charset=utf-8', headers: { 'Cache-Control': 'private, no-store' }, body: JSON.stringify(payload) });
const notFound = (route, path) => json(route, 404, { error: 'not_found', path });

const CONV_IDS = ['c1', 'c2', 'c-empty', 'c-denied', 'c-new'];
const SEND_ALLOWED = ['body', 'idempotency_key', 'kind', 'reply_to', 'reply_to_seq', 'bridge_ref'];
const SENDER_OVERRIDES = ['from', 'from_actor', 'sender', 'sender_actor', 'author'];

async function mockApi(route) {
  const request = route.request();
  const url = new URL(request.url());
  const path = url.pathname;
  const method = request.method();
  const requestActor = state.actor;
  apiRequestCount++;

  if (path === '/auth/me') {
    if (state.me401) return json(route, 401, { authenticated: false });
    return route.fulfill({ json: state.me });
  }
  if (path === '/auth/login') return route.fulfill({ status: 200, contentType: 'text/html', body: '<html><title>Sign in</title></html>' });
  if (path === '/auth/logout') return route.fulfill({ status: 200, contentType: 'text/html', body: '<html><title>Signed out</title></html>' });

  const csrfHeader = request.headers()['x-csrf-token'];
  const csrfOk = csrfHeader === state.me.csrf_token;
  const postBody = () => { try { return JSON.parse(request.postData() || ''); } catch (e) { return null; } };

  if (path === '/api/conversations/participants' && method === 'GET') {
    if (state.participantsFailure) return json(route, 503, { error: 'verification_unavailable' });
    const payload = { ...PARTICIPANTS, self: state.unknownIdentity ? null : participant(requestActor, 'human', state.me.email) };
    if (state.participantsGate) await state.participantsGate.promise;
    return route.fulfill({ json: payload });
  }
  if (path === '/api/conversations' && method === 'GET') {
    const allowed = ['state', 'include_archived', 'limit', 'cursor'];
    for (const key of url.searchParams.keys()) if (!allowed.includes(key)) return json(route, 400, { error: 'unknown_query_param', param: key });
    return route.fulfill({ json: { ...listPayload(), self_actor: requestActor } });
  }
  if (path === '/api/conversations' && method === 'POST') {
    if (!csrfOk) return json(route, 403, { error: 'csrf_invalid' });
    const payload = postBody();
    if (!payload || typeof payload !== 'object') return json(route, 400, { error: 'malformed_json' });
    const banned = ['member_ids', 'participants_ids'].concat(SENDER_OVERRIDES);
    for (const key of Object.keys(payload)) if (banned.includes(key)) return json(route, 400, { error: 'unknown_fields', field: key });
    if (payload.kind !== 'direct' && payload.kind !== 'group') return json(route, 400, { error: 'invalid_conversation_kind' });
    if (typeof payload.title !== 'string' || !payload.title.trim() || payload.title.length > 120) return json(route, 400, { error: 'invalid_title' });
    if (!Array.isArray(payload.members) || payload.members.some((m) => typeof m !== 'string')) return json(route, 400, { error: 'invalid_members' });
    if (typeof payload.idempotency_key !== 'string' || !IDKEY.test(payload.idempotency_key)) return json(route, 400, { error: 'invalid_idempotency_key' });
    if (payload.kind === 'direct' && payload.members.length !== 1) return json(route, 400, { error: 'invalid_direct_members' });
    createLog.push({ payload, csrf: csrfHeader });
    const fingerprint = JSON.stringify({ kind: payload.kind, title: payload.title, members: [...payload.members].sort() });
    const replay = state.createStore.get(payload.idempotency_key);
    if (replay && replay.fingerprint !== fingerprint) return json(route, 409, { error: 'conflict', reason: 'idempotency_conflict' });
    const members = [member(requestActor, 'human', 'owner', 0)].concat(payload.members.map((actor) => member(actor, actor.includes('human:') ? 'human' : 'agent', 'member', 0)));
    if (!replay) {
      state.createCount++;
      state.createStore.set(payload.idempotency_key, { fingerprint });
    }
    if (state.createGate) await state.createGate.promise;
    // Deliberate uncertain outcome: accepted above, response lost as a 500.
    if (state.failNextCreates > 0) { state.failNextCreates--; return json(route, 500, { error: 'response_lost_after_commit' }); }
    state.messageStore['c-new'] = [];
    state.nextMessageSeq['c-new'] = 1;
    return json(route, replay ? 200 : 201, { ...detailShape('c-new', payload.kind, payload.title, 0, members), replay: !!replay });
  }

  const messagesMatch = path.match(/^\/api\/conversations\/([^/]+)\/messages$/);
  if (messagesMatch) {
    const conversationId = messagesMatch[1];
    if (state.denied.has(conversationId)) return json(route, 403, { error: 'forbidden', reason: 'not_a_member' });
    if (!CONV_IDS.includes(conversationId)) return notFound(route, path);
    if (method === 'POST') {
      if (!csrfOk) return json(route, 403, { error: 'csrf_invalid' });
      const payload = postBody();
      if (!payload || typeof payload !== 'object') return json(route, 400, { error: 'malformed_json' });
      for (const key of SENDER_OVERRIDES) if (key in payload) return json(route, 400, { error: 'sender_override_forbidden', field: key });
      for (const key of Object.keys(payload)) if (!SEND_ALLOWED.includes(key)) return json(route, 400, { error: 'unknown_fields', field: key });
      if (typeof payload.body !== 'string' || !payload.body) return json(route, 400, { error: 'invalid_body' });
      if (typeof payload.idempotency_key !== 'string' || !IDKEY.test(payload.idempotency_key)) return json(route, 400, { error: 'invalid_idempotency_key' });
      sendLog.push({ conversationId, payload, csrf: csrfHeader });
      if (state.sendGate) await state.sendGate.promise;
      if (state.csrfFailures > 0) { state.csrfFailures--; await json(route, 403, { error: 'csrf_invalid' }); state.events.push('csrf-response'); return; }
      if (state.denied.has(conversationId)) return json(route, 403, { error: 'forbidden', reason: 'member_removed' });
      if (state.failNextSends > 0) { state.failNextSends--; return json(route, 500, { error: 'simulated_failure' }); }
      const replay = state.idempotent[conversationId] && state.idempotent[conversationId][payload.idempotency_key];
      const created = replay || message('m-new-' + state.nextMessageSeq[conversationId], conversationId, state.nextMessageSeq[conversationId], requestActor, 'human', payload.body);
      if (!replay) {
        state.nextMessageSeq[conversationId] = (state.nextMessageSeq[conversationId] || 1) + 1;
        state.idempotent[conversationId] = state.idempotent[conversationId] || {};
        state.idempotent[conversationId][payload.idempotency_key] = created;
        state.messageStore[conversationId] = (state.messageStore[conversationId] || []).concat([created]);
      }
      state.events.push('post-commit:' + requestActor);
      if (state.sendResponseGate) await state.sendResponseGate.promise;
      await json(route, replay ? 200 : 201, Object.assign({}, created, { replay: !!replay, message: created }));
      state.events.push('post-response:' + requestActor);
      return;
    }
    if (method === 'GET') {
      const allowed = ['after_seq', 'since_seq', 'before_seq', 'limit'];
      for (const key of url.searchParams.keys()) if (!allowed.includes(key)) return json(route, 400, { error: 'unknown_query_param', param: key });
      const after = url.searchParams.has('after_seq') ? Number(url.searchParams.get('after_seq')) : null;
      const before = url.searchParams.has('before_seq') ? Number(url.searchParams.get('before_seq')) : null;
      if (after !== null && before !== null) return json(route, 400, { error: 'invalid_pagination' });
      if ((after !== null && (!Number.isInteger(after) || after < 0)) || (before !== null && (!Number.isInteger(before) || before < 1))) return json(route, 400, { error: 'invalid_pagination' });
      messagesLog.push({ conversationId, query: Object.fromEntries(url.searchParams.entries()) });
      if (state.historySnapshotGate?.actor === requestActor) {
        state.events.push('history-wait:' + requestActor);
        await state.historySnapshotGate.promise;
      }
      const all = state.messageStore[conversationId] || [];
      state.events.push('history-snapshot:' + requestActor + ':' + all.length);
      let page;
      if (conversationId === 'c2' && before === null && after === null) {
        page = messagesPage('c2', all.filter((m) => m.seq >= 8), { has_more_before: true, prev_before_seq: 8 });
      } else if (before !== null) {
        const older = all.filter((m) => m.seq < before).slice(-50);
        page = messagesPage(conversationId, older, { has_more_before: older.length > 0 && Math.min.apply(null, older.map((m) => m.seq)) > 1, prev_before_seq: older.length ? Math.min.apply(null, older.map((m) => m.seq)) : null });
      } else if (after !== null) {
        page = messagesPage(conversationId, all.filter((m) => m.seq > after), { prev_before_seq: null });
      } else {
        page = messagesPage(conversationId, all.slice(-50));
      }
      page.self_actor = requestActor;
      if (state.historyGate && state.historyGate.id === conversationId) await state.historyGate.promise;
      return route.fulfill({ json: page });
    }
    return json(route, 405, { error: 'method_not_allowed' });
  }

  const readMatch = path.match(/^\/api\/conversations\/([^/]+)\/read$/);
  if (readMatch && method === 'POST') {
    const conversationId = readMatch[1];
    if (state.denied.has(conversationId)) return json(route, 403, { error: 'forbidden', reason: 'not_a_member' });
    if (!CONV_IDS.includes(conversationId)) return notFound(route, path);
    if (!csrfOk) return json(route, 403, { error: 'csrf_invalid' });
    const payload = postBody();
    if (!payload || typeof payload !== 'object') return json(route, 400, { error: 'malformed_json' });
    for (const key of Object.keys(payload)) if (!['seq', 'read_seq', 'message_id'].includes(key)) return json(route, 400, { error: 'unknown_fields', field: key });
    if ('last_read_message_id' in payload) return json(route, 400, { error: 'unknown_fields', field: 'last_read_message_id' });
    const seq = typeof payload.seq === 'number' ? payload.seq : 0;
    readLog.push({ conversationId, payload, csrf: csrfHeader });
    return json(route, 200, { conversation_id: conversationId, actor: SELF, read_seq: seq, last_seq: state.nextMessageSeq[conversationId] - 1 || 2, unread_count: 0, last_read_at: NOW });
  }

  const detailMatch = path.match(/^\/api\/conversations\/([^/]+)$/);
  if (detailMatch && method === 'GET') {
    const conversationId = detailMatch[1];
    if (state.denied.has(conversationId)) return json(route, 403, { error: 'forbidden', reason: 'not_a_member' });
    const detail = detailsPayload()[conversationId];
    if (!detail) return notFound(route, path);
    return route.fulfill({ json: detail });
  }

  return notFound(route, path);
}

/* Node-side condition polling (logs live in Node, not in the page). */
async function until(fn, timeout, step) {
  const limit = Date.now() + (timeout || 5000);
  while (Date.now() < limit) {
    if (await fn()) return;
    await new Promise((resolve) => setTimeout(resolve, step || 50));
  }
  throw new Error('condition not met within ' + (timeout || 5000) + 'ms');
}
function gate(extra = {}) {
  let release;
  const promise = new Promise((resolve) => { release = resolve; });
  return { promise, release, ...extra };
}
function switchToAda() {
  state.actor = 'human:user-456';
  state.me = { ...ME, email: 'ada@fake.local', name: 'Ada Lovelace', csrf_token: 'ada-csrf' };
}
const diskKey = (actor, id) => 'conv-outbox:v2:' + JSON.stringify([actor, id]);

async function runSuite(page) {
  let passed = 0, failed = 0;
  const pageErrors = [];
  const consoleErrors = [];
  const failedRequests = [];
  page.on('pageerror', (error) => pageErrors.push(String(error).split('\n')[0]));
  page.on('console', (message) => { if (message.type() === 'error') consoleErrors.push(message.text().split('\n')[0]); });
  page.on('requestfailed', (request) => failedRequests.push(request.url() + ' ' + (request.failure() && request.failure().errorText)));
  // The route persists on the page for the whole suite.
  await page.route('**/*', (route) => {
    const url = new URL(route.request().url());
    if (url.pathname.startsWith('/auth/') || url.pathname.startsWith('/api/')) return mockApi(route);
    return route.fallback();
  });

  // Fresh state per test: same-origin blank-ish page, cleared storage, theme set
  // before the app boots (the theme boot reads localStorage at document start).
  async function freshPage({ theme, viewport } = {}) {
    resetFixtures();
    await page.setViewportSize(viewport || { width: 1440, height: 900 });
    await page.goto(base + '/conversations.css'); // same origin, no app boot
    await page.evaluate(() => { try { localStorage.clear(); sessionStorage.clear(); } catch (e) {} });
    if (theme) await page.evaluate((value) => { try { localStorage.setItem('dakit-theme', value); } catch (e) {} }, theme);
    pageErrors.length = 0;
    consoleErrors.length = 0;
    failedRequests.length = 0;
  }
  const expectNoPageErrors = () => {
    if (pageErrors.length) throw new Error('page errors: ' + pageErrors.join(' | '));
    // Deliberate 4xx/5xx API fixtures log browser resource errors; those are
    // by design. Any other console error (broken local CSS/JS/fonts, JS
    // exceptions surfaced as console errors) fails the test.
    const unexpected = consoleErrors.filter((text) => !/^Failed to load resource/.test(text));
    if (unexpected.length) throw new Error('console errors: ' + unexpected.join(' | '));
  };
  /* Type a draft and send it; one defensive re-press guards against a
     re-render landing between fill and Enter. */
  async function sendDraft(text) {
    await page.fill('#composer-field', text);
    await page.press('#composer-field', 'Enter');
    try {
      await until(() => sendLog.length > 0, 3000, 50);
    } catch (e) {
      await page.press('#composer-field', 'Enter');
      await until(() => sendLog.length > 0, 5000, 50);
    }
  }

  await test('conversation list renders summaries with unread badges and totals', async () => {
    await freshPage();
    await page.goto(base + '/');
    await page.waitForSelector('#conv-list .dk-split__item');
    assert.equal(await page.locator('#conv-list .dk-split__item').count(), 4, 'four conversations listed');
    const names = await page.locator('#conv-list .dk-split__name').allTextContents();
    assert.deepEqual(names, ['projA:worker-1', 'Milestone M2', 'Empty room', 'Removed member'], 'titles come from the server (summaries carry no members)');
    // Desktop auto-opens the first conversation, which consumes its unread:
    // the thread view renders and the remaining badges are c2 + c-denied.
    await page.waitForSelector('.msg__body');
    await page.waitForFunction(() => document.querySelector('#unread-total').textContent === '4');
    const badges = await page.locator('#conv-list .dk-count').allTextContents();
    assert.deepEqual(badges, ['2', '2'], 'unread badges from unread_count after the auto-opened conversation is read');
    await page.waitForFunction(() => document.querySelector('#conv-state').textContent.startsWith('Updated'));
    expectNoPageErrors();
  });

  await test('thread renders contract messages as inert text and lists active member actors', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    const bodies = page.locator('.msg__body');
    assert.equal(await bodies.count(), 2, 'two messages rendered');
    const hostile = bodies.nth(1);
    assert.equal(await hostile.locator('img').count(), 0, 'no img element inside message body');
    assert.ok((await hostile.textContent()).includes('<img src=x'), 'hostile markup shown as literal text');
    assert.equal(await page.evaluate(() => window.__pwned), undefined, 'onerror never ran');
    assert.equal(await page.locator('.conv-detail-head__title').textContent(), 'projA:worker-1', 'detail title from the conversation resource');
    const meta = await page.locator('.conv-detail-head__meta').textContent();
    assert.ok(meta.includes('You (human)') && meta.includes('projA:worker-1 (agent)'), 'active members listed with opaque actors and kinds');
    assert.equal(await page.locator('.msg__sender').first().textContent(), 'You', 'verified self.actor renders as You');
    assert.equal(await page.locator('.msg__sender').nth(1).textContent(), 'projA:worker-1', 'agent sender label from sender_actor');
    expectNoPageErrors();
  });

  await test('read cursor posts the rendered seq and consumes the 200 JSON result', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    await until(() => readLog.length === 1);
    assert.equal(readLog[0].conversationId, 'c1');
    assert.deepEqual(Object.keys(readLog[0].payload).sort(), ['seq'], 'read body is exactly { seq }');
    assert.equal(readLog[0].payload.seq, 2, 'read seq is the last rendered message seq');
    assert.equal(readLog[0].csrf, ME.csrf_token, 'read mutation carries the CSRF token');
    expectNoPageErrors();
  });

  await test('send flow posts body plus idempotency key and confirms in thread', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body'); // thread settled; later renders keep the draft via snapshot
    await sendDraft('hello thread');
    await until(() => sendLog.length === 1);
    assert.equal(sendLog[0].payload.body, 'hello thread');
    assert.ok(IDKEY.test(sendLog[0].payload.idempotency_key), 'idempotency key matches the contract grammar');
    assert.equal(sendLog[0].csrf, ME.csrf_token, 'send carries the CSRF token');
    await page.waitForSelector('.msg__body >> text=hello thread');
    expectNoPageErrors();
  });

  await test('failed send keeps the draft, retry reuses key and payload', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    await page.waitForSelector('#composer-field');
    state.failNextSends = 1;
    await sendDraft('will fail once');
    await page.waitForSelector('.dk-badge--danger');
    assert.ok(await page.locator('.msg__body >> text=will fail once').count(), 'failed draft stays visible');
    await page.click('button[aria-label="Retry sending this message"]');
    await until(() => sendLog.length === 2);
    assert.equal(sendLog[1].payload.idempotency_key, sendLog[0].payload.idempotency_key, 'retry reuses the idempotency key');
    assert.equal(sendLog[1].payload.body, sendLog[0].payload.body, 'retry keeps the exact draft');
    await page.waitForFunction(() => !document.querySelector('.dk-badge--danger'), null, { timeout: 5000 });
    await page.waitForSelector('.msg__body >> text=will fail once');
    expectNoPageErrors();
  });

  await test('failed draft survives a full reload and can be retried', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    await page.waitForSelector('#composer-field');
    state.failNextSends = 1;
    await sendDraft('draft that must survive');
    await page.waitForSelector('.dk-badge--danger');
    const firstKey = sendLog[0].payload.idempotency_key;
    const firstPayload = { ...sendLog[0].payload };
    const saved = await page.evaluate((key) => JSON.parse(localStorage.getItem(key)), diskKey(SELF, 'c1'));
    assert.equal(saved[0].actor, SELF);
    assert.equal(saved[0].conversationId, 'c1');
    await page.reload();
    await page.waitForSelector('.dk-badge--danger');
    assert.ok(await page.locator('.msg__body >> text=draft that must survive').count(), 'draft restored from localStorage');
    state.failNextSends = 0;
    await page.click('button[aria-label="Retry sending this message"]');
    await page.waitForFunction(() => !document.querySelector('.dk-badge--danger'), null, { timeout: 5000 });
    assert.equal(sendLog[1].payload.idempotency_key, firstKey, 'persisted draft retries with the original key');
    assert.deepEqual(sendLog[1].payload, firstPayload, 'same actor reload retains the exact key and payload');
    expectNoPageErrors();
  });

  await test('auth expiry redirects to the login page', async () => {
    await freshPage();
    state.me401 = true;
    await page.goto(base + '/');
    await page.waitForURL('**/auth/login');
  });

  await test('offline state disables sending and recovers on reconnect (network state reset afterwards)', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    const context = page.context();
    try {
      await context.setOffline(true);
      // Playwright's offline emulation does not dispatch window events in
      // this single-process mode; the UI's offline branch is event-driven,
      // so deliver the same event the browser would deliver.
      await page.evaluate(() => window.dispatchEvent(new Event('offline')));
      await page.waitForSelector('#composer-offline');
      assert.ok(await page.locator('.composer__send[disabled]').count(), 'send disabled while offline');
      const draft = 'typed while offline';
      // page.fill's actionability pipeline stalls under offline emulation in
      // single-process mode; focus + keyboard is the same user path anyway.
      await page.focus('#composer-field');
      await page.keyboard.type(draft);
      await context.setOffline(false);
      await page.evaluate(() => window.dispatchEvent(new Event('online')));
      await page.waitForFunction(() => !document.getElementById('composer-offline'));
      assert.equal(await page.inputValue('#composer-field'), draft, 'draft text preserved across offline transition');
      assert.equal(await page.locator('.composer__send[disabled]').count(), 0, 'send re-enabled after reconnect');
    } finally {
      await context.setOffline(false); // never leak offline mode into later tests
    }
    expectNoPageErrors();
  });

  await test('load older messages pages by before_seq without duplicates', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c2');
    await page.waitForSelector('button >> text=Load older messages');
    await page.click('button >> text=Load older messages');
    await page.waitForFunction(() => !document.querySelector('.conv-thread__top button'), null, { timeout: 5000 });
    const olderCalls = messagesLog.filter((entry) => entry.conversationId === 'c2' && entry.query.before_seq);
    assert.equal(olderCalls.length, 1, 'exactly one older-history request');
    assert.equal(olderCalls[0].query.before_seq, '8', 'cursor is prev_before_seq from the first page');
    assert.ok(!('before' in olderCalls[0].query), 'no message-id cursor is ever sent');
    assert.equal(await page.locator('.msg__body').count(), 9, 'all nine seqs, no duplicates');
    expectNoPageErrors();
  });

  await test('new conversation dialog offers verified participants and creates with title + canonical members', async () => {
    await freshPage();
    await page.goto(base + '/');
    await page.waitForSelector('#conv-list .dk-split__item');
    await page.click('#new-conversation');
    await page.waitForSelector('#identity-list .conv-identity');
    assert.equal(await page.locator('#identity-list .conv-identity').count(), 2, 'participants listed, verified self excluded');
    await page.fill('#identity-filter', 'worker-1');
    assert.equal(await page.locator('#identity-list .conv-identity').count(), 1, 'filter narrows participants');
    await page.click('#identity-list .conv-identity input');
    await page.click('#new-conversation-create');
    await until(() => createLog.length === 1);
    assert.equal(createLog[0].payload.kind, 'direct');
    assert.equal(createLog[0].payload.title, 'projA:worker-1', 'direct title derived from the participant display name');
    assert.deepEqual(createLog[0].payload.members, ['projA:worker-1'], 'members are canonical opaque actors');
    assert.ok(IDKEY.test(createLog[0].payload.idempotency_key), 'create is idempotent');
    assert.equal(createLog[0].csrf, ME.csrf_token, 'create carries the CSRF token');
    await page.waitForFunction(() => location.search.includes('conversation=c-new'));
    await page.waitForSelector('.conv-detail-head__title');
    expectNoPageErrors();
  });

  await test('membership 403 shows an access error, stops the poll loop and preserves drafts', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c-denied');
    await page.waitForSelector('#thread-error');
    assert.ok((await page.locator('#thread-error').textContent()).includes('You do not have access'), 'access error surfaced');
    assert.equal(await page.locator('#thread-error button').count(), 0, 'no retry loop offered for membership failures');
    await page.waitForSelector('#composer-field');
    assert.equal(await page.locator('#composer-field').isDisabled(), true);
    assert.equal(await page.locator('.composer__send').isDisabled(), true);
    await page.evaluate(() => {
      document.querySelector('.composer__send').click();
      document.getElementById('composer').dispatchEvent(new Event('submit', { cancelable: true }));
      document.getElementById('composer-field').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    const requestsAfterError = apiRequestCount;
    await page.waitForTimeout(1200);
    assert.ok(apiRequestCount - requestsAfterError <= 1, 'no unauthorized request loop (list poll only)');
    assert.equal(sendLog.length, 0, 'denied click, submit and Enter never POST');
    expectNoPageErrors();
  });

  await test('unknown identity hides outboxes; legacy keys are purged without attribution', async () => {
    await freshPage();
    const hold = state.participantsGate = gate();
    state.unknownIdentity = true;
    await page.evaluate(({ key, actor }) => {
      const entry = { actor, conversationId: 'c1', clientId: 'p1', body: 'private old draft', idempotencyKey: 'same-key', pending: 'failed' };
      localStorage.setItem(key, JSON.stringify([entry]));
      localStorage.setItem('conv-outbox:c1', JSON.stringify([entry]));
    }, { key: diskKey(SELF, 'c1'), actor: SELF });
    await page.goto(base + '/?conversation=c1');
    assert.equal(await page.locator('.msg__body').count(), 0, 'identity pending renders no draft');
    assert.equal(sendLog.length, 0);
    hold.release(); state.participantsGate = null;
    await page.waitForTimeout(150);
    // Retry verification with no authoritative self. No message self_actor fallback.
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.waitForTimeout(150);
    assert.equal(await page.locator('.msg__body').count(), 0);
    assert.equal(await page.evaluate(() => localStorage.getItem('conv-outbox:c1')), null);
    assert.equal(sendLog.length, 0);
    expectNoPageErrors();
  });

  await test('Grace failed send then Ada reload never restores, sends or reuses Grace draft', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('Grace secret reload');
    await page.waitForSelector('.dk-badge--danger');
    const oldKey = sendLog[0].payload.idempotency_key;
    switchToAda();
    await page.reload();
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
    await page.waitForSelector('.msg__body');
    assert.ok(!(await page.locator('#conv-detail').textContent()).includes('Grace secret reload'));
    assert.equal(await page.locator('[aria-label="Retry sending this message"]').count(), 0);
    assert.equal(await page.evaluate((key) => localStorage.getItem(key), diskKey(SELF, 'c1')), null, 'prior actor disk data purged');
    await page.fill('#composer-field', 'Ada message');
    await page.press('#composer-field', 'Enter');
    await until(() => sendLog.length === 2);
    assert.equal(sendLog[1].payload.body, 'Ada message');
    assert.notEqual(sendLog[1].payload.idempotency_key, oldKey);
    assert.equal(sendLog[1].csrf, state.me.csrf_token);
    expectNoPageErrors();
  });

  await test('live account change clears failed and unsent drafts before Ada chrome renders', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('Grace secret live');
    await page.waitForSelector('.dk-badge--danger');
    await page.fill('#composer-field', 'Grace unsent live');
    await page.evaluate(() => {
      window.__leaked = false;
      new MutationObserver(() => {
        if (document.getElementById('account-name').textContent === 'Ada Lovelace' &&
          (document.getElementById('conv-detail').textContent.includes('Grace secret live') ||
           (document.getElementById('composer-field') || {}).value === 'Grace unsent live')) window.__leaked = true;
      }).observe(document.body, { childList: true, subtree: true, characterData: true });
    });
    switchToAda();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
    await page.waitForSelector('.msg__body');
    assert.equal(await page.evaluate(() => window.__leaked), false);
    assert.equal(await page.inputValue('#composer-field'), '');
    assert.equal(await page.locator('[aria-label="Retry sending this message"]').count(), 0);
    assert.equal(sendLog.length, 1, 'live refresh never replays old draft');
    expectNoPageErrors();
  });

  await test('retry preflight detects account change even without focus event', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('Grace retry secret');
    await page.waitForSelector('.dk-badge--danger');
    switchToAda();
    await page.click('[aria-label="Retry sending this message"]');
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
    assert.equal(sendLog.length, 1, 'no old-user POST with Ada credentials');
    assert.ok(!(await page.locator('#conv-detail').textContent()).includes('Grace retry secret'));
    await page.waitForSelector('.msg__body');
    await page.fill('#composer-field', 'Ada can continue');
    await page.press('#composer-field', 'Enter');
    await until(() => sendLog.length === 2);
    assert.equal(sendLog[1].payload.body, 'Ada can continue', 'new actor route resumes after aborting old retry');
    expectNoPageErrors();
  });

  for (const outcome of ['success', 'csrf_invalid']) {
    await test('old-user in-flight send ' + outcome + ' cannot attach or replay after account switch', async () => {
      await freshPage();
      await page.goto(base + '/?conversation=c1');
      await page.waitForSelector('.msg__body');
      const hold = state.sendGate = gate();
      await sendDraft('Grace held send');
      await until(() => sendLog.length === 1);
      switchToAda();
      if (outcome === 'success') {
        await page.evaluate(() => window.dispatchEvent(new Event('focus')));
        await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
        // Same mock store continues through the account switch. Ada's initial
        // history GET must finish before the held Grace POST commits, or a
        // later fulfill of that GET observes the 201 via shared history.
        await page.waitForFunction(() => document.querySelectorAll('.msg__body').length === 2);
        assert.ok(state.events.includes('history-snapshot:human:user-456:2'));
      } else state.csrfFailures = 1;
      const laterHistory = state.historySnapshotGate = gate({actor:'human:user-456'});
      hold.release(); state.sendGate = null;
      await until(() => state.events.includes(outcome === 'success' ? 'post-response:' + SELF : 'csrf-response'));
      await page.evaluate(() => new Promise(requestAnimationFrame));
      await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
      if (outcome === 'success') {
        assert.equal(state.messageStore.c1.length, 3, 'same store committed old actor message');
        assert.ok(state.events.indexOf('history-snapshot:human:user-456:2') < state.events.indexOf('post-commit:' + SELF));
        assert.equal(await page.locator('.msg__body').count(), 2, 'stale 201 cannot append');
      }
      assert.equal(sendLog.length, 1, 'no replay with new CSRF/account');
      assert.ok(!(await page.locator('#conv-detail').textContent()).includes('Grace held send'));
      assert.equal(await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('conv-outbox:')).some((k) => localStorage[k].includes('Grace held send'))), false);
      expectNoPageErrors();
      laterHistory.release(); state.historySnapshotGate = null;
    });
  }

  await test('commit before new-actor history includes authorized third row, stale 201 cannot duplicate it', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    const store = state.messageStore;
    const hold = state.sendGate = gate();
    const responseHold = state.sendResponseGate = gate();
    await sendDraft('Grace held send');
    await until(() => sendLog.length === 1);
    switchToAda();
    const historyHold = state.historySnapshotGate = gate({actor:'human:user-456'});
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await until(() => state.events.includes('history-wait:human:user-456'));
    hold.release(); state.sendGate = null;
    await until(() => state.events.includes('post-commit:' + SELF));
    assert.equal(state.messageStore, store, 'one storage context throughout switch');
    assert.equal(store.c1.length, 3);
    historyHold.release(); state.historySnapshotGate = null;
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace' && document.querySelectorAll('.msg__body').length === 3);
    assert.ok(state.events.indexOf('post-commit:' + SELF) < state.events.indexOf('history-snapshot:human:user-456:3'));
    responseHold.release(); state.sendResponseGate = null;
    await until(() => state.events.includes('post-response:' + SELF));
    await page.evaluate(() => new Promise(requestAnimationFrame));
    assert.equal(await page.locator('.msg__body').count(), 3);
    assert.equal(await page.locator('.msg__body').filter({hasText:'Grace held send'}).count(), 1, 'authorized history row appears exactly once');
    assert.equal(sendLog.length, 1, 'no replay with new account');
    assert.equal(await page.evaluate(() => Object.keys(localStorage).filter(k => k.startsWith('conv-outbox:')).some(k => localStorage[k].includes('Grace held send'))), false);
    expectNoPageErrors();
  });

  await test('uncertain accepted create retries exact canonical payload/key once without duplication', async () => {
    await freshPage();
    await page.goto(base + '/');
    await page.waitForSelector('.msg__body');
    await page.click('#new-conversation');
    await page.click('#identity-list input[value="projA:worker-1"]');
    state.failNextCreates = 1;
    await page.click('#new-conversation-create');
    await page.waitForSelector('#new-conversation-error:not([hidden])');
    assert.equal(state.createCount, 1, 'first request accepted despite 500');
    await page.click('#new-conversation-create');
    await until(() => createLog.length === 2);
    assert.deepEqual(createLog[1].payload, createLog[0].payload);
    assert.equal(state.createCount, 1, 'retry is replay, not duplicate');
    await page.waitForURL('**/*conversation=c-new');
    expectNoPageErrors();
  });

  await test('changed create payload rotates key; unchanged sorted members retain key', async () => {
    await freshPage();
    await page.goto(base + '/');
    await page.waitForSelector('.msg__body');
    await page.click('#new-conversation');
    await page.check('input[name="conv-kind"][value="group"]');
    await page.fill('#group-title', 'Pair group');
    await page.click('#identity-list input[value="projA:worker-1"]');
    state.failNextCreates = 3;
    await page.click('#new-conversation-create');
    await page.waitForSelector('#new-conversation-error:not([hidden])');
    await page.fill('#group-title', 'Changed group');
    await page.click('#identity-list input[value="platform-ui"]');
    await page.click('#new-conversation-create');
    await until(() => createLog.length === 2);
    await page.waitForSelector('#new-conversation-error:not([hidden])');
    assert.notEqual(createLog[1].payload.idempotency_key, createLog[0].payload.idempotency_key);
    assert.deepEqual(createLog[1].payload.members, ['platform-ui', 'projA:worker-1']);
    await page.uncheck('#identity-list input[value="projA:worker-1"]');
    await page.check('#identity-list input[value="projA:worker-1"]');
    await page.click('#new-conversation-create');
    await until(() => createLog.length === 3);
    assert.deepEqual(createLog[2].payload, createLog[1].payload, 'same membership despite toggling order');
    assert.equal(state.createCount, 2);
    expectNoPageErrors();
  });

  await test('current membership loss disables composer/retry, preserves visible own text and stops POST/poll', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('own failed before removal');
    await page.waitForSelector('.dk-badge--danger');
    await page.fill('#composer-field', 'own unsent before removal');
    state.denied.add('c1');
    await page.evaluate(() => window.dispatchEvent(new Event('online')));
    await page.waitForSelector('#thread-error:not([hidden])');
    assert.equal(await page.inputValue('#composer-field'), 'own unsent before removal');
    assert.equal(await page.locator('#composer-field').isDisabled(), true);
    assert.equal(await page.locator('.composer__send').isDisabled(), true);
    assert.equal(await page.locator('[aria-label="Retry sending this message"]').isDisabled(), true);
    const count = apiRequestCount;
    await page.evaluate(() => {
      document.querySelector('.composer__send').click();
      document.querySelector('[aria-label="Retry sending this message"]').click();
      document.getElementById('composer').dispatchEvent(new Event('submit', { cancelable: true }));
      document.getElementById('composer-field').dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
    });
    await page.waitForTimeout(10500);
    assert.equal(sendLog.length, 1, 'neither draft can be sent after removal');
    assert.equal(apiRequestCount, count, 'thread poll stopped beyond its normal interval');
    expectNoPageErrors();
  });

  await test('UTF-8 limit rejects multibyte overflow and accepts exact 16,384-byte payload', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    await page.fill('#composer-field', '😀'.repeat(4097));
    await page.press('#composer-field', 'Enter');
    assert.equal(sendLog.length, 0);
    assert.equal(await page.evaluate(() => document.getElementById('composer-field').validity.customError), true);
    await page.fill('#composer-field', '😀'.repeat(4096));
    await page.press('#composer-field', 'Enter');
    await until(() => sendLog.length === 1);
    assert.equal(Buffer.byteLength(sendLog[0].payload.body, 'utf8'), 16384);
    expectNoPageErrors();
  });

  await test('opaque actor components cannot alias another outbox or legacy colon key', async () => {
    await freshPage();
    state.actor = 'human:opaque:%:"';
    const ownKey = diskKey(state.actor, 'c1');
    const foreignKey = diskKey('human:opaque', '%:":c1');
    await page.evaluate(({ ownKey, foreignKey, actor }) => {
      localStorage.setItem(ownKey, JSON.stringify([{ actor, conversationId: 'c1', clientId: 'own', body: 'own opaque draft', idempotencyKey: 'opaque-key', pending: 'failed' }]));
      localStorage.setItem(foreignKey, JSON.stringify([{ actor: 'human:opaque', conversationId: '%:":c1', clientId: 'other', body: 'foreign opaque draft', idempotencyKey: 'foreign-key', pending: 'failed' }]));
      localStorage.setItem('conv-outbox:' + actor + ':c1', JSON.stringify([{ body: 'legacy ambiguous draft', idempotencyKey: 'legacy-key' }]));
    }, { ownKey, foreignKey, actor: state.actor });
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.dk-badge--danger');
    const text = await page.locator('#conv-detail').textContent();
    assert.ok(text.includes('own opaque draft'));
    assert.ok(!text.includes('foreign opaque draft') && !text.includes('legacy ambiguous draft'));
    assert.deepEqual(await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('conv-outbox:'))), [ownKey]);
    expectNoPageErrors();
  });

  await test('unavailable identity verification seals drafts until the same actor can be verified again', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('sealed own draft');
    await page.waitForSelector('.dk-badge--danger');
    const first = { ...sendLog[0].payload };
    state.participantsFailure = true;
    await page.click('[aria-label="Retry sending this message"]');
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Verifying session');
    assert.equal(await page.locator('.msg__body').count(), 0, 'unknown identity cannot render even a previously verified outbox');
    assert.equal(sendLog.length, 1);
    state.participantsFailure = false;
    await page.click('#conv-list-state button');
    await page.waitForSelector('.dk-badge--danger');
    await page.click('[aria-label="Retry sending this message"]');
    await until(() => sendLog.length === 2);
    assert.deepEqual(sendLog[1].payload, first, 'reverified same actor retains exact payload and key');
    expectNoPageErrors();
  });

  await test('route change aborts old history; late messages cannot render or move the read cursor', async () => {
    await freshPage();
    const hold = state.historyGate = gate({ id: 'c1' });
    await page.goto(base + '/?conversation=c1');
    await until(() => messagesLog.some((r) => r.conversationId === 'c1'));
    await page.click('#conv-list a[href$="conversation=c2"]');
    await page.waitForSelector('.msg__body >> text=Closer to the milestone.');
    hold.release(); state.historyGate = null;
    await until(() => failedRequests.some((r) => r.includes('/c1/messages') && r.includes('ERR_ABORTED')));
    assert.ok(!(await page.locator('#conv-detail').textContent()).includes(HOSTILE));
    assert.equal(readLog.filter((r) => r.conversationId === 'c1').length, 0);
    expectNoPageErrors();
  });

  await test('signout purges actor data and aborts held sends before navigation', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    const hold = state.sendGate = gate();
    await sendDraft('secret held during signout');
    await page.evaluate(() => document.getElementById('sign-out').click());
    await page.waitForURL('**/auth/login');
    hold.release(); state.sendGate = null;
    assert.equal(await page.evaluate(() => Object.keys(localStorage).filter((k) => k.startsWith('conv-outbox:')).length), 0);
    assert.equal(sendLog.length, 1);
    expectNoPageErrors();
  });

  await test('old-user accepted create response cannot route a new account to the old intent', async () => {
    await freshPage();
    await page.goto(base + '/');
    await page.waitForSelector('.msg__body');
    await page.click('#new-conversation');
    await page.click('#identity-list input[value="projA:worker-1"]');
    const hold = state.createGate = gate();
    await page.click('#new-conversation-create');
    await until(() => createLog.length === 1);
    switchToAda();
    await page.evaluate(() => window.dispatchEvent(new Event('focus')));
    await page.waitForFunction(() => document.getElementById('account-name').textContent === 'Ada Lovelace');
    hold.release(); state.createGate = null;
    await page.waitForTimeout(150);
    assert.equal(new URL(page.url()).searchParams.get('conversation'), 'c1');
    assert.equal(await page.locator('#new-conversation-dialog').isVisible(), false);
    assert.equal(createLog.length, 1, 'no replay as Ada');
    expectNoPageErrors();
  });

  await test('same-actor csrf_invalid refresh retries only once with exact send payload/key', async () => {
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.csrfFailures = 2;
    await sendDraft('csrf bounded retry');
    await page.waitForSelector('.dk-badge--danger');
    assert.equal(sendLog.length, 2, 'single refresh retry, no loop');
    assert.deepEqual(sendLog[1].payload, sendLog[0].payload);
    assert.equal(await page.locator('.composer__send').isDisabled(), true);
    expectNoPageErrors();
  });

  await test('official mobile split and 16px account chevron work in both themes', async () => {
    for (const theme of ['light', 'dark']) {
      await freshPage({ theme, viewport: { width: 390, height: 844 } });
      await page.goto(base + '/');
      await page.waitForSelector('#conv-list .dk-split__item');
      assert.equal(await page.locator('#conv-detail').isVisible(), false, 'mobile list only');
      assert.equal(await page.locator('.dk-split__list').isVisible(), true);
      await page.click('#conv-list .dk-split__item >> nth=0');
      await page.waitForSelector('.msg__body');
      assert.equal(await page.locator('.dk-split__list').isVisible(), false);
      assert.equal(await page.locator('#conv-detail').isVisible(), true);
      await page.click('.dk-split__back');
      assert.equal(await page.locator('#conv-detail').isVisible(), false);
      assert.equal(await page.locator('.dk-split__list').isVisible(), true);
      await page.setViewportSize({ width: 1440, height: 900 });
      await page.click('#conv-list .dk-split__item >> nth=0');
      await page.waitForSelector('.msg__body');
      assert.equal(await page.locator('.dk-split__list').isVisible(), true);
      assert.equal(await page.locator('#conv-detail').isVisible(), true);
      const box = await page.locator('.dk-account-trigger__chev').boundingBox();
      assert.equal(box.width, 16); assert.equal(box.height, 16);
      expectNoPageErrors();
    }
  });

  await test('screenshots: four theme/viewport screens plus scenario states', async () => {
    mkdirSync(outDir, { recursive: true });
    const combos = [
      ['desktop-light', 1440, 900, 'light'],
      ['desktop-dark', 1440, 900, 'dark'],
      ['mobile-light', 390, 844, 'light'],
      ['mobile-dark', 390, 844, 'dark']
    ];
    for (const [name, width, height, theme] of combos) {
      await freshPage({ theme, viewport: { width, height } });
      await page.goto(base + '/?conversation=c1');
      await page.waitForSelector('.msg__body');
      await page.evaluate(() => document.fonts.ready);
      await page.screenshot({ path: join(outDir, `conversations-${name}.png`) });
      await freshPage({ theme, viewport: { width, height } });
      await page.goto(base + '/');
      await page.waitForSelector('#conv-list .dk-split__item');
      await page.screenshot({ path: join(outDir, `conversations-${name}-list.png`) });
      if (!existsSync(join(outDir, `conversations-${name}.png`))) throw new Error('screenshot missing: ' + name);
    }
    // Scenario: empty thread
    await freshPage({ viewport: { width: 1440, height: 900 } });
    await page.goto(base + '/?conversation=c-empty');
    await page.waitForSelector('.dk-empty__title');
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({ path: join(outDir, 'conversations-scenario-empty.png') });
    // Scenario: membership denied error state
    await freshPage();
    await page.goto(base + '/?conversation=c-denied');
    await page.waitForSelector('#thread-error');
    await page.screenshot({ path: join(outDir, 'conversations-scenario-error.png') });
    // Scenario: failed send with retry affordance
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    state.failNextSends = 1;
    await sendDraft('message that fails on purpose');
    await page.waitForSelector('.dk-badge--danger');
    await page.screenshot({ path: join(outDir, 'conversations-scenario-retry.png') });
    // Scenario: keyboard focus reaches the composer (focus-visible ring)
    await freshPage();
    await page.goto(base + '/?conversation=c1');
    await page.waitForSelector('.msg__body');
    for (let i = 0; i < 30; i++) {
      await page.keyboard.press('Tab');
      if (await page.evaluate(() => document.activeElement && document.activeElement.id === 'composer-field')) break;
    }
    assert.equal(await page.evaluate(() => document.activeElement && document.activeElement.id), 'composer-field', 'keyboard reaches the composer');
    assert.equal(await page.locator('.dk-badge--danger').count(), 0, 'keyboard screenshot has clean storage');
    await page.screenshot({ path: join(outDir, 'conversations-scenario-keyboard.png') });
    for (const shot of ['empty', 'error', 'retry', 'keyboard']) {
      if (!existsSync(join(outDir, `conversations-scenario-${shot}.png`))) throw new Error('scenario screenshot missing: ' + shot);
    }
    expectNoPageErrors();
  });

  return { passed, failed, pageErrors, consoleErrors };

  async function test(name, fn) {
    try { await fn(); passed++; console.log('ok -', name); }
    catch (error) {
      failed++;
      console.error('FAIL -', name, '\n ', String(error).split('\n').slice(0, 3).join('\n  '));
      try {
        console.error('  debug: url=' + page.url());
        console.error('  debug: composer=' + await page.locator('#composer-field').count(),
          ' badges=' + await page.locator('.dk-badge--danger').count(),
          ' bodies=' + await page.locator('.msg__body').count(),
          ' outbox=' + await page.evaluate(() => localStorage.getItem('conv-outbox:c1')));
        console.error('  debug: detailHead=' + (await page.locator('.conv-detail-head__title').count() ? await page.locator('.conv-detail-head__title').textContent() : 'none'),
          ' threadError=' + (await page.locator('#thread-error').count() ? (await page.locator('#thread-error').textContent()).slice(0, 120) : 'none'),
          ' pageErrors=' + pageErrors.join(';'));
      } catch (e) { console.error('  debug dump failed: ' + String(e).split('\n')[0]); }
    } finally {
      for (const key of ['sendGate','sendResponseGate','historySnapshotGate','historyGate','participantsGate','createGate']) { state?.[key]?.release(); if (state) state[key] = null; }
    }
  }
}

let result = null;
for (let attempt = 1; attempt <= 3 && !result; attempt++) {
  let browser;
  try {
    browser = await chromium.launch({ args: LAUNCH_ARGS });
    const context = await browser.newContext();
    const page = await context.newPage();
    result = await runSuite(page);
    await browser.close();
  } catch (error) {
    console.log(`browser lost on suite attempt ${attempt}: ${String(error).split('\n')[0]}`);
    if (browser) { try { await browser.close(); } catch (e) { /* already dead */ } }
    await new Promise((resolve) => setTimeout(resolve, 2000));
  }
}
server.close();
if (!result) { console.error('BROWSER TESTS FAILED: browser never survived a full suite'); process.exit(1); }
console.log(`${result.passed} passed, ${result.failed} failed` + (result.failed ? `; page errors: ${result.pageErrors.concat(result.consoleErrors).join(' | ')}` : ''));
process.exit(result.failed ? 1 : 0);

// Private candidate. Import is inert: fixture/network/SQLite are loaded only by openBackend.
import fs from 'node:fs';
import { promises as fsp } from 'node:fs';
import { resolve, join, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import { createHash, randomBytes } from 'node:crypto';
import { spawn } from 'node:child_process';
import net from 'node:net';
import { performance } from 'node:perf_hooks';
import { createRequire } from 'node:module';

export function need(value, code) { if (!value) throw new Error(code); }
export const MAX_SCRATCH = 512 * 1024 * 1024;
export function literalOrigin(value) {
  const u = new URL(value);
  need(u.protocol === 'http:' && u.hostname === '127.0.0.1' && /^[0-9]+$/.test(u.port)
    && +u.port > 0 && +u.port <= 65535 && u.pathname === '/' && !u.search && !u.hash
    && !u.username && !u.password, 'literal_owned_origin_required');
  return u.origin;
}
export class Budget {
  constructor(now = () => performance.now(), ms = 35000) { this.now = now; this.end = now() + ms; }
  remaining(cap = 35000) { const n = Math.floor(Math.min(cap, this.end - this.now())); need(n >= 1, 'composition_deadline'); return n; }
}
export class ByteCap {
  constructor(limit) { this.limit = limit; this.buffers = []; this.bytes = 0; }
  feed(value) { this.bytes += value.length; need(this.bytes <= this.limit, 'capture_byte_limit'); this.buffers.push(Buffer.from(value)); }
  value() { return Buffer.concat(this.buffers); }
}
export class ChildSlots {
  constructor() { this.active = new Map(); }
  reserve(kind, owner) { need(['receiver', 'cli'].includes(kind) && !this.active.has(kind), 'child_slot_busy'); this.active.set(kind, owner); }
  release(kind, owner) { need(this.active.get(kind) === owner, 'child_slot_identity'); this.active.delete(kind); }
}
export function allocationBudget(entries) {
  need(entries.length <= 4096, 'scratch_entry_limit'); let total = 0;
  for (const e of entries) { need(e.plain === true && Number.isSafeInteger(e.bytes) && e.bytes >= 0, 'scratch_metadata_unknown'); total += e.bytes; need(total <= MAX_SCRATCH, 'scratch_budget_exceeded'); }
  return total;
}
export function permittedTarget(origin, value) { const u = new URL(value); return literalOrigin(origin) === u.origin && !u.username && !u.password && !u.hash; }
function sha(value) { return createHash('sha256').update(value).digest('hex'); }
function plainObject(value) { return value && Object.getPrototypeOf(value) === Object.prototype; }
function exact(value, keys) { need(plainObject(value) && Object.keys(value).sort().join('|') === [...keys].sort().join('|'), 'input_shape'); }
const SOURCES = ['hub2/tests/node-fixture.mjs', 'hub2/tests/core-integration-worker.ts', 'hub2/node/compat.ts',
  'hub2/node/runtime.ts', 'hub2/node/server.ts', 'hub2/host/agent_hub_host/agent.py',
  'hub2/host/agent_hub_host/messages.py', 'hub2/host/agent_hub_host/client.py', 'hub2/host/agent_hub_host/transport.py',
  'host/agent_hub_host/cli.py', 'host/agent_hub_host/msg_cli.py', 'host/agent_hub_host/config.py', 'host/agent_hub_host/bus.py'];
export function options(value) {
  exact(value, ['root', 'scratch_parent', 'python', 'adapter', 'source_pins']);
  for (const k of ['root', 'scratch_parent', 'python']) need(typeof value[k] === 'string' && isAbsolute(value[k]) && !value[k].includes('\0'), 'absolute_owned_path_required');
  need(resolve(value.scratch_parent) === resolve(value.root, '.local/tmp'), 'scratch_parent_scope');
  exact(value.adapter, ['path', 'sha256', 'factory_sha256']); need(isAbsolute(value.adapter.path) && /^[a-f0-9]{64}$/.test(value.adapter.sha256) && /^[a-f0-9]{64}$/.test(value.adapter.factory_sha256), 'adapter_pin_missing');
  exact(value.source_pins, SOURCES); for (const hash of Object.values(value.source_pins)) need(/^[a-f0-9]{64}$/.test(hash), 'source_pin_missing');
  return value;
}
export function cliInvocation(python, configPath, recipient, body, key, baseEnv) {
  need(isAbsolute(configPath), 'owned_cli_config_required');
  return {argv: ['-B', '-m', 'agent_hub_host.cli', 'msg', 'send', '--to', recipient, '--body', body, '--key', key],
    env: {...baseEnv, HUB_CONFIG_FILE: configPath}};
}
export function parseCliMessage(raw, recipient, key) {
  const value = JSON.parse(raw);
  exact(value, ['id', 'from', 'to', 'thread_id', 'state', 'epoch', 'idempotency_key']);
  need(typeof value.id === 'string' && /^[A-Za-z0-9_-]{1,180}$/.test(value.id)
    && Number.isSafeInteger(value.epoch) && value.epoch > 0 && value.to === recipient
    && value.idempotency_key === key && typeof value.from === 'string' && value.from.length > 0 && value.from.length <= 180
    && typeof value.state === 'string' && value.state.length > 0 && value.state.length <= 80
    && (value.thread_id === null || typeof value.thread_id === 'string' && value.thread_id.length <= 180), 'cli_message_shape');
  return value;
}

export class StartupCustody {
  constructor({now = () => performance.now(), phaseMs = 500, onState = () => {}} = {}) {
    this.now = now; this.phaseMs = phaseMs; this.onState = onState; this.startState = 'not_started';
    this.server = null; this.runtime = null; this.serverClosed = false; this.runtimeClosed = false;
    this.faults = []; this.cleanupEnd = null; this.closeTask = null; this.lateTask = null; this.runtimeCloseTask = null;
  }
  fault(code) { if (!this.faults.includes(code)) this.faults.push(code); }
  emit() { try { this.onState(this.summary()); } catch { this.fault('lifecycle_receipt_unknown'); } }
  attachRuntime(runtime) { this.runtime = runtime; }
  start(factory) {
    need(this.startState === 'not_started' && this.cleanupEnd === null, 'startup_not_replayable'); this.startState = 'pending';
    try { this.pending = Promise.resolve(factory()); } catch (error) { this.pending = Promise.reject(error); }
    this.pending = this.pending.then(server => {
      this.server = server; this.startState = 'resolved'; this.emit();
      if (this.cleanupEnd !== null) this.lateTask = this.closeServer(); // Tracked, never void/ignored.
      return server;
    }, error => { this.startState = 'rejected'; this.fault('startup_failed'); this.emit(); throw error; });
    return this.pending;
  }
  async settle(operation, code) {
    const ms = Math.floor(Math.min(this.phaseMs, this.cleanupEnd - this.now()));
    if (ms <= 0) { this.fault(code); this.emit(); return false; }
    let timer;
    try { await Promise.race([operation(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error(code)), ms); })]); return true; }
    catch { this.fault(code); return false; }
    finally { clearTimeout(timer); this.emit(); }
  }
  closeServer() {
    if (this.closeTask) return this.closeTask;
    if (!this.server) return Promise.resolve(false);
    this.closeTask = this.settle(() => this.server.close(), 'server_cleanup_unknown').then(ok => { this.serverClosed = ok; this.emit(); return ok; });
    return this.closeTask;
  }
  async closeAll(ms = 2000) {
    if (this.cleanupEnd === null) this.cleanupEnd = this.now() + ms; // Never renewed by late resolution.
    if (this.startState === 'pending') await this.settle(() => this.pending, 'startup_cleanup_unknown');
    if (this.server) await this.closeServer();
    if (this.runtime) {
      this.runtimeCloseTask ??= this.settle(() => this.runtime.dispose(), 'runtime_cleanup_unknown').then(ok => { this.runtimeClosed = ok; return ok; });
      await this.runtimeCloseTask;
    }
    this.emit(); return this.canRestore();
  }
  canRestore() { return this.faults.length === 0 && this.startState !== 'pending'
    && (!this.server || this.serverClosed) && (!this.runtime || this.runtimeClosed); }
  summary() { return {startup: this.startState, server_seen: !!this.server, server_closed: this.serverClosed,
    runtime_closed: this.runtimeClosed, faults: [...this.faults], cleanup_known: this.canRestore(), custody_retained: true}; }
}

function privateWrite(path, bytes) {
  const fd = fs.openSync(path, fs.constants.O_CREAT | fs.constants.O_EXCL | fs.constants.O_NOFOLLOW | fs.constants.O_WRONLY, 0o600);
  try { fs.fchmodSync(fd, 0o600); fs.writeFileSync(fd, bytes); fs.fsyncSync(fd); } finally { fs.closeSync(fd); }
  const parent = fs.openSync(resolve(path, '..'), fs.constants.O_RDONLY | fs.constants.O_DIRECTORY | fs.constants.O_NOFOLLOW);
  try { fs.fsyncSync(parent); } finally { fs.closeSync(parent); }
}
export function readPlain(path, cap) {
  const fd = fs.openSync(path, fs.constants.O_RDONLY | fs.constants.O_NOFOLLOW | fs.constants.O_NONBLOCK);
  try { const s = fs.fstatSync(fd); need(s.isFile() && s.nlink === 1 && s.size <= cap, 'plain_bounded_file_required'); const raw = Buffer.alloc(cap + 1); let total = 0;
    while (total < raw.length) { const n = fs.readSync(fd, raw, total, raw.length - total, null); if (!n) break; total += n; }
    need(total <= cap, 'bounded_read'); return raw.subarray(0, total); }
  finally { fs.closeSync(fd); }
}
function sleep(ms) { return new Promise(done => setTimeout(done, ms)); }
export function cleanEnvironment(env) {
  for (const key of Object.keys(env)) need(!(/^(HUB(?:2)?_|APLEXER_)/.test(key) || /^(HTTP|HTTPS|ALL|NO)_PROXY$/i.test(key) || ['NODE_OPTIONS','NODE_USE_ENV_PROXY','PYTHONHOME','PYTHONPATH','VIRTUAL_ENV'].includes(key)), 'inherited_context_forbidden');
}
export async function openBackend(input, {signal} = {}) {
  cleanEnvironment(process.env);
  need(!signal?.aborted, 'external_abort');
  const cfg = options(input), budget = new Budget(), slots = new ChildSlots(), workers = [], sockets = [], requests = new Set();
  let fixture = null, runtime = null, leaf = null, origin = null, fatal = null, disposed = false, watcher = null;
  let hostToken, operatorToken, binding, recipient, firstBody, laterBody, receiverStarts = 0, lifecycleReceipt = 0;
  const lifetime = new StartupCustody({onState: state => { if (leaf) privateWrite(join(leaf, 'lifetime-' + lifecycleReceipt++ + '.private.json'), Buffer.from(JSON.stringify(state) + '\n')); }});
  const oldFetch = globalThis.fetch, oldConnect = net.Socket.prototype.connect, oldUmask = process.umask(0o077);
  const fail = code => { fatal ??= code; for (const w of workers) if (!w.closed) w.kill(); };
  const interrupted = () => fail('external_abort'); signal?.addEventListener('abort', interrupted, {once: true});
  const check = () => { need(!fatal, fatal || 'fixture_fault'); return budget.remaining(); };
  // Guard is installed BEFORE dynamic source imports/runtime construction/I/O.
  globalThis.fetch = async (resource, init = {}) => {
    const value = typeof resource === 'string' || resource instanceof URL ? String(resource) : resource.url;
    let allowed = false; try { allowed = !!origin && permittedTarget(origin, value); } catch {}
    if (!allowed) { fail('foreign_fetch_forbidden'); throw new Error('foreign_fetch_forbidden'); }
    return oldFetch(resource, { ...init, redirect: 'error' });
  };
  net.Socket.prototype.connect = function (...args) {
    const a = Array.isArray(args[0]) ? args[0] : args;
    const op = typeof a[0] === 'object' && a[0] !== null ? a[0] : {port: a[0], host: a[1]};
    const u = origin ? new URL(origin) : null;
    if (!u || op.path || op.host !== '127.0.0.1' || Number(op.port) !== Number(u.port)) {
      fail('foreign_socket_forbidden'); throw new Error('foreign_socket_forbidden');
    }
    return oldConnect.apply(this, args);
  };
  const outer = setTimeout(() => fail('composition_deadline'), budget.remaining());
  function scan(budgeted = true) {
    if (!leaf) return;
    const entries = [], pending = [[leaf, 0]]; let visited = 0;
    while (pending.length) {
      if (budgeted) budget.remaining();
      const [dir, depth] = pending.pop(); need(depth <= 8, 'scratch_depth_limit');
      const directory = fs.opendirSync(dir);
      try {
        while (true) {
          const entry = directory.readSync(); if (!entry) break;
          need(++visited <= 4096, 'scratch_entry_limit');
          const path = join(dir, entry.name); let s;
          try { s = fs.lstatSync(path); } catch (e) { if (e.code === 'ENOENT') continue; throw e; }
          if (s.isDirectory() && !s.isSymbolicLink()) pending.push([path, depth + 1]);
          else entries.push({plain: s.isFile() && !s.isSymbolicLink() && s.nlink === 1,
            bytes: Math.max(s.size, (s.blocks || 0) * 512)});
          need(entries.length + pending.length <= 4096, 'scratch_entry_limit');
        }
      } finally { directory.closeSync(); }
    }
    allocationBudget(entries);
  }
  function guard() { check(); scan(); check(); }
  async function bounded(operation, cap = 5000) {
    guard(); let timer;
    try { return await Promise.race([operation(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('operation_deadline')), budget.remaining(cap)); })]); }
    finally { clearTimeout(timer); guard(); }
  }
  async function until(test, cap = 5000) {
    const end = performance.now() + budget.remaining(cap);
    while (true) { guard(); const answer = await test(); guard(); need(performance.now() < end, 'operation_deadline'); if (answer) return answer;
      const ms = Math.floor(Math.min(10, end - performance.now(), budget.remaining())); need(ms > 0, 'expected_event_absent'); await sleep(ms); }
  }
  function child(kind, argv, config, env, name) {
    guard(); const owner = {closed: false, process: null, events: [], exit: null, fault: null, connections: 0, connected: false};
    slots.reserve(kind, owner); workers.push(owner); // Custody BEFORE spawn.
    const out = new ByteCap(65536), err = new ByteCap(16384); let line = Buffer.alloc(0);
    owner.kill = () => { if (owner.process && !owner.closed && !owner.exited) { try { owner.process.kill('SIGKILL'); } catch {} } };
    owner.done = new Promise(resolveDone => { owner.resolveDone = resolveDone; });
    owner.close = async () => {
      owner.cleaning = true;
      if (!owner.closed && kind === 'receiver' && owner.process && !owner.stopAttempted) {
        owner.stopAttempted = true; const stop = Buffer.from('{"control":"stop"}\n');
        owner.controlBytes = (owner.controlBytes || 0) + stop.length;
        if (owner.controlBytes <= 8192) { try { owner.process.stdin.write(stop); } catch {} }
      }
      if (!owner.closed && kind === 'receiver') await Promise.race([owner.done, sleep(100)]);
      if (!owner.closed) owner.kill(); await Promise.race([owner.done, sleep(1500).then(() => { throw new Error('owned_child_not_reaped'); })]); need(!owner.fault, 'owned_child_fault'); };
    const accept = raw => {
      try {
        const event = JSON.parse(raw.toString('utf8')); need(plainObject(event), 'adapter_event_shape');
        if (event.kind === 'connected') { exact(event, ['kind']); need(!owner.connected, 'duplicate_connection'); owner.connected = true; owner.connections++; }
        else if (event.kind === 'closed') { exact(event, ['kind']); need(owner.connected, 'unmatched_close'); owner.connected = false; }
        else if (event.kind === 'received') { exact(event, ['kind', 'message_id', 'which']); need(owner.connected && typeof event.message_id === 'string' && ['first', 'later'].includes(event.which), 'received_shape'); }
        else if (event.kind === 'sent_error') { exact(event, ['kind', 'message_id', 'reason']); need(owner.connected && typeof event.message_id === 'string' && event.reason === 'native_mail_outcome_uncertain', 'error_shape'); }
        else if (event.kind === 'receipt_posted') { exact(event, ['kind', 'message_id', 'which', 'native_receipt']); need(owner.connected && event.which === 'later' && typeof event.message_id === 'string', 'receipt_shape'); }
        else throw new Error('unknown_adapter_event');
        need(Number.isSafeInteger(owner.process.pid) && owner.process.pid > 0, 'owned_child_pid_unknown');
        owner.events.push({...event, connection: owner.connections, pid: owner.process.pid}); need(owner.events.length <= 128, 'adapter_event_cap');
      } catch { owner.fault = 'adapter_event_invalid'; fail(owner.fault); }
    };
    try {
      budget.remaining(); owner.process = spawn(cfg.python, argv, {cwd: cfg.root, env, stdio: ['pipe', 'pipe', 'pipe'], detached: false});
      owner.process.stdout.on('error', () => { owner.fault = 'capture_stream_error'; fail(owner.fault); });
      owner.process.stderr.on('error', () => { owner.fault = 'capture_stream_error'; fail(owner.fault); });
      owner.process.stdout.on('data', data => { try { out.feed(data); if (kind === 'receiver') { line = Buffer.concat([line, data]); need(line.length <= 65536, 'adapter_line_cap'); let at; while ((at = line.indexOf(10)) >= 0) { const row = line.subarray(0, at); line = line.subarray(at + 1); if (row.length) accept(row); } } } catch { owner.fault = 'stdout_limit'; fail(owner.fault); } });
      owner.process.stderr.on('data', data => { try { err.feed(data); } catch { owner.fault = 'stderr_limit'; fail(owner.fault); } });
      owner.process.once('exit', () => { owner.exited = true; });
      owner.process.once('error', () => { owner.fault = 'child_spawn_unknown'; fail(owner.fault); });
      owner.process.once('close', (code, signal) => { owner.closed = true; owner.exit = code; owner.signal = signal;
        if (code === 2 || (!owner.cleaning && (kind === 'cli' ? code !== 0 : name === 'receiver-crash' ? code !== 73 : code !== 0))) { owner.fault ??= 'child_failed'; fail(owner.fault); }
        try { privateWrite(join(leaf, name + '.stdout.private'), out.value()); privateWrite(join(leaf, name + '.stderr.private'), err.value()); }
        catch { owner.fault ??= 'child_receipt_unknown'; fatal ??= owner.fault; }
        slots.release(kind, owner); owner.resolveDone(); });
      if (config != null) config.remaining_ms = Math.min(config.remaining_ms, budget.remaining());
      const raw = config == null ? Buffer.alloc(0) : Buffer.from(JSON.stringify(config)); need(raw.length <= 8192, 'control_limit');
      owner.process.stdin.on('error', () => {});
      if (kind === 'receiver') { owner.controlBytes = raw.length + 1; need(owner.controlBytes <= 8192, 'control_limit'); owner.process.stdin.write(Buffer.concat([raw, Buffer.from('\n')])); }
      else owner.process.stdin.end(raw);
      budget.remaining(); return owner;
    } catch (e) { if (owner.process) owner.kill(); else { owner.closed = true; slots.release(kind, owner); owner.resolveDone(); } throw e; }
  }
  function envFor(pythonpath) { return {PATH: '/usr/bin:/bin', LC_ALL: 'C', HOME: leaf, TMPDIR: leaf,
    PYTHONPATH: pythonpath, PYTHONDONTWRITEBYTECODE: '1', PYTHONNOUSERSITE: '1',
    ...(process.env.AGENT_HUB_HEAVY_TEST_HELD === undefined ? {} : {AGENT_HUB_HEAVY_TEST_HELD: process.env.AGENT_HUB_HEAVY_TEST_HELD})}; }
  async function api(path, method = 'GET', body, token = operatorToken, expected = 200) {
    guard(); need(path.startsWith('/api/') && !/[\r\n]/.test(path), 'public_api_path_required');
    const controller = new AbortController(); requests.add(controller); const timer = setTimeout(() => controller.abort(), budget.remaining(2000));
    try {
      const response = await bounded(() => fetch(origin + path, {method, signal: controller.signal,
        headers: {'Content-Type': 'application/json', ...(token ? {Authorization: 'Bearer ' + token} : {})},
        ...(body === undefined ? {} : {body: JSON.stringify(body)})}), 2000);
      const chunks = []; let size = 0; for await (const part of response.body) { size += part.length; need(size <= 65536, 'api_response_cap'); chunks.push(part); guard(); }
      const value = JSON.parse(Buffer.concat(chunks).toString('utf8')); need(response.status === expected, 'public_api_status_' + response.status); return value;
    } finally { clearTimeout(timer); controller.abort(); requests.delete(controller); }
  }
  async function bootstrapSocket() {
    const {WebSocket} = createRequire(join(cfg.root, 'hub2/package.json'))('ws'); const frames = [];
    const custody = {socket: null}; sockets.push(custody); // Before constructor/partial creation.
    const socket = new WebSocket(origin.replace(/^http/, 'ws') + '/api/connect', {headers: {Authorization: 'Bearer ' + hostToken}, maxPayload: 65536, followRedirects: false});
    custody.socket = socket; socket.on('error', () => fail('bootstrap_socket_error'));
    socket.on('message', raw => { try { need(raw.length <= 65536 && frames.length < 32, 'bootstrap_frame_cap'); const frame = JSON.parse(raw.toString()); need(['resync', 'session.offer', 'ack'].includes(frame.type), 'unexpected_bootstrap_frame'); frames.push(frame); } catch { fail('bootstrap_frame_unknown'); } });
    await bounded(() => new Promise((done, reject) => { socket.once('open', done); socket.once('error', reject); }), 2000);
    return {socket, wait: p => until(() => frames.find(p), 2500), frames};
  }
  try {
    // Source/interpreter/adapter pins are metadata/code only; no config/profile read.
    need(fs.lstatSync(cfg.root).isDirectory() && !fs.lstatSync(cfg.root).isSymbolicLink(), 'plain_source_root_required');
    for (const [name, digest] of Object.entries(cfg.source_pins)) need(sha(readPlain(join(cfg.root, name), 2 * 1024 * 1024)) === digest, 'source_changed');
    need(sha(readPlain(cfg.adapter.path, 131072)) === cfg.adapter.sha256, 'adapter_changed');
    need(sha(readPlain(resolve(cfg.adapter.path, '..', 'native_mail_restart_fixture.py'), 131072)) === cfg.adapter.factory_sha256, 'adapter_factory_changed');
    need(fs.statSync(cfg.python).isFile(), 'validated_python_file_required'); // Root supplies validated existing interpreter, including normal venv symlinks.
    await fsp.mkdir(cfg.scratch_parent, {recursive: true, mode: 0o700});
    leaf = await fsp.mkdtemp(join(cfg.scratch_parent, 'public-native-mail-')); await fsp.chmod(leaf, 0o700);
    privateWrite(join(leaf, 'intent.private.json'), Buffer.from(JSON.stringify({kind: 'owned_public_native_mail_fixture', adapter_sha256: cfg.adapter.sha256, complete: false}) + '\n'));
    watcher = setInterval(() => { try { guard(); } catch { fail('scratch_or_deadline_unknown'); } }, 25);
    const compat = await import(pathToFileURL(join(cfg.root, 'hub2/node/compat.ts')).href); compat.registerTypeScriptResolveHook(); compat.installCompatGlobals();
    const entry = await import(pathToFileURL(join(cfg.root, 'hub2/tests/core-integration-worker.ts')).href);
    const {Hub2Runtime} = await import(pathToFileURL(join(cfg.root, 'hub2/node/runtime.ts')).href);
    const {startServer} = await import(pathToFileURL(join(cfg.root, 'hub2/node/server.ts')).href);
    const tokenPart = randomBytes(12).toString('hex'); operatorToken = 'synthetic-mail-operator-' + tokenPart;
    runtime = new Hub2Runtime({root: cfg.root, entry: entry.default, classes: {CONTROL: entry.CoreControl, PROJECTS: entry.CoreProject, MACHINES: entry.CoreMachine, ACCOUNTS: entry.CoreAccount, MAILBOXES: entry.CoreMailbox},
      env: {HUB2_DEV_OPERATOR_TOKEN: operatorToken, HUB2_DEFAULT_ACCOUNT_ID: 'synthetic-mail-account', HUB2_ENGINE_ACCOUNTS: JSON.stringify({opencode: 'synthetic-mail-account'}), HUB2_CREDENTIAL_KEY: '11'.repeat(32), HUB2_FIXTURE_ASSET_HTML: '<title>Private synthetic mail fixture</title>'}, dataDir: join(leaf, 'node-db')});
    lifetime.attachRuntime(runtime);
    fixture = await bounded(() => lifetime.start(() => startServer(runtime, {host: '127.0.0.1', port: 0})), 2500);
    origin = literalOrigin('http://127.0.0.1:' + fixture.port);
  } catch (error) { await close({failure: error.name}); throw error; }

  async function close(summary = {}) {
    if (disposed) return; disposed = true; signal?.removeEventListener('abort', interrupted); clearTimeout(outer); clearInterval(watcher); const errors = [];
    for (const w of workers) { try { await w.close(); } catch { errors.push('child_cleanup_unknown'); } }
    for (const controller of requests) { try { controller.abort(); } catch { errors.push('request_cleanup_unknown'); } }
    for (const s of sockets) { try { s.socket?.terminate(); } catch { errors.push('socket_cleanup_unknown'); } }
    if (!await lifetime.closeAll(2000)) errors.push('owned_lifetime_cleanup_unknown');
    try { if (leaf) { scan(false); privateWrite(join(leaf, 'result.private.json'), Buffer.from(JSON.stringify({...summary, ...(fatal || errors.length ? {status: 'FAILED_OR_UNKNOWN'} : {}), fatal, lifetime: lifetime.summary(), cleanup_errors: errors, complete: false, retirement_approved: false}) + '\n')); } } catch { errors.push('result_or_scratch_unknown'); }
    if (lifetime.canRestore()) { globalThis.fetch = oldFetch; net.Socket.prototype.connect = oldConnect; process.umask(oldUmask); }
    // Unknown/pending lifetime retains guards+custody until the independent outer kill.
    need(errors.length === 0, 'owned_cleanup_unknown'); need(!fatal, 'fixture_fault');
  }
  async function snapshot({firstId, firstEpoch, laterId, laterEpoch}) {
    guard(); const {DatabaseSync} = await import('node:sqlite'); const path = join(leaf, 'receiver', 'journal.sqlite');
    const s = fs.lstatSync(path); need(s.isFile() && !s.isSymbolicLink() && s.uid === process.getuid() && (s.mode & 0o077) === 0 && s.nlink === 1 && s.size <= MAX_SCRATCH, 'own_journal_required');
    const db = new DatabaseSync(path, {readOnly: true});
    try {
      const row = (id, epoch) => { if (!id) return null; need(Number.isSafeInteger(epoch) && epoch > 0, 'mail_epoch_required'); const value = db.prepare('SELECT id,epoch,state,receipt FROM native_mail WHERE id=? AND epoch=?').get(id, epoch); if (!value) return null;
        return {id: value.id, state: value.state, receipt: value.receipt === null ? null : JSON.parse(value.receipt), receiptOutbox: !!db.prepare('SELECT 1 FROM outbox WHERE key=?').get('mail:' + id + ':' + epoch)}; };
      const raw = readPlain(join(leaf, 'receiver', 'effects.jsonl'), 65536).toString('utf8');
      const effects = raw.trim() ? raw.trimEnd().split('\n').map(line => JSON.parse(line)) : [];
      need(effects.length <= 2 && effects.every(v => plainObject(v) && typeof v.id === 'string' && [firstId, laterId].includes(v.id)), 'effect_scope');
      return {first: row(firstId, firstEpoch), later: row(laterId, laterEpoch), effects};
    } finally { db.close(); }
  }
  return {
    get base() { return origin; }, now: () => performance.now(), budget,
    async seedPublicBinding() {
      const project = 'synthetic/mail-fairness', machine = 'synthetic-mail-machine', account = 'synthetic-mail-account';
      await api('/api/repos/' + encodeURIComponent(project), 'PATCH', {canonical_url: 'https://github.com/synthetic/mail-fairness', purpose: 'Private synthetic mail fixture', status: 'active', aliases: [], domains: []});
      const pair = await api('/api/enrollments/pair', 'POST', {machine_id: machine, alias: 'Private synthetic machine'}, operatorToken, 201);
      hostToken = (await api('/api/enrollments/complete', 'POST', {code: pair.pairing_code}, null, 201)).token;
      await api('/api/heartbeat', 'POST', {os: 'synthetic', arch: 'synthetic', labels: ['fixture'], engines: ['opencode'], capabilities: ['headless'], resources: {mem_available_mb: 42000, observed_at: Date.now()}}, hostToken);
      await api('/api/account', 'PATCH', {provider: 'codex', enabled: true, min_remaining: 10});
      const access = 'fixture.' + Buffer.from(JSON.stringify({exp: Math.floor(Date.now() / 1000) + 864000})).toString('base64url') + '.signature';
      await api('/api/account/credentials', 'PUT', {expected_generation: 0, files: {'.codex/auth.json': JSON.stringify({tokens: {access_token: access, refresh_token: 'synthetic-local-only', account_id: 'fixture-id'}, last_refresh: new Date().toISOString()})}});
      await api('/api/account/quota', 'PUT', {remaining: 80, captured_at: Date.now(), valid_until: Date.now() + 300000, source: 'synthetic-native-integration-fixture'});
      await api('/api/account/hosts', 'PUT', {hosts: [machine]});
      await api('/api/model-policy', 'PUT', {expected_version: 0, candidates: [{id: 'synthetic-mail-model', engine: 'opencode', model: 'synthetic-mail-model', account_id: account, weight: 1, capabilities: [], enabled: true}]});
      const workspace = join(leaf, 'workspace'); await fsp.mkdir(workspace, {mode: 0o700});
      const intake = (await api('/api/intakes?project=' + encodeURIComponent(project), 'POST', {title: 'Private uncertain mail fixture', text: 'Synthetic local transport only; no native process is launched.', acceptance_criteria: ['Synthetic receiver evidence remains unverified outside this fixture.'], code_task: false, key: 'mail-intake'}, operatorToken, 201)).intake;
      const bs = await bootstrapSocket();
      const started = (await api('/api/intakes/' + intake.id + '/start?project=' + encodeURIComponent(project), 'POST', {machine_id: machine, workspace, engine: 'opencode', override_reason: 'Explicit synthetic transport fixture; no native model process', interactive: false, timeout_seconds: 60, key: 'mail-start'}, operatorToken, 201)).session;
      const offered = await bs.wait(v => v.type === 'session.offer' && v.offer.session_id === started.id); const offer = offered.offer;
      need(bs.frames.filter(v => v.type === 'session.offer').length === 1, 'unexpected_bootstrap_offer');
      bs.socket.send(JSON.stringify({type: 'offer_ack', session_id: started.id, epoch: offer.epoch}));
      await bs.wait(v => v.type === 'ack' && v.session_id === started.id && v.epoch === offer.epoch);
      await api('/api/sessions/' + started.id + '/admission', 'POST', {operation_id: offer.operation_id, epoch: offer.epoch}, hostToken);
      await api('/api/sessions/' + started.id + '/started', 'POST', {operation_id: offer.operation_id, epoch: offer.epoch, native_id: '61ee2e26-87f1-4753-9a0e-83f7ead5e5c5'}, hostToken, 201);
      const session = (await api('/api/sessions/' + started.id)).session, offers = (await api('/api/offers', 'GET', undefined, hostToken)).offers;
      need(session.state === 'running' && session.operation_id === offer.operation_id && session.epoch === offer.epoch && session.workspace === workspace && offers.length === 0, 'started_binding_not_settled');
      bs.socket.terminate();
      const agent = (await api('/api/named-agents', 'POST', {name: 'mail-fairness', display_name: 'Private synthetic forward', forward_session: session.id})).agent;
      need(agent.id === 'agent:mail-fairness' && agent.forward_session === session.id, 'canonical_recipient_unverified'); recipient = agent.id;
      binding = {session_id: session.id, native_id: session.native_id, operation_id: session.operation_id, epoch: session.epoch, workspace, tag: 'task-hub2-' + session.id};
      firstBody = 'Synthetic uncertain-first ' + randomBytes(8).toString('hex'); laterBody = 'Synthetic later ' + randomBytes(8).toString('hex');
      await fsp.mkdir(join(leaf, 'receiver'), {mode: 0o700});
      for (const name of ['effects.jsonl', 'api.jsonl', 'status.jsonl']) privateWrite(join(leaf, 'receiver', name), Buffer.alloc(0));
      return {session, offers, recipient};
    },
    async openReviewedPublicReceiver({mode}) {
      need(['crash', 'receive'].includes(mode) && receiverStarts < 2 && (receiverStarts === 0 ? mode === 'crash' : mode === 'receive'), 'no_receiver_replay');
      const remaining_ms = budget.remaining(); receiverStarts++;
      const config = {origin, token: hostToken, directory: join(leaf, 'receiver'), mode, machine_id: 'synthetic-mail-machine', binding, recipient, first_body: firstBody, later_body: laterBody, remaining_ms};
      const w = child('receiver', ['-B', '-u', cfg.adapter.path], config, envFor(join(cfg.root, 'hub2/host')), 'receiver-' + mode);
      const event = (predicate, cap = 5000) => until(() => { need(!w.fault, w.fault || 'receiver_fault'); const v = w.events.find(predicate); if (v) return v; need(!w.closed, 'receiver_ended_before_event'); return null; }, cap);
      return {owner: w, get unexpectedDispatches() { return w.fault || w.exit === 2 ? 1 : 0; },
        waitConnected: cap => event(v => v.kind === 'connected', cap), waitExit: async () => { await bounded(() => w.done); need(!w.fault, 'receiver_fault'); return w.exit; },
        waitActualReceived: (id, cap) => event(v => v.kind === 'received' && v.message_id === id, cap),
        waitActualSentError: (id, reason, cap) => event(v => v.kind === 'sent_error' && v.message_id === id && v.reason === reason, cap),
        waitActualReceiptPosted: (id, cap) => event(v => v.kind === 'receipt_posted' && v.message_id === id && v.which === 'later', cap)};
    },
    async sendActualCliMessage(which, target) {
      need(target === recipient && ['uncertain-first', 'later-after-real-uncertainty'].includes(which), 'cli_scope');
      const configPath = join(leaf, 'operator.private.json'); if (!fs.existsSync(configPath)) privateWrite(configPath, Buffer.from(JSON.stringify({base_url: origin, access_token: operatorToken})));
      const body = which === 'uncertain-first' ? firstBody : laterBody;
      const call = cliInvocation(cfg.python, configPath, recipient, body, which, envFor(join(cfg.root, 'host')));
      const w = child('cli', call.argv, null, call.env, 'cli-' + which);
      await bounded(() => w.done, 5000); need(w.exit === 0 && !w.fault, 'actual_cli_failed');
      return parseCliMessage(readPlain(join(leaf, 'cli-' + which + '.stdout.private'), 65536).toString('utf8'), recipient, which);
    },
    publicMessageStatus: async id => (await api('/api/messages/' + encodeURIComponent(id) + '/status')).message,
    waitPublicSessionAttention: (id, reason) => until(async () => (await api('/api/sessions/' + encodeURIComponent(id))).session.attention?.reason === reason),
    readOwnJournalReadonly: snapshot,
    closeOwnedWorker: async worker => { await worker.owner.close(); },
    retainAndDisposeOwnFixture: close,
    get evidenceDirectory() { return leaf; },
  };
}

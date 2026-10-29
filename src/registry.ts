import { sanitizeCpuUsage } from './cpu-headroom';
import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from './env';
import { verifyBearerScope } from './auth/token';
import { getSession, verifyCsrf } from './auth/session';
import { approvalAuthorityMatches } from './host_enrollment';

async function safeJson(req: Request): Promise<any> {
  if (!req.body) return null;
  const reader = req.body.getReader();
  let size = 0;
  const chunks: Uint8Array[] = [];
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      if (value) {
        size += value.length;
        if (size > 32768) {
          await reader.cancel();
          return null;
        }
        chunks.push(value);
      }
    }
    const all = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      all.set(chunk, offset);
      offset += chunk.length;
    }
    const text = new TextDecoder().decode(all);
    return JSON.parse(text);
  } catch (e) {
    return null;
  }
}

const ROUTE_LABEL = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
// Routes one heartbeat may advertise; equals MAX_ADVERTISED_ROUTES in
// host/agent_hub_host/fleet.py and the per-machine subscription limit.
export const MAX_ADVERTISED_ROUTES = 128;
function sanitizeHeartbeat(body: any) {
  if (!body || body.type !== 'heartbeat') return null;
  const hb: any = { type: 'heartbeat' };
  const cpu = sanitizeCpuUsage(body.cpu_usage); if (cpu) hb.cpu_usage = cpu;
  if (typeof body.at === 'string' && body.at.length <= 64) hb.at = body.at;
  if (typeof body.mem_avail_mb === 'number' && Number.isFinite(body.mem_avail_mb)) hb.mem_avail_mb = body.mem_avail_mb;
  if (body.launcher_resources && Number.isFinite(body.launcher_resources.active_mem_mb)
      && body.launcher_resources.active_mem_mb >= 0 && body.launcher_resources.active_mem_mb <= 1e9
      && typeof body.launcher_resources.observed_at === 'string' && body.launcher_resources.observed_at.length <= 64) {
    hb.launcher_resources = { active_mem_mb: body.launcher_resources.active_mem_mb, observed_at: body.launcher_resources.observed_at };
    const disk = body.launcher_resources;
    if (['active_disk_mb', 'disk_floor_mb', 'next_disk_mb'].every(k =>
        typeof disk[k] === 'number' && Number.isFinite(disk[k]) && disk[k] >= 0 && disk[k] <= 1e9)) {
      for (const k of ['active_disk_mb', 'disk_floor_mb', 'next_disk_mb']) hb.launcher_resources[k] = disk[k];
      if (Array.isArray(disk.disk_paths) && disk.disk_paths.length <= 128 && disk.disk_paths.every((p: any) =>
          p && typeof p.path === 'string' && p.path.startsWith('/') && p.path.length <= 1024 && !/[\x00-\x1f\x7f]/.test(p.path) &&
          typeof p.free_gb === 'number' && Number.isFinite(p.free_gb) && p.free_gb >= 0 && p.free_gb <= 1e9)) {
        hb.launcher_resources.disk_paths = disk.disk_paths.map((p: any) => ({path: p.path, free_gb: p.free_gb}));
      }
      if (disk.disk_aliases && typeof disk.disk_aliases === 'object' && !Array.isArray(disk.disk_aliases) &&
          Object.entries(disk.disk_aliases).length <= 64 && Object.entries(disk.disk_aliases).every(([alias, target]) =>
            alias.length > 0 && alias.length <= 512 && !/[\x00-\x1f\x7f]/.test(alias) &&
            typeof target === 'string' && target.startsWith('/') && target.length <= 1024 && !/[\x00-\x1f\x7f]/.test(target))) {
        hb.launcher_resources.disk_aliases = disk.disk_aliases;
      }
      if (Array.isArray(disk.disk_task_ids) && disk.disk_task_ids.length <= 256 &&
          disk.disk_task_ids.every((id: any) => typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$/.test(id))) {
        hb.launcher_resources.disk_task_ids = disk.disk_task_ids;
      }
    }
  }
  if (typeof body.mem_total_mb === 'number' && Number.isFinite(body.mem_total_mb) && body.mem_total_mb > 0) hb.mem_total_mb = body.mem_total_mb;
  if (typeof body.disk_free_gb === 'number' && Number.isFinite(body.disk_free_gb)) hb.disk_free_gb = body.disk_free_gb;
  if (typeof body.load === 'object' && Array.isArray(body.load)) {
    hb.load = body.load.filter((l: any) => typeof l === 'number' && Number.isFinite(l)).slice(0, 3);
  }
  if (typeof body.gate === 'object' && body.gate) {
    hb.gate = {};
    if (typeof body.gate.launch_allowed === 'boolean') hb.gate.launch_allowed = body.gate.launch_allowed;
    if (typeof body.gate.reason === 'string' && body.gate.reason.length <= 256) hb.gate.reason = body.gate.reason;
    if (typeof body.gate.at === 'string' && body.gate.at.length <= 64) hb.gate.at = body.gate.at;
  }
  if (Array.isArray(body.running_jobs)) {
    hb.running_jobs = body.running_jobs.filter((j: any) => typeof j === 'string' && j.length <= 128).slice(0, 50);
  }
  // Executable routes the host's dispatcher accepts (intake 6cbb862a). Bounded
  // label tuples only; a malformed list is dropped, never partially kept. A
  // host with more routes sends fleet_routes_overflow instead of a truncated
  // list, which clears the advertisement (no route evidence).
  if (Number.isInteger(body.fleet_routes_overflow) && body.fleet_routes_overflow > MAX_ADVERTISED_ROUTES && body.fleet_routes_overflow <= 100000) {
    hb.fleet_routes_overflow = body.fleet_routes_overflow;
  } else if (Array.isArray(body.fleet_routes) && body.fleet_routes.length <= MAX_ADVERTISED_ROUTES && body.fleet_routes.every((r: any) =>
      r && typeof r === 'object' && typeof r.engine === 'string' && ROUTE_LABEL.test(r.engine)
      && typeof r.account === 'string' && ROUTE_LABEL.test(r.account)
      && (r.profile === null || r.profile === undefined || (typeof r.profile === 'string' && ROUTE_LABEL.test(r.profile))))) {
    hb.fleet_routes = body.fleet_routes.map((r: any) => ({ engine: r.engine, account: r.account, profile: r.profile ?? null }));
  }
  if (typeof body.sessions_summary === 'object' && body.sessions_summary) {
    hb.sessions_summary = {};
    for (const k of Object.keys(body.sessions_summary)) {
      if (typeof k === 'string' && k.length <= 64 && typeof body.sessions_summary[k] === 'number' && Number.isFinite(body.sessions_summary[k])) {
        hb.sessions_summary[k] = body.sessions_summary[k];
      }
    }
  }
  return hb;
}

const machineState = new Map<string, { lastSeenAt: number, lastPersistedAt: number, fingerprint: string, hb: any, routesAt?: number }>();

function getFingerprint(hb: any): string {
  return JSON.stringify({
    cpu_usage: hb.cpu_usage,
    gate: hb.gate,
    launcher_resources: hb.launcher_resources,
    running_jobs: hb.running_jobs,
    sessions_summary: hb.sessions_summary,
    fleet_routes: hb.fleet_routes,
    fleet_routes_overflow: hb.fleet_routes_overflow
  });
}

/**
 * Latest check-in time for a machine. Storage is written at most every five minutes for an
 * unchanged heartbeat, so prefer the in-memory time this Durable Object saw when it is newer.
 */
export function effectiveCheckin(machineId: string, persisted: unknown): string | null {
  const stored = typeof persisted === 'string' ? Date.parse(persisted) : NaN;
  const seen = machineState.get(machineId)?.lastSeenAt;
  const best = Math.max(Number.isFinite(stored) ? stored : -Infinity, typeof seen === 'number' ? seen : -Infinity);
  return Number.isFinite(best) ? new Date(best).toISOString() : null;
}

/** Latest time the host advertised fleet_routes (intake 6cbb862a): an unchanged
 * heartbeat is persisted at most every five minutes, so prefer the newer
 * in-memory observation, exactly as effectiveCheckin does. */
export function effectiveRoutesAt(machineId: string, persisted: unknown): string | null {
  const stored = typeof persisted === 'string' ? Date.parse(persisted) : NaN;
  const seen = machineState.get(machineId)?.routesAt;
  const best = Math.max(Number.isFinite(stored) ? stored : -Infinity, typeof seen === 'number' ? seen : -Infinity);
  return Number.isFinite(best) ? new Date(best).toISOString() : null;
}

export function updateHeartbeatMemoryOnly(machineId: string) {
  const now = Date.now();


  const st = machineState.get(machineId);
  if (!st) {
    machineState.set(machineId, { lastSeenAt: now, lastPersistedAt: 0, fingerprint: '', hb: null });
  } else {
    st.lastSeenAt = Math.max(st.lastSeenAt, now);
  }
}

export function updateHeartbeat(machineId: string, rawPayload: any, sql: SqlStorage) {
  const hb = sanitizeHeartbeat(rawPayload);
  if (!hb) return;

  const now = Date.now();
  const cpu=hb.cpu_usage;
  if(cpu && cpu.host_oom_kills_delta>0 && now-Date.parse(cpu.observed_at)>=-30000 && now-Date.parse(cpu.observed_at)<=180000
    && sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='resource_choking_events'").toArray().length) {
    sql.exec('INSERT INTO resource_choking_events VALUES(?,?,?) ON CONFLICT(machine_id,reason) DO UPDATE SET observed_at=excluded.observed_at WHERE excluded.observed_at>resource_choking_events.observed_at',machineId,'host_oom',cpu.observed_at);
  }
  let st = machineState.get(machineId);
  // Route advertisement carries its own freshness (fleet_routes_at): a
  // heartbeat without the field (e.g. the role-job poller) keeps the last
  // advertisement and its original time, so it can age out but never looks
  // newer than it is.
  const advertisedNow = !!hb.fleet_routes;
  if (advertisedNow) hb.fleet_routes_at = new Date(now).toISOString();
  else if (!hb.fleet_routes_overflow) {
    let previous: any = st?.hb;
    if (!previous) {
      try { previous = JSON.parse(String((sql.exec('SELECT last_heartbeat FROM machines WHERE id = ?', machineId).toArray()[0] as any)?.last_heartbeat ?? 'null')); } catch { previous = null; }
    }
    if (Array.isArray(previous?.fleet_routes) && typeof previous.fleet_routes_at === 'string') {
      hb.fleet_routes = previous.fleet_routes; hb.fleet_routes_at = previous.fleet_routes_at;
    }
  }
  const fingerprint = getFingerprint(hb);
  
  if (!st) {
    st = { lastSeenAt: now, lastPersistedAt: 0, fingerprint: '', hb: null };
    machineState.set(machineId, st);
  }
  st.lastSeenAt = now;
  if (advertisedNow) st.routesAt = now;
  st.hb = hb;

  if (fingerprint !== st.fingerprint || now - st.lastPersistedAt >= 300000) {
    st.fingerprint = fingerprint;
    st.lastPersistedAt = now;
    sql.exec(
      'UPDATE machines SET last_checkin_at = ?, last_heartbeat = ? WHERE id = ?',
      new Date(now).toISOString(),
      JSON.stringify(hb),
      machineId
    );
  }
}

export function handleWebSocketMessageRegistry(ws: WebSocket, message: string | ArrayBuffer, sql: SqlStorage) {
  try {
    const attachment: any = ws.deserializeAttachment();
    if (!attachment || !attachment.machineId || !attachment.credentialId) {
      ws.close(1008, 'Invalid attachment');
      return;
    }

    const credArr = sql.exec('SELECT * FROM credentials WHERE id = ?', attachment.credentialId).toArray() as any[];
    const cred = credArr[0] as any;
    if (!cred || cred.revoked_at || !Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= Date.now()) {
      ws.close(1008, 'Credential revoked or expired');
      return;
    }

    if (typeof message !== 'string') return;
    if (message.length > 4096) {
      ws.close(1009, 'Message too large');
      return;
    }

    const payload = JSON.parse(message);
    if (payload.type === 'heartbeat') {
      updateHeartbeat(attachment.machineId, payload, sql);
    }
  } catch (e) {
    ws.close(1008, 'Invalid payload');
  }
}

export function handleWebSocketCloseRegistry(ws: WebSocket, sql: SqlStorage) {
    try {
        const attachment: any = ws.deserializeAttachment();
        if (attachment && attachment.machineId) {
            // Memory is preserved for honest last checkin (until 900s expires)
        }
    } catch (e) {
    }
}

export async function handleRegistryRoutes(
  request: Request,
  path: string,
  sql: SqlStorage,
  ctx: Pick<DurableObjectState, 'acceptWebSocket'>,
  env: Env,
  txn?: <T>(fn: () => T) => T
): Promise<Response | null> {
  // Synchronous-after-last-await sections below never yield, so no concurrent
  // request or Promise continuation can interleave between the final live
  // authority read and the guarded mutation. Where the platform offers an
  // atomic block it is used; otherwise the conditional UPDATE is the
  // compare-and-swap and denial mutates nothing.
  const runInTxn = <T>(fn: () => T): T => {
    const storage = (ctx as unknown as { storage?: { transactionSync?: <U>(fn: () => U) => U } })?.storage;
    if (txn) return txn(fn);
    if (storage && typeof storage.transactionSync === 'function') return storage.transactionSync(fn);
    return fn();
  };
  if (path === '/api/machines/register') {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await safeJson(request);
    if (!body || typeof body !== 'object') return Response.json({ error: 'malformed_json' }, { status: 400 });

    const hostname = body.hostname;
    const os = body.os;
    const arch = body.arch ?? '';
    const network = body.network;
    const roles = body.roles;
    const capabilities = body.capabilities;

    if (typeof hostname !== 'string' || hostname.length > 256) return Response.json({ error: 'invalid_hostname' }, { status: 400 });
    if (typeof os !== 'string' || os.length > 256) return Response.json({ error: 'invalid_os' }, { status: 400 });
    if (typeof arch !== 'string' || arch.length > 64 || (arch !== '' && !/^[A-Za-z0-9._-]{1,64}$/.test(arch))) return Response.json({ error: 'invalid_arch' }, { status: 400 });
    if (typeof network !== 'string' || network.length > 256) return Response.json({ error: 'invalid_network' }, { status: 400 });
    
    if (!Array.isArray(roles) || !roles.every((r: any) => typeof r === 'string')) return Response.json({ error: 'invalid_roles' }, { status: 400 });
    if (typeof capabilities !== 'object' || capabilities === null) return Response.json({ error: 'invalid_capabilities' }, { status: 400 });

    const rolesJson = JSON.stringify(roles);
    const capJson = JSON.stringify(capabilities);
    if (rolesJson.length > 4096 || capJson.length > 16384) return Response.json({ error: 'payload_too_large' }, { status: 400 });

    // Host-enrollment authority (R5 R2 prerequisite, ported from
    // f20b35a60af2de17c91303dd317da70b44f56174 without M9 metrics parts).
    // Crypto preparation (verifyBearerScope's token hash) already happened
    // asynchronously above; every live check below is SYNCHRONOUS inside the
    // same atomic block as the mutation — no awaited re-verifier sits
    // between the final authority read and the write, so a revocation,
    // expiry, refresh rotation or re-enrollment racing the streamed body is
    // honored instead of writing with stale authorization. Denial mutates
    // nothing (no metrics/receipt side effects live on this surface).
    const presentedId = cred.id as string;
    return runInTxn(() => {
      // Fresh sync storage authority: the presenting token's live row, by
      // server-issued id (never by client body), re-read after the body.
      const live = sql.exec('SELECT * FROM credentials WHERE id = ?', presentedId).toArray()[0] as any;
      if (!live || live.revoked_at) return Response.json({ error: 'Unauthorized' }, { status: 401 });
      if (!Number.isFinite(Date.parse(live.expires_at)) || Date.parse(live.expires_at) <= Date.now()) {
        return Response.json({ error: 'Unauthorized' }, { status: 401 });
      }
      if (live.kind !== 'host') return Response.json({ error: 'Unauthorized' }, { status: 401 });
      let liveScopes: unknown;
      try { liveScopes = typeof live.scopes === 'string' ? JSON.parse(live.scopes) : live.scopes; } catch { liveScopes = null; }
      if (!Array.isArray(liveScopes) || !(liveScopes as unknown[]).includes('host')) {
        return Response.json({ error: 'Unauthorized' }, { status: 401 });
      }
      const machineId = live.machine_id as string;
      if (!machineId) return Response.json({ error: 'Unauthorized' }, { status: 401 });

      // A superseded refresh family must not reclaim a newer machine
      // registration. Registration may only confirm the caller's own live
      // binding (same-credential re-registration, including the binding just
      // established by this credential's own admin-approved device issuance)
      // or claim an unbound machine. Takeover of a live foreign binding is
      // the device-pairing authority's job (approve + token issuance sets the
      // binding); the register surface never performs it. A revoked
      // predecessor is denied even for an unbound machine; the current
      // refresh descendant still bootstraps with its live credential.
      const current = sql.exec('SELECT id, credential_id, approval_seq FROM machines WHERE id = ?', machineId).toArray()[0] as any;
      if (!current) return Response.json({ error: 'unknown_machine' }, { status: 404 });
      if (
        typeof current.credential_id === 'string' &&
        current.credential_id !== '' &&
        current.credential_id !== live.id
      ) {
        return Response.json({ error: 'forbidden', reason: 'stale_machine_binding' }, { status: 403 });
      }
      // Defense in depth: the credential's enrollment authority must still
      // match the machine's current authority for non-owners (new authority,
      // unbound bootstrap and takeover attempts). The currently bound owner is
      // exempt: routine re-registration continues while a later approval is
      // still pending, until a new valid issuance actually replaces the binding
      // (approval witness and bound owner are distinct). A delayed older
      // approved family (or its refresh descendants) that somehow holds a live
      // credential can never reclaim the machine. NULL on either side is
      // pre-migration legacy or unbound bootstrap and skips the check; the
      // binding CAS above remains the decider there.
      const isBoundOwner = current.credential_id === live.id;
      if (!isBoundOwner && !approvalAuthorityMatches(live.approval_seq, current.approval_seq)) {
        return Response.json({ error: 'forbidden', reason: 'stale_machine_binding' }, { status: 403 });
      }

      const registered = sql.exec(`
        UPDATE machines
        SET hostname = ?, os = ?, arch = ?, network = ?, roles = ?, capabilities = ?, credential_id = ?
        WHERE id = ? AND (credential_id IS NULL OR credential_id = '' OR credential_id = ?)
      `, hostname, os, arch, network, rolesJson, capJson, live.id, machineId, live.id);
      if (registered.rowsWritten !== 1) {
        const raced = sql.exec('SELECT credential_id FROM machines WHERE id = ?', machineId).toArray()[0] as any;
        if (!raced) return Response.json({ error: 'unknown_machine' }, { status: 404 });
        if (raced.credential_id !== live.id) {
          return Response.json({ error: 'forbidden', reason: 'stale_machine_binding' }, { status: 403 });
        }
      }

      // machine_id comes only from the presented credential, never from the body
      return Response.json({ status: 'registered', machine_id: machineId });
    });
  }

  if (path === '/api/machines/available') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host');
    if (!cred) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const now = Date.now();
    const machines = sql.exec('SELECT * FROM machines WHERE drained = 0').toArray() as any[];
    const available = machines.filter((m: any) => {
      const memState = machineState.get(m.id);
      let lastSeen = 0;
      if (memState) {
        lastSeen = memState.lastSeenAt;
      } else if (m.last_checkin_at) {
        lastSeen = Date.parse(m.last_checkin_at);
      }
      return Number.isFinite(lastSeen) && (now - lastSeen) <= 900000;
    }).map((m: any) => ({
      id: m.id,
      name: m.name || m.hostname,
      hostname: m.hostname,
      os: m.os ?? '',
      arch: (m as any).arch ?? '',
      ssh: m.network === 'home' ? undefined : m.hostname,
      roles: JSON.parse(m.roles || '[]'),
      capabilities: JSON.parse(m.capabilities || '{}')
    }));

    return Response.json({ machines: available });
  }

  if (path === '/api/machines') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    const session = await getSession(env, request);
    if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const machines = sql.exec('SELECT * FROM machines').toArray() as any[];
    return Response.json({ machines: machines.map((m: any) => ({ ...m, roles: JSON.parse(m.roles as string || '[]'), capabilities: JSON.parse(m.capabilities as string || '{}') })) });
  }

  if (path.startsWith('/api/machines/') && path.endsWith('/drain')) {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const session = await getSession(env, request);
    if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });

    const id = path.split('/')[3];
    const res = sql.exec('UPDATE machines SET drained = 1 WHERE id = ?', id);
    if (res.rowsWritten === 0) return Response.json({ error: 'not_found' }, { status: 404 });
    return Response.json({ status: 'drained' });
  }

  if (path.startsWith('/api/machines/') && path.endsWith('/undrain')) {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const session = await getSession(env, request);
    if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });
    if (!verifyCsrf(request, session)) return Response.json({ error: 'CSRF token missing' }, { status: 403 });

    const id = path.split('/')[3];
    const res = sql.exec('UPDATE machines SET drained = 0 WHERE id = ?', id);
    if (res.rowsWritten === 0) return Response.json({ error: 'not_found' }, { status: 404 });
    return Response.json({ status: 'undrained' });
  }

  if (path === '/api/host/poll') {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await safeJson(request);
    if (!body || body.type !== 'heartbeat') return Response.json({ error: 'malformed_json' }, { status: 400 });

    updateHeartbeat(cred.machine_id, body, sql);
    return Response.json({ offers: [] });
  }

  if (path === '/api/host/connect') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    if (request.headers.get('Upgrade') !== 'websocket') return Response.json({ error: 'Expected Upgrade: websocket' }, { status: 426 });
    
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    
    ctx.acceptWebSocket(server);
    server.serializeAttachment({ machineId: cred.machine_id, credentialId: cred.id });

    // DO NOT mark last_checkin_at or update heartbeat until an actual heartbeat is received.
    // Memory state only: we can just add an initial state that doesn't trigger check-in?
    // Actually, registration is not checkin. So we do nothing.
    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  }

  return null;
}

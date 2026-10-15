import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from './env';
import { verifyBearerScope } from './auth/token';
import { getSession, verifyCsrf } from './auth/session';

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

function sanitizeHeartbeat(body: any) {
  if (!body || body.type !== 'heartbeat') return null;
  const hb: any = { type: 'heartbeat' };
  if (typeof body.at === 'string' && body.at.length <= 64) hb.at = body.at;
  if (typeof body.mem_avail_mb === 'number' && Number.isFinite(body.mem_avail_mb)) hb.mem_avail_mb = body.mem_avail_mb;
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

const machineState = new Map<string, { lastSeenAt: number, lastPersistedAt: number, fingerprint: string, hb: any }>();

function getFingerprint(hb: any): string {
  return JSON.stringify({
    gate: hb.gate,
    running_jobs: hb.running_jobs,
    sessions_summary: hb.sessions_summary
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
  const fingerprint = getFingerprint(hb);
  let st = machineState.get(machineId);
  
  if (!st) {
    st = { lastSeenAt: now, lastPersistedAt: 0, fingerprint: '', hb: null };
    machineState.set(machineId, st);
  }
  st.lastSeenAt = now;
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
  env: Env
): Promise<Response | null> {
  if (path === '/api/machines/register') {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body = await safeJson(request);
    if (!body || typeof body !== 'object') return Response.json({ error: 'malformed_json' }, { status: 400 });

    const hostname = body.hostname;
    const os = body.os;
    const network = body.network;
    const roles = body.roles;
    const capabilities = body.capabilities;

    if (typeof hostname !== 'string' || hostname.length > 256) return Response.json({ error: 'invalid_hostname' }, { status: 400 });
    if (typeof os !== 'string' || os.length > 256) return Response.json({ error: 'invalid_os' }, { status: 400 });
    if (typeof network !== 'string' || network.length > 256) return Response.json({ error: 'invalid_network' }, { status: 400 });
    
    if (!Array.isArray(roles) || !roles.every((r: any) => typeof r === 'string')) return Response.json({ error: 'invalid_roles' }, { status: 400 });
    if (typeof capabilities !== 'object' || capabilities === null) return Response.json({ error: 'invalid_capabilities' }, { status: 400 });

    const rolesJson = JSON.stringify(roles);
    const capJson = JSON.stringify(capabilities);
    if (rolesJson.length > 4096 || capJson.length > 4096) return Response.json({ error: 'payload_too_large' }, { status: 400 });

    sql.exec(`
      UPDATE machines 
      SET hostname = ?, os = ?, network = ?, roles = ?, capabilities = ?, credential_id = ?
      WHERE id = ?
    `, hostname, os, network, rolesJson, capJson, cred.id, cred.machine_id);

    // machine_id comes only from the presented credential, never from the body
    return Response.json({ status: 'registered', machine_id: cred.machine_id });
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

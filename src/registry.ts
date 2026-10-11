import { SqlStorage, DurableObjectState } from "@cloudflare/workers-types";
import { Env } from './env';
import { verifyBearerScope } from './auth/token';
import { getSession, verifyCsrf } from './auth/session';

export async function handleRegistryRoutes(
  request: Request,
  path: string,
  sql: SqlStorage,
  ctx: DurableObjectState,
  env: Env
): Promise<Response | null> {
  // POST /api/machines/register
  if (path === '/api/machines/register') {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body: any = await request.json().catch(() => null);
    if (!body || typeof body !== 'object') return Response.json({ error: 'invalid_request' }, { status: 400 });

    const { hostname, os, network, roles, capabilities } = body;
    if (typeof hostname !== 'string' || hostname.length > 256) return Response.json({ error: 'invalid_hostname' }, { status: 400 });
    if (typeof os !== 'string' || os.length > 256) return Response.json({ error: 'invalid_os' }, { status: 400 });
    if (typeof network !== 'string' || network.length > 256) return Response.json({ error: 'invalid_network' }, { status: 400 });
    
    if (!Array.isArray(roles) || !roles.every(r => typeof r === 'string')) return Response.json({ error: 'invalid_roles' }, { status: 400 });
    if (typeof capabilities !== 'object' || capabilities === null) return Response.json({ error: 'invalid_capabilities' }, { status: 400 });

    const rolesJson = JSON.stringify(roles);
    const capJson = JSON.stringify(capabilities);
    if (rolesJson.length > 4096 || capJson.length > 4096) return Response.json({ error: 'payload_too_large' }, { status: 400 });

    sql.exec(`
      UPDATE machines 
      SET hostname = ?, os = ?, network = ?, roles = ?, capabilities = ?, credential_id = ?
      WHERE id = ?
    `, hostname, os, network, rolesJson, capJson, cred.id, cred.machine_id);

    return Response.json({ status: 'registered' });
  }

  // GET /api/machines/available
  if (path === '/api/machines/available') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host');
    // Note: The spec says Admin GET /api/machines requires valid HMAC admin session, but /api/machines/available might just require work scope?
    // Wait, let's allow it for work scope so hosts can find each other.
    if (!cred) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const now = Date.now();
    const machines = sql.exec('SELECT * FROM machines WHERE drained = 0').toArray();
    const available = machines.filter((m: any) => {
      if (!m.last_checkin_at) return false;
      const t = Date.parse(m.last_checkin_at);
      return Number.isFinite(t) && (now - t) <= 900000;
    }).map((m: any) => ({
      id: m.id,
      hostname: m.hostname,
      ssh: m.hostname,
      roles: JSON.parse(m.roles || '[]'),
      capabilities: JSON.parse(m.capabilities || '{}')
    }));

    return Response.json({ machines: available });
  }

  // GET /api/machines
  if (path === '/api/machines') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    const session = await getSession(env, request);
    if (!session || !session.isAdmin) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const machines = sql.exec('SELECT * FROM machines').toArray();
    return Response.json({ machines: machines.map(m => ({ ...m, roles: JSON.parse(m.roles as string || '[]'), capabilities: JSON.parse(m.capabilities as string || '{}') })) });
  }

  // POST /api/machines/<id>/drain
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

  // POST /api/machines/<id>/undrain
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

  // POST /api/host/poll
  if (path === '/api/host/poll') {
    if (request.method !== 'POST') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'POST' } });
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const body: any = await request.json().catch(() => null);
    if (!body || body.type !== 'heartbeat') return Response.json({ error: 'invalid_request' }, { status: 400 });

    updateHeartbeat(cred.machine_id, body, sql);
    // Poll returns protocol-compatible offerlist(empty while M4 notimplemented)
    return Response.json({ offers: [] });
  }

  // GET /api/host/connect
  if (path === '/api/host/connect') {
    if (request.method !== 'GET') return new Response('Method Not Allowed', { status: 405, headers: { Allow: 'GET' } });
    if (request.headers.get('Upgrade') !== 'websocket') return Response.json({ error: 'Expected Upgrade: websocket' }, { status: 426 });
    
    const cred = await verifyBearerScope(sql, request.headers.get('Authorization'), 'host', { kind: 'host' });
    if (!cred || !cred.machine_id) return Response.json({ error: 'Unauthorized' }, { status: 401 });

    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);
    
    ctx.acceptWebSocket(server);
    server.serializeAttachment({ machineId: cred.machine_id, credentialId: cred.id });
    
    // Update heartbeat with checkin timestamp just in case
    sql.exec('UPDATE machines SET last_checkin_at = ? WHERE id = ?', new Date().toISOString(), cred.machine_id);

    return new Response(null, {
      status: 101,
      webSocket: client,
    });
  }

  return null;
}

// In-memory state for websocket heartbeats to avoid writing to SQL every beat (max once/300s)
const machineLastHb = new Map<string, { ts: number, hb: any }>();

export function updateHeartbeat(machineId: string, hbPayload: any, sql: SqlStorage) {
  const now = Date.now();
  const last = machineLastHb.get(machineId);
  
  let stateChanged = true;
  if (last) {
    // Check if state changed. We only care about fields in hbPayload.
    stateChanged = JSON.stringify(last.hb) !== JSON.stringify(hbPayload);
  }

  if (stateChanged || !last || now - last.ts > 300000) {
    machineLastHb.set(machineId, { ts: now, hb: hbPayload });
    sql.exec(
      'UPDATE machines SET last_checkin_at = ?, last_heartbeat = ? WHERE id = ?',
      new Date(now).toISOString(),
      JSON.stringify(hbPayload),
      machineId
    );
  } else {
    // Still update memory
    machineLastHb.set(machineId, { ts: now, hb: hbPayload });
  }
}

export function handleWebSocketMessageRegistry(ws: WebSocket, message: string | ArrayBuffer, sql: SqlStorage) {
  try {
    const attachment: any = ws.deserializeAttachment();
    if (!attachment || !attachment.machineId || !attachment.credentialId) {
      ws.close(1008, 'Invalid attachment');
      return;
    }

    // ALWAYS revalidate credential expiry/revocation on incoming messages
    const cred = sql.exec('SELECT * FROM credentials WHERE id = ?', attachment.credentialId).toArray()[0] as any;
    if (!cred || cred.revoked_at || !Number.isFinite(Date.parse(cred.expires_at)) || Date.parse(cred.expires_at) <= Date.now()) {
      ws.close(1008, 'Credential revoked or expired');
      return;
    }

    if (typeof message !== 'string') return; // ignore binary
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


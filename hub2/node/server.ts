// Node HTTP front for the hub2 worker: plain requests to entry.fetch, and
// RFC6455 upgrades bridged into the DO WebSocket surface. Binds loopback only
// in production; the edge (caddy / CloudFront) terminates external traffic.

import http from 'node:http';
import { Readable } from 'node:stream';
import { WebSocketServer, type WebSocket as WireSocket } from 'ws';
import { responseWebSocket } from './compat.ts';
import { decodeFrame, type ServerSideSocket } from './wire.ts';
import type { Hub2Runtime } from './runtime.ts';

type PairClient = { _server: ServerSideSocket };

function rawHeaders(request: http.IncomingMessage): Headers {
  const headers = new Headers();
  for (const [key, values] of Object.entries(request.headers)) {
    if (values === undefined) continue;
    for (const value of Array.isArray(values) ? values : [values]) headers.append(key, value);
  }
  return headers;
}

async function buildRequest(request: http.IncomingMessage, origin: string): Promise<Request> {
  const method = request.method ?? 'GET';
  const headers = rawHeaders(request);
  const hasBody = !['GET', 'HEAD', 'OPTIONS'].includes(method);
  const body = hasBody ? (Readable.toWeb(request) as unknown) : undefined;
  return new Request(origin + (request.url ?? '/'), {
    method,
    headers,
    body: body as never,
    ...(hasBody ? ({ duplex: 'half' } as never) : {}),
  });
}

export async function writeResponse(response: Response, out: http.ServerResponse): Promise<void> {
  const headers: Record<string, string | string[]> = {};
  response.headers.forEach((value, key) => {
    if (key.toLowerCase() === 'set-cookie') return;
    headers[key] = headers[key] ? headers[key] + ', ' + value : value;
  });
  // Set-Cookie must never be comma-folded: a merged header makes browsers apply
  // the later cookie's Max-Age=0 to the session cookie.
  const cookies = response.headers.getSetCookie();
  if (cookies.length) headers['set-cookie'] = cookies;
  out.writeHead(response.status, headers);
  if (!response.body) {
    out.end();
    return;
  }
  await new Promise<void>((resolve, reject) => {
    Readable.fromWeb(response.body as never)
      .pipe(out)
      .on('finish', resolve)
      .on('error', reject);
  });
}

async function rejectUpgrade(socket: { write(s: string): unknown; destroy(): unknown }, response: Response): Promise<void> {
  const text = (await response.text()).slice(0, 8192);
  const headers: string[] = [
    `HTTP/1.1 ${response.status} ${response.statusText || 'error'}`,
    'Content-Type: application/json; charset=utf-8',
    `Content-Length: ${Buffer.byteLength(text)}`,
    'Connection: close',
  ];
  response.headers.forEach((value, key) => {
    if (key.toLowerCase() !== 'content-type' && key.toLowerCase() !== 'content-length') {
      headers.push(`${key}: ${value}`);
    }
  });
  (socket as import('node:net').Socket).write(headers.join('\r\n') + '\r\n\r\n' + text);
  (socket as import('node:net').Socket).destroy();
}

export function startServer(
  runtime: Hub2Runtime,
  options: { host?: string; port?: number } = {},
): Promise<{ close(): Promise<void>; port: number }> {
  const wss = new WebSocketServer({ noServer: true, perMessageDeflate: false });
  const server = http.createServer((request, response) => {
    void (async () => {
      try {
        const host = request.headers.host ?? `127.0.0.1:${options.port ?? 0}`;
        const upstream = await runtime.fetch(await buildRequest(request, `http://${host}`));
        if (upstream.status === 101 && responseWebSocket(upstream)) {
          // An upgrade response leaked into the plain path: refuse politely.
          response.writeHead(426, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({ error: 'websocket_upgrade_required' }));
          return;
        }
        await writeResponse(upstream, response);
      } catch (error) {
        console.error('[hub2-node] request failed:', error);
        if (!response.headersSent) {
          response.writeHead(500, { 'Content-Type': 'application/json' });
          response.end(JSON.stringify({ error: 'internal_error' }));
        } else {
          response.destroy();
        }
      }
    })();
  });

  server.on('upgrade', (request, socket, head) => {
    void (async () => {
      try {
        const host = request.headers.host ?? `127.0.0.1:${options.port ?? 0}`;
        const upstream = await runtime.fetch(await buildRequest(request, `http://${host}`));
        const pair = responseWebSocket(upstream) as PairClient | undefined;
        if (upstream.status !== 101 || !pair?._server) {
          await rejectUpgrade(socket, upstream);
          return;
        }
        const serverSocket = pair._server;
        wss.handleUpgrade(request, socket, head, wire => {
          bindWire(serverSocket, wire);
        });
      } catch (error) {
        console.error('[hub2-node] upgrade failed:', error);
        socket.destroy();
      }
    })();
  });

  function bindWire(serverSocket: ServerSideSocket, wire: WireSocket): void {
    serverSocket.bindWire({
      send: data => wire.send(data),
      close: (code, reason) => {
        try {
          wire.close(code, reason);
        } catch {
          wire.terminate();
        }
      },
    });
    serverSocket.accept();
    let missedPongs=0;
    wire.on('pong',()=>{missedPongs=0;});
    const keepalive=setInterval(()=>{
      if(wire.readyState!==1)return;
      if(missedPongs>=2){wire.terminate();return;}
      missedPongs++;
      wire.ping();
    },20_000);
    keepalive.unref();
    wire.once('close',()=>clearInterval(keepalive));
    wire.on('message', data => {
      const buffer = Array.isArray(data) ? Buffer.concat(data) : Buffer.from(data as Buffer);
      serverSocket.__deliver?.message(decodeFrame(buffer));
    });
    wire.on('close', () => serverSocket.wireClosed());
    wire.on('error', error => {
      serverSocket.__deliver?.error(error);
    });
  }

  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(options.port ?? 0, options.host ?? '127.0.0.1', () => {
      const address = server.address();
      const port = typeof address === 'object' && address ? address.port : options.port ?? 0;
      resolve({
        port,
        close: async () => {
          for (const wire of wss.clients) wire.terminate();
          await new Promise<void>(done => {
            server.close(() => done());
            // Keep-alive clients would hold close() open; drop them like
            // Miniflare.dispose did so restarts are deterministic.
            server.closeIdleConnections();
            setTimeout(() => { server.closeAllConnections(); done(); }, 200).unref();
          });
          await runtime.dispose();
        },
      });
    });
  });
}

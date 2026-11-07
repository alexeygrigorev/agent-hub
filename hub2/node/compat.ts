// Node compatibility shims for the hub2 worker source. The worker code is
// written against the Workers runtime surface (WebSocketPair, Response
// initialized with a webSocket and status 101); these globals let the exact
// same source run on plain Node (undici globals + node:sqlite). No Cloudflare
// package is imported at runtime.

import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { resolve as resolvePath, dirname as dirnamePath } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { WebSocketPair } from './wire.ts';

const responseSockets = new WeakMap<object, unknown>();

type ResponseInitWithSocket = ResponseInit & { webSocket?: unknown };

const ShimResponse = class Response extends globalThis.Response {
  constructor(body?: BodyInit | null, init?: ResponseInitWithSocket) {
    const socket = init?.webSocket;
    if (socket !== undefined || init?.status === 101) {
      // undici rejects status 101 and drops unknown init members; carry the
      // paired socket out-of-band and present the requested status.
      const { webSocket: _socket, ...rest } = init ?? {};
      super(body ?? null, { ...rest, status: 200 });
      if (socket !== undefined) responseSockets.set(this, socket);
      Object.defineProperty(this, 'status', { value: init?.status ?? 200, configurable: true });
      // Worker code (sql-receipt wrapping) reads .webSocket off a response to
      // rebuild it; expose the captured socket the way workerd does.
      Object.defineProperty(this, 'webSocket', { get: () => socket, configurable: true });
      return;
    }
    super(body, init);
  }
};

/** Reads the server-side socket captured from `new Response(null, {status: 101, webSocket})`. */
export function responseWebSocket(response: object): unknown | undefined {
  return responseSockets.get(response);
}

/** Resolves `.ts` sibling imports made without an explicit extension, which the
 * worker bundle format allowed and Node type stripping does not. */
export function registerTypeScriptResolveHook(): void {
  registerHooks({
    resolve(specifier, context, nextResolve) {
      if (specifier.startsWith('.') && context.parentURL && !specifier.endsWith('.ts')) {
        const from = fileURLToPath(context.parentURL);
        const candidate = resolvePath(dirnamePath(from), specifier + '.ts');
        if (existsSync(candidate)) return nextResolve(pathToFileURL(candidate).href, context);
      }
      return nextResolve(specifier, context);
    },
  });
}

const ShimRequest = class Request extends globalThis.Request {
  constructor(input: RequestInfo | URL, init?: RequestInit) {
    if (init && typeof (init as { body?: unknown }).body !== 'undefined' && typeof (init as { headers?: unknown }).headers === 'object' && !Array.isArray(init as unknown)) {
      // Request-instance forwarding (workerd style): copy wire properties.
      const source = init as globalThis.Request;
      // `new Request(url, someRequest)` (workerd-style forwarding): copy the
      // request's wire properties into a plain init; undici brand-checks its
      // own getters so prototype delegation fails, and a stream body needs
      // the duplex marker the Workers runtime implies.
      const forwarded = {
        method: source.method,
        headers: source.headers,
        body: source.body,
        redirect: source.redirect,
        signal: source.signal,
        duplex: 'half',
      } as RequestInit;
      super(input as never, forwarded);
      return;
    }
    // Plain inits: undici requires duplex:'half' for stream bodies; the
    // Workers runtime defaults to it.
    super(input as never, init?.body ? ({ ...init, duplex: 'half' } as never) : init);
  }
};

export function installCompatGlobals(): void {
  const globals = globalThis as unknown as { Response: unknown; Request: unknown; WebSocketPair: unknown };
  globals.Response = ShimResponse;
  globals.Request = ShimRequest;
  globals.WebSocketPair = WebSocketPair;
}

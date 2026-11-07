// Static asset binding (env.ASSETS) served from hub2/public. run_worker_first
// semantics: the worker routes everything; the binding is only consulted
// explicitly (currently the dashboard root).

import { readFile, stat } from 'node:fs/promises';
import { join, normalize, sep } from 'node:path';

const CONTENT_TYPES: Record<string, string> = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.ico': 'image/x-icon',
  '.txt': 'text/plain; charset=utf-8',
};

export function assetFetcher(directory: string): { fetch(request: Request): Promise<Response> } {
  return {
    async fetch(request: Request): Promise<Response> {
      let pathname: string;
      try {
        pathname = decodeURIComponent(new URL(request.url).pathname);
      } catch {
        return new Response('bad_request', { status: 400 });
      }
      if (pathname === '/' || pathname === '') pathname = '/index.html';
      const resolved = normalize(join(directory, pathname));
      if (resolved !== directory && !resolved.startsWith(directory + sep)) {
        return new Response('not_found', { status: 404 });
      }
      try {
        const info = await stat(resolved);
        if (!info.isFile()) return new Response('not_found', { status: 404 });
        const body = await readFile(resolved);
        const type = CONTENT_TYPES[resolved.slice(resolved.lastIndexOf('.'))] ?? 'application/octet-stream';
        return new Response(body, {
          status: 200,
          headers: { 'Content-Type': type, 'Content-Length': String(info.size) },
        });
      } catch {
        return new Response('not_found', { status: 404 });
      }
    },
  };
}

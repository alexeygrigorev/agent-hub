// Static asset binding (env.ASSETS) served from hub2/public. run_worker_first
// semantics: the worker routes everything; the binding is only consulted
// explicitly (currently the dashboard root).

import { readFile, readlink, stat } from 'node:fs/promises';
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

export function assetFetcher(directory: string, cliReleaseRoot?: string): { fetch(request: Request): Promise<Response> } {
  return {
    async fetch(request: Request): Promise<Response> {
      let pathname: string;
      try {
        pathname = decodeURIComponent(new URL(request.url).pathname);
      } catch {
        return new Response('bad_request', { status: 400 });
      }
      if (pathname === '/' || pathname === '') pathname = '/index.html';
      try {
        let assetRoot = directory;
        if (cliReleaseRoot && pathname.startsWith('/cli/')) {
          const target = await readlink(join(cliReleaseRoot, 'current')).catch((err: NodeJS.ErrnoException) => {
            if (err.code === 'ENOENT') return undefined;
            throw err;
          });
          // A missing pointer is the initial deployment or its rollback. Once
          // active, resolve one immutable release per request with no fallback.
          if (target !== undefined) {
            if (!/^releases\/[0-9a-f]{40}$/.test(target)) return new Response('not_found', { status: 404 });
            assetRoot = join(cliReleaseRoot, target);
            pathname = pathname.slice('/cli'.length);
          }
        }
        const resolved = normalize(join(assetRoot, pathname));
        if (resolved !== assetRoot && !resolved.startsWith(assetRoot + sep)) {
          return new Response('not_found', { status: 404 });
        }
        const info = await stat(resolved);
        if (!info.isFile()) return new Response('not_found', { status: 404 });
        const body = await readFile(resolved);
        const type = CONTENT_TYPES[resolved.slice(resolved.lastIndexOf('.'))] ?? 'application/octet-stream';
        return new Response(body, {
          status: 200,
          headers: { 'Content-Type': type, 'Content-Length': String(body.length) },
        });
      } catch {
        return new Response('not_found', { status: 404 });
      }
    },
  };
}

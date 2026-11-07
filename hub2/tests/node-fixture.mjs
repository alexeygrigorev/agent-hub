// Node runtime fixture for hub2 semantic tests: hosts the real worker entry
// and Durable Object classes on the production Node server stack (SQLite
// persistence, WebSocket bridge) on an ephemeral loopback port. Replaces the
// former Miniflare harness after the Cloudflare runtime retirement.

import { mkdtemp, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { resolve, dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { registerTypeScriptResolveHook, installCompatGlobals } from '../node/compat.ts';
import { Hub2Runtime } from '../node/runtime.ts';
import { startServer } from '../node/server.ts';
import { assetFetcher } from '../node/assets.ts';

const here = dirname(fileURLToPath(import.meta.url));

// Install before any hub2 source is imported by the runner module graph.
registerTypeScriptResolveHook();
installCompatGlobals();

/** Fresh CI checkouts lack .local/tmp; ensure it exists and return a new
 * uniquely-named directory under it. */
export async function makeTempDir(prefix) {
  const parent = resolve(here, '../.local/tmp');
  await mkdir(parent, { recursive: true });
  return mkdtemp(join(parent, prefix));
}

export async function createFixture(spec) {
  await mkdir(resolve(spec.root ?? here, '../.local/tmp'), { recursive: true });
  const dataDir = spec.dataDir ?? await mkdtemp(resolve(spec.root ?? here, '../.local/tmp/hub2-node-fixture-'));
  const runtime = new Hub2Runtime({
    classes: spec.classes,
    entry: spec.entry,
    env: { ...spec.env, ASSETS: assetFetcher(resolve(here, '../public')) },
    dataDir,
  });
  const server = await startServer(runtime, { host: '127.0.0.1', port: 0 });
  const fixture = {
    origin: `http://127.0.0.1:${server.port}`,
    dataDir,
    fetch(path, init) {
      return fetch(this.origin + path, init);
    },
    async dispose() {
      await server.close();
    },
  };
  return fixture;
}

/** Delays a runner restart the way Miniflare dispose/recreate did, keeping the
 * same persistence directory so Durable Object state survives. */
export async function restartFixture(current, spec) {
  await current.dispose();
  return createFixture({ ...spec, dataDir: current.dataDir });
}

// hub2 on Node: plain-process server over the same worker source, with the
// Durable Object databases as local SQLite files (DB on the machine).
//
//   node --experimental-transform-types hub2/node/main.ts \
//     --host 127.0.0.1 --port 8787 \
//     --data-dir /var/lib/agent-hub/hub2-db \
//     --dev-vars /opt/agent-hub/.dev.vars
//
// No Cloudflare package is imported; node:sqlite, undici globals and ws are
// the only runtime pieces beyond the hub2 source itself.

import { dirname, isAbsolute, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { mkdirSync, readFileSync, lstatSync, constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { createHash } from 'node:crypto';
import { registerTypeScriptResolveHook, installCompatGlobals } from './compat.ts';

registerTypeScriptResolveHook();
installCompatGlobals();

const here = dirname(fileURLToPath(import.meta.url));

function parseArgs(argv: string[]): Record<string, string> {
  const out: Record<string, string> = {};
  for (let index = 0; index < argv.length; index += 1) {
    const arg = argv[index];
    if (!arg.startsWith('--')) continue;
    const eq = arg.indexOf('=');
    if (eq > 0) out[arg.slice(2, eq)] = arg.slice(eq + 1);
    else {
      const next = argv[index + 1];
      if (next !== undefined && !next.startsWith('--')) {
        out[arg.slice(2)] = next;
        index += 1;
      } else out[arg.slice(2)] = 'true';
    }
  }
  return out;
}

const args = parseArgs(process.argv.slice(2));
const host = args.host ?? process.env.HUB2_BIND_HOST ?? '127.0.0.1';
const port = Number(args.port ?? process.env.HUB2_BIND_PORT ?? 8787);
const dataDir = resolve(args['data-dir'] ?? '/var/lib/agent-hub/hub2-db');
const devVars = args['dev-vars'] ?? join(here, '../../.dev.vars');
const publicDir = resolve(args.public ?? join(here, '../public'));
const cliReleaseRoot = args['cli-release-root'];
if (cliReleaseRoot !== undefined && !isAbsolute(cliReleaseRoot)) {
  throw new Error('--cli-release-root requires an absolute path');
}

function optionalSourceHash(path: string): string {
  const info = lstatSync(path);
  if (!info.isFile() || info.size > 16 * 1024 * 1024) throw new Error('observer_unavailable');
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}
async function optionalObserverHash(path: string): Promise<string> {
  const handle = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const info = await handle.stat();
    if (!info.isFile() || info.size > 16 * 1024 * 1024) throw new Error('observer_unavailable');
    const bytes = Buffer.alloc(info.size + 1);
    const { bytesRead } = await handle.read(bytes, 0, bytes.length, 0);
    if (bytesRead !== info.size) throw new Error('observer_unavailable');
    return createHash('sha256').update(bytes.subarray(0, bytesRead)).digest('hex');
  } finally { await handle.close(); }
}
// Capture primary source identity before importing it. Later observer checks
// compare both this load-time measurement and current installed bytes.
let loadedPrimarySources: Record<string, string> | undefined;
try {
  loadedPrimarySources = Object.fromEntries([fileURLToPath(import.meta.url), join(here, '../src/worker.ts')]
    .map(path => [path, optionalSourceHash(path)]));
} catch { /* Optional source proof is unavailable; primary imports still run. */ }

const [{ default: entry, ControlDO, ProjectDO, MachineDO, AccountDO, MailboxDO }, { Hub2Runtime }, { assetFetcher }, { loadEnvironment }, { startServer }] =
  await Promise.all([
    import('../production-worker.ts'),
    import('./runtime.ts'),
    import('./assets.ts'),
    import('./config.ts'),
    import('./server.ts'),
  ]);

const envVars = await loadEnvironment(devVars === 'none' ? undefined : devVars, args['dev-vars'] !== undefined);
const classes = { CONTROL: ControlDO, PROJECTS: ProjectDO, MACHINES: MachineDO, ACCOUNTS: AccountDO, MAILBOXES: MailboxDO };
// Optional observer code/config may be missing or broken without stopping the
// primary server. A late bootstrap is never attached after primary ingress may
// have begun: that incarnation stays unavailable, not retroactively complete.
let alertEvidence: import('../src/types.ts').CredentialMonitorEvidence | undefined;
let acceptingObserver = true;
const observerLoad = (async () => {
  if (!envVars.HUB2_ALERT_EVIDENCE_DIR) return undefined;
  if (!loadedPrimarySources) return undefined;
  const observerPath = join(here, 'alert-evidence.ts');
  const loadedArtifacts = { ...loadedPrimarySources, [observerPath]: await optionalObserverHash(observerPath) };
  const { AlertEvidence } = await import('./alert-evidence.ts');
  const observer = await AlertEvidence.load(envVars.HUB2_ALERT_EVIDENCE_DIR, { loadedArtifacts });
  if (!acceptingObserver) { void observer.close().catch(() => {}); return undefined; }
  return observer;
})();
let observerTimer: ReturnType<typeof setTimeout> | undefined;
try {
  alertEvidence = await Promise.race([observerLoad, new Promise<undefined>(resolve => {
    observerTimer = setTimeout(() => resolve(undefined), 250);
  })]);
} catch { /* No raw error/config/path reaches logs or primary responses. */ }
finally { acceptingObserver = false; clearTimeout(observerTimer); }
void observerLoad.then(value => { if (value !== alertEvidence) void value?.close().catch(() => {}); }).catch(() => {});
const runtimeEnv = { ...envVars, ASSETS: assetFetcher(publicDir, cliReleaseRoot),
  CREDENTIAL_MONITOR_EVIDENCE: alertEvidence };
// Do not serve /health before existing object migrations and recovery finish.
// This runs before Hub2Runtime starts its alarm sweep.
mkdirSync(dataDir, { recursive: true });
const { probeSchemaReadiness } = await import('./schema-readiness.ts');
await probeSchemaReadiness({ classes: classes as never, env: runtimeEnv, dataDir });
const runtime = new Hub2Runtime({
  classes: classes as never,
  entry,
  env: runtimeEnv,
  dataDir,
});

const publicOrigin = envVars.AUTH_CALLBACK_URL ? new URL(envVars.AUTH_CALLBACK_URL).origin : undefined;
const listening = await startServer(runtime, { host, port, publicOrigin });
console.log(
  `[hub2-node] listening http://${host}:${listening.port} release=${envVars.HUB2_RELEASE_MODE ?? 'unconfigured'} data=${dataDir}`,
);

let closing = false;
async function shutdown(signal: string): Promise<void> {
  if (closing) return;
  closing = true;
  console.log(`[hub2-node] ${signal} received, closing`);
  await listening.close();
  process.exit(0);
}
process.on('SIGTERM', () => void shutdown('SIGTERM'));
process.on('SIGINT', () => void shutdown('SIGINT'));

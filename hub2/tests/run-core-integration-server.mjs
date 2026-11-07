// TEST ONLY: actual five-DO Node composition, durable private bootstrap, real provider egress.
// Never imported by a deployment, installer or production host. Run on the Linux test host.
import { mkdtemp, mkdir, readFile, writeFile, chmod, open, realpath } from 'node:fs/promises';
import { constants } from 'node:fs';
import { resolve, join, dirname, basename } from 'node:path';
import { randomBytes, createHash } from 'node:crypto';
import { createFixture } from './node-fixture.mjs';

const root = resolve(import.meta.dirname, '../..'), parent = resolve(root, '.local/tmp');
await mkdir(parent, { recursive: true });
if (process.argv.length > 2 && (process.argv[2] !== '--resume' || process.argv.length !== 4)) throw new Error('usage: --resume PRIVATE_DIRECTORY');
const directory = process.argv[2] ? await realpath(process.argv[3]) : await mkdtemp(join(parent, 'live-five-do-'));
if (dirname(directory) !== await realpath(parent) || !basename(directory).startsWith('live-five-do-')) throw new Error('fixture_directory_outside_private_root');
await chmod(directory, 0o700);
const secretPath = join(directory, 'fixture-secrets.json');
async function privateRead(path) {
  const file = await open(path, constants.O_RDONLY | constants.O_NOFOLLOW);
  try {
    const info = await file.stat();
    if (!info.isFile() || info.uid !== process.getuid() || (info.mode & 0o777) !== 0o600 || info.size > 4096) throw new Error('fixture_secret_not_private');
    return JSON.parse(await file.readFile('utf8'));
  } finally { await file.close(); }
}
if (!process.argv[2]) {
  const file = await open(secretPath, 'wx', 0o600);
  try {
    await file.writeFile(JSON.stringify({ operator_token: randomBytes(32).toString('base64url'), credential_key: randomBytes(32).toString('hex') }) + '\n');
    await file.sync();
  } finally { await file.close(); }
}
// Reload the exact durable values before constructing the runtime or allowing provider actions.
const secrets = await privateRead(secretPath);
if (!/^[A-Za-z0-9_-]{43}$/.test(secrets.operator_token) || !/^[0-9a-f]{64}$/.test(secrets.credential_key)) throw new Error('fixture_secret_corrupt');
const token = secrets.operator_token, key = secrets.credential_key;
const account = process.env.HUB2_LIVE_ACCOUNT || 'zai-central';
const core = await import('./core-integration-worker.ts');
const fixture = await createFixture({
  root,
  entry: core.default,
  classes: { CONTROL: core.CoreControl, PROJECTS: core.CoreProject, MACHINES: core.CoreMachine, ACCOUNTS: core.CoreAccount, MAILBOXES: core.CoreMailbox },
  env: {
    HUB2_DEV_OPERATOR_TOKEN: token,
    HUB2_DEFAULT_ACCOUNT_ID: account,
    HUB2_ENGINE_ACCOUNTS: JSON.stringify({ zcode: account }),
    HUB2_CREDENTIAL_KEY: key,
  },
  dataDir: join(directory, 'persist'),
});
const config = join(directory, 'operator.json');
await writeFile(config, JSON.stringify({ server: fixture.origin, token }) + '\n', { mode: 0o600 });
const ready = {
  server: fixture.origin,
  operator_config: config,
  private_directory: directory,
  real_provider_fetch: true,
  models_started: false,
  secrets_durable_and_reloaded: !!process.argv[2],
  resumed: !!process.argv[2],
  bundle_sha256: 'n/a-node-runtime',
};
await writeFile(join(directory, 'ready.json'), JSON.stringify(ready) + '\n', { mode: 0o600 });
console.log(JSON.stringify(ready));
await new Promise((resolve) => { process.once('SIGINT', resolve); process.once('SIGTERM', resolve); });

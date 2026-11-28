// Test-only preload: node --import ./hub2/tests/fs-latency.mjs with
// TEST_FS_LATENCY_MS=<ms> delays every node:fs/promises path call and
// every FileHandle operation by that many milliseconds before it starts. It
// models slow runner storage deterministically; no result is altered. Child
// processes inherit it through NODE_OPTIONS. Unset or 0 leaves fs untouched.
import fs from 'node:fs';
import { syncBuiltinESMExports } from 'node:module';

const latency = Number(process.env.TEST_FS_LATENCY_MS ?? 0);
if (!Number.isFinite(latency) || latency < 0) throw new Error('TEST_FS_LATENCY_MS must be a non-negative number');
if (latency > 0) {
  const pause = () => new Promise(resolve => setTimeout(resolve, latency));
  const delayed = original => async function (...args) { await pause(); return original.apply(this, args); };
  const promises = fs.promises;
  for (const name of ['open', 'lstat', 'stat', 'readdir', 'readFile', 'writeFile', 'rename', 'link', 'unlink', 'mkdir', 'rm'])
    promises[name] = delayed(promises[name]);
  const probe = await promises.open(process.execPath, 'r');
  const FileHandle = Object.getPrototypeOf(probe);
  await probe.close();
  for (const name of ['stat', 'read', 'write', 'writeFile', 'readFile', 'sync', 'datasync', 'truncate', 'close'])
    FileHandle[name] = delayed(FileHandle[name]);
  syncBuiltinESMExports();
}

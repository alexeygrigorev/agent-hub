// One discovered inventory for CI and local runs; every suite gets a fresh process.
import { readdir, mkdir } from 'node:fs/promises';
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';

const root = resolve(import.meta.dirname, '..');
const files = await readdir(resolve(root, 'test'));
const suites = files.filter(file => file.endsWith('.browser.mjs') || file.endsWith('.checks.mjs'))
  .concat(['machine-pairing.mjs', 'conversation-platform.mjs']).sort();
process.env.TMPDIR ||= resolve(root, '.local/tmp');
await mkdir(process.env.TMPDIR, { recursive: true });
let active;
function stop(signal) {
  if (active?.pid) {
    try { process.kill(-active.pid, signal); } catch (error) { if (error.code !== 'ESRCH') throw error; }
  }
}
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => {
  stop('SIGKILL');
  process.exit(signal === 'SIGINT' ? 130 : 143);
});
let failures = 0;
// Some suites invoke the real Python CLI against loopback fixtures. Never pass
// the live worker's identity or credential selectors to those subprocesses.
const fixtureEnv = Object.fromEntries(Object.entries(process.env).filter(([key]) =>
  !/^(APLEXER_|HUB_|AGENT_HUB_)/.test(key) &&
  !['XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME', 'XDG_RUNTIME_DIR', 'GH_CONFIG_DIR'].includes(key)));
for (const suite of suites) {
  console.log(`=== ${suite} ===`);
  const child = spawn(process.execPath, [resolve(root, 'test', suite)], {
    cwd: root, stdio: 'inherit', detached: true,
    // Native-session qualification is an explicit manual check, never a CI side effect.
    env: { ...fixtureEnv, PAIRING_NATIVE_WORKSPACE: '', INTEGRATION_NATIVE_WORKSPACE: '' },
  });
  active = child;
  const timer = setTimeout(() => stop('SIGKILL'), 180_000);
  const code = await new Promise(resolve => {
    child.once('error', error => { console.error(error.message); resolve(1); });
    child.once('exit', (code, signal) => resolve(code ?? signal));
  });
  clearTimeout(timer);
  stop('SIGKILL'); // Remove any descendants left by a failed suite.
  active = null;
  if (code !== 0) { failures++; console.error(`FAIL ${suite}: ${code}`); }
  else console.log(`PASS ${suite}`);
}
console.log(`${suites.length} standalone suites; ${failures} failed`);
process.exitCode = failures ? 1 : 0;

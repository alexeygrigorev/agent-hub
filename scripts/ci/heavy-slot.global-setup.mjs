// Host-wide heavy-test cap for directly invoked Node test harnesses (736ba282 AC6h).
// scripts/capped-test-run.sh holds one of the shared flock slots for the whole
// run; its holder child exits (releasing the slot) when this process closes
// the pipe, including on crash. Runs already under the wrapper are marked
// AGENT_HUB_HEAVY_TEST_HELD=1 and never ask for a second slot. CI runners and
// Windows have no shared host to protect; AGENT_HUB_HEAVY_TEST_AUTO=0 opts out.
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export function heavySlotWanted(env, platform) {
  return env.AGENT_HUB_HEAVY_TEST_HELD !== '1' && !env.CI && env.AGENT_HUB_HEAVY_TEST_AUTO !== '0' && platform !== 'win32';
}

export default async function setup() {
  if (!heavySlotWanted(process.env, process.platform)) return;
  const wrapper = fileURLToPath(new URL('../capped-test-run.sh', import.meta.url));
  const holder = spawn('bash', [wrapper, '--', 'bash', '-c', 'echo heavy-slot-held; read -r _'],
    { stdio: ['pipe', 'pipe', 'inherit'] });
  let acquired = false;
  let released = false;
  holder.on('exit', (code, signal) => {
    if (acquired && !released) {
      console.error(`node tests: heavy-test slot holder exited unexpectedly (${code ?? signal}); stopping this test process`);
      process.kill(process.pid, 'SIGTERM');
    }
  });
  const gotSlot = await new Promise((resolve, reject) => {
    holder.stdout.on('data', (chunk) => {
      if (String(chunk).includes('heavy-slot-held')) { acquired = true; resolve(true); }
    });
    holder.on('error', () => resolve(false));
    holder.on('exit', (code) => {
      // 3 = no slot within the wait budget: refuse to pile onto the host.
      if (code === 3) reject(new Error('heavy-test slot not acquired; host test cap is full'));
      resolve(false); // wrapper/env problem (exit 2): fail open, as before this cap existed
    });
  });
  if (!gotSlot) return;
  holder.stdout.resume();
  process.env.AGENT_HUB_HEAVY_TEST_HELD = '1'; // nested runners reuse this slot
  return () => { released = true; holder.stdin.end(); };
}

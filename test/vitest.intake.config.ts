import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-pool-workers';
export default defineConfig({
  cacheDir: './.local/intake/vite-cache',
  plugins: [cloudflareTest({ wrangler: { configPath: './wrangler.jsonc' }, miniflare: { bindings: {
    HUB_ADMINS: 'admin@fake.local', SESSION_SECRET: 'intake-fixture-session-secret-at-least-32-characters',
    HUB_ADMIN_TOKEN_SHA256: 'a'.repeat(64),
  } } })],
  test: { include: ['test/intake.test.ts'], fileParallelism: false, maxWorkers: 1 },
});

import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-pool-workers';
export default defineConfig({
  plugins: [cloudflareTest({ wrangler: { configPath: './wrangler.jsonc' }, miniflare: { bindings: {
    HUB_ADMINS: 'admin@fake.local', SESSION_SECRET: 'local-only-session-secret-at-least-32-characters',
  } } })],
  test: { include: ['test/session-control.test.ts'], fileParallelism: false },
});

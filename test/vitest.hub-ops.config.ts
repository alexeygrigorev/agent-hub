import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-pool-workers';

// Bounded hub-ops reproducer config: reuses actual app source + local SQLite,
// includes ONLY test/hub-ops-liveness.test.ts, max 2 selected cases per run via -t.
export default defineConfig({
  cacheDir: './.local/hub-ops/vite-cache',
  plugins: [cloudflareTest({ wrangler: { configPath: './wrangler.jsonc' }, miniflare: { bindings: {
    AUTH_BASE_URL: 'https://auth.fake.local',
    AUTH_CLIENT_ID: 'fake_client_id',
    AUTH_CALLBACK_URL: 'http://localhost:19130/auth/callback',
    AUTH_ISSUER: 'https://auth.fake.local',
    AUTH_JWKS_URL: 'https://auth.fake.local/.well-known/jwks.json',
    HUB_ADMINS: 'admin@fake.local',
    SESSION_SECRET: 'hubops-fixture-session-secret-at-least-32-chars',
  } } })],
  test: { include: ['test/hub-ops-liveness.test.ts'], fileParallelism: false, maxWorkers: 1 },
});

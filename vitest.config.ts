import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-pool-workers';

// The pool serves src/worker.ts (default fetch/scheduled handler plus the Hub
// Durable Object export) directly to workerd. There is no wrangler config: the
// Cloudflare deployment was retired in 7470372a and wrangler configs must not
// come back. The compatibility date is capped at the newest date the pinned
// workerd binary supports (1.20260815.x -> 2026-08-22); production runs this
// same source on Node 24, which does not use compatibility dates at all.
export default defineConfig({
  plugins: [
    cloudflareTest({
      main: './src/worker.ts',
      miniflare: {
        compatibilityDate: '2026-08-22',
        compatibilityFlags: ['nodejs_als'],
        durableObjects: { HUB: { className: 'Hub', useSQLite: true } },
        bindings: {
          AUTH_BASE_URL: 'https://auth.fake.local',
          AUTH_CLIENT_ID: 'fake_client_id',
          AUTH_CALLBACK_URL: 'http://localhost:19130/auth/callback',
          AUTH_ISSUER: 'https://auth.fake.local',
          AUTH_JWKS_URL: 'https://auth.fake.local/.well-known/jwks.json',
          HUB_ADMINS: 'admin@fake.local,another@fake.local',
          SESSION_SECRET: 'super_secret_session_key_32_chars_long_for_tests',
        },
      },
    }),
  ],
  test: {
    include: ['test/**/*.test.ts'],
    fileParallelism: false,
    globalSetup: ['./test/heavy-slot.global-setup.mjs'],
  },
});

import { defineConfig } from 'vitest/config';
import { cloudflareTest } from '@cloudflare/vitest-pool-workers';

export default defineConfig({
  plugins: [
    cloudflareTest({
      wrangler: { configPath: './wrangler.jsonc' },
      miniflare: { bindings: {
        AUTH_BASE_URL: 'https://auth.fake.local',
        AUTH_CLIENT_ID: 'fake_client_id',
        AUTH_CALLBACK_URL: 'http://localhost:19130/auth/callback',
        AUTH_ISSUER: 'https://auth.fake.local',
        AUTH_JWKS_URL: 'https://auth.fake.local/.well-known/jwks.json',
        HUB_ADMINS: 'admin@fake.local,another@fake.local',
        SESSION_SECRET: 'super_secret_session_key_32_chars_long_for_tests',
      } },
    }),
  ],
});

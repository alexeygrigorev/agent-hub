import { env } from 'cloudflare:test';
import { describe, it, expect } from 'vitest';
import type { Env } from '../src/env';

describe('Custom Domain & Callback Contract (hub.dtcdev.click)', () => {
  it('env defines optional AUTH_CALLBACK_URLS field with registered origins', () => {
    const hubEnv = env as unknown as Env;
    expect(hubEnv.AUTH_CALLBACK_URLS).toBeDefined();
    expect(hubEnv.AUTH_CALLBACK_URLS).toContain('https://hub.dtcdev.click/auth/callback');
    expect(hubEnv.AUTH_CALLBACK_URLS).toContain('https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback');
    expect(hubEnv.AUTH_CALLBACK_URLS).toContain('http://localhost:19130/auth/callback');
  });

  it('env configures HUB_ADDITIONAL_ADMINS with alexey and orin', () => {
    const hubEnv = env as unknown as Env;
    expect(hubEnv.HUB_ADDITIONAL_ADMINS).toBeDefined();
    expect(hubEnv.HUB_ADDITIONAL_ADMINS).toContain('alexey@datatalks.club');
    expect(hubEnv.HUB_ADDITIONAL_ADMINS).toContain('orin@datatalks.club');
  });

  it('Env interface supports custom domain contract', () => {
    const testConfig: Env = {
      HUB: {} as any,
      AUTH_BASE_URL: 'https://auth.dtcdev.click',
      AUTH_CLIENT_ID: '1tplqm9l6q6545mrk0gjv6eaeh',
      AUTH_CALLBACK_URL: 'https://hub.dtcdev.click/auth/callback',
      AUTH_CALLBACK_URLS: 'https://hub.dtcdev.click/auth/callback,https://agent-hub.cloudflare-ai-agent-de9ca0.workers.dev/auth/callback,http://localhost:19130/auth/callback',
      AUTH_ISSUER: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs',
      AUTH_JWKS_URL: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs/.well-known/jwks.json',
      HUB_ADMINS: '',
      HUB_ADDITIONAL_ADMINS: 'alexey@datatalks.club,orin@datatalks.club',
      SESSION_SECRET: 'test_session_secret_32_characters_long',
    };
    expect(testConfig.AUTH_CALLBACK_URL).toBe('https://hub.dtcdev.click/auth/callback');
    expect(testConfig.AUTH_CALLBACK_URLS).toContain('https://hub.dtcdev.click/auth/callback');
    expect(testConfig.HUB_ADDITIONAL_ADMINS).toContain('alexey@datatalks.club');
  });
});

// Environment assembly for the Node deployment: the same variables wrangler
// served from vars + .dev.vars, loaded from a plain KEY=VALUE file with the
// same precedence (.dev.vars wins). Secrets never live in this repository.

import { readFile } from 'node:fs/promises';

/** Matches wrangler.jsonc `vars` (non-secret defaults). */
export const WRANGLER_VARS: Record<string, string> = {
  HUB2_RELEASE_MODE: 'active',
  AUTH_CLIENT_ID: '1tplqm9l6q6545mrk0gjv6eaeh',
  AUTH_ISSUER: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs',
  AUTH_BASE_URL: 'https://auth.dtcdev.click',
  AUTH_CALLBACK_URL: 'https://hub.dtcdev.click/auth/callback',
  AUTH_JWKS_URL: 'https://cognito-idp.us-east-1.amazonaws.com/us-east-1_H7nJu52Bs/.well-known/jwks.json',
  HUB_ADMINS: 'alexey@datatalks.club,orin@datatalks.club',
};

export function parseDevVars(text: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const rawLine of text.split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) continue;
    const eq = line.indexOf('=');
    if (eq <= 0) continue;
    const key = line.slice(0, eq).trim();
    let value = line.slice(eq + 1).trim();
    if (
      (value.startsWith('"') && value.endsWith('"') && value.length >= 2) ||
      (value.startsWith("'") && value.endsWith("'") && value.length >= 2)
    ) {
      value = value.slice(1, -1);
    }
    out[key] = value;
  }
  return out;
}

export async function loadEnvironment(devVarsPath: string | undefined): Promise<Record<string, string>> {
  const env: Record<string, string> = { ...WRANGLER_VARS };
  if (devVarsPath) {
    try {
      Object.assign(env, parseDevVars(await readFile(devVarsPath, 'utf8')));
    } catch {
      // Missing .dev.vars keeps the wrangler.jsonc defaults.
    }
  }
  for (const [key, value] of Object.entries(process.env)) {
    if (key.startsWith('HUB2_') || key.startsWith('AUTH_') || key.startsWith('HUB_')) env[key] = value!;
  }
  return env;
}

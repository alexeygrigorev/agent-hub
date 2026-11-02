// Resolve the repo-pinned package from CI or the stable user-owned local donor.
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { userInfo } from 'node:os';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const PINNED_PLAYWRIGHT_VERSION = '1.64.1';

export function ownedPlaywrightPackage(home = userInfo().homedir) {
  return resolve(home, '.local/share/agent-hub/playwright/node_modules/playwright/package.json');
}

export function resolvePlaywrightPackage(
  projectPackageFile = fileURLToPath(new URL('../package.json', import.meta.url)),
  home = userInfo().homedir,
) {
  const localPackage = resolve(dirname(projectPackageFile), 'node_modules/playwright/package.json');
  if (existsSync(localPackage)) return localPackage;

  const ownedPackage = ownedPlaywrightPackage(home);
  if (existsSync(ownedPackage)) return ownedPackage;

  throw new Error(
    `Pinned Playwright ${PINNED_PLAYWRIGHT_VERSION} is unavailable at ${localPackage} or ${ownedPackage}; ` +
    'run the documented setup in docs/TEST-COVERAGE.md',
  );
}

// Managed sessions can set HOME to a private engine directory. Playwright's
// default cache must still point to the Chromium installed for the real user.
process.env.PLAYWRIGHT_BROWSERS_PATH = resolve(userInfo().homedir, '.cache/ms-playwright');

const { chromium } = createRequire(resolvePlaywrightPackage())('playwright');

export function launchBrowser(options = {}) {
  return chromium.launch(options);
}

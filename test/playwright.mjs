// Reuse a local installation or an explicitly selected dependency donor.
import { createRequire } from 'node:module';
import { resolve } from 'node:path';

const packageFile = process.env.PLAYWRIGHT_PACKAGE
  ? resolve(process.env.PLAYWRIGHT_PACKAGE)
  : new URL('../package.json', import.meta.url);
const { chromium } = createRequire(packageFile)('playwright');

export function launchBrowser(options = {}) {
  return chromium.launch({
    ...options,
    ...(process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {}),
  });
}

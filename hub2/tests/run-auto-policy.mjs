// Run: node --experimental-transform-types hub2/tests/run-auto-policy.mjs
import {registerHooks} from 'node:module';import {existsSync} from 'node:fs';import {fileURLToPath} from 'node:url';
registerHooks({resolve(s,c,n){if(s.startsWith('.')&&c.parentURL&&!s.endsWith('.ts')){const u=new URL(s+'.ts',c.parentURL);if(existsSync(fileURLToPath(u)))return n(u.href,c);}return n(s,c);}});
await import('./auto-policy.test.ts');

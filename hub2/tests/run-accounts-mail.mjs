// Run: node --experimental-transform-types hub2/tests/run-accounts-mail.mjs
// Native SQLite semantics only. Cloudflare row costs need the separate workerd gate.
import { registerHooks } from 'node:module';
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
registerHooks({resolve(specifier,context,next){
 if(specifier.startsWith('.')&&context.parentURL&&!specifier.endsWith('.ts')){
  const candidate=new URL(specifier+'.ts',context.parentURL);
  if(existsSync(fileURLToPath(candidate)))return next(candidate.href,context);
 }
 return next(specifier,context);
}});
await import('./accounts-mail.test.ts');

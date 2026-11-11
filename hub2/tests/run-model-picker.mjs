import {registerHooks} from 'node:module';
import {existsSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
registerHooks({resolve(specifier,context,next){
 if(specifier.startsWith('.')&&context.parentURL&&!specifier.endsWith('.ts')){
  const candidate=new URL(specifier+'.ts',context.parentURL);
  if(existsSync(fileURLToPath(candidate)))return next(candidate.href,context);
 }
 return next(specifier,context);
}});
await import('./model-picker.test.ts');
await import('./routing-simulation.test.ts');
await import('./machine-capabilities.test.ts');

await import('./alert-evidence.test.ts');

await import('./workspace-routing.test.ts');

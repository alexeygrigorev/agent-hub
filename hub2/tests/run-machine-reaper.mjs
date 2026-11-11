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
await import('./machine-reaper.test.ts');
await import('./machine-picker-evidence.test.ts');

await import('./native-inventory.test.ts');

import {registerTypeScriptResolveHook, installCompatGlobals} from '../node/compat.ts';
registerTypeScriptResolveHook();
installCompatGlobals();
await import('./schema-readiness.test.ts');

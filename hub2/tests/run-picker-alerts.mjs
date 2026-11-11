import { registerTypeScriptResolveHook } from '../node/compat.ts';
registerTypeScriptResolveHook();
await import('./picker-alerts.test.ts');

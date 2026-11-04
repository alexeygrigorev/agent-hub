// Meter adapter failure must not manufacture a zero-SQL budget result.
import assert from 'node:assert/strict';
import {resolve} from 'node:path';
import {nativeMeter} from './normal-day-meter.mjs';
import {build} from 'esbuild';
import {Miniflare,convertV4MiniflareOptions} from 'miniflare';
import {mkdir,mkdtemp} from 'node:fs/promises';
const root=resolve(import.meta.dirname,'../..');
const storageOnly=nativeMeter(root);
storageOnly.handleStructuredLogs({level:'info',message:'HUB2_METER '+JSON.stringify({type:'storage',method:'setAlarm',succeeded:true})});
assert.equal(storageOnly.valid(),false,'storage events cannot stand in for SQL instrumentation');
const uninstalled=nativeMeter(root);
uninstalled.handleStructuredLogs({level:'info',message:'HUB2_METER '+JSON.stringify({type:'sql',instance:'synthetic',reads:1,writes:0,valid:true})});
assert.equal(uninstalled.valid(),false,'SQL events without the exact installed source adapter are invalid');
await mkdir(resolve(root,'.local/tmp'),{recursive:true});
const meter=nativeMeter(root),persist=await mkdtemp(resolve(root,'.local/tmp/native-meter-proof-'));
const bundle=await build({plugins:[meter.plugin],entryPoints:[resolve(root,'hub2/tests/normal-day-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const classes={CONTROL:'CoreControl',PROJECTS:'CoreProject',MACHINES:'CoreMachine',ACCOUNTS:'CoreAccount',MAILBOXES:'CoreMailbox'},token='synthetic-native-meter-only-000000000000000';
const mf=new Miniflare(convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:meter.log,handleStructuredLogs:meter.handleStructuredLogs,workers:[{name:'native-meter-proof-only',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',durableObjects:Object.fromEntries(Object.entries(classes).map(([binding,className])=>[binding,{className,useSQLite:true}])),bindings:{HUB2_DEV_OPERATOR_TOKEN:token}}]}));
try{
 const base=(await mf.ready).origin,headers={Authorization:'Bearer '+token};
 const response=await fetch(base+'/_fixture/control/global/state',{headers});assert.equal(response.status,200);
 const id=crypto.randomUUID();const barrier=await fetch(base+'/_fixture/barrier',{method:'POST',headers:{...headers,'Content-Type':'application/json'},body:JSON.stringify({id})});assert.equal(barrier.status,200);await meter.barrier(id);
 assert(meter.valid(),'actual source hook + native SQLite cursors required');const report=meter.report();assert(report.sql_hook_installed&&report.sql_events>0);assert(meter.phase('startup').reads>0&&meter.phase('startup').writes>0);
 console.log(JSON.stringify({passed:true,missing_sql_hook_fails_closed:true,storage_only_is_not_zero_sql_proof:true,real_native_sqlite_hook_positive:true}));
}finally{await mf.dispose();}

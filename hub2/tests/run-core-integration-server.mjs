// TEST ONLY: actual five-DO local composition, durable private bootstrap, real provider egress.
// Never imported by a deployment, installer or production host. Run on the Linux test host.
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {mkdir,mkdtemp,readFile,writeFile,chmod,open,realpath} from 'node:fs/promises';
import {constants} from 'node:fs';
import {resolve,join,dirname,basename} from 'node:path';
import {randomBytes,createHash} from 'node:crypto';
const root=resolve(import.meta.dirname,'../..'),parent=resolve(root,'.local/tmp');
if(process.platform==='win32')throw new Error('private_live_fixture_requires_linux');
await mkdir(parent,{recursive:true});
if(process.argv.length>2&&(process.argv[2]!=='--resume'||process.argv.length!==4))throw new Error('usage: --resume PRIVATE_DIRECTORY');
const directory=process.argv[2]?await realpath(process.argv[3]):await mkdtemp(join(parent,'live-five-do-'));
if(dirname(directory)!==await realpath(parent)||!basename(directory).startsWith('live-five-do-'))throw new Error('fixture_directory_outside_private_root');
await chmod(directory,0o700);
const secretPath=join(directory,'fixture-secrets.json');
async function privateRead(path){
 const file=await open(path,constants.O_RDONLY|constants.O_NOFOLLOW);
 try{const info=await file.stat();if(!info.isFile()||info.uid!==process.getuid()||(info.mode&0o777)!==0o600||info.size>4096)throw new Error('fixture_secret_not_private');return JSON.parse(await file.readFile('utf8'));}
 finally{await file.close();}
}
if(!process.argv[2]){
 const file=await open(secretPath,'wx',0o600);
 try{await file.writeFile(JSON.stringify({operator_token:randomBytes(32).toString('base64url'),credential_key:randomBytes(32).toString('hex')})+'\n');await file.sync();}
 finally{await file.close();}
 const dir=await open(directory,'r');try{await dir.sync();}finally{await dir.close();}
}
// Reload the exact durable values before constructing Miniflare or allowing provider actions.
const secrets=await privateRead(secretPath);
if(!/^[A-Za-z0-9_-]{43}$/.test(secrets.operator_token)||!/^[0-9a-f]{64}$/.test(secrets.credential_key))throw new Error('fixture_secret_corrupt');
const token=secrets.operator_token,key=secrets.credential_key;
const bundle=await build({entryPoints:[resolve(root,'hub2/tests/core-integration-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const names={CONTROL:'CoreControl',PROJECTS:'CoreProject',MACHINES:'CoreMachine',ACCOUNTS:'CoreAccount',MAILBOXES:'CoreMailbox'};
const account=process.env.HUB2_LIVE_ACCOUNT||'zai-central';
const options=convertV4MiniflareOptions({resourcePersistencePath:join(directory,'persist'),host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{
 name:'hub2-private-live-fixture',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',
 durableObjects:Object.fromEntries(Object.entries(names).map(([binding,className])=>[binding,{className,useSQLite:true}])),
 bindings:{HUB2_DEV_OPERATOR_TOKEN:token,HUB2_DEFAULT_ACCOUNT_ID:account,HUB2_ENGINE_ACCOUNTS:JSON.stringify({zcode:account}),HUB2_CREDENTIAL_KEY:key,HUB2_FIXTURE_ASSET_HTML:await readFile(resolve(root,'hub2/public/index.html'),'utf8')},
}]});
const mf=new Miniflare(options);
try{
 const server=(await mf.ready).origin,config=join(directory,'operator.json');
 await writeFile(config,JSON.stringify({server,token})+'\n',{mode:0o600});
 const ready={server,operator_config:config,private_directory:directory,real_provider_fetch:true,models_started:false,secrets_durable_and_reloaded:true,resumed:!!process.argv[2],bundle_sha256:createHash('sha256').update(bundle.outputFiles[0].text).digest('hex')};
 await writeFile(join(directory,'ready.json'),JSON.stringify(ready)+'\n',{mode:0o600});
 console.log(JSON.stringify(ready));
 await new Promise(resolve=>{process.once('SIGINT',resolve);process.once('SIGTERM',resolve);});
}finally{await mf.dispose();}

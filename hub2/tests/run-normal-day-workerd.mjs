// Whole-day acceptance requires complete canonical coverage. Partial rows never pass.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {WebSocket} from 'ws';
import {nativeMeter}from'./normal-day-meter.mjs';
import {resolve,relative,sep} from 'node:path';
import {readFile,writeFile,mkdir,mkdtemp,readdir} from 'node:fs/promises';
import {createHash,webcrypto} from 'node:crypto';
const root=resolve(import.meta.dirname,'../..');
const canonical={starts:70,user_intakes:20,scheduled_occurrences:49,messages:100,claim_cycles:50,host_transitions:16,accounts:6,root_writer_api_calls:490,operator_api_calls:200,retry_episodes:20,quota_stale_batches:48,discovery_batches:4};
const budgetMetrics=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
// Measurement envelope for the canonical Workerd workload, distinct from the
// deliberately broad semantic-test envelope. Route selection reads one policy,
// one directory page, four machine states and six account states; the later
// saga effect has its own reservation. Sixteen calls leave bounded headroom for
// that fan-out. The DO duration is 16 instances x 256MiB x 500ms. This remains
// a test envelope, not production sizing.
const normalDayUnits=Object.fromEntries(budgetMetrics.map(k=>[k,0]));
Object.assign(normalDayUnits,{worker_requests:1,worker_cpu_ms:500,do_requests:19,do_ws_messages:1,do_gb_ms:2048,background_invocations:1,sqlite_rows_read:128,sqlite_rows_written:16,sqlite_byte_days:65536});
const publicRequestUnits={...normalDayUnits,worker_cpu_ms:250,do_requests:11,do_gb_ms:512,sqlite_rows_read:64,sqlite_rows_written:8};
const routePlan=(calls,wall,cpu,gbms,reads,writes,label)=>({units:{...normalDayUnits,worker_cpu_ms:cpu,do_requests:calls+3,do_gb_ms:gbms,sqlite_rows_read:reads,sqlite_rows_written:writes},max_do_calls:calls,max_wall_ms:wall,provenance:`Measured canonical ${label} upper bound; test-only, not production sizing`});
const normalDayBudget=process.env.HUB2_COST_PROFILE?await readFile(process.env.HUB2_COST_PROFILE,'utf8'):JSON.stringify({version:1,normal:{units:normalDayUnits,max_do_calls:16,max_wall_ms:500,by_kind:{
 public_request:{units:publicRequestUnits,max_do_calls:8,max_wall_ms:250,provenance:'Unclassified public fallback; test-only, not production sizing'},
 public_read:routePlan(3,250,200,192,32,5,'read'),public_write:routePlan(4,300,250,320,64,12,'write'),
 fleet_read:routePlan(6,250,250,384,128,5,'bounded fleet read'),project_request:routePlan(4,250,250,256,64,12,'project operation'),
 claim_request:routePlan(3,250,250,192,32,12,'claim mutation'),message_request:routePlan(4,250,250,256,32,12,'mailbox operation'),
 account_request:routePlan(2,250,250,128,32,12,'account operation'),
 placement_request:{units:normalDayUnits,max_do_calls:16,max_wall_ms:500,provenance:'Canonical high-fanout placement; test-only, not production sizing'},
 connection_request:{units:normalDayUnits,max_do_calls:16,max_wall_ms:500,provenance:'Canonical authenticated WebSocket handshake; test-only, not production sizing'},
 provider_refresh:{units:normalDayUnits,max_do_calls:16,max_wall_ms:500,provenance:'Canonical provider collector call; test-only, not production sizing'}
},provenance:'Per-kind canonical Workerd workload bounds; test-only, not production sizing'},safety:{units:normalDayUnits,max_do_calls:16,max_wall_ms:500,provenance:'Canonical Workerd safety envelope; test-only, not production sizing'}});
const liveSourceDir=resolve(root,'hub2/src'),sourceDir=process.env.HUB2_COST_SOURCE_DIR?resolve(process.env.HUB2_COST_SOURCE_DIR):liveSourceDir;
const sourceFiles=(await readdir(sourceDir)).filter(n=>n.endsWith('.ts')).map(n=>n.slice(0,-3)).sort();
const hashes=async()=>Object.fromEntries(await Promise.all(sourceFiles.map(async n=>[n,createHash('sha256').update(await readFile(resolve(sourceDir,n+'.ts'))).digest('hex')])));
const sourceBefore=await hashes(),bootstrap='synthetic-normal-day-bootstrap-000000000000000',project='normal-day/fixture';let token=bootstrap;
const names={control:'CoreControl',project:'CoreProject',machine:'CoreMachine',account:'CoreAccount',mailbox:'CoreMailbox',budget:'CoreBudget'};
await mkdir(resolve(root,'.local/tmp'),{recursive:true});const persist=await mkdtemp(resolve(root,'.local/tmp/normal-day-'));
const nativeTelemetry=nativeMeter(root,sourceDir);
const sourceSnapshotPlugin={name:'test-only-cost-source-snapshot',setup(builder){builder.onResolve({filter:/.*/},args=>{if(!process.env.HUB2_COST_SOURCE_DIR||!args.path.startsWith('.'))return;const candidate=resolve(args.resolveDir,args.path);if(!candidate.startsWith(liveSourceDir+sep))return;const target=resolve(sourceDir,relative(liveSourceDir,candidate));return{path:target.endsWith('.ts')?target:target+'.ts'};});}};
const cacheVariant=process.env.HUB2_CACHE_VARIANT||'on';if(!['on','off'].includes(cacheVariant))throw new Error('invalid_cache_variant');
const cachePlugin={name:'test-only-read-cache-off',setup(builder){builder.onResolve({filter:/^\.\/read-cache$/},args=>args.importer===resolve(root,'hub2/src/db.ts')?{path:resolve(root,'hub2/tests/normal-day-cache-off.ts')}:undefined);}};
const bundle=await build({plugins:[sourceSnapshotPlugin,nativeTelemetry.plugin,...(cacheVariant==='off'?[cachePlugin]:[])],entryPoints:[resolve(root,'hub2/tests/normal-day-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers','node:async_hooks']});
const alertSinkScript="const ids=new Set();export default{async fetch(r){if(new URL(r.url).pathname==='/_stats')return Response.json({ids:[...ids]});if(r.headers.get('Authorization')!=='Bearer synthetic-normal-alert')return new Response(null,{status:403});const body=await r.json(),id=r.headers.get('Idempotency-Key');if(!id||body.message_id!==id)return new Response(null,{status:400});const duplicate=ids.has(id);ids.add(id);return Response.json({accepted:true,duplicate,id},{status:202});}};";
const configuredCronInvocations=Number(process.env.HUB2_BUDGET_CRON_PER_DAY||96);if(!Number.isSafeInteger(configuredCronInvocations)||configuredCronInvocations<1||configuredCronInvocations>1440)throw new Error('invalid_fixture_cron_cadence');let cronDeliveryIds=[];
const options=convertV4MiniflareOptions({resourcePersistencePath:persist,host:'127.0.0.1',port:0,log:nativeTelemetry.log,handleStructuredLogs:nativeTelemetry.handleStructuredLogs,workers:[{name:'hub2-normal-day-local',outboundService:'normal-day-alert-sink',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_als'],durableObjects:Object.fromEntries(Object.entries(names).map(([k,className])=>[({control:'CONTROL',project:'PROJECTS',machine:'MACHINES',account:'ACCOUNTS',mailbox:'MAILBOXES',budget:'BUDGET'})[k],{className,useSQLite:true}])),bindings:{HUB_ALERT_WEBHOOK:'https://normal-day-alert.fixture',HUB_ALERT_WEBHOOK_TOKEN:'synthetic-normal-alert',HUB2_DEV_OPERATOR_TOKEN:bootstrap,HUB2_BUDGET_ENVELOPES:normalDayBudget,AUTH_BASE_URL:'https://oidc.fixture',AUTH_CLIENT_ID:'normal-day-auth-client',AUTH_CALLBACK_URL:'https://hub.fixture/auth/callback',AUTH_ISSUER:'https://oidc.fixture',AUTH_JWKS_URL:'https://oidc.fixture/jwks',HUB_ADMINS:'normal@example.test',HUB2_DEFAULT_ACCOUNT_ID:'normal-account-0',HUB2_ENGINE_ACCOUNTS:JSON.stringify({opencode:'normal-account-0'}),HUB2_CREDENTIAL_KEY:'11'.repeat(32),HUB2_FIXTURE_ASSET_HTML:await readFile(resolve(root,'hub2/public/index.html'),'utf8')}},{name:'normal-day-alert-sink',script:alertSinkScript,modules:true,compatibilityDate:'2024-04-05'}]});
const bundleSha=createHash('sha256').update(bundle.outputFiles[0].text).digest('hex');await mkdir(resolve(root,'.local'),{recursive:true});await writeFile(resolve(root,'.local/cache-bundle-'+cacheVariant+'.mjs'),bundle.outputFiles[0].text,{mode:0o600});
let mf=new Miniflare(options),base,operatorFamily,refreshToken,accessExpires,writerToken,writerFamily,virtualOffset=0;const role_ledger={operator:0,root_writer:0};
const objects=[['control','global'],['project',project],...Array.from({length:4},(_,i)=>['machine','normal-machine-'+i]),...Array.from({length:6},(_,i)=>['account','normal-account-'+i]),['mailbox','global'],['budget','global']];
const drivenAlarms={};
const request_ledger={},flows=[],coverage={},missing=[],errors=[],hosts=[],intakes=[];let historicalIntake;
async function api(path,method='GET',body,auth=token){const actor=auth===bootstrap?'fixture':!auth?'public':hosts.some(h=>h.token===auth)?'machine':auth===token?'operator':auth===writerToken?'root_writer':'bounded-child';const key=actor+' '+method+' '+path.split('?')[0].replace(/[0-9a-f]{8}-[0-9a-f-]{27,}/gi,':id');request_ledger[key]=(request_ledger[key]||0)+1;const r=await fetch(base+path,{method,headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+auth}:{})},...(body===undefined?{}:{body:JSON.stringify(body)})});const text=await r.text();let data;try{data=JSON.parse(text);}catch{data={error:'non_json',fixture_error:text.slice(0,1000)};}return{status:r.status,data};}
async function roleApi(role,path,method='GET',body){role_ledger[role]++;if(role==='root_writer')assert(writerToken&&writerToken!==token,'root/writer must use independently issued client credential');return api(path,method,body,role==='root_writer'?writerToken:token);}
async function raw(path,init={}){const key='issuer '+(init.method||'GET')+' '+path.split('?')[0];request_ledger[key]=(request_ledger[key]||0)+1;return fetch(base+path,init);}
function expect(r,status,label){assert.equal(r.status,status,`${label}: ${r.status} ${JSON.stringify(r.data)}`);return r.data;}
const fixture=(kind,id,action='state',body)=>api(`/_fixture/${kind}/${encodeURIComponent(id)}/${action}`,body===undefined?'GET':'POST',body,bootstrap);
async function snapshot(){const out={};for(const [kind,id]of objects){const d=expect(await fixture(kind,id),200,'metrics');assert.equal(d.counts.measurement_valid,true);out[kind+':'+id]=d;}return out;}
function difference(a,b){const by_object={};let reads=0,writes=0,alarms=0;for(const k of Object.keys(b)){const x=a[k]?.counts||{},y=b[k].counts;const d={reads:y.rows_read-(x.rows_read||0),writes:y.rows_written-(x.rows_written||0),alarm_operations:y.alarm_operations-(x.alarm_operations||0)};if(d.reads<0||d.writes<0){by_object[k]={snapshot_counter_reset:true,before:x,after:y};continue;}by_object[k]=d;reads+=d.reads;writes+=d.writes;alarms+=d.alarm_operations;}return{reads,writes,combined_rows:reads+writes,alarm_operations:alarms,by_object};}
async function querySnapshot(){const out={};for(const[kind,id]of objects)out[kind+':'+id]=expect(await fixture(kind,id,'profile'),200,'native profile');return out;}
function queryDifference(a,b){const out={};for(const owner of Object.keys(b)){const profile={};for(const[q,v]of Object.entries(b[owner].profile)){const old=a[owner]?.profile[q]||{calls:0,reads:0,writes:0},d={calls:v.calls-old.calls,reads:v.reads-old.reads,writes:v.writes-old.writes};if(d.calls)profile[q]=d;}out[owner]=profile;}return out;}
function storageDifference(a,b){const out={};for(const owner of Object.keys(b)){out[owner]={};for(const method of ['getAlarm','setAlarm','deleteAlarm']){const x=a[owner]?.storage?.[method]||{succeeded:0,failed:0},y=b[owner].storage?.[method]||{succeeded:0,failed:0};out[owner][method]={succeeded:y.succeeded-x.succeeded-(method==='getAlarm'?1:0),failed:y.failed-x.failed};}}return out;}
async function telemetryBarrier(){const id=crypto.randomUUID();expect(await api('/_fixture/barrier','POST',{id},bootstrap),200,'meter barrier');await nativeTelemetry.barrier(id);}
function nativeCounts(data){return{reads:data.reads,writes:data.writes,combined_rows:data.reads+data.writes,sql_calls:data.sql_calls,measurement_valid:data.invalid===0,alarm_operations:Object.values(data.storage).reduce((n,c)=>n+c.succeeded+c.failed,0),classes:data.classes,native_objects:data.objects,storage:data.storage,proven_alarm_rows_written:data.storage.setAlarm.succeeded};}
async function driveDueAlarms(){
 // Test-only clock driver. All business work still executes the actual source
 // alarm handler, with its actual namespace calls and native cursor counts.
 for(let turn=0;turn<32;turn++){
  let invoked=0;
  for(const[kind,id]of objects){
   const state=expect(await fixture(kind,id),200,'clock-driver durable alarm');
   if(state.alarm!==null&&state.alarm<=Date.now()+virtualOffset){
    expect(await fixture(kind,id,'alarm',{}),200,'clock-driver due production handler');
    drivenAlarms[kind]=(drivenAlarms[kind]||0)+1;invoked++;
   }
  }
  if(!invoked)return;
 }
 throw new Error('due_alarm_driver_exceeded_bounded_rounds');
}
async function measure(name,fn){
 console.error('normal-day phase start: '+name);
 nativeTelemetry.setPhase('observer_before:'+name);const before=await snapshot(),queriesBefore=await querySnapshot();await telemetryBarrier();
 nativeTelemetry.setPhase(name);try{await fn();if(name!=='completed_steady_idle')await driveDueAlarms();}catch(e){errors.push({flow:name,error:String(e.stack)});}await telemetryBarrier();const native=nativeTelemetry.phase(name);
 nativeTelemetry.setPhase('observer_after:'+name);const after=await snapshot(),queriesAfter=await querySnapshot();await telemetryBarrier();
 console.error('normal-day phase finished: '+name+' '+(native.reads+native.writes)+' SQL rows');
 flows.push({name,...nativeCounts(native),snapshot_diagnostic:difference(before,after),queries:queryDifference(queriesBefore,queriesAfter),observer_costs:{before:nativeCounts(nativeTelemetry.phase('observer_before:'+name)),after:nativeCounts(nativeTelemetry.phase('observer_after:'+name))}});
}
async function socket(auth){request_ledger['machine WS /api/connect']=(request_ledger['machine WS /api/connect']||0)+1;const frames=[],ws=new WebSocket(base.replace(/^http/,'ws')+'/api/connect',{headers:{Authorization:'Bearer '+auth}});ws.on('message',m=>{try{frames.push(JSON.parse(m.toString()));}catch{}});await new Promise((ok,no)=>{ws.once('open',ok);ws.once('error',no);});return{ws,frames,async wait(predicate){const end=Date.now()+5000;while(Date.now()<end){const frame=frames.find(predicate);if(frame)return frame;await new Promise(r=>setTimeout(r,5));}throw new Error('missing real WebSocket frame');}};}
async function lifecycle(task,index,beforeFinish,exitCode=0){
 const connections=await Promise.all(hosts.map(h=>socket(h.token)));try{
  const fault=process.env.HUB2_NORMAL_DAY_SLICE!=='twenty'&&index<19?(index<7?'reserve':index<13?'activate':index<16?'started':'finish'):null;
  if(fault==='reserve'||fault==='activate')expect(await fixture('project',project,'outbound-fault',{paths:['/_internal/'+fault]}),200,'actual selected-machine response loss');
  const path=`/api/intakes/${encodeURIComponent(task.id)}/start?project=${encodeURIComponent(project)}`,body={workspace:'/synthetic/normal-day',interactive:false,timeout_seconds:60,key:'start-user-'+index};
  const started=expect(await api(path,'POST',body),fault==='reserve'||fault==='activate'?202:201,'automatic start').session;
  const selected=hosts.findIndex(h=>h.id===started.machine_id);assert(selected>=0,'actual policy must select an enrolled host');const host=hosts[selected],connection=connections[selected];
  const replay=expect(await api(path,'POST',body),200,'automatic lost-response replay while chosen host occupied');assert.equal(replay.session.id,started.id);assert.equal(replay.session.machine_id,host.id);
  if(fault==='reserve'||fault==='activate')expect(await fixture('project',project,'due-saga',{operation_id:started.operation_id}),200,'persisted dispatch backoff retry');
  const offer=(await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===started.id)).offer;
  if(index===0&&process.env.HUB2_NORMAL_DAY_SLICE!=='twenty'){const accountPath='/api/accounts/'+encodeURIComponent(offer.account_id)+'/api/account';expect(await api(accountPath,'PATCH',{provider:'codex',enabled:false,capacity:1,min_remaining:10}),200,'selected quota/account changed after successful start');try{const original=expect(await api(path,'POST',body),200,'auto replay before fresh placement despite unavailable selected account');assert.equal(original.session.id,started.id);assert.equal(original.session.operation_id,started.operation_id);if(started.epoch!==null)assert.equal(original.session.epoch,started.epoch);else assert(Number.isSafeInteger(original.session.epoch)&&original.session.epoch>=1);}finally{expect(await api(accountPath,'PATCH',{provider:'codex',enabled:true,capacity:1,min_remaining:10}),200,'restore selected account before admission');}coverage.auto_start_replay_fenced=true;}
  assert.equal(offer.launch.engine,'opencode');assert.equal(offer.launch.model,'synthetic-test-model');
  await finishOffered(task,index,host,connection,started,offer,beforeFinish,exitCode,fault);if(fault)coverage.retry_episodes=(coverage.retry_episodes||0)+1;
 }finally{for(const connection of connections)connection.ws.close();}
}
async function finishOffered(task,index,host,connection,started,offer,beforeFinish,exitCode=0,fault=null){
 connection.ws.send(JSON.stringify({type:'offer_ack',session_id:started.id,epoch:offer.epoch}));await connection.wait(v=>v.type==='ack'&&v.session_id===started.id);
 const admission=expect(await api(`/api/sessions/${started.id}/admission`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch},host.token),200,'admission');assert(Number.isSafeInteger(admission.runtime_deadline),'synthetic admission must expose its runtime deadline');
 const worker=expect(await api(`/api/sessions/${started.id}/credentials`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch},host.token),200,'bounded child bootstrap');expect(await api('/api/intakes/'+task.id,'GET',undefined,worker.token),200,'child first assigned-task read');
 const native='11111111-2222-4333-8444-'+String(index).padStart(12,'0');
 if(fault==='started')expect(await fixture('project',project,'fault',{paths:['/_internal/started']}),200,'lost started response');
 expect(await api(`/api/sessions/${started.id}/started`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch,native_id:native},host.token),fault==='started'?202:201,'native started');
 if(fault==='started')expect(await fixture('machine',host.id,'due-outbox',{id:'started:'+offer.operation_id}),200,'durable started retry');
 if(beforeFinish)await beforeFinish({connection,session:started,offer,hostToken:host.token});
 assert(Number.isSafeInteger(admission.observed_at),'synthetic admission must expose its observed timestamp');
 const evidence={source:'aplexer_retirement',fixture:true,native_identity:{id:native,tag:'task-hub2-'+started.id,workspace:offer.launch.workspace},containment_empty:true,native_deadline:{deadline_ms:admission.runtime_deadline,exceeded:false},native_exit:{code:exitCode,exited_at_ms:admission.observed_at+1}};
 if(fault==='finish')expect(await fixture('project',project,'fault',{paths:['/_internal/finish']}),200,'lost finish response');
 expect(await api(`/api/sessions/${started.id}/finish`,'POST',{operation_id:offer.operation_id,epoch:offer.epoch,native_id:native,exit_code:exitCode,native_exit_confirmed:true,evidence},host.token),fault==='finish'?202:201,'native finish');
 if(fault==='finish')expect(await fixture('machine',host.id,'due-outbox',{id:'finish:'+offer.operation_id}),200,'durable finish retry');
 assert.equal(expect(await api(`/api/intakes/${task.id}?project=${encodeURIComponent(project)}`),200,'terminal').intake.status,exitCode===0?'completed-awaiting-review':'failed');

}
try{
 base=(await mf.ready).origin;
 // Snapshot activates all configured objects; cold schema/constructor work is retained.
 nativeTelemetry.setPhase('cold_first_creation');const cold=await snapshot();await telemetryBarrier();flows.push({name:'cold_first_creation',...nativeCounts(nativeTelemetry.phase('cold_first_creation')),observer_induced:true,snapshot_diagnostic:difference({},cold)});
 await measure('actual_oidc_issuer_and_device_bearer',async()=>{
  const response=await raw('/auth/login',{redirect:'manual'});assert.equal(response.status,302);const location=new URL(response.headers.get('Location')),nonce=location.searchParams.get('nonce'),state=location.searchParams.get('state'),browserCookie=response.headers.getSetCookie()[0].split(';')[0];
  const key=await webcrypto.subtle.generateKey({name:'RSASSA-PKCS1-v1_5',modulusLength:2048,publicExponent:new Uint8Array([1,0,1]),hash:'SHA-256'},true,['sign','verify']),jwk={...await webcrypto.subtle.exportKey('jwk',key.publicKey),kid:'normal-key',alg:'RS256',use:'sig'};
  const header=Buffer.from(JSON.stringify({alg:'RS256',kid:'normal-key'})).toString('base64url'),payload=Buffer.from(JSON.stringify({sub:'normal-day-user',email:'normal@example.test',email_verified:true,iss:'https://oidc.fixture',aud:'normal-day-auth-client',nonce,token_use:'id',exp:Math.floor(Date.now()/1000)+3600})).toString('base64url');const signature=Buffer.from(await webcrypto.subtle.sign('RSASSA-PKCS1-v1_5',key.privateKey,Buffer.from(header+'.'+payload))).toString('base64url');
  expect(await api('/_fixture/provider','POST',{jwks:{keys:[jwk]},challenge:location.searchParams.get('code_challenge'),token:header+'.'+payload+'.'+signature},bootstrap),200,'provider fixture');
  const callback=await raw('/auth/callback?code=fixture-code&state='+encodeURIComponent(state),{redirect:'manual',headers:{Cookie:browserCookie}});assert.equal(callback.status,303);const sessionCookie=callback.headers.getSetCookie().filter(c=>c.startsWith('hub2_session='))[0].split(';')[0];
  const current=await(await raw('/auth/session',{headers:{Cookie:sessionCookie}})).json();
  const device=expect(await api('/api/device/code','POST',{client_id:'normal-day-cli'},null),200,'actual device code');
  const approved=await raw('/api/device/approve',{method:'POST',headers:{Cookie:sessionCookie,'Content-Type':'application/json','x-csrf-token':current.csrf_token},body:JSON.stringify({user_code:device.user_code})});assert.equal(approved.status,200);
  const issued=expect(await api('/api/device/token','POST',{device_code:device.device_code},null),200,'actual CLI credential');token=issued.access_token;operatorFamily=issued.family_id;refreshToken=issued.refresh_token;accessExpires=Date.now()+issued.expires_in*1000;assert(token);coverage.production_operator_issuer=true;
  const writerDevice=expect(await api('/api/device/code','POST',{client_id:'normal-day-rootwriter'},null),200,'separate root/writer device code');const writerApproval=await raw('/api/device/approve',{method:'POST',headers:{Cookie:sessionCookie,'Content-Type':'application/json','x-csrf-token':current.csrf_token},body:JSON.stringify({user_code:writerDevice.user_code})});assert.equal(writerApproval.status,200);const writer=expect(await api('/api/device/token','POST',{device_code:writerDevice.device_code},null),200,'actual separate root/writer credential');writerToken=writer.access_token;writerFamily=writer.family_id;assert(writerToken!==token&&writerFamily!==operatorFamily);coverage.role_separated_credentials=true;
 });
 // Pin all measured workload operations to one synthetic UTC day after the
 // real issuer/device bootstrap, so JWT validation itself uses wall-clock time.
 const wallNow=Date.now(),wallDate=new Date(wallNow),simulatedDayStart=Date.UTC(wallDate.getUTCFullYear(),wallDate.getUTCMonth(),wallDate.getUTCDate());
 virtualOffset=simulatedDayStart-wallNow;accessExpires+=virtualOffset;
 expect(await fixture('control','global','clock',{offset:virtualOffset}),200,'anchor the native workload replay to UTC day start');
 await measure('bootstrap_registry_enrollment_and_six_accounts',async()=>{
  expect(await api('/api/repos/'+encodeURIComponent(project),'PATCH',{canonical_url:'https://github.com/synthetic/normal-day',purpose:'Canonical normal-day acceptance fixture',status:'active',aliases:[],domains:[]}),200,'project');
  for(let i=0;i<4;i++){const id='normal-machine-'+i,p=expect(await api('/api/enrollments/pair','POST',{machine_id:id,alias:id}),201,'pair');const auth=expect(await api('/api/enrollments/complete','POST',{code:p.pairing_code},null),201,'enroll').token;hosts.push({id,token:auth});expect(await api('/api/heartbeat','POST',{os:'synthetic',arch:'fixture',labels:[],engines:['opencode','shell'],capabilities:['headless','interactive']},auth),200,'host online');}
  for(let i=0;i<6;i++){const prefix=`/api/accounts/normal-account-${i}/api/account`;expect(await api(prefix,'PATCH',{provider:'codex',enabled:true,capacity:1,min_remaining:10}),200,'account');const access='fixture.'+Buffer.from(JSON.stringify({exp:Math.floor(Date.now()/1000)+864000})).toString('base64url')+'.signature';expect(await api(prefix+'/credentials','PUT',{expected_generation:0,files:{'.codex/auth.json':JSON.stringify({tokens:{access_token:access,refresh_token:'synthetic-native-fixture',account_id:'fixture-id'},last_refresh:new Date().toISOString()})}}),200,'encrypted synthetic provider bundle');expect(await api(prefix+'/discovery','POST',{}),200,'actual provider collector over synthetic HTTP');expect(await api(prefix+'/hosts','PUT',{hosts:hosts.map(h=>h.id)}),200,'approve actual fixture hosts');}expect(await api('/api/model-policy','PUT',{expected_version:0,candidates:Array.from({length:6},(_,i)=>({id:'normal-model-'+i,engine:'opencode',model:'synthetic-test-model',account_id:'normal-account-'+i,weight:i+1,capabilities:[],enabled:true}))}),200,'normal model policy');coverage.accounts=6;
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('previous_day_failed_intake_setup',async()=>{
  expect(await fixture('control','global','clock',{offset:-86400000}),200,'previous-day actual API fixture clock');
  try{
   for(let i=0;i<6;i++)expect(await api(`/api/accounts/normal-account-${i}/api/account/discovery`,'POST',{}),200,'historical actual collector');
   historicalIntake=expect(await api('/api/intakes?project='+encodeURIComponent(project),'POST',{title:'Retained failed task from previous day',text:'Synthetic actual API history, not raw SQL seed.',acceptance_criteria:['Retry preserves prior failed attempt provenance.'],key:'previous-day-task'}),201,'historical intake').intake;
   await lifecycle(historicalIntake,9000,undefined,1);
  }finally{expect(await fixture('control','global','clock',{offset:virtualOffset}),200,'restore anchored current normal day');}
 });
 await measure('twenty_user_intakes',async()=>{for(let i=0;i<20;i++){const t=expect(await api('/api/intakes?project='+encodeURIComponent(project),'POST',{title:'User fixture '+i,text:'Synthetic normal-day workload, no native process launched.',acceptance_criteria:['Result remains awaiting independent review.'],key:'user-'+i}),201,'create').intake;intakes.push(t);}coverage.user_intakes=intakes.length;});
 coverage.starts=0;
 await measure('twenty_happy_user_lifecycles',async()=>{for(let i=0;i<intakes.length;i++){await lifecycle(intakes[i],i,process.env.HUB2_NORMAL_DAY_SLICE==='twenty'||i!==0?undefined:async context=>{const{fullMessages}=await import('./normal-day-full-messages.mjs');coverage.full_message_proof=await fullMessages({api,expect,roleApi,...context,family:writerFamily,client:'normal-day-rootwriter'});coverage.messages=100;});coverage.starts++;}});
 if(historicalIntake)await measure('one_retained_failed_task_retry',async()=>{await lifecycle(historicalIntake,9001);coverage.starts++;coverage.retry_episodes=(coverage.retry_episodes||0)+1;});
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('fifty_claim_create_renew_release_cycles',async()=>{
  for(let i=0;i<50;i++){
   const claim=expect(await roleApi('operator','/api/claims?project='+encodeURIComponent(project),'POST',{key:'normal-claim-'+i,intake_id:intakes[i%intakes.length].id,paths:['src/normal-fixture-'+i+'.ts'],mode:'edit',ttl_ms:300000}),201,'claim create').claim;
   const path='/api/claims/'+encodeURIComponent(claim.id);
   expect(await roleApi('root_writer',path+'/renew?project='+encodeURIComponent(project),'POST',{ttl_ms:300000}),200,'claim renewal');
   expect(await roleApi('operator',path+'/release?project='+encodeURIComponent(project),'POST',{}),200,'claim release');
  }coverage.claim_cycles=50;
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('remaining_root_writer_and_operator_api_traffic',async()=>{
  const rootRemainder=canonical.root_writer_api_calls-role_ledger.root_writer,operatorRemainder=canonical.operator_api_calls-role_ledger.operator;
  assert(rootRemainder>=0&&operatorRemainder>=0,'canonical role traffic must not be double-counted');
  for(let i=0;i<rootRemainder;i++)expect(await roleApi('root_writer','/api/intakes/'+encodeURIComponent(intakes[i%intakes.length].id)+'?project='+encodeURIComponent(project)),200,'root/writer intake detail traffic');
  for(let i=0;i<operatorRemainder;i++)expect(await roleApi('operator','/api/fleet'),200,'operator dashboard fleet traffic');
  assert.equal(role_ledger.root_writer,canonical.root_writer_api_calls);assert.equal(role_ledger.operator,canonical.operator_api_calls);
  coverage.root_writer_api_calls=role_ledger.root_writer;coverage.operator_api_calls=role_ledger.operator;
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('four_transitions_per_four_actual_host_sockets',async()=>{
  let transitions=0;
  async function observed(host,connected){for(let i=0;i<20;i++){const page=expect(await api('/api/fleet'),200,'actual fleet presence');if(page.items.find(m=>m.id===host.id)?.connected===connected)return;await new Promise(r=>setTimeout(r,10));}throw new Error('actual host presence did not transition');}
  for(const host of hosts)for(let repeat=0;repeat<2;repeat++){const c=await socket(host.token);await observed(host,true);transitions++;await new Promise(resolve=>{c.ws.once('close',resolve);c.ws.close();});await observed(host,false);transitions++;}
  coverage.host_transitions=transitions;
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('forty_nine_scheduled_starts_and_provider_day',async()=>{
  async function advance(target){const offset=Math.round(target-Date.now());assert(Math.abs(offset)<=86400000,'synthetic UTC replay clock must remain bounded');virtualOffset=offset;expect(await fixture('control','global','clock',{offset}),200,'actual day clock');if(target>=accessExpires-60000){const issued=expect(await api('/api/token/refresh','POST',{refresh_token:refreshToken},null),200,'actual expiring day credential refresh');token=issued.access_token;refreshToken=issued.refresh_token;accessExpires=target+issued.expires_in*1000;}}
  for(let i=0;i<6;i++)expect(await api(`/api/accounts/normal-account-${i}/api/account/discovery`,'PUT',{enabled:true,interval_ms:21600000}),200,'enable actual six-hour discovery');
  await advance(Date.now()+virtualOffset+1000);for(let i=0;i<6;i++)expect(await fixture('account','normal-account-'+i,'alarm',{}),200,'initial enabled discovery alarm');
  const launch={workspace:'/synthetic/normal-day',interactive:false,timeout_seconds:60};
  const rootSchedule=expect(await api('/api/schedules?project='+encodeURIComponent(project),'POST',{key:'normal-root-half-hour',intake_id:intakes[0].id,kind:'root',interval_ms:1800000,timezone:'UTC',overlap:'skip',payload:{start:launch}}),201,'root schedule').schedule;
  const writer=expect(await api('/api/schedules?project='+encodeURIComponent(project),'POST',{key:'normal-writer-daily',intake_id:intakes[0].id,kind:'writer',interval_ms:86400000,timezone:'UTC',overlap:'skip',payload:{start:launch}}),201,'writer schedule').schedule;
  const scheduleDayStart=Date.now()+virtualOffset+1000,writerDue=scheduleDayStart+47*1800000;
  expect(await fixture('project',project,'align-schedules',{root_id:rootSchedule.id,root_due:scheduleDayStart,writer_id:writer.id,writer_due:writerDue}),200,'align existing recurring deadlines to this measured UTC day');
  let occurrences=0,discoveryBatches=0;
  for(let tick=1;tick<=48;tick++){
   const rootState=expect(await api('/api/schedules/'+encodeURIComponent(rootSchedule.id)+'?project='+encodeURIComponent(project)),200,'actual next root deadline').schedule,due=rootState.next_due,target=due+50;await advance(target);let discovered=false;
   for(let i=0;i<6;i++){const prefix=`/api/accounts/normal-account-${i}/api/account`;const state=expect(await api(prefix+'/discovery'),200,'scheduled discovery deadline');if(state.discovery.next_at<=target){expect(await fixture('account','normal-account-'+i,'alarm',{}),200,'due actual provider discovery');discovered=true;}const view=expect(await api(prefix+'/quota'),200,'stale authorized view triggers genuine collector');assert(view.fresh===true||view.quota?.valid_until>target,'actual quota must refresh');assert(String(view.quota?.source||'').startsWith('provider:codex:generation:'),'quota requires actual collector generation provenance');}
   if(discovered)discoveryBatches++;coverage.quota_stale_batches=tick;
   const sockets=await Promise.all(hosts.map(h=>socket(h.token)));
   try{
    expect(await fixture('project',project,'alarm',{}),200,'actual scheduler alarm');
    for(const schedule of[rootSchedule,...(tick===48?[writer]:[])]){
     const records=expect(await api('/api/schedules/'+encodeURIComponent(schedule.id)+'/occurrences?project='+encodeURIComponent(project)+'&after='+(schedule===writer?0:due-1)),200,'durable schedule provenance').items;
     const record=records.find(r=>r.due_at<=target&&r.state==='completed');assert(record,'actual completed scheduler occurrence required: '+JSON.stringify(records));const result=JSON.parse(record.result_json),task=expect(await api('/api/intakes/'+encodeURIComponent(result.intake_id)+'?project='+encodeURIComponent(project)),200,'scheduled child intake').intake;
     assert.equal(task.schedule_id,schedule.id);assert.equal(task.occurrence_id,record.id);assert.equal(task.parent_intake_id,intakes[0].id);assert.equal(task.provenance.type,'schedule');
     let chosen=-1;for(let i=0;i<sockets.length;i++)if(sockets[i].frames.some(v=>v.type==='session.offer'&&v.offer.session_id===result.session_id))chosen=i;if(chosen<0){await new Promise(r=>setTimeout(r,20));for(let i=0;i<sockets.length;i++)if(sockets[i].frames.some(v=>v.type==='session.offer'&&v.offer.session_id===result.session_id))chosen=i;}assert(chosen>=0,'automatic scheduled offer must reach actual selected host');const host=hosts[chosen],connection=sockets[chosen],offered=(await connection.wait(v=>v.type==='session.offer'&&v.offer.session_id===result.session_id)).offer;
     await finishOffered(task,10000+occurrences,host,connection,{id:result.session_id},offered);occurrences++;coverage.starts++;
    }
   }finally{for(const c of sockets)c.ws.close();}
  }
  coverage.scheduled_occurrences=occurrences;coverage.discovery_batches=discoveryBatches;assert.equal(occurrences,49);assert.equal(discoveryBatches,4);
  expect(await api('/api/schedules/'+encodeURIComponent(rootSchedule.id)+'/pause?project='+encodeURIComponent(project),'POST',{}),200,'stop configured synthetic root job after simulated day');expect(await api('/api/schedules/'+encodeURIComponent(writer.id)+'/pause?project='+encodeURIComponent(project),'POST',{}),200,'stop configured synthetic writer job');
  for(let i=0;i<6;i++)expect(await api(`/api/accounts/normal-account-${i}/api/account/discovery`,'PUT',{enabled:false,interval_ms:21600000}),200,'stop synthetic discovery after measured day');
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('retained_archive_search_and_history',async()=>{
  const archived=[];
  for(let i=0;i<5;i++){
   const path='/api/intakes/'+intakes[i].id+'?project='+encodeURIComponent(project),before=expect(await api(path),200,'retained task before archive').intake;
   const after=expect(await api(path,'PATCH',{key:'normal-archive-'+i,expected_updated_at:before.updated_at,status:'archived',note:'Synthetic post-day retained archive verification'}),200,'archive actual terminal task').intake;
   assert.equal(after.status,'archived');assert.deepEqual(after.original_ask,before.original_ask);archived.push(after.id);
  }
  let after='',found=[],pages=0;const seen=new Set();
  do{assert(++pages<=10,'bounded archive pagination');const page=expect(await api('/api/intakes?project='+encodeURIComponent(project)+'&status=archived&q=User%20fixture&limit=2'+(after?'&cursor='+encodeURIComponent(after):'')),200,'bounded retained archive search');found.push(...page.items.map(v=>v.id));if(page.next){assert(!seen.has(page.next),'archive cursor must advance');seen.add(page.next);}after=page.next;}while(after);
  assert.deepEqual(found.sort(),archived.sort());
  const history=expect(await api('/api/intakes/'+historicalIntake.id+'/history?project='+encodeURIComponent(project)),200,'retained retry history');
  assert.equal(history.events.filter(e=>e.kind==='native_session_started').length,2);assert.equal(history.events.filter(e=>e.kind==='finish_evidence_received').length,2);
  const starts=history.events.filter(e=>e.kind==='session_start_requested');assert.equal(starts.length,2);assert.notEqual(starts[0].body.session_id,starts[1].body.session_id);assert.equal(starts[1].body.retry_of_session,starts[0].body.session_id);
  coverage.retained_archive_search=true;coverage.retained_failed_retry_history=true;
 });
 if(process.env.HUB2_NORMAL_DAY_SLICE!=='twenty')await measure('actual_persisted_cold_restart',async()=>{
  await mf.dispose();mf=new Miniflare(options);base=(await mf.ready).origin;
  expect(await fixture('control','global','clock',{offset:virtualOffset}),200,'restore only synthetic clock after actual process restart');
  const task=expect(await api('/api/intakes/'+intakes[0].id+'?project='+encodeURIComponent(project)),200,'retained archived task after cold restart').intake;assert.equal(task.status,'archived');
  const retry=expect(await api('/api/intakes/'+historicalIntake.id+'?project='+encodeURIComponent(project)),200,'retained retry after cold restart').intake;assert.equal(retry.status,'completed-awaiting-review');
  const fleet=expect(await api('/api/fleet'),200,'actual cold fleet projection');assert.equal(fleet.items.length,4);assert(fleet.items.every(m=>m.connected===false),'closed sockets must not become phantom online hosts');
  for(let i=0;i<6;i++){const quota=expect(await api(`/api/accounts/normal-account-${i}/api/account/quota`),200,'encrypted provider metadata after cold restart');assert.equal(quota.account.provider,'codex');assert(String(quota.quota.source).startsWith('provider:codex:generation:'));}
  coverage.actual_persisted_cold_restart=true;
 });
 coverage.root_writer_api_calls=role_ledger.root_writer;coverage.operator_api_calls=role_ledger.operator;
 if(!coverage.claim_cycles)missing.push('50 claim cycles outside this explicit twenty-start comparison slice');
 await measure('configured_budget_alert_cron_overhead',async()=>{
  for(let i=0;i<configuredCronInvocations;i++)expect(await api('/_fixture/budget-cron','POST',{},bootstrap),200,'actual configured Worker.scheduled invocation');
  cronDeliveryIds=expect(await api('/_fixture/alert-stats','GET',undefined,bootstrap),200,'synthetic alert receiver ids').ids;
 });
 // Missing retained workflows are explicit failing coverage, never zero coefficients.
 if(!coverage.messages)missing.push('100 full native messages outside explicit twenty-start comparison slice');
 for(const[k,n]of Object.entries(canonical))if(coverage[k]!==n)missing.push(k+': measured '+(coverage[k]??0)+' of '+n);
 if(!coverage.retained_archive_search||!coverage.retained_failed_retry_history)missing.push('retained archive/search/history outside this component slice');
 if(!coverage.actual_persisted_cold_restart)missing.push('actual persisted cold restart outside this component slice');
 missing.push('populated older-schema migration and configured production prompt inventory remain separate uncovered scenarios');
 let budgetStatus,budgetLedger;
 await measure('post_workload_budget_status_view',async()=>{
  // This is an explicit observer phase after canonical workload counters are frozen.
  // Keep it out of operator_api_calls while still measuring its real request cost.
  budgetStatus=expect(await api('/api/budget'),200,'authenticated durable budget status');
  budgetLedger=expect(await fixture('budget','global','ledger'),200,'exact fixture BudgetDO all-day ledger');
  assert.equal(budgetStatus.timezone,'UTC');
  assert.equal(budgetStatus.allowances,'assumed_exhausted');
  assert.equal(budgetStatus.alert_threshold_usd,'0.030000000000');
  assert.equal(budgetStatus.daily_ceiling_usd,'0.100000000000');
  assert.equal(budgetStatus.recovery_reserve_usd,'0.010000000000');
  assert.equal(budgetStatus.controller_metering_valid,true);
  assert.equal(budgetStatus.health,'degraded','missing provider-rated usage must remain visible as degraded');
  assert.equal(budgetStatus.provider.state,'unknown','unavailable provider reconciliation must never be reported as zero');
  assert.deepEqual(Object.keys(budgetStatus.units).sort(),[...budgetMetrics].sort());
  for(const metric of budgetMetrics)assert.match(budgetStatus.units[metric],/^(0|[1-9][0-9]*)$/,`finite budget counter required: ${metric}`);
  assert.deepEqual(Object.keys(budgetStatus.category_costs_usd).sort(),['durable_objects','sqlite','worker','workers_kv']);
  for(const amount of Object.values(budgetStatus.category_costs_usd))assert.match(amount,/^(0|[1-9][0-9]*)\.[0-9]{12}$/,'finite category cost required');
 });
 await measure('completed_steady_idle',async()=>{await new Promise(r=>setTimeout(r,50));});
 const idle=flows.at(-1);assert.equal(idle.combined_rows,0,'idle SQL must be zero');assert.equal(idle.alarm_operations,0,'idle alarm work must be zero');
 const profiles={},storage_profiles={};for(const[kind,id]of objects){const d=expect(await fixture(kind,id,'profile'),200,'profile');profiles[kind+':'+id]=d.profile;storage_profiles[kind+':'+id]=d.storage;}
 const final=await snapshot();await telemetryBarrier();const sourceAfter=await hashes(),stable=JSON.stringify(sourceBefore)===JSON.stringify(sourceAfter);
 const telemetry=nativeTelemetry.report();const total=Object.values(telemetry.phases).reduce((n,f)=>n+f.reads+f.writes,0),alarm_operations=Object.values(telemetry.phases).reduce((n,f)=>n+Object.values(f.storage).reduce((m,c)=>m+c.succeeded+c.failed,0),0);
 const provenAlarmWrites=Object.values(telemetry.phases).reduce((n,f)=>n+f.storage.setAlarm.succeeded,0);const complete=missing.length===0&&Object.entries(canonical).every(([k,n])=>coverage[k]===n);
 const canonicalComplete=Object.entries(canonical).every(([k,n])=>coverage[k]===n)&&coverage.role_separated_credentials&&coverage.retained_archive_search&&coverage.retained_failed_retry_history&&coverage.actual_persisted_cold_restart;
 const report={passed:complete&&stable&&nativeTelemetry.valid()&&!errors.length,canonical_passed:!!canonicalComplete&&stable&&nativeTelemetry.valid()&&!errors.length,engine:'actual native workerd SQLite',canonical_source:'docs/hub2/ARCHITECTURE.md:124-146',production_source_directory:sourceDir,bundle_sha256:bundleSha,budget_profile_sha256:createHash('sha256').update(normalDayBudget).digest('hex'),source_snapshot_manifest_sha256:process.env.HUB2_SOURCE_MANIFEST?createHash('sha256').update(await readFile(process.env.HUB2_SOURCE_MANIFEST)).digest('hex'):null,cache_variant:cacheVariant,component_slice:process.env.HUB2_NORMAL_DAY_SLICE||'implemented-flows',cache_adapter_sha256:createHash('sha256').update(await readFile(resolve(root,'hub2/tests/normal-day-cache-off.ts'))).digest('hex'),canonical,coverage,role_ledger,request_ledger,missing,errors,profiles,storage_profiles,configured_alert_cron:{invocations:configuredCronInvocations,cadence_minutes:1440/configuredCronInvocations,basis:process.env.HUB2_BUDGET_CRON_PER_DAY?'explicit test parameter; must match deployment manifest':'test-only 15-minute candidate assumption, NOT production approval',delivery_ids:cronDeliveryIds,transport:'synthetic 202/idempotent receiver; real channel delivery proven separately'},alarm_clock_fixture:{physical_shift_ms:7*86400000,driver:'bounded explicit invocation of actual handlers when durable virtual deadlines are due',driven_alarms:drivenAlarms,prevents:'Native real-clock refiring of virtual past timestamps; not a production optimization'},budget_status:budgetStatus,budget_all_days_ledger:budgetLedger,settled_ledger_reclaimed_pusd:budgetLedger?String(budgetLedger.kinds.filter(k=>k.state==='settled').reduce((sum,k)=>sum+BigInt(k.estimate_pusd)-BigInt(k.actual_pusd),0n)):null,settled_ledger_reclaim_semantics:'Actual durable settled reservation estimate minus its recorded settlement, not a counterfactual native-cost subtraction or verified provider bill.',persistence_directory:persist,budget_status_snapshot_semantics:'The authenticated response is observed before its own reservation settles; the separate post_workload_budget_status_view native phase includes its full reservation and settlement cost.',native_observed_sql_cost_usd_pusd:String(Object.values(telemetry.phases).reduce((sum,f)=>sum+BigInt(f.reads)*1000n+BigInt(f.writes)*1000000n,0n)+BigInt(provenAlarmWrites)*1000000n),native_sql_cost_semantics:'Actual native read/write cursor units plus proven setAlarm row writes at marginal rates; excludes unobserved CPU, DO duration and storage. Not total observed provider bill.',proven_alarm_rows_written:provenAlarmWrites,measured_partial_combined_rows:total+provenAlarmWrites,conservative_sql_and_all_alarm_calls:total+alarm_operations,observed_source_invocations:{worker_http_requests:Object.entries(request_ledger).filter(([k])=>!k.startsWith('fixture ')).reduce((n,[,v])=>n+v,0),do_fetch_requests:Object.values(telemetry.phases).reduce((n,p)=>n+p.source_do_requests,0),alarm_handler_entries:Object.values(telemetry.phases).reduce((n,p)=>n+p.source_alarm_entries,0),websocket_messages:Object.values(telemetry.phases).reduce((n,p)=>n+p.source_ws_messages,0),semantics:'Actual source method entries plus public request ledger; not configured upper units. Fixture metadata bypasses source fetch; source alarm handlers are explicitly driven.'},source_stable:stable,source_hashes:sourceBefore,native_telemetry:telemetry,native_meter_valid:nativeTelemetry.valid(),native_meter_fixture_sha256:createHash('sha256').update(await readFile(resolve(root,'hub2/tests/normal-day-meter.mjs'))).digest('hex'),flows,measured_partial_rows:total,alarm_operations,complete,threshold:budgetStatus?.alert_threshold_usd??null,legacy_20000_gate_withdrawn:true,required_paid_cost_alert_usd_per_day:0.03,final_alarms:Object.fromEntries(Object.entries(final).map(([k,v])=>[k,v.alarm])),note:'Actual measured partial workload, not a daily forecast. Synthetic provider HTTP and native process receipts are explicit fixtures; quota is generated by the actual provider collector, never manual snapshot PUT. Operator APIs use actual expiring hashed credential issued through OIDC/device flow; bootstrap secret only authorizes test-only observations/provider transport configuration. Provider-rated usage remains visibly unknown until an authorized source is available. Missing coverage FAILS acceptance.'};
 if(process.env.HUB2_COST_HTTP_CPU_MS){
  report.conditional_runtime_workload_passed=report.passed;report.conditional_runtime_canonical_passed=report.canonical_passed;
  report.passed=false;report.canonical_passed=false;
  report.runtime_assumptions={cpu_upper_ms:Number(process.env.HUB2_COST_HTTP_CPU_MS),settle_deadline_ms:Number(process.env.HUB2_COST_HTTP_CPU_MS)===100?200:500,post_settle_gb_ms:Number(process.env.HUB2_COST_HTTP_CPU_MS)===100?160:512,http_only:true,production_cpu_enforcement_verified:false,acceptance:'Conditional fixture comparison only; source adapter supplies CPU/HTTP eligibility assumptions. Production runtime canary and final exact-source gate required.'};
 }
 if(process.env.HUB2_NORMAL_DAY_REPORT)await writeFile(process.env.HUB2_NORMAL_DAY_REPORT,JSON.stringify(report,null,2)+'\n');
 console.log(JSON.stringify(report));if(!report.passed)process.exitCode=1;
}finally{await mf.dispose();}

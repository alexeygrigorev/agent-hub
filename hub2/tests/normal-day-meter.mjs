// Test-only cursor telemetry. No query text, bindings, credentials or production knob.
import {Log,LogLevel}from'miniflare';
import{readFile}from'node:fs/promises';
import{resolve}from'node:path';
const blank=()=>({reads:0,writes:0,sql_calls:0,invalid:0,source_do_requests:0,source_alarm_entries:0,source_ws_messages:0,storage:{getAlarm:{succeeded:0,failed:0},setAlarm:{succeeded:0,failed:0},deleteAlarm:{succeeded:0,failed:0}},classes:{},objects:{}});
export function nativeMeter(root,sourceDir=resolve(root,'hub2/src')){
 let phase='startup',events=0,sqlEvents=0,hookInstalled=false,overflow=false;const phases=new Map(),instances=new Map(),barriers=new Set(),waiters=new Map(),activeEffects=new Map(),effects={};let effectHook=false,receiptHook=false,allReads=0,allWrites=0;const uncertainties=[];const receiptTotals={valid:0,missing_or_invalid:0,reads:0,writes:0};
 const record=data=>{if(++events>500000){overflow=true;return;}if(data.type==='effect_uncertain'){if(uncertainties.length<64)uncertainties.push(data);return;}if(data.type==='receipt'){if(data.valid){receiptTotals.valid++;receiptTotals.reads+=data.reads;receiptTotals.writes+=data.writes;}else receiptTotals.missing_or_invalid++;return;}if(data.type==='effect_begin'){activeEffects.set(data.id,{reads:allReads,writes:allWrites,phase});return;}
 if(data.type==='effect_end'){
  const before=activeEffects.get(data.id);if(!before){overflow=true;return;}activeEffects.delete(data.id);
  const key=data.kind,p=effects[key]??={count:0,max_reads:0,max_writes:0,max_direct_calls:0,max_wall_ms:0,total_overlap_reads:0,total_overlap_writes:0};
  const reads=allReads-before.reads,writes=allWrites-before.writes;
  p.count++;p.max_reads=Math.max(p.max_reads,reads);p.max_writes=Math.max(p.max_writes,writes);p.max_direct_calls=Math.max(p.max_direct_calls,data.calls);p.max_wall_ms=Math.max(p.max_wall_ms,data.wall_ms);p.total_overlap_reads+=reads;p.total_overlap_writes+=writes;return;
 }const total=phases.get(phase)||blank();phases.set(phase,total);if(['do_request','alarm_entry','ws_entry'].includes(data.type)){total[data.type==='do_request'?'source_do_requests':data.type==='alarm_entry'?'source_alarm_entries':'source_ws_messages']++;return;}if(data.type==='sql'){sqlEvents++;
  if(!data.valid||![data.reads,data.writes].every(v=>Number.isFinite(v)&&v>=0)){total.invalid++;return;}allReads+=data.reads;allWrites+=data.writes;total.reads+=data.reads;total.writes+=data.writes;total.sql_calls++;
  if(instances.size>8192){overflow=true;return;}const instance=instances.get(data.instance)||{reads:0,writes:0,calls:0};instance.reads+=data.reads;instance.writes+=data.writes;instance.calls++;instances.set(data.instance,instance);
  const kind=data.owner?.kind||'unknown',c=total.classes[kind]??={reads:0,writes:0,sql_calls:0};c.reads+=data.reads;c.writes+=data.writes;c.sql_calls++;const owner=kind+':'+(data.owner?.id||'unknown'),o=total.objects[owner]??={reads:0,writes:0,sql_calls:0};o.reads+=data.reads;o.writes+=data.writes;o.sql_calls++;
 }else if(data.type==='storage'&&total.storage[data.method])total.storage[data.method][data.succeeded?'succeeded':'failed']++;};
 class FixtureLog extends Log{constructor(){super(LogLevel.INFO);}logWithLevel(level,message){const text=String(message).replace(/\u001b\[[0-9;]*m/g,''),meter=text.indexOf('HUB2_METER '),barrier=text.indexOf('HUB2_BARRIER ');if(meter>=0){try{record(JSON.parse(text.slice(meter+11)));}catch{overflow=true;}return;}if(barrier>=0){const id=text.slice(barrier+13).trim();barriers.add(id);waiters.get(id)?.();waiters.delete(id);return;}if(level<=LogLevel.ERROR)super.logWithLevel(level,message);}}
 const plugin={name:'test-only-native-cursor-meter',setup(builder){
 builder.onLoad({filter:/(control|project|machine|account|mailbox|budget)-do\.ts$/},async args=>{
  const kind=args.path.split(/[\\/]/).pop().replace('-do.ts','');
  if(args.path!==resolve(sourceDir,kind+'-do.ts'))return;
  let source=await readFile(args.path,'utf8');
  const candidateCpu=Number(process.env.HUB2_COST_HTTP_CPU_MS||0);
  if(candidateCpu){
   if(![100,500].includes(candidateCpu))throw new Error('invalid_fixture_cpu_assumption');
   const constructor=/new SqlReceiptMeter\(this\.db(?:,\s*\{[^}]*\})?\)/;
   if(!constructor.test(source))throw new Error('fixture_cpu_constructor_contract_changed:'+kind);
   source=source.replace(constructor,'new SqlReceiptMeter(this.db,{hibernates_after_handler:true,cpu_upper_ms:'+candidateCpu+'})');
  }
  if(kind==='budget'){if(!/clock(?:\s*:\s*\(\)\s*=>\s*number)?\s*=\s*Date.now/.test(source))throw new Error('fixture_budget_clock_contract_changed');source=source.replace(/clock(?:\s*:\s*\(\)\s*=>\s*number)?\s*=\s*Date.now/,'clock:()=>number=()=>Date.now()');}
  const fetchEntry=/\b(?:async\s+)?fetch\([a-zA-Z_$][\w$]*\s*:\s*Request\)\s*:\s*Promise<Response>\s*\{/;
  if(!fetchEntry.test(source))throw new Error('native_request_meter_contract_changed:'+kind);
  source=source.replace(fetchEntry,match=>match+"\n console.log('HUB2_METER '+JSON.stringify({type:'do_request',kind:'"+kind+"'}));");
  source=source.replace(/\basync\s+alarm\(\)\s*(?::\s*Promise<void>)?\s*\{/,match=>match+"\n console.log('HUB2_METER '+JSON.stringify({type:'alarm_entry',kind:'"+kind+"'}));");
  if(kind==='machine')source=source.replace(/\basync\s+webSocketMessage\([^)]*\)\s*:\s*Promise<void>\s*\{/,match=>match+"\n console.log('HUB2_METER '+JSON.stringify({type:'ws_entry',kind:'machine'}));");
  return{contents:source,loader:'ts'};
 });
 builder.onLoad({filter:/sql-receipt\.ts$/},async args=>{
  if(args.path!==resolve(sourceDir,'sql-receipt.ts'))return;
  let source=await readFile(args.path,'utf8');const point='const r=parseSqlReceipt(response.headers.get(SQL_RECEIPT_HEADER),operationId);';
  if(!source.includes(point))throw new Error('sql_receipt_telemetry_contract_changed');
  source=source.replace(point,point+"\n  console.log('HUB2_METER '+JSON.stringify({type:'receipt',valid:!!r,reads:r?.reads,writes:r?.writes}));");
  receiptHook=true;return{contents:source,loader:'ts'};
 });
 builder.onLoad({filter:/budget-edge\.ts$/},async args=>{
  if(args.path!==resolve(sourceDir,'budget-edge.ts'))return;
  let source=await readFile(args.path,'utf8');
  if(Number(process.env.HUB2_COST_HTTP_CPU_MS)===100){
   if(!source.includes('usage.do_gb_ms+512')||!source.includes('controller.abort(),500'))throw new Error('fixture_runtime_tuple_contract_changed');
   source=source.replace('usage.do_gb_ms+512','usage.do_gb_ms+160').replace('controller.abort(),500','controller.abort(),200');
  }
  source=source.replace("if(performance.now()-started>plan.max_wall_ms)return error('budget_effect_outcome_uncertain',503);","if(performance.now()-started>plan.max_wall_ms){console.log('HUB2_METER '+JSON.stringify({type:'effect_uncertain',kind,reason:'wall_limit',elapsed:performance.now()-started,limit:plan.max_wall_ms}));return error('budget_effect_outcome_uncertain',503);}");
  source=source.replace(" }catch(e){\n  // Any started"," }catch(e){\n console.log('HUB2_METER '+JSON.stringify({type:'effect_uncertain',kind,reason:'exception',name:e?.name,message:String(e?.message||e).slice(0,240)}));\n  // Any started");
  const begin=[" const safety=SAFE.has(kind),plan=planFor(env,safety,kind);"," const safety=SAFE.has(kind),plan=planFor(env,safety);"].find(value=>source.includes(value)),end="\n}\n\nexport function budgetAlertFields";
  if(!begin||!source.includes(end))throw new Error('budget_effect_meter_contract_changed');
  source=source.replace(begin," const __fixtureEffectId=crypto.randomUUID(),__fixtureEffectStart=performance.now();let __fixtureEffectCalls=0;console.log('HUB2_METER '+JSON.stringify({type:'effect_begin',id:__fixtureEffectId,kind}));try{\n"+begin);
  source=source.replace("if(++calls>","__fixtureEffectCalls++;if(++calls>");
  source=source.replace(end,"\n}finally{console.log('HUB2_METER '+JSON.stringify({type:'effect_end',id:__fixtureEffectId,kind,calls:__fixtureEffectCalls,wall_ms:performance.now()-__fixtureEffectStart}));}"+end);
  effectHook=true;return{contents:source,loader:'ts'};
 });
builder.onLoad({filter:/[/\\]hub2[/\\]src[/\\]db\.ts$/},async args=>{
  if(args.path!==resolve(sourceDir,'db.ts'))return;hookInstalled=true;let source=await readFile(args.path,'utf8');const cls='export class MeteredDb {',write='    this.totals.rows_read += read;',invalid='  private invalidateMeasurement(): void {';
  for(const needle of[cls,write,invalid])if(!source.includes(needle))throw new Error('native_meter_source_contract_changed');
  source=source.replace(cls,cls+'\n  private readonly __fixtureMeterId=crypto.randomUUID();');
  source=source.replace(write,"    console.log('HUB2_METER '+JSON.stringify({type:'sql',instance:this.__fixtureMeterId,owner:(this.sql as any).__fixtureOwner,reads:read,writes:written,valid:true}));\n"+write);
  source=source.replace(invalid,invalid+"\n    console.log('HUB2_METER '+JSON.stringify({type:'sql',instance:this.__fixtureMeterId,valid:false}));");
  return{contents:source,loader:'ts'};
 });}};
 const log=new FixtureLog();
 return{plugin,log,handleStructuredLogs({level,message}){log.logWithLevel(level==='error'||level==='warn'?LogLevel.ERROR:LogLevel.INFO,message);},setPhase(name){phase=name;},phase(name){return structuredClone(phases.get(name)||blank());},valid(){return hookInstalled&&sqlEvents>0&&!overflow&&[...phases.values()].every(x=>x.invalid===0);},report(){return{events,sql_events:sqlEvents,sql_hook_installed:hookInstalled,overflow,instances:instances.size,effect_hook_installed:effectHook,receipt_hook_installed:receiptHook,observed_trusted_sql_receipts:{...receiptTotals},receipt_semantics:'Actual nonce-validated namespace receipts consumed by the production helper. Nested/overlapping totals are not distinct billable rows; invalid receipts are counted, never certified as zero SQL.',effect_profiles:structuredClone(effects),effect_uncertainties:structuredClone(uncertainties),unfinished_effects:activeEffects.size,effect_profile_semantics:'Observed maxima only. Each interval includes all SQL events while active, including nested/concurrent work and ledger overhead. May overcount; not a production hard bound or distinct cost sum.',phases:Object.fromEntries(phases)};},instance(id){return instances.get(id);},async barrier(id){if(barriers.delete(id))return;await new Promise((yes,no)=>{const timeout=setTimeout(()=>{waiters.delete(id);no(new Error('native_meter_barrier_timeout'));},5000);waiters.set(id,()=>{clearTimeout(timeout);barriers.delete(id);yes();});});}};
}

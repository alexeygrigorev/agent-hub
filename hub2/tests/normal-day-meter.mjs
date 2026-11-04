// Test-only cursor telemetry. No query text, bindings, credentials or production knob.
import {Log,LogLevel}from'miniflare';
import{readFile}from'node:fs/promises';
import{resolve}from'node:path';
const blank=()=>({reads:0,writes:0,sql_calls:0,invalid:0,storage:{getAlarm:{succeeded:0,failed:0},setAlarm:{succeeded:0,failed:0},deleteAlarm:{succeeded:0,failed:0}},classes:{},objects:{}});
export function nativeMeter(root){
 let phase='startup',events=0,sqlEvents=0,hookInstalled=false,overflow=false;const phases=new Map(),instances=new Map(),barriers=new Set(),waiters=new Map();
 const record=data=>{if(++events>500000){overflow=true;return;}const total=phases.get(phase)||blank();phases.set(phase,total);if(data.type==='sql'){sqlEvents++;
  if(!data.valid||![data.reads,data.writes].every(v=>Number.isFinite(v)&&v>=0)){total.invalid++;return;}total.reads+=data.reads;total.writes+=data.writes;total.sql_calls++;
  if(instances.size>8192){overflow=true;return;}const instance=instances.get(data.instance)||{reads:0,writes:0,calls:0};instance.reads+=data.reads;instance.writes+=data.writes;instance.calls++;instances.set(data.instance,instance);
  const kind=data.owner?.kind||'unknown',c=total.classes[kind]??={reads:0,writes:0,sql_calls:0};c.reads+=data.reads;c.writes+=data.writes;c.sql_calls++;const owner=kind+':'+(data.owner?.id||'unknown'),o=total.objects[owner]??={reads:0,writes:0,sql_calls:0};o.reads+=data.reads;o.writes+=data.writes;o.sql_calls++;
 }else if(data.type==='storage'&&total.storage[data.method])total.storage[data.method][data.succeeded?'succeeded':'failed']++;};
 class FixtureLog extends Log{constructor(){super(LogLevel.INFO);}logWithLevel(level,message){const text=String(message).replace(/\u001b\[[0-9;]*m/g,''),meter=text.indexOf('HUB2_METER '),barrier=text.indexOf('HUB2_BARRIER ');if(meter>=0){try{record(JSON.parse(text.slice(meter+11)));}catch{overflow=true;}return;}if(barrier>=0){const id=text.slice(barrier+13).trim();barriers.add(id);waiters.get(id)?.();waiters.delete(id);return;}if(level<=LogLevel.ERROR)super.logWithLevel(level,message);}}
 const plugin={name:'test-only-native-cursor-meter',setup(builder){builder.onLoad({filter:/[/\\]hub2[/\\]src[/\\]db\.ts$/},async args=>{
  if(args.path!==resolve(root,'hub2/src/db.ts'))return;hookInstalled=true;let source=await readFile(args.path,'utf8');const cls='export class MeteredDb {',write='    this.totals.rows_read += read;',invalid='  private invalidateMeasurement(): void {';
  for(const needle of[cls,write,invalid])if(!source.includes(needle))throw new Error('native_meter_source_contract_changed');
  source=source.replace(cls,cls+'\n  private readonly __fixtureMeterId=crypto.randomUUID();');
  source=source.replace(write,"    console.log('HUB2_METER '+JSON.stringify({type:'sql',instance:this.__fixtureMeterId,owner:(this.sql as any).__fixtureOwner,reads:read,writes:written,valid:true}));\n"+write);
  source=source.replace(invalid,invalid+"\n    console.log('HUB2_METER '+JSON.stringify({type:'sql',instance:this.__fixtureMeterId,valid:false}));");
  return{contents:source,loader:'ts'};
 });}};
 const log=new FixtureLog();
 return{plugin,log,handleStructuredLogs({level,message}){log.logWithLevel(level==='error'||level==='warn'?LogLevel.ERROR:LogLevel.INFO,message);},setPhase(name){phase=name;},phase(name){return structuredClone(phases.get(name)||blank());},valid(){return hookInstalled&&sqlEvents>0&&!overflow&&[...phases.values()].every(x=>x.invalid===0);},report(){return{events,sql_events:sqlEvents,sql_hook_installed:hookInstalled,overflow,instances:instances.size,phases:Object.fromEntries(phases)};},instance(id){return instances.get(id);},async barrier(id){if(barriers.delete(id))return;await new Promise((yes,no)=>{const timeout=setTimeout(()=>{waiters.delete(id);no(new Error('native_meter_barrier_timeout'));},5000);waiters.set(id,()=>{clearTimeout(timeout);barriers.delete(id);yes();});});}};
}

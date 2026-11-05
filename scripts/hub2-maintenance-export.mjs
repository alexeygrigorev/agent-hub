// Operator-only transfer tooling. No secret in argv/environment/output; no redirects or fallback.
import assert from 'node:assert/strict';
import {createReadStream,createWriteStream} from 'node:fs';
import {mkdir,readFile,writeFile,lstat,open} from 'node:fs/promises';
import {resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import {createHash} from 'node:crypto';
import {createGzip} from 'node:zlib';
import {pipeline} from 'node:stream/promises';
import {once} from 'node:events';

export async function readConfig(path){
 const info=await lstat(path);assert(info.isFile()&&!info.isSymbolicLink(),'private_regular_config_required');
 if(process.platform!=='win32')assert((info.mode&0o077)===0&&info.uid===process.getuid(),'private_owned_config_required');
 const value=JSON.parse(await readFile(path,'utf8')),url=new URL(value.server);
 assert(url.protocol==='https:'||url.protocol==='http:'&&['127.0.0.1','localhost','[::1]'].includes(url.hostname),'secure_server_required');
 assert(!url.username&&!url.password&&!url.search&&!url.hash&&url.pathname==='/','origin_only_required');
 assert(typeof value.token==='string'&&value.token.length>=32,'token_required');
 return {server:url.origin,token:value.token};
}
export async function requestJson(config,path,{method='GET',body,deadline=Date.now()+20000}={}){
 assert(path.startsWith('/api/'),'api_path_required');
 const response=await fetch(config.server+path,{method,redirect:'error',headers:{Authorization:'Bearer '+config.token,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)}),signal:AbortSignal.timeout(Math.max(1,Math.min(20000,deadline-Date.now())))});
 if(!response.ok){await response.body?.cancel();throw Error('transfer_http_'+response.status);}
 const chunks=[];let size=0;
 for await(const chunk of response.body){size+=chunk.length;if(size>32*1024*1024)throw Error('transfer_response_bound');chunks.push(chunk);}
 return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
export async function privateDirectory(path){await mkdir(path,{mode:0o700});}
export async function privateJson(path,value){
 const handle=await open(path,'wx',0o600);
 try{await handle.writeFile(JSON.stringify(value,null,2)+'\n');await handle.sync();}finally{await handle.close();}
}
export async function shaFile(path){const hash=createHash('sha256');for await(const chunk of createReadStream(path))hash.update(chunk);return hash.digest('hex');}
const same=(a,b)=>JSON.stringify(Object.entries(a).sort())===JSON.stringify(Object.entries(b).sort());
function fenced(value,generation){
 assert(value.writes_disabled===true&&typeof value.maintenance_generation==='string'&&value.maintenance_generation.length<=128&&value.maintenance_generation.length>0,'write_fenced_maintenance_required');
 if(generation!==undefined)assert.equal(value.maintenance_generation,generation,'maintenance_generation_changed');
}
export async function exportSnapshot(config,output,{chunkRows=100,deadline=Date.now()+1200000}={}){
 assert(Number.isInteger(chunkRows)&&chunkRows>=1&&chunkRows<=100,'chunk_bound');
 await privateDirectory(output);
 const before=await requestJson(config,'/api/backup/status',{deadline});fenced(before);
 assert(Number(before.api_version)>=5&&before.row_counts&&Object.keys(before.row_counts).length<=1024,'backup_api_v5_required');
 const tables=Object.keys(before.row_counts).sort();let total=0;
 for(const table of tables){assert(/^[A-Za-z_][A-Za-z0-9_]*$/.test(table),'invalid_table');const n=before.row_counts[table];assert(Number.isSafeInteger(n)&&n>=0,'invalid_count');total+=n;}
 assert(total<=5000000,'total_row_bound');
 const schema=await requestJson(config,'/api/backup/export?schema=1',{deadline});fenced(schema,before.maintenance_generation);
 assert(schema.schema&&Object.keys(schema.schema).length<=4096,'schema_bound');
 const path=resolve(output,'export.json.gz'),gzip=createGzip(),file=createWriteStream(path,{flags:'wx',mode:0o600});
 const completed=pipeline(gzip,file);completed.catch(()=>{});
 const write=async value=>{if(!gzip.write(value))await once(gzip,'drain');};
 const counts={};let requests=2,bytes=0;
 try{
  await write(JSON.stringify({exported_at:before.exported_at,snapshot:'hub-do-sqlite',schema:schema.schema,row_counts:before.row_counts}).slice(0,-1)+',"tables":{');
  for(let index=0;index<tables.length;index++){
   const table=tables[index],offsetMode=(before.without_rowid||[]).includes(table);let count=0,after='first',offset=0;
   await write((index?',':'')+JSON.stringify(table)+':[');
   for(;;){
    assert(Date.now()<deadline&&requests++<60000,'export_execution_bound');
    const cursor=offsetMode?'offset='+offset:'after='+encodeURIComponent(after);
    const page=await requestJson(config,'/api/backup/export?table='+encodeURIComponent(table)+'&'+cursor+'&limit='+chunkRows,{deadline});
    fenced(page,before.maintenance_generation);
    assert(page.table===table&&page.total===before.row_counts[table]&&Array.isArray(page.rows)&&page.rows.length<=chunkRows,'chunk_binding_or_drift');
    for(const row of page.rows){const text=JSON.stringify(row);bytes+=Buffer.byteLength(text);assert(bytes<=4*1024**3,'export_byte_bound');await write((count?',':'')+text);count++;}
    assert(count<=before.row_counts[table],'table_count_overflow');
    if(page.rows.length===0||count===before.row_counts[table])break;
    if(offsetMode)offset=count;
    else{assert(typeof page.next_after==='string'&&page.next_after!==after,'cursor_did_not_advance');after=page.next_after;}
   }
   assert.equal(count,before.row_counts[table],'exact_table_count_required');counts[table]=count;await write(']');
  }
  await write('}}');gzip.end();await completed;
  const archive=await open(path,'r');try{await archive.sync();}finally{await archive.close();}
  const after=await requestJson(config,'/api/backup/status',{deadline});fenced(after,before.maintenance_generation);
  assert(same(before.row_counts,after.row_counts),'final_count_drift');
  const manifest={version:1,source_generation_claim:before.maintenance_generation,source_exported_at:before.exported_at,sha256:await shaFile(path),tables:counts,total_rows:total,requests:requests+1,
   server_reported_writes_disabled:true,row_counts_stable:true,live_state_verified:false,
   consistency:'unverified_row_counts_only'};
  await privateJson(resolve(output,'manifest.json'),manifest);return manifest;
 }catch(error){gzip.destroy();await completed.catch(()=>{});await privateJson(resolve(output,'failed.json'),{complete:false,error:'bounded_export_refused'});throw error;}
}
if(process.argv[1]&&resolve(process.argv[1])===fileURLToPath(import.meta.url)){
 const args=process.argv.slice(2),value=flag=>args[args.indexOf(flag)+1];
 try{assert(args.includes('--config')&&args.includes('--output'),'config_and_output_required');
 const result=await exportSnapshot(await readConfig(value('--config')),resolve(value('--output')));
 console.log(JSON.stringify({tables:Object.keys(result.tables).length,total_rows:result.total_rows,sha256:result.sha256,
   server_reported_writes_disabled:result.server_reported_writes_disabled,row_counts_stable:result.row_counts_stable,
   live_state_verified:result.live_state_verified}));
 }catch{console.error('Maintenance export refused; no complete manifest published.');process.exitCode=1;}
}

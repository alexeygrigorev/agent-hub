/** Complete host-owned registry observations. Never used as launch/exit authorization. */
import {MeteredDb} from './db';
import {sha256Hex} from './crypto';
import {HttpError} from './http';
import type {JsonRecord} from './types';

export const NATIVE_INVENTORY_SCHEMA = [
  'CREATE TABLE native_inventory_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1),metadata_json TEXT NOT NULL)',
  'CREATE TABLE native_inventory_rows(native_id TEXT PRIMARY KEY,row_json TEXT NOT NULL) WITHOUT ROWID',
  'CREATE TABLE native_inventory_upload(singleton INTEGER PRIMARY KEY CHECK(singleton=1),metadata_json TEXT NOT NULL)',
  'CREATE TABLE native_inventory_chunks(chunk_index INTEGER PRIMARY KEY,data TEXT NOT NULL)',
];
const UUID=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const MAX_BYTES=16*1024*1024, CHUNK_BYTES=24000, FRESH_MS=90_000;
const fields=['id','tag','workspace','engine','agent','profile','agent_profile','phase','cwd','created_at_ms','last_activity_ms','state','worker_alive'];
type Meta={snapshot_id:string;started_at_ms:number;observed_at_ms:number;row_count:number;chunk_count:number;sha256:string};
export type NativeRow={id:string;tag:string;workspace:string;engine:string|null;agent:string|null;profile:string|null;agent_profile:string|null;phase:string;cwd:string|null;created_at_ms:number|null;last_activity_ms:number|null;state:string;worker_alive:boolean|null};
function fail(code='invalid_native_inventory',status=400):never{throw new HttpError(code,status);}
function timestamp(value:unknown):value is number{return Number.isSafeInteger(value)&&Number(value)>=0;}
function path(value:unknown):value is string{return typeof value==='string'&&!value.includes('\0')&&new TextEncoder().encode(value).length<=8192&&(/^(\/|[A-Za-z]:[\\/]|\\\\)/).test(value);}
function validateRows(value:unknown,count:number):NativeRow[]{
 if(!Array.isArray(value)||value.length!==count)fail();
 let previous='';
 for(const row of value){
  if(!row||typeof row!=='object'||Array.isArray(row)||Object.keys(row).length!==fields.length||fields.some(key=>!Object.hasOwn(row,key)))fail();
  if(typeof row.id!=='string'||!UUID.test(row.id)||row.id<=previous||!path(row.workspace)||typeof row.tag!=='string'||!row.tag||row.tag.includes('\0')||new TextEncoder().encode(row.tag).length>512)fail();
  previous=row.id;
  for(const key of ['engine','agent','profile','agent_profile'])if(row[key]!==null&&(typeof row[key]!=='string'||! /^[A-Za-z0-9_.:+-]{1,128}$/.test(row[key])))fail();
  if(!['starting','running','exiting','stopping','exited','failed','unknown'].includes(row.phase)||row.cwd!==null&&!path(row.cwd))fail();
  if(!['starting','running','exiting','exited','failed','broken','unknown'].includes(row.state)||row.worker_alive!==null&&typeof row.worker_alive!=='boolean')fail();
  for(const key of ['created_at_ms','last_activity_ms'])if(row[key]!==null&&!timestamp(row[key]))fail();
 }
 return value as NativeRow[];
}
function meta(body:JsonRecord,now:number):Meta{
 if(Object.keys(body).some(key=>!['snapshot_id','started_at_ms','observed_at_ms','row_count','chunk_count','sha256','chunk_index','data'].includes(key)))fail();
 const m=body as unknown as Meta;
 if(typeof m.snapshot_id!=='string'||!UUID.test(m.snapshot_id)||!timestamp(m.started_at_ms)||!timestamp(m.observed_at_ms)||m.observed_at_ms<m.started_at_ms||m.observed_at_ms-m.started_at_ms>6000||m.observed_at_ms>now+30_000||now-m.observed_at_ms>120_000||!Number.isInteger(m.row_count)||m.row_count<0||m.row_count>5000||!Number.isInteger(m.chunk_count)||m.chunk_count<1||m.chunk_count>Math.ceil(MAX_BYTES/CHUNK_BYTES)||typeof m.sha256!=='string'||! /^[0-9a-f]{64}$/.test(m.sha256))fail();
 return {snapshot_id:m.snapshot_id,started_at_ms:m.started_at_ms,observed_at_ms:m.observed_at_ms,row_count:m.row_count,chunk_count:m.chunk_count,sha256:m.sha256};
}
function newer(a:Meta,b:Meta){return a.observed_at_ms>b.observed_at_ms||a.observed_at_ms===b.observed_at_ms&&a.snapshot_id>b.snapshot_id;}
export class MachineNativeInventory {
 private queue:Promise<unknown>=Promise.resolve();
 constructor(private db:MeteredDb,private ctx:DurableObjectState){}
 receive(body:JsonRecord):Promise<JsonRecord>{
  const next=this.queue.then(()=>this.accept(body));this.queue=next.catch(()=>undefined);return next;
 }
 private async accept(body:JsonRecord):Promise<JsonRecord>{
  const m=meta(body,Date.now()),encoded=JSON.stringify(m),index=body.chunk_index,data=body.data;
  if(!Number.isInteger(index)||Number(index)<0||Number(index)>=m.chunk_count||typeof data!=='string'||!data.length||data.length>CHUNK_BYTES||/[^\x20-\x7e]/.test(data))fail();
  const active=this.metadata();
  if(active?.snapshot_id===m.snapshot_id){if(JSON.stringify(active)!==encoded)fail('native_inventory_conflict',409);return {complete:true,replayed:true,snapshot_id:m.snapshot_id};}
  if(active&&!newer(m,active))fail('native_inventory_older_snapshot',409);
  this.db.transaction(this.ctx,()=>{
   const stage=this.db.one<{metadata_json:string}>('SELECT metadata_json FROM native_inventory_upload WHERE singleton=1');
   if(stage&&stage.metadata_json!==encoded){
    const old=JSON.parse(stage.metadata_json) as Meta;
    if(old.snapshot_id===m.snapshot_id||!newer(m,old))fail('native_inventory_conflict',409);
   }
   if(!stage||stage.metadata_json!==encoded){this.db.run('DELETE FROM native_inventory_chunks');this.db.run('INSERT OR REPLACE INTO native_inventory_upload(singleton,metadata_json) VALUES(1,?)',encoded);}
   const prior=this.db.one<{data:string}>('SELECT data FROM native_inventory_chunks WHERE chunk_index=?',index);
   if(prior&&prior.data!==data)fail('native_inventory_conflict',409);
   if(!prior)this.db.run('INSERT INTO native_inventory_chunks(chunk_index,data) VALUES(?,?)',index,data);
  });
  const chunks=this.db.all<{chunk_index:number;data:string}>('SELECT chunk_index,data FROM native_inventory_chunks ORDER BY chunk_index');
  if(chunks.length!==m.chunk_count)return {complete:false,snapshot_id:m.snapshot_id,received_chunks:chunks.length};
  const raw=chunks.map(row=>row.data).join('');
  if(raw.length>MAX_BYTES||await sha256Hex(raw)!==m.sha256)fail('native_inventory_digest_mismatch',409);
  let parsed:unknown;try{parsed=JSON.parse(raw);}catch{fail();}
  const rows=validateRows(parsed,m.row_count);
  // Hashing yielded; queue serialization retains this exact staging identity.
  this.db.transaction(this.ctx,()=>{
   this.db.run('DELETE FROM native_inventory_rows');
   for(const row of rows)this.db.run('INSERT INTO native_inventory_rows(native_id,row_json) VALUES(?,?)',row.id,JSON.stringify(row));
   this.db.run('INSERT OR REPLACE INTO native_inventory_meta(singleton,metadata_json) VALUES(1,?)',encoded);
   this.db.run('DELETE FROM native_inventory_chunks');this.db.run('DELETE FROM native_inventory_upload');
  });
  return {complete:true,snapshot_id:m.snapshot_id,row_count:rows.length};
 }
 metadata():Meta|undefined{
  const row=this.db.one<{metadata_json:string}>('SELECT metadata_json FROM native_inventory_meta WHERE singleton=1');
  return row?JSON.parse(row.metadata_json) as Meta:undefined;
 }
 status(now=Date.now()):JsonRecord{
  const m=this.metadata();return m?{...m,complete:true,fresh:now>=m.observed_at_ms-30_000&&now-m.observed_at_ms<=FRESH_MS}:{complete:false,fresh:false};
 }
}

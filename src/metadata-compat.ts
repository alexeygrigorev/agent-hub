import type {SqlStorage} from '@cloudflare/workers-types';

type State=Record<string,any>;
const initialized=new WeakMap<object,Map<string,MetadataCompat>>();
export function readCompatibleMetadata(sql:SqlStorage,table:string,key:string|number,base:State):State {
 return initialized.get(sql)?.get(table)?.read(key,base)??base;
}
const identifier=(name:string)=>/^[A-Za-z_][A-Za-z0-9_]*$/.test(name);
const VALUE_BYTE_LIMIT=64*1024;
export class MetadataCompatibilityError extends Error {
 readonly status=503;
 readonly code='metadata_observation_pending';
 constructor(){super('Metadata compatibility state is unavailable');}
}
/** Point-owned optional metadata. Legacy tables are never ALTERed or copied.
 * JSON preserves explicit NULL independently from a missing sidecar field. */
export class MetadataCompat {
 /** Reuse only the exact initialized metadata contract on this SQL handle.
  * Reader wrappers may be created after the owning service initialized tables. */
 static forInitializedSchema(sql:SqlStorage,table:string,fields:readonly string[],pk='id'):MetadataCompat {
  const existing=initialized.get(sql)?.get(table);
  if(existing&&existing.pk===pk&&fields.length===existing.allowed.size&&new Set(fields).size===fields.length&&fields.every(field=>existing.allowed.has(field)))return existing;
  return new MetadataCompat(sql,table,fields,pk);
 }
 private columns:Set<string>;
 private allowed:Set<string>;
 constructor(private sql:SqlStorage,private table:string,fields:readonly string[],private pk='id') {
  if(!identifier(table)||!identifier(pk)||fields.length>8||fields.some(field=>!identifier(field)))throw new MetadataCompatibilityError();
  this.allowed=new Set(fields);
  sql.exec(`CREATE TABLE IF NOT EXISTS metadata_compat_fields(scope TEXT NOT NULL,record_key TEXT NOT NULL,field TEXT NOT NULL,value TEXT NOT NULL,
   PRIMARY KEY(scope,record_key,field))`);
  this.columns=new Set(sql.exec('PRAGMA table_info('+table+')').toArray().map((row:any)=>String(row.name)));
  const accessors=initialized.get(sql)??new Map<string,MetadataCompat>();accessors.set(table,this);initialized.set(sql,accessors);
 }
 private overrides(key:string|number):Map<string,any> {
  const rows=this.sql.exec(`SELECT field,length(CAST(value AS BLOB)) AS value_bytes,
    CASE WHEN length(CAST(value AS BLOB))<=${VALUE_BYTE_LIMIT} THEN value ELSE NULL END AS value
    FROM metadata_compat_fields WHERE scope=? AND record_key=? ORDER BY field LIMIT 9`,this.table,String(key)).toArray() as any[];
  if(rows.length>8)throw new MetadataCompatibilityError();
  const result=new Map<string,any>();
  for(const row of rows) {
   if(!this.allowed.has(row.field)||typeof row.value!=='string'||!Number.isSafeInteger(row.value_bytes)||row.value_bytes>VALUE_BYTE_LIMIT)throw new MetadataCompatibilityError();
   try{result.set(row.field,JSON.parse(row.value));}catch{throw new MetadataCompatibilityError();}
  }
  return result;
 }
 read(key:string|number,base?:State):State {
  const current=base??this.sql.exec('SELECT * FROM '+this.table+' WHERE '+this.pk+'=?',key).toArray()[0]??{};
  return {...current,...Object.fromEntries(this.overrides(key))};
 }
 patch(key:string|number,patch:State):void {
  const entries=Object.entries(patch);
  if(entries.length>8||entries.some(([field,value])=>!this.allowed.has(field)||value===undefined))throw new MetadataCompatibilityError();
  const encodedValues=new Map<string,string>();
  for(const [field,value] of entries) {
   let encoded:string|undefined;try{encoded=JSON.stringify(value);}catch{throw new MetadataCompatibilityError();}
   if(typeof encoded!=='string'||new TextEncoder().encode(encoded).byteLength>VALUE_BYTE_LIMIT)throw new MetadataCompatibilityError();
   encodedValues.set(field,encoded);
  }
  const sidecar=this.overrides(key),direct:[string,any][]=[];
  for(const [field,value] of entries) {
   if(this.columns.has(field)&&!sidecar.has(field))direct.push([field,value]);
   else {
    const encoded=encodedValues.get(field)!;
    this.sql.exec(`INSERT INTO metadata_compat_fields VALUES(?,?,?,?) ON CONFLICT(scope,record_key,field) DO UPDATE SET value=excluded.value
      WHERE metadata_compat_fields.value IS NOT excluded.value`,this.table,String(key),field,encoded);
   }
  }
  if(direct.length)this.sql.exec('UPDATE '+this.table+' SET '+direct.map(([field])=>field+'=?').join(',')+' WHERE '+this.pk+'=? AND ('+direct.map(([field])=>field+' IS NOT ?').join(' OR ')+')',...direct.map(([,value])=>value),key,...direct.map(([,value])=>value));
 }
}

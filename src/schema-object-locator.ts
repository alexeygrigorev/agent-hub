import type {SqlStorage} from '@cloudflare/workers-types';

type Identity={name:string;type:'index'|'trigger';table:string};
type Located={rowid:number;sql:string};
type Locator={rowid:number;name:string;type:string;tbl_name:string};
// Schema queries deliberately contain a comment: WriteWatch must not memoize
// live verification even if an operator changed schema outside its DDL hook.
const snapshots=new WeakMap<object,Map<string,Locator>>();
const hints=new WeakMap<object,Map<string,number>>();
const durableOwners=new WeakSet<object>();
const LOCATOR_TABLE='schema_object_locators';
function configureDurableLocators(sql:SqlStorage):void {
  // Re-run native IF NOT EXISTS: an earlier configure may have been inside
  // an outer transaction that later rolled back the table creation.
  sql.exec(`CREATE TABLE IF NOT EXISTS ${LOCATOR_TABLE}(object_type TEXT NOT NULL,table_name TEXT NOT NULL,name TEXT NOT NULL,
    object_rowid INTEGER NOT NULL,PRIMARY KEY(object_type,table_name,name)) WITHOUT ROWID`);
  durableOwners.add(sql);
}
const key=(object:Identity)=>JSON.stringify([object.type,object.table,object.name]);
const validName=(value:unknown):value is string=>typeof value==='string'&&/^[A-Za-z_][A-Za-z0-9_]{0,127}$/.test(value);
const locatorBytes=(identity:string,row:Locator)=>128+2*(identity.length+row.name.length+row.type.length+row.tbl_name.length);

export class SchemaObjectObservationLimit extends Error {
  readonly code='schema_object_observation_limit';
  readonly status=503;
  constructor(){super('Schema object observation is incomplete');}
}

/** Deployment hints locate rows only. They never prove identity, existence
 * or definition. Invalid/absent hints simply use bounded metadata discovery. */
export function configureSchemaObjectHints(sql:SqlStorage,raw:unknown):void {
  configureDurableLocators(sql);
  hints.delete(sql);
  if(typeof raw==='string') {
    if(raw.length>4096)return;
    try{raw=JSON.parse(raw);}catch{return;}
  }
  if(!raw||typeof raw!=='object')return;
  const entries=Array.isArray(raw)?raw.map(row=>[row?.name,row?.rowid]):Object.entries(raw);
  if(entries.length>8)return;
  const accepted=new Map<string,number>();
  for(const [name,rowid] of entries) {
    if(!validName(name)||!Number.isSafeInteger(rowid)||rowid<1||accepted.has(name))return;
    accepted.set(name,rowid);
  }
  hints.set(sql,accepted);
}

function snapshot(sql:SqlStorage):Map<string,Locator> {
  // Bound the raw schema prefix before identity filtering. Retain only
  // locators, never SQL definitions or cached absence/validation verdicts.
  const rows=sql.exec('SELECT /* live schema metadata */ rowid,name,type,tbl_name FROM sqlite_master ORDER BY rowid LIMIT 1025').toArray() as Locator[];
  if(rows.length>1024)throw new SchemaObjectObservationLimit();
  const result=new Map<string,Locator>();let bytes=0;
  for(const row of rows) {
    if(!Number.isSafeInteger(row.rowid)||row.rowid<1||typeof row.name!=='string'||typeof row.type!=='string'||typeof row.tbl_name!=='string')
      throw new SchemaObjectObservationLimit();
    const identity=JSON.stringify([row.type,row.tbl_name,row.name]);
    bytes+=locatorBytes(identity,row);
    if(bytes>2*1024*1024)throw new SchemaObjectObservationLimit();
    result.set(identity,row);
  }
  snapshots.set(sql,result);return result;
}

type DurableLocator={object_rowid:unknown};
function durableLocator(sql:SqlStorage,identity:Identity):DurableLocator|null|undefined {
  if(!durableOwners.has(sql))return;
  return sql.exec(`SELECT /* live locator metadata */ object_rowid FROM ${LOCATOR_TABLE} WHERE object_type=? AND table_name=? AND name=?`,
    identity.type,identity.table,identity.name).toArray()[0] as DurableLocator|undefined??null;
}
function persistLocator(sql:SqlStorage,identity:Identity,rowid:number,observed?:DurableLocator|null):void {
  if(!durableOwners.has(sql))return;
  // A readSchemaObject caller just observed this row in the same synchronous
  // call. Carry it locally, never cache its absence or verification verdict.
  const current=observed===undefined?durableLocator(sql,identity):observed;
  if(current?.object_rowid===rowid)return;
  if(!current) {
    // Only this tiny metadata table is counted, never application history.
    // Raw sentinel first; no COUNT aggregate may traverse an unknown source.
    const rows=sql.exec(`SELECT /* live locator metadata */ object_type,table_name,name FROM ${LOCATOR_TABLE} LIMIT 1025`).toArray();
    if(rows.length>=1024)throw new SchemaObjectObservationLimit();
    for(const row of rows)if(!['index','trigger'].includes(String(row.object_type))||!validName(row.table_name)||!validName(row.name))throw new SchemaObjectObservationLimit();
  }
  sql.exec(`INSERT INTO ${LOCATOR_TABLE}(object_type,table_name,name,object_rowid) VALUES(?,?,?,?)
    ON CONFLICT(object_type,table_name,name) DO UPDATE SET object_rowid=excluded.object_rowid
    WHERE ${LOCATOR_TABLE}.object_rowid IS NOT excluded.object_rowid`,identity.type,identity.table,identity.name,rowid);
}
function remember(sql:SqlStorage,identity:Identity,rowid:number,observed?:DurableLocator|null):void {
  persistLocator(sql,identity,rowid,observed);
  let locators=snapshots.get(sql)??new Map<string,Locator>();
  const identityKey=key(identity),row={rowid,name:identity.name,type:identity.type,tbl_name:identity.table};
  if(!locators.has(identityKey)) {
    let bytes=locatorBytes(identityKey,row);
    for(const [name,value] of locators)bytes+=locatorBytes(name,value);
    if(locators.size>=1024||bytes>2*1024*1024)locators=new Map();
  }
  locators.set(identityKey,row);snapshots.set(sql,locators);
}

function validate(identity:Identity):void {
  if(!validName(identity.name)||!validName(identity.table)||!['index','trigger'].includes(identity.type))throw new SchemaObjectObservationLimit();
}
function read(sql:SqlStorage,identity:Identity,rowid:number):Located|null {
  const actual=sql.exec(`SELECT /* live schema metadata */ rowid,sql FROM sqlite_master
    WHERE rowid=? AND name=? AND type=? AND tbl_name=?`,rowid,identity.name,identity.type,identity.table).toArray()[0] as Located|undefined;
  if(!actual)return null;
  if(typeof actual.sql!=='string')throw new SchemaObjectObservationLimit();
  return {rowid:actual.rowid,sql:actual.sql};
}

/** Every lookup verifies actual live identity and SQL through the rowid.
 * Durable metadata stores ROWIDs only and survives eviction; every candidate
 * still needs this live physical proof. Wrong/reused hints and locators fall back to a
 * shared bounded schema snapshot. Absence is never cached as authority. */
export function readSchemaObject(sql:SqlStorage,identity:Identity):Located|null {
  validate(identity);
  const locator=snapshots.get(sql)?.get(key(identity));
  const observed=durableLocator(sql,identity);
  const stored=observed?.object_rowid;
  const durable=Number.isSafeInteger(stored)&&Number(stored)>0?Number(stored):undefined;
  const hinted=hints.get(sql)?.get(identity.name);
  for(const rowid of new Set([locator?.rowid,durable,hinted]))if(rowid!==undefined) {
    const actual=read(sql,identity,rowid);
    if(actual){remember(sql,identity,actual.rowid,observed);return actual;}
  }
  const discovered=snapshot(sql).get(key(identity));
  const actual=discovered?read(sql,identity,discovered.rowid):null;
  if(actual)remember(sql,identity,actual.rowid,observed);
  return actual;
}

/** Call immediately after an actual CREATE/replace. New schema objects are
 * normally the last row; verify that exact identity and live definition to
 * refresh the locator without rescanning metadata. Otherwise fall back. */
export function noteCreatedSchemaObject(sql:SqlStorage,identity:Identity):Located|null {
  validate(identity);
  const last=sql.exec('SELECT /* live schema metadata */ rowid,name,type,tbl_name,sql FROM sqlite_master ORDER BY rowid DESC LIMIT 1').toArray()[0] as (Locator&{sql:string})|undefined;
  if(last&&last.name===identity.name&&last.type===identity.type&&last.tbl_name===identity.table) {
    if(typeof last.sql!=='string'||!Number.isSafeInteger(last.rowid)||last.rowid<1)throw new SchemaObjectObservationLimit();
    remember(sql,identity,last.rowid);return {rowid:last.rowid,sql:last.sql};
  }
  return readSchemaObject(sql,identity);
}

type Entry={table:string;rows:unknown[];bytes:number};
const MAX_BYTES=256*1024,MAX_ENTRIES=64;
/** Instance-local query data, never an authorization decision. All writes must
 * pass before(); rollback must clear(). No trigger/cascade side effects allowed. */
export class ReadCache{
 private entries=new Map<string,Entry>();private bytes=0;private hits=0;private misses=0;
 private table(sql:string):string|undefined{
  if(sql.length>16384||/;|--|\/\*/.test(sql))return;
  const tokens=sql.replace(/'(?:''|[^'])*'/g,"''");
  if(/["\x60\[\]]/.test(tokens)||/\bIN\b(?!\s*\()/i.test(tokens))return;
  if(!/^\s*SELECT\b/i.test(tokens)||/\b(?:JOIN|WITH|UNION|INTERSECT|EXCEPT|OVER|CURRENT_TIMESTAMP|CURRENT_DATE|CURRENT_TIME)\b/i.test(tokens)||(tokens.match(/\bSELECT\b/gi)||[]).length!==1||[...tokens.matchAll(/\b([A-Za-z_]\w*)\s*\(/g)].some(m=>m[1].toUpperCase()!=='IN'))return;
  const from=tokens.match(/\bFROM\s+([A-Za-z_]\w*)\b/i);if(!from)return;
  const tail=tokens.slice(from.index!+from[0].length);
  // No table lists, schema/quoted names, aliases or second relation.
  if(/^\s*(?:,|\.|["\x60\[])/.test(tail)||!/^\s*(?:$|WHERE\b|ORDER\s+BY\b|GROUP\s+BY\b|LIMIT\b)/i.test(tail)||/\bFROM\b/i.test(tail))return;
  return from[1].toLowerCase();
 }
 private key(sql:string,bindings:unknown[]):string|undefined{
  if(bindings.length>64)return;
  let bound=(sql.length+32)*12;
  for(const value of bindings){bound+=typeof value==='string'?value.length*12:64;if(bound>MAX_BYTES)return;}
  if(!bindings.every(v=>v===null||typeof v==='string'||typeof v==='boolean'||typeof v==='number'&&Number.isFinite(v)))return;
  return JSON.stringify([sql,bindings]);
 }
 get(sql:string,bindings:unknown[]):unknown[]|undefined{
  const table=this.table(sql),key=table&&this.key(sql,bindings),entry=key?this.entries.get(key):undefined;
  if(!entry){this.misses++;return;}
  this.entries.delete(key!);this.entries.set(key!,entry);this.hits++;
  return structuredClone(entry.rows);
 }
 put(sql:string,bindings:unknown[],rows:unknown[]):void{
  const table=this.table(sql),key=table&&this.key(sql,bindings);if(!table||!key)return;
  let bytes=256+key.length*2;
  const charge=(value:unknown,depth=0):boolean=>{
   if(depth>8)return false;
   if(value===null||typeof value==='number'||typeof value==='boolean')bytes+=16;
   else if(typeof value==='string')bytes+=64+value.length*2;
   else if(value instanceof ArrayBuffer)bytes+=64+value.byteLength;
   else if(ArrayBuffer.isView(value)){if(!(value.buffer instanceof ArrayBuffer))return false;bytes+=128+value.buffer.byteLength;}
   else if(Array.isArray(value)){bytes+=64+value.length*8;for(const v of value)if(!charge(v,depth+1))return false;}
   else if(value&&typeof value==='object'&&(Object.getPrototypeOf(value)===Object.prototype||Object.getPrototypeOf(value)===null)){bytes+=128;for(const [k,v] of Object.entries(value)){bytes+=64+k.length*2;if(!charge(v,depth+1))return false;}}
   else return false;
   return bytes<=MAX_BYTES;
  };
  if(!charge(rows))return;
  let copy:unknown[];try{copy=structuredClone(rows);}catch{return;}
  const prior=this.entries.get(key);if(prior){this.bytes-=prior.bytes;this.entries.delete(key);}
  while(this.entries.size>=MAX_ENTRIES||this.bytes+bytes>MAX_BYTES){const oldest=this.entries.keys().next().value!;this.bytes-=this.entries.get(oldest)!.bytes;this.entries.delete(oldest);}
  this.entries.set(key,{table,rows:copy,bytes});this.bytes+=bytes;
 }
 before(sql:string):void{
  const tokens=sql.replace(/'(?:''|[^'])*'/g,"''");
  if(/;|--|\/\*/.test(tokens)){this.clear();return;}
  if(/^\s*SELECT\b/i.test(tokens))return;
  const mutation=tokens.match(/^\s*(?:INSERT(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?\s+INTO|REPLACE\s+INTO|UPDATE(?:\s+OR\s+(?:IGNORE|REPLACE|ABORT|FAIL|ROLLBACK))?|DELETE\s+FROM)\s+([A-Za-z_]\w*)\b/i);
  if(!mutation){this.clear();return;}
  if(/^\s*[.\"\x60\[]/.test(tokens.slice(mutation[0].length))){this.clear();return;}
  const table=mutation[1].toLowerCase();
  for(const [key,entry] of this.entries)if(entry.table===table){this.bytes-=entry.bytes;this.entries.delete(key);}
 }
 clear():void{this.entries.clear();this.bytes=0;}
 metrics(){return{hits:this.hits,misses:this.misses,entries:this.entries.size,bytes:this.bytes};}
}


/** New Hub2 namespaces only. External DDL/direct storage writes require a
 * cache-off maintenance deployment; legacy schemas must not be imported. */
export function assertCacheSchema(statements:readonly string[]):void{
 for(const sql of statements){
  const tokens=sql.replace(/'(?:''|[^'])*'/g,"''");
  if(/--|\/\*/.test(tokens)||/\bCREATE\s+(?:(?:TEMP|TEMPORARY)\s+)?(?:TRIGGER|VIEW|VIRTUAL\s+TABLE)\b/i.test(tokens)||/\bON\s+(?:DELETE|UPDATE)\s+(?:CASCADE|SET\s+(?:NULL|DEFAULT))\b/i.test(tokens))throw new Error('unsupported_read_cache_schema');
 }
}

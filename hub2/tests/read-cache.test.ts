import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ReadCache,assertCacheSchema} from '../src/read-cache';import {MeteredDb} from '../src/db';import {verifyKnownCredential} from '../src/auth';
function fixture(){const sql=new DatabaseSync(':memory:'),cache=new ReadCache();let calls=0;
 const all=(q:string,...args:any[])=>{const hit=cache.get(q,args);if(hit!==undefined)return hit as any[];cache.before(q);calls++;const rows=sql.prepare(q).all(...args);cache.put(q,args,rows);return rows as any[];};
 const run=(q:string,...args:any[])=>{cache.before(q);calls++;return sql.prepare(q).run(...args);};
 const tx=(fn:()=>unknown)=>{sql.exec('BEGIN');try{const result=fn();sql.exec('COMMIT');return result;}catch(e){sql.exec('ROLLBACK');cache.clear();throw e;}};
 run('CREATE TABLE config(id TEXT PRIMARY KEY,value TEXT)');run('CREATE TABLE other(id TEXT PRIMARY KEY,value TEXT)');run('INSERT INTO config VALUES(?,?)','a','old');return{sql,cache,all,run,tx,calls:()=>calls};}
test('read data is cloned and table-aware writes invalidate before SQL, preserving unrelated table entries',()=>{
 const f=fixture(),q='SELECT * FROM config WHERE id=?';assert.equal(f.all(q,'a')[0].value,'old');const before=f.calls();
 const row=f.all(q,'a')[0];row.value='caller corruption';assert.equal(f.all(q,'a')[0].value,'old');assert.equal(f.calls(),before);
 f.run('INSERT INTO other VALUES(?,?)','x','unrelated');assert.equal(f.all(q,'a')[0].value,'old');assert.equal(f.calls(),before+1);
 f.run('UPDATE config SET value=? WHERE id=?','new','a');assert.equal(f.all(q,'a')[0].value,'new');assert.equal(f.calls(),before+3);
 f.run('DELETE FROM config WHERE id=?','a');assert.equal(f.all(q,'a').length,0);
 f.run('INSERT OR REPLACE INTO config VALUES(?,?)','a','replacement');assert.equal(f.all(q,'a')[0].value,'replacement');
});
test('rollback clears uncommitted cached reads and interleaved async writer is observed on next read',async()=>{
 const f=fixture(),q='SELECT * FROM config WHERE id=?';f.all(q,'a');
 assert.throws(()=>f.tx(()=>{f.run('UPDATE config SET value=? WHERE id=?','uncommitted','a');assert.equal(f.all(q,'a')[0].value,'uncommitted');throw Error('abort');}));
 assert.equal(f.all(q,'a')[0].value,'old');
 let resume!:()=>void;const awaited=new Promise<void>(resolve=>resume=resolve);
 const reader=(async()=>{assert.equal(f.all(q,'a')[0].value,'old');await awaited;return f.all(q,'a')[0].value;})();
 f.run('UPDATE config SET value=? WHERE id=?','after await','a');resume();assert.equal(await reader,'after await');
});
test('cached credential rows are rechecked for expiry and revocation invalidates credentials data',()=>{
 const f=fixture();f.run('CREATE TABLE credentials(actor_id TEXT,kind TEXT,resource_id TEXT,generation TEXT,family_id TEXT,client_id TEXT,principal_email TEXT,expires_at INTEGER,revoked_at INTEGER,scopes_json TEXT)');
 f.run('INSERT INTO credentials VALUES(?,?,?,?,?,?,?,?,?,?)','credential:test','operator','human:test','generation','family','phone','a@example.test',Date.now()+60000,null,'["operator:admin"]');
 const env:any={HUB_ADMINS:'a@example.test'},q='SELECT * FROM credentials WHERE actor_id=?';
 assert(verifyKnownCredential(f.all(q,'credential:test')[0],env));
 f.run('UPDATE credentials SET revoked_at=? WHERE actor_id=?',Date.now(),'credential:test');assert.equal(verifyKnownCredential(f.all(q,'credential:test')[0],env),undefined);
 f.run('UPDATE credentials SET revoked_at=NULL,expires_at=? WHERE actor_id=?',Date.now()-1,'credential:test');assert.equal(verifyKnownCredential(f.all(q,'credential:test')[0],env),undefined);
});
test('LRU entry/byte bounds, scalar keys, functions/joins/subqueries and unknown SQL fail safely',()=>{
 const c=new ReadCache(),q='SELECT * FROM config WHERE id=?';
 for(let i=0;i<80;i++)c.put(q,[i],[{value:'small'}]);assert.equal(c.metrics().entries,64);assert.equal(c.get(q,[0]),undefined);assert(c.get(q,[79]));
 c.put(q,['huge'],[{value:'x'.repeat(300000)}]);assert(c.metrics().bytes<=256*1024);assert.equal(c.get(q,['huge']),undefined);
 for(const sql of ["SELECT * FROM config WHERE expires_at>datetime('now')",'SELECT random() FROM config','SELECT * FROM config JOIN other ON config.id=other.id','WITH c AS(SELECT * FROM config) SELECT * FROM c','SELECT * FROM config WHERE id=(SELECT id FROM other)','SELECT * FROM config;DELETE FROM other']){c.put(sql,[],[{id:'bad'}]);assert.equal(c.get(sql,[]),undefined);}
 c.put(q,[Infinity],[{id:'bad'}]);assert.equal(c.get(q,[Infinity]),undefined);
 c.before('unknown operation');assert.equal(c.metrics().entries,0);
 c.put(q,['a'],[{value:'cached'}]);c.before('UPDATE main.config SET value=1');assert.equal(c.metrics().entries,0);
});
test('TTL comparisons with scalar cutoff values have separate keys; SQL clock functions are never cached',()=>{
 const f=fixture();f.run('CREATE TABLE expiring(id INTEGER,expires_at INTEGER)');f.run('INSERT INTO expiring VALUES(1,100)');
 assert.equal(f.all('SELECT * FROM expiring WHERE expires_at>?',50).length,1);assert.equal(f.all('SELECT * FROM expiring WHERE expires_at>?',150).length,0);
 const q="SELECT * FROM expiring WHERE expires_at>unixepoch('now')",before=f.cache.metrics().hits;f.all(q);f.all(q);assert.equal(f.cache.metrics().hits,before);
});

test('volatile SQLite keywords, qualified/quoted relations and ambiguous mutation SQL cannot evade cache rules',()=>{
 const c=new ReadCache();
 for(const expression of ['CURRENT_TIMESTAMP','current_date','Current_Time']){
  const q='SELECT '+expression+' AS now FROM credentials WHERE actor_id=?';c.put(q,['a'],[{now:'stale clock'}]);assert.equal(c.get(q,['a']),undefined);
 }
 for(const q of ['SELECT * FROM main.credentials','SELECT * FROM temp.credentials','SELECT * FROM "credentials"','SELECT * FROM [credentials]','/* leading comment */ SELECT * FROM credentials']){c.put(q,[],[{id:'a'}]);assert.equal(c.get(q,[]),undefined);}
 for(const q of ['UPDATE temp.credentials SET expires_at=0','UPDATE "credentials" SET expires_at=0','/* unknown */ DELETE FROM credentials']){
  c.put('SELECT * FROM credentials',[],[{id:'a'}]);c.before(q);assert.equal(c.metrics().entries,0);
 }
});
test('tiny views cannot retain oversized backing buffers and query key construction has an aggregate bound',()=>{
 const c=new ReadCache(),q='SELECT * FROM credentials WHERE id=?';
 c.put(q,['blob'],[{blob:new Uint8Array(new ArrayBuffer(1024*1024),0,1)}]);assert.equal(c.metrics().entries,0);
 c.put(q,['x'.repeat(1024*1024)],[{id:'x'}]);assert.equal(c.metrics().entries,0);
 c.put(q,Array.from({length:65},()=>0),[{id:'x'}]);assert.equal(c.metrics().entries,0);
 const rows=[{blob:new Uint8Array([1,2]),nested:{value:'original'}}];c.put(q,['small'],rows);rows[0].blob[0]=9;rows[0].nested.value='changed';
 const copy=c.get(q,['small']) as any[];assert.equal(copy[0].blob[0],1);assert.equal(copy[0].nested.value,'original');copy[0].blob[0]=8;assert.equal((c.get(q,['small']) as any[])[0].blob[0],1);
});
test('new Hub2 schema audit rejects implicit trigger and foreign-key side effects before execution',()=>{
 assert.doesNotThrow(()=>assertCacheSchema(['CREATE TABLE child(id TEXT,parent TEXT REFERENCES parent(id))','CREATE INDEX child_id ON child(id)']));
 for(const sql of ['CREATE VIEW v AS SELECT * FROM credentials','CREATE VIRTUAL TABLE text_search USING fts5(body)','CREATE TRIGGER side_effect AFTER UPDATE ON config BEGIN DELETE FROM credentials;END','CREATE TEMP TRIGGER t AFTER INSERT ON config BEGIN SELECT 1;END','CREATE TABLE c(id INTEGER REFERENCES p(id) ON DELETE CASCADE)','CREATE TABLE c(id INTEGER REFERENCES p(id) ON UPDATE SET NULL)','CREATE TABLE c(id INTEGER REFERENCES p(id) ON DELETE SET DEFAULT)','CREATE /* hidden */ TRIGGER x AFTER UPDATE ON config BEGIN SELECT 1;END'])assert.throws(()=>assertCacheSchema([sql]),/unsupported_read_cache_schema/);
});
test('actual MeteredDb mount invalidates all/run mutations before execution and clears aborted transaction reads',()=>{
 const sql=new DatabaseSync(':memory:');
 const adapter:any={exec(q:string,...a:any[]){const s=sql.prepare(q),rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}};
 const state:any={storage:{transactionSync(fn:any){sql.exec('BEGIN');try{const result=fn();sql.exec('COMMIT');return result;}catch(e){sql.exec('ROLLBACK');throw e;}}}};
 const db=new MeteredDb(adapter);db.run('CREATE TABLE config(id INTEGER,value TEXT)');db.run('INSERT INTO config VALUES(1,?)','old');
 // Node SQLite has no native cursor row counters: seed real query data only for this hook test. Native cost/cache insertion is verified in workerd.
 (db as any).cache.put('SELECT * FROM config',[],sql.prepare('SELECT * FROM config').all());
 assert.equal(db.all<any>('SELECT * FROM config')[0].value,'old');assert.equal(db.snapshot().read_cache.hits,1);
 db.all('UPDATE config SET value=? RETURNING id','via all');assert.equal(db.all<any>('SELECT * FROM config')[0].value,'via all');
 assert.throws(()=>db.transaction(state,()=>{db.run('UPDATE config SET value=?','uncommitted');assert.equal(db.all<any>('SELECT * FROM config')[0].value,'uncommitted');(db as any).cache.put('SELECT * FROM config',[],sql.prepare('SELECT * FROM config').all());throw Error('rollback');}));
 assert.equal(db.all<any>('SELECT * FROM config')[0].value,'via all');
 db.run('UPDATE config SET value=?','via run');assert.equal(db.all<any>('SELECT * FROM config')[0].value,'via run');
});
test('quoted volatile function names and IN table shorthand cannot hide dependencies; scalar IN lists remain data cached',()=>{
 const c=new ReadCache();
 for(const q of ['SELECT "random"() AS value FROM config','SELECT [random]() AS value FROM config','SELECT '+String.fromCharCode(96)+'random'+String.fromCharCode(96)+'() AS value FROM config','SELECT * FROM config WHERE id IN other','SELECT * FROM config WHERE id IN (SELECT id FROM other)']){c.put(q,[],[{id:'unsafe'}]);assert.equal(c.get(q,[]),undefined);}
 const q='SELECT * FROM config WHERE id IN (?,?)';c.put(q,['a','b'],[{id:'a'}]);assert.deepEqual(c.get(q,['a','b']),[{id:'a'}]);
 c.before('UPDATE config SET value=1');assert.equal(c.get(q,['a','b']),undefined);
});

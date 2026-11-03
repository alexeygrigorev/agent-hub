import { expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { WriteWatch } from '../src/write-watch';

it('invalidates cached queue heads after lazy DELETE/UPDATE RETURNING', async () => {
  const stub=(env as any).HUB.get((env as any).HUB.idFromName('returning-cache-'+crypto.randomUUID()));
  await (runInDurableObject as any)(stub,async(_hub:any,state:any)=>{
    const raw=state.storage.sql;
    raw.exec('CREATE TABLE returning_queue(id TEXT PRIMARY KEY,due INTEGER)');
    raw.exec("INSERT INTO returning_queue VALUES('first',1),('second',2)");
    const watch=new WriteWatch(),sql=watch.wrap(raw);
    const head=()=>sql.exec('SELECT id,due FROM returning_queue ORDER BY due LIMIT 1').toArray();
    expect(head()).toEqual([{id:'first',due:1}]);
    expect(sql.exec("DELETE FROM returning_queue WHERE id='first' RETURNING id").toArray()).toEqual([{id:'first'}]);
    expect(head()).toEqual([{id:'second',due:2}]);
    sql.exec("UPDATE returning_queue SET due=10 WHERE id='second' RETURNING id").one();
    expect(head()).toEqual([{id:'second',due:10}]);
    sql.exec("DELETE FROM returning_queue WHERE id='second' RETURNING id").toArray();
    expect(head()).toEqual([]);
  });
});

it('invalidates interleaved reads before lazy RETURNING exposes its final write counter',()=>{
  const database=['first','second'];
  let statements=0;
  function result(rows:string[]) {
    let index=0;
    const cursor:any={columnNames:['id'],rowsRead:0,rowsWritten:0,
      next(){return index<rows.length?{done:false,value:{id:rows[index++]}}:{done:true};},
      [Symbol.iterator](){return this;},toArray(){return Array.from(this);},
      one(){const rows=this.toArray();if(rows.length!==1)throw new Error('not one');return rows[0];},
      raw(){const source=this;return{next(){const row=source.next();return row.done?row:{done:false,value:[row.value.id]};},[Symbol.iterator](){return this;}};},
    };
    return cursor;
  }
  const raw={exec(query:string){
    statements++;
    if(query.startsWith('SELECT'))return result(database.slice(0,1));
    let advanced=false,complete=false;
    const cursor:any={...result([]),get rowsWritten(){return complete?1:0;},
      next(){if(!advanced){advanced=true;return{done:false,value:{id:database.shift()}};}complete=true;return{done:true};},
    };
    return cursor;
  }};
  const sql=new WriteWatch().wrap(raw as any);
  const head=()=>sql.exec('SELECT id FROM lazy_queue LIMIT 1').toArray();
  expect(head()).toEqual([{id:'first'}]);
  const deleting=sql.exec('DELETE FROM lazy_queue RETURNING id');
  expect(head()).toEqual([{id:'first'}]); // a read may recache before the first mutation step
  expect(deleting.next().value).toEqual({id:'first'});
  expect(deleting.rowsWritten).toBe(0); // native counter deliberately remains deferred
  expect(head()).toEqual([{id:'second'}]);
  const rawCursor=sql.exec('DELETE FROM lazy_queue RETURNING id').raw();
  expect(typeof rawCursor.next).toBe('function');
  expect(head()).toEqual([{id:'second'}]);
  expect(rawCursor.next()).toEqual({done:false,value:['second']});
  expect(head()).toEqual([]);
  expect(rawCursor.next().done).toBe(true);
  // Only the explicitly requested two deletes and five head reads execute.
  // Cache invalidation itself must never issue SQL.
  expect(statements).toBe(7);
});

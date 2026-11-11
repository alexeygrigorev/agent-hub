import {test} from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {createServer, connect} from 'node:net';
import {mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, symlinkSync, writeFileSync} from 'node:fs';
import {join, resolve} from 'node:path';
import {DatabaseSync} from 'node:sqlite';
import {probeSchemaReadiness} from '../node/schema-readiness.ts';
import {NodeDurableStorage, NodeSqlStorage} from '../node/sqlite-storage.ts';
import {MeteredDb, initializeSchema} from '../src/db';
import {ControlDO} from '../src/control-do';
import {MachineDO} from '../src/machine-do';

function directory() {
  const parent=resolve('.local/tmp');mkdirSync(parent,{recursive:true});
  return mkdtempSync(join(parent,'schema-readiness-'));
}
function pathFor(dir:string,binding:string,name='global') {
  const hash=createHash('sha1').update(`${binding}\u0000${name}`).digest('hex').slice(0,10);
  return join(dir,`${binding}-${name.replace(/[^A-Za-z0-9_.-]/g,'_').slice(0,60)}-${hash}.sqlite`);
}
function seed(path:string,version?:number) {
  const db=new DatabaseSync(path);
  db.exec("CREATE TABLE retained(id TEXT PRIMARY KEY,value TEXT); INSERT INTO retained VALUES('original','private user content')");
  if(version!==undefined){db.exec('CREATE TABLE hub2_schema(singleton INTEGER PRIMARY KEY,version INTEGER NOT NULL)');db.prepare('INSERT INTO hub2_schema VALUES(1,?)').run(version);}
  db.close();
}
function inspect(path:string) {
  const db=new DatabaseSync(path);
  try{return {version:(db.prepare('SELECT version FROM hub2_schema').get() as any).version,
    rows:db.prepare('SELECT * FROM retained ORDER BY id').all()};}finally{db.close();}
}

test('failed constructor migration rejects although its transaction leaves the old accepted version',async()=>{
  const dir=directory(),file=pathFor(dir,'CONTROL');seed(file,1);
  class Broken {
    constructor(ctx:any){
      // Deliberately ignore initialization's return value, as older constructors
      // did. The probe must await the blockConcurrencyWhile promise itself.
      void initializeSchema(ctx,new MeteredDb(ctx.storage.sql),2,[],[
        ['CREATE TABLE must_rollback(id TEXT PRIMARY KEY)','INVALID MIGRATION SQL private user content'],
      ]);
    }
  }
  try{
    await assert.rejects(probeSchemaReadiness({classes:{CONTROL:Broken},env:{},dataDir:dir}),
      error=>error instanceof Error&&error.message==='schema_readiness_failed');
    assert.equal(inspect(file).version,1);
    const db=new DatabaseSync(file);
    assert.equal(db.prepare("SELECT name FROM sqlite_master WHERE name='must_rollback'").all().length,0);
    assert.equal(db.prepare('SELECT COUNT(*) AS n FROM retained').get()!.n,1);db.close();
  }finally{rmSync(dir,{recursive:true});}
});

test('real legacy Control and Machine constructors defer, apply and reopen upgraded rows without identity metadata',async()=>{
  const dir=directory(),control=pathFor(dir,'CONTROL'),machine=pathFor(dir,'MACHINES','host');
  seed(control);seed(machine);
  const classes={CONTROL:ControlDO,MACHINES:MachineDO} as any;
  try{
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'defer'},dataDir:dir}),2);
    assert.equal(inspect(control).version,4);assert.equal(inspect(machine).version,7);
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'apply'},dataDir:dir}),2);
    assert.equal(inspect(control).version,6);assert.equal(inspect(machine).version,8);
    const db=new DatabaseSync(control);db.exec("INSERT INTO retained VALUES('after-upgrade','concurrent write')");db.close();
    const before=inspect(control).rows;
    const newControl=pathFor(dir,'CONTROL','new-object'),newMachine=pathFor(dir,'MACHINES','new-host');
    seed(newControl);seed(newMachine);
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'defer'},dataDir:dir}),4);
    assert.deepEqual(inspect(control).rows,before);assert.equal(inspect(machine).version,8);
    assert.equal(inspect(newControl).version,4);assert.equal(inspect(newMachine).version,7);
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'apply'},dataDir:dir}),4);
    assert.equal(inspect(newControl).version,6);assert.equal(inspect(newMachine).version,8);
    const check=new DatabaseSync(control);
    assert.equal(check.prepare("SELECT name FROM sqlite_master WHERE name='hub2_identity'").all().length,0);check.close();
  }finally{rmSync(dir,{recursive:true});}
});

const startupFailures = [
  {setup: 'migration-failure', name: 'a migration fails but the old schema version remains valid', diagnostic: /schema_readiness_failed/},
  {setup: 'missing-file', name: 'the explicit environment file is missing', diagnostic: /could not read Hub2 environment file/},
  {setup: 'mode-conflict', name: 'the process overrides the persisted migration mode', diagnostic: /conflicting HUB2_SCHEMA_MIGRATIONS/},
];
for (const {setup, name, diagnostic} of startupFailures) {
  test(`actual main never listens when ${name}`,async()=>{
    const dir=directory(),file=pathFor(dir,'CONTROL');seed(file);
    const server=createServer();
    await new Promise<void>((resolve,reject)=>{server.once('error',reject);server.listen(0,'127.0.0.1',resolve);});
    const port=(server.address() as {port:number}).port;
    await new Promise<void>((resolve,reject)=>server.close(error=>error?reject(error):resolve()));
    let child:ReturnType<typeof spawn>|undefined;
    try{
      await probeSchemaReadiness({classes:{CONTROL:ControlDO} as any,env:{HUB2_SCHEMA_MIGRATIONS:'defer'},dataDir:dir});
      assert.equal(inspect(file).version,4);
      let devVars = 'none';
      if (setup === 'migration-failure') {
        // Leave the DB on an accepted v4 even if the migration fails.
        const db = new DatabaseSync(file);
        db.exec('CREATE TABLE memory_rules(id TEXT PRIMARY KEY,data_json TEXT NOT NULL)');
        db.close();
      } else {
        devVars = join(dir, '.dev.vars');
        if (setup === 'mode-conflict') writeFileSync(devVars, 'HUB2_SCHEMA_MIGRATIONS=defer\n');
      }
      const before = readFileSync(file);
      child=spawn(process.execPath,['--experimental-transform-types',resolve(import.meta.dirname, '../node/main.ts'),
        '--host','127.0.0.1','--port',String(port),'--data-dir',dir,'--dev-vars',devVars],
        {env:{...process.env,HUB2_SCHEMA_MIGRATIONS:'apply'},stdio:['ignore','pipe','pipe']});
      let stdout='',stderr='';
      child.stdout!.on('data',chunk=>{stdout=(stdout+String(chunk)).slice(-65536);});
      child.stderr!.on('data',chunk=>{stderr=(stderr+String(chunk)).slice(-65536);});
      const exit=await new Promise<{code:number|null;timedOut:boolean}>((resolve,reject)=>{
        let timedOut=false;
        const timeout=setTimeout(()=>{timedOut=true;child!.kill('SIGKILL');},10_000);
        child!.once('error',error=>{clearTimeout(timeout);reject(error);});
        child!.once('close',code=>{clearTimeout(timeout);resolve({code,timedOut});});
      });
      assert.equal(exit.timedOut,false,'main must refuse startup without hanging');
      assert.notEqual(exit.code,0);
      assert.doesNotMatch(stdout,/listening http/);
      assert.match(stderr,diagnostic);
      if (setup !== 'migration-failure') assert.deepEqual(readFileSync(file),before);
      assert.equal(inspect(file).version,4);
      const refused=await new Promise<boolean>((resolve,reject)=>{
        const socket=connect({host:'127.0.0.1',port});
        socket.setTimeout(1000,()=>{socket.destroy();reject(new Error('unexpected listener timeout'));});
        socket.once('connect',()=>{socket.destroy();resolve(false);});
        socket.once('error',error=>{socket.destroy();if((error as NodeJS.ErrnoException).code==='ECONNREFUSED')resolve(true);else reject(error);});
      });
      assert.equal(refused,true,'failed readiness must not expose even the health listener');
    }finally{
      if(child&&child.exitCode===null&&child.signalCode===null)child.kill('SIGKILL');
      rmSync(dir,{recursive:true});
    }
  });
}

test('all asynchronous constructor callbacks and namespace dependencies finish without alarm delivery',async()=>{
  const dir=directory();seed(pathFor(dir,'CONTROL'));seed(pathFor(dir,'MACHINES','host'));
  let alarms=0,callbacks=0;
  class Control {
    constructor(private ctx:any){
      void ctx.blockConcurrencyWhile(async()=>{await Promise.resolve();callbacks++;await ctx.storage.setAlarm(Date.now()-1);});
    }
    fetch(){return Response.json({ready:true});}
    alarm(){alarms++;}
  }
  class Machine {
    constructor(ctx:any,env:any){
      void ctx.blockConcurrencyWhile(async()=>{
        const reply=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://internal/constructor-recovery');
        assert.equal((await reply.json()).ready,true);
        await ctx.blockConcurrencyWhile(async()=>{await Promise.resolve();callbacks++;});
      });
    }
    alarm(){alarms++;}
  }
  try{
    assert.equal(await probeSchemaReadiness({classes:{CONTROL:Control,MACHINES:Machine},env:{},dataDir:dir}),2);
    assert.equal(callbacks,2);assert.equal(alarms,0);
    assert(!readdirSync(dir).some(file=>file.endsWith('-wal')),'probe must close all writable SQLite handles');
    const db=new DatabaseSync(pathFor(dir,'CONTROL'));assert(db.prepare('SELECT due FROM hub2_alarm').get());db.close();
  }finally{rmSync(dir,{recursive:true});}
});

test('database census is bounded before any initialization begins',async()=>{
  const dir=directory();let calls=0;
  class Probe {constructor(){calls++;}}
  for(let i=0;i<5001;i++)writeFileSync(join(dir,`CONTROL-${i}.sqlite`),'');
  try{
    await assert.rejects(probeSchemaReadiness({classes:{CONTROL:Probe},env:{},dataDir:dir}),/^Error: schema_readiness_failed$/);
    assert.equal(calls,0);
  }finally{rmSync(dir,{recursive:true});}
});

test('unknown namespaces and symlinks fail closed before any constructor runs',async()=>{
  for(const kind of ['unknown','symlink']){
    const dir=directory();let calls=0;
    class Probe {constructor(){calls++;}}
    const file=pathFor(dir,'CONTROL');seed(file);
    if(kind==='unknown')seed(join(dir,'UNRECOGNIZED-private.sqlite'));
    else symlinkSync(file,join(dir,'CONTROL-private.sqlite'));
    try{
      await assert.rejects(probeSchemaReadiness({classes:{CONTROL:Probe},env:{},dataDir:dir}),/^Error: schema_readiness_failed$/);
      assert.equal(calls,0);
    }finally{rmSync(dir,{recursive:true});}
  }
});

test('missing constructor dependency is refused without creating a new database',async()=>{
  const dir=directory();seed(pathFor(dir,'MACHINES','host'));
  class Control {}
  class Machine {
    constructor(ctx:any,env:any){void ctx.blockConcurrencyWhile(async()=>{
      // ProjectDO's real recovery catches failed registration and schedules a
      // retry. That must not hide a missing database dependency from the probe.
      try{await env.CONTROL.get(env.CONTROL.idFromName('missing-private-name')).fetch('https://internal/');}
      catch{await ctx.storage.setAlarm(Date.now()+30_000);}
    });}
  }
  try{
    await assert.rejects(probeSchemaReadiness({classes:{CONTROL:Control,MACHINES:Machine},env:{},dataDir:dir}),/^Error: schema_readiness_failed$/);
    assert.equal(readdirSync(dir).filter(file=>file.endsWith('.sqlite')).length,1);
  }finally{rmSync(dir,{recursive:true});}
});

test('real MachineDO readiness accepts pending stop commands overdue by more than 24h while preserving alarm range guards',async()=>{
  const dir=directory(),machine=pathFor(dir,'MACHINES','hetzner-aplexer');seed(machine);
  const classes={MACHINES:MachineDO} as any;
  const staleCreatedAt=Date.now()-36*3600_000;
  try{
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'defer'},dataDir:dir}),1);
    assert.equal(inspect(machine).version,7);
    const db=new DatabaseSync(machine);
    db.prepare(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at)
      VALUES('cmd-stale-stop','sess-stale','hetzner-aplexer',1,'stop-1','digest-1','stop',NULL,'pending',NULL,?,?)`).run(staleCreatedAt,staleCreatedAt);
    db.close();
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'defer'},dataDir:dir}),1);
    assert.equal(inspect(machine).version,7);
    const afterDefer=new DatabaseSync(machine);
    assert.equal((afterDefer.prepare('SELECT due FROM hub2_alarm WHERE singleton=1').get() as any)?.due,staleCreatedAt+60_000);
    afterDefer.close();
    assert.equal(await probeSchemaReadiness({classes,env:{HUB2_SCHEMA_MIGRATIONS:'apply'},dataDir:dir}),1);
    assert.equal(inspect(machine).version,8);

    const sql=new NodeSqlStorage(join(dir,'alarm-bounds.sqlite'));
    const storage=new NodeDurableStorage(sql);
    try{
      await storage.setAlarm(0);
      assert.equal(await storage.getAlarm(),0);
      await assert.rejects(storage.setAlarm(1.5),/^TypeError: alarm_due_not_safe_integer$/);
      await assert.rejects(storage.setAlarm(-1),/^RangeError: alarm_due_out_of_range$/);
      await assert.rejects(storage.setAlarm(Date.now()+5*365*24*60*60*1000+60_000),/^RangeError: alarm_due_out_of_range$/);
    }finally{sql.close();}
  }finally{rmSync(dir,{recursive:true});}
});

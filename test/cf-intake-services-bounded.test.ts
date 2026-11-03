import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { IntakeService } from '../src/intake';
import { IntakeVerifier, VERIFIER_INTERVAL, VERIFIER_OBSERVATION_BATCH } from '../src/intake-verifier';
import { IntakeTriage, TRIAGE_INTERVAL } from '../src/intake-triage';

const NOW = Date.parse('2026-11-03T21:00:00Z');
const fixture = (fn: (hub: any, sql: any) => Promise<void>) => {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('cf-intake-page-' + crypto.randomUUID()));
  return (runInDurableObject as any)(stub, (hub: any, state: any) => fn(hub,state.storage.sql));
};
function measure(raw: any) {
  let cursors: any[] = [];
  const sql: any = { exec(q: string,...b: any[]) { const c = raw.exec(q,...b); cursors.push(c); return c; } };
  return { sql, reset() { cursors=[]; }, rows() { return cursors.reduce((n,c)=>n+Number(c.rowsRead),0); } };
}
function services(sql: any) {
  return {
    statusAuthorizedTasks() { throw new Error('unbounded all-history read forbidden'); },
    watchdogTasks(after: string, limit: number) { return IntakeService.prototype.watchdogTasks.call({ sql } as any,after,limit); },
    statusTaskById(_p: any,id: string) { const r = sql.exec('SELECT data FROM intake_tasks WHERE id=?',id).toArray()[0]; return r && JSON.parse(r.data); },
  };
}
function task(raw: any,id: string,status = 'open',done = false,triaged = false) {
  const history: any[] = [];
  if (done) history.push({ id: 'done-'+id,kind:'note',actor:'test',captured_at:NOW,text:'DONE\nevidence: demo@0123456789abcdef\nlive check: synthetic passed\nresult: AC1 met' });
  if (triaged) history.push({ id:'triage-'+id,kind:'note',actor:'test',captured_at:NOW,text:'TRIAGE ACTUAL — bounded fixture' });
  raw.exec('INSERT INTO intake_tasks(id,data) VALUES(?,?)',id,JSON.stringify({ id,project:'demo',title:'Synthetic task',status,verification:'required',
    acceptance_criteria:'AC1 synthetic',created_at:NOW-172800000,updated_at:NOW,version:1,history }));
}
function session(raw: any,id: string,intakeId: string,workspace: string) {
  raw.exec(`INSERT INTO session_control_sessions(id,actor,request_key,request_json,engine,mode,state,created_at,updated_at)
    VALUES(?,'test',?,?,'codex','headless','exited',?,?)`,id,id,JSON.stringify({intake_task_id:intakeId,workspace}),new Date(NOW).toISOString(),new Date(NOW).toISOString());
}

describe('bounded triage and post-DONE verifier cron observations', () => {
  it('verifier advances a durable cursor past sparse retained history and bounds workspace lookups', () => fixture(async (_hub,raw) => {
    for(let i=0;i<2000;i++) { task(raw,'closed-'+i,'done'); session(raw,'old-session-'+i,'unrelated','demo'); }
    for(let i=0;i<70;i++) task(raw,'open-'+String(i).padStart(3,'0'),'open',i===69);
    for(let i=0;i<1000;i++) session(raw,'same-task-'+i,'open-069',i===999?'demo':'/retired/worktree');
    const m=measure(raw); const intake=services(m.sql);let now=NOW;const started:any[]=[];
    const make=()=>new IntakeVerifier(m.sql,intake,(fn:any)=>fn(),{start:async input=>{started.push(input);return{status:202,body:{id:'verifier-1'}};}},()=>now);
    const first=make();first.init();m.reset();
    const a=await first.tick();
    expect(a).toMatchObject({launched:0,observed_tasks:VERIFIER_OBSERVATION_BATCH,observation_truncated:true});
    expect(m.rows()).toBeLessThan(350);
    // A fresh service instance resumes the durable source cursor.
    now+=VERIFIER_INTERVAL;const second=make();second.init();m.reset();
    const b=await second.tick();
    expect(b.launched).toBe(1);
    expect(started[0].workspace).toBe('demo');
    expect(started[0].intake_task_id).toBe('open-069');
    expect(m.rows()).toBeLessThan(200);
  }));

  it('triage bounds requested history, preserves unseen pending tasks, and progresses after restart', () => fixture(async (_hub,raw) => {
    for(let i=0;i<2000;i++) task(raw,'closed-'+i,'done');
    for(let i=0;i<70;i++) task(raw,'open-'+String(i).padStart(3,'0'),'open',false,i!==69);
    const m=measure(raw);const intake=services(m.sql);let now=NOW;const starts:any[]=[];
    const watchdog:any={project(tasks:any[]){return tasks.map(t=>({id:t.id,forgotten:true}));}};
    const make=()=>new IntakeTriage(m.sql,intake as any,watchdog,(fn:any)=>fn(),{start:async input=>{starts.push(input);return{status:202,body:{id:'triage-1'}};}},()=>now);
    const first=make();first.init();
    for(let i=0;i<2000;i++) raw.exec("INSERT INTO intake_triage_requests VALUES(?,?,'old-review','triaged',?)",'past-'+i,'past-'+i,NOW-172800000);
    raw.exec("INSERT INTO intake_triage_requests VALUES('open-069','pending',NULL,'requested',?)",NOW);
    m.reset();const a=await first.tick();
    expect(a.state).toBe('observation_incomplete');
    expect(raw.exec("SELECT state FROM intake_triage_requests WHERE task_id='open-069'").one().state).toBe('requested');
    expect(starts).toHaveLength(0);
    expect(m.rows()).toBeLessThan(350);
    raw.exec("DELETE FROM intake_triage_requests WHERE task_id='open-069'");
    now+=TRIAGE_INTERVAL;const second=make();second.init();m.reset();
    const b=await second.tick();
    expect(b).toMatchObject({state:'reviewer_started',requested:['open-069']});
    expect(starts).toHaveLength(1);
    expect(m.rows()).toBeLessThan(180);
  }));
});

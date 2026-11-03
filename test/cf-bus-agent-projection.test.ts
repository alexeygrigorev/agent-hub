import { describe,expect,it } from 'vitest';
import { env,runInDurableObject } from 'cloudflare:test';
import { BusService } from '../src/bus';
import { subscribedBusAgents } from '../src/bus-agent-projection';
const withHub=(fn:(hub:any,state:any)=>void)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('bus-projection-'+crypto.randomUUID())),fn);

describe('request-owned sparse bus identity projection',()=>{
  it('bounds every sparse legacy page and refuses incomplete role/host authority',()=>withHub((hub,state)=>{
    const raw=state.storage.sql;
    for(let i=0;i<1200;i++)raw.exec('INSERT INTO agents(tag,project,role,machine_id) VALUES(?,?,?,?)',
      'legacy-'+i,'project',i===1199?'reviewer':null,i===1199?'owned':'foreign');
    let rowsRead=0;
    const sql={exec(query:string,...args:any[]){const cursor=raw.exec(query,...args);const values=cursor.toArray();rowsRead+=cursor.rowsRead;return{toArray:()=>values};}};
    let role:any[]|null=null,host:any[]|null=null;
    for(let i=0;i<13;i++) {
      rowsRead=0;role=state.storage.transactionSync(()=>subscribedBusAgents(sql,'role','reviewer'));
      expect(rowsRead).toBeLessThan(160);
      rowsRead=0;host=state.storage.transactionSync(()=>subscribedBusAgents(sql,'host','owned'));
      expect(rowsRead).toBeLessThan(160);
      if(i<12){expect(role).toBe(null);expect(host).toBe(null);}
    }
    expect(role?.map(row=>row.tag)).toEqual(['legacy-1199']);
    expect(host?.map(row=>row.tag)).toEqual(['legacy-1199']);
    rowsRead=0;expect(subscribedBusAgents(sql,'host','owned')).toHaveLength(1);expect(rowsRead).toBeLessThan(12);
    // Future canonical changes immediately retract both old authorities.
    raw.exec("UPDATE agents SET role='writer',machine_id='foreign' WHERE tag='legacy-1199'");
    expect(subscribedBusAgents(sql,'role','reviewer')).toEqual([]);
    expect(subscribedBusAgents(sql,'host','owned')).toEqual([]);
    expect(raw.exec('SELECT COUNT(*) AS n FROM bus_agent_members').one().n).toBe(0);
  }));

  it('role send retries retain migration progress outside the send transaction',()=>withHub((hub,state)=>{
    const raw=state.storage.sql;
    for(let i=0;i<350;i++)raw.exec('INSERT INTO agents(tag,project,role,machine_id) VALUES(?,?,?,?)',
      'send-'+i,'project',i===349?'reviewer':null,'host');
    const bus=new BusService(hub.sql,(fn:any)=>state.storage.transactionSync(fn));
    const send=()=>bus.send({isAdmin:true,actor:'human:reviewer'}, {senderActor:'human:reviewer',toActor:'role:reviewer',kind:'note',body:'review ready',idempotencyKey:'role-ready'});
    for(let i=0;i<3;i++) {
      expect(send).toThrow('bus_registry_projection_incomplete');
      expect(raw.exec("SELECT after_rowid FROM bus_agent_subscriptions WHERE key='role:reviewer'").one().after_rowid).toBe((i+1)*100);
    }
    expect(send().id).toBeTruthy();
    expect(raw.exec('SELECT COUNT(*) AS n FROM messages').one().n).toBe(1);
    hub.sql.exec("INSERT INTO agents(tag,project,role,machine_id) VALUES('second-reviewer','project','reviewer','foreign')");
    expect(bus.validateRecipient('role:reviewer')).toBe(false); // never picks one shared role holder
  }));

  it('preserves role ambiguity and refuses overflow rather than truncating a host authority set',()=>withHub((hub,state)=>{
    const raw=state.storage.sql;
    for(let i=0;i<130;i++)raw.exec("INSERT INTO agents(tag,project,role,machine_id) VALUES(?,'project','writer','owned')",'member-'+i);
    expect(subscribedBusAgents(raw,'role','writer')).toBe(null);
    expect(subscribedBusAgents(raw,'role','writer')).toBe(null); // cap128 is explicit unknown
    expect(subscribedBusAgents(raw,'host','owned')).toBe(null);
    expect(subscribedBusAgents(raw,'host','owned')).toBe(null);
    const states=raw.exec('SELECT overflow,pending FROM bus_agent_subscriptions ORDER BY key').toArray();
    expect(states.every((row:any)=>row.overflow===1&&row.pending===128)).toBe(true);
    // An overflow remains unknown even if retained members disappear; it
    // never silently becomes an apparently complete truncated observation.
    raw.exec("DELETE FROM agents WHERE tag='member-0'");
    expect(subscribedBusAgents(raw,'role','writer')).toBe(null);
  }));
});

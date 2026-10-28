import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { MCP_ROUTES, toolRequest, dispatchToolRequest } from '../src/mcp-tools';
const inDO: any = runInDurableObject;
const native = '3261084c-1f4f-4d9d-b88c-1eb74aa1810a';

describe('verified external intake assignments', () => {
  it('registers through the append route shared by CLI/MCP and expires live evidence with the host census', async () => {
    const stub = (env as any).HUB.get((env as any).HUB.idFromName('hub'));
    await inDO(stub, async (h: any) => {
      const auth = async () => ({caller:{kind:'human',isAdmin:true,actor:'human:fixture'},scopes:['admin']});
      const call = (path: string, payload?: any) => h.intakeService.handle(new Request('https://hub.test/api/intake'+path,
        {method:payload?'POST':'GET',...(payload?{body:JSON.stringify(payload)}:{})}),auth);
      const created = await (await call('/tasks',{key:'external-create',project:'demo',title:'External assignment',text:'Synthetic task',source:{type:'unknown'}})).json();
      const id = created.task.id;
      const append = (key: string, version: number, event: any) => call('/tasks/'+id+'/events',{key,expected_version:version,event});
      const event = {kind:'session',native_id:native,host:'external-fixture',verify_live:true};
      expect(() => h.intakeService.event(event,{history:[]},{admin:false},Date.now())).toThrow('operator_required');
      expect((await append('missing-native',1,event)).status).toBe(409);
      h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('external-fixture',?,'autoreset','running',?,'isolated-workspace')",native,new Date().toISOString());
      h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('external-fixture',?)",new Date().toISOString());
      const mcp = toolRequest('https://hub.test','intake_append',
        {id,payload:{key:'verify-external',expected_version:1,event}},'Bearer fixture');
      const response = await dispatchToolRequest(mcp,'intake_append',r=>h.intakeService.handle(r,auth));
      expect(response.status,await response.clone().text()).toBe(200);
      const result: any = await response.json();
      expect(result.task.payload_data.history.at(-1)).toMatchObject({evidence:'external_live_verified',authority:'host_census_verified',native_id:native});
      expect(result.task.assigned_sessions).toContainEqual(expect.objectContaining({native_id:native,machine:'external-fixture',state:'running',liveness:'live',linkage:'external_session'}));
      h.sql.exec("DELETE FROM fleet_catalog_seen WHERE machine_id='external-fixture'");
      const stale = await (await call('/tasks/'+id)).json();
      expect(stale.task.assigned_sessions[0]).toMatchObject({liveness:'unknown',state:'unknown'});
      expect((await append('stale-native',2,event)).status).toBe(409);
      expect((await append('bad-native',2,{...event,verify_live:'yes'})).status).toBe(400);
      h.sql.exec("INSERT OR REPLACE INTO fleet_catalog_seen(machine_id,observed_at) VALUES('external-fixture',?)",new Date().toISOString());
      h.sql.exec("UPDATE fleet_sessions SET phase='exited' WHERE native_id=?",native);
      const exited = await (await call('/tasks/'+id)).json();
      expect(exited.task.assigned_sessions[0]).toMatchObject({state:'exited',liveness:'terminal'});
      expect((await append('terminal-native',2,event)).status).toBe(409);
      h.sql.exec("UPDATE fleet_sessions SET phase='stopped' WHERE native_id=?",native);
      const stopped = await (await call('/tasks/'+id)).json();
      expect(stopped.task.assigned_sessions[0]).toMatchObject({state:'stopped',liveness:'terminal'});
      h.sql.exec("UPDATE fleet_sessions SET phase='running' WHERE native_id=?",native);
      expect((await append('wrong-host',2,{...event,host:'different-host'})).status).toBe(409);
      expect((await append('short-id',2,{...event,native_id:'3261084c'})).status).toBe(409);
      h.sql.exec("INSERT INTO fleet_sessions(machine_id,native_id,tag,phase,observed_at,workspace) VALUES('second-host',?,'other','running',?,'elsewhere')",native,new Date().toISOString());
      expect((await append('ambiguous-host',2,{...event,host:null})).status).toBe(409);
    });
    expect(MCP_ROUTES.find(t=>t.name==='intake_append')?.description).toContain('verify_live');
  });
});

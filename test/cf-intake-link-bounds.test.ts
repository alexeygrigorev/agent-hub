import {it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {linkedTaskIds,openWorkPreventsIdleReap} from '../src/intake-session';
it('caps raw indexed intake references before link filters and protects unknown owners',async()=>{
 const stub=(env as any).HUB.get((env as any).HUB.idFromName('intake-links-'+crypto.randomUUID()));
 await (runInDurableObject as any)(stub,async(h:any,state:any)=>{
  const raw=state.storage.sql;
  raw.exec("WITH RECURSIVE n(i) AS(SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i<2000) INSERT INTO intake_projection_link_refs SELECT 'shared-native',printf('old-%04d',i) FROM n");
  await h.meter.run('bounded-reference',()=>expect(()=>linkedTaskIds(h.sql,['shared-native'],()=>false,1)).toThrow('intake_link_observation_limited'));
  expect(h.meter.snapshot()['bounded-reference'].rows_read).toBeLessThanOrEqual(129);
  expect(openWorkPreventsIdleReap(h.sql,{id:'session',request_json:'{}',binding_json:JSON.stringify({native_id:'shared-native'})})).toBe(true);
  raw.exec("INSERT INTO intake_task_projection(id,data,links) VALUES('known','{}',?)",JSON.stringify([{hub_session_id:'known-session'}]));
  expect(linkedTaskIds(h.sql,['known-session'],(e:any)=>e.hub_session_id==='known-session',2)).toEqual(['known']);
 });
});

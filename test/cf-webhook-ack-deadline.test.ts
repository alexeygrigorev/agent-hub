import {describe,it,expect} from 'vitest';
import {env,runInDurableObject} from 'cloudflare:test';
import {WebhookService} from '../src/webhooks';
const withHub=(fn:(hub:any,state:any)=>Promise<void>)=>(runInDurableObject as any)(
  (env as any).HUB.get((env as any).HUB.idFromName('webhook-ack-deadline-'+crypto.randomUUID())),fn);
function seed(raw:any,id:string,now:number,attempts=0){
  const at=new Date(now).toISOString(),deadline=new Date(now+600000).toISOString();
  raw.exec(`INSERT INTO session_control_commands(id,session_id,kind,actor,request_key,payload_json,state,generation,correlation,created_at,updated_at)
    VALUES(?,'session','send','test',?,'{}','claimed',1,?,?,?)`,'command-'+id,id,id,at,at);
  raw.exec(`INSERT INTO session_deliveries(id,source,source_id,delivery_key,target_label,body,status,command_id,attempts,next_attempt_at,created_at,updated_at)
    VALUES(?,'webhook','source',?,'target','body','queued',?,?,?,?,?)`,id,id,'command-'+id,attempts,deadline,at,at);
}
describe('webhook command ACK deadline',()=>{
  it('waits on one original deadline then fails finitely instead of permanent minute polling',()=>withHub(async(hub,state)=>{
    let now=Date.now(),start=now;seed(state.storage.sql,'stuck',now,4);
    const service=new WebhookService(hub.sql,(fn:any)=>state.storage.transactionSync(fn),()=>now,()=>null,null);
    expect(await service.processBackground('stuck')).toBe(start+600000);
    now=start+300000;expect(await service.processBackground('stuck')).toBe(start+600000);
    expect(state.storage.sql.exec("SELECT next_attempt_at FROM session_deliveries WHERE id='stuck'").one().next_attempt_at).toBe(new Date(start+600000).toISOString());
    now=start+600000;expect(await service.processBackground('stuck')).toBe(null);
    const result=state.storage.sql.exec("SELECT status,attempts,last_error FROM session_deliveries WHERE id='stuck'").one();
    expect(result).toMatchObject({status:'failed',attempts:5,last_error:'command_ack_timeout'});
    expect(state.storage.sql.exec("SELECT id FROM hub_background_work WHERE id='webhook-delivery:stuck'").toArray()).toEqual([]);
  }));

  it('a known command ACK wakes only its indexed delivery before the deadline',()=>withHub(async(hub,state)=>{
    const now=Date.now(),raw=state.storage.sql;seed(raw,'acked',now);seed(raw,'unrelated',now);
    const mutation=raw.exec('UPDATE session_control_commands SET ack_json=? WHERE id=?',JSON.stringify({outcome:'delivered'}),'command-acked');mutation.toArray();
    expect(mutation.rowsRead).toBeLessThan(100);
    const queued=raw.exec("SELECT id,due_ms FROM hub_background_work WHERE kind='webhook-delivery' ORDER BY id").toArray();
    expect(queued[0].due_ms).toBeLessThan(now+600000);expect(queued[1].due_ms).toBe(now+600000);
    const service=new WebhookService(hub.sql,(fn:any)=>state.storage.transactionSync(fn),()=>now+1000,()=>null,null);
    expect(await service.processBackground('acked')).toBe(null);
    expect(raw.exec("SELECT status FROM session_deliveries WHERE id='acked'").one().status).toBe('delivered');
    expect(raw.exec("SELECT status FROM session_deliveries WHERE id='unrelated'").one().status).toBe('queued');
  }));
});

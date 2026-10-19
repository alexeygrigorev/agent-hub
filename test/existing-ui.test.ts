import {describe,it,expect} from 'vitest';
import {env,SELF,runInDurableObject} from 'cloudflare:test';
import {createSessionCookie} from '../src/auth/session';
import type {Hub} from '../src/hub';
const csrf='ui-current-csrf';
async function human(){return (await createSessionCookie(env as any,{sub:'ui-human',email:'admin@fake.local',isAdmin:true,csrfToken:csrf,exp:Math.floor(Date.now()/1000)+3600})).split(';')[0];}
async function req(path:string,method='GET',body?:any,cookie?:string){return SELF.fetch('https://hub.test'+path,{method,headers:{Accept:'text/html',...(cookie?{Cookie:cookie}:{}),'Content-Type':'application/json','X-CSRF-Token':csrf},body:body===undefined?undefined:JSON.stringify(body),redirect:'manual'});}
async function challenge(){const r=await req('/api/device/code','POST',{client_kind:'agent',name:'ui:worker-'+crypto.randomUUID(),requested_scopes:['bus:read','bus:send']});expect(r.status).toBe(200);return await r.json() as any;}
describe('existing UI real routes',()=>{
  it('renders recoverable OIDC errors for browsers while retaining JSON for API clients',async()=>{
    const response=await req('/auth/callback');expect(response.status).toBe(400);expect(response.headers.get('Content-Type')).toContain('text/html');
    expect(await response.text()).toContain('Sign-in could not finish');
    const api=await SELF.fetch('https://hub.test/auth/callback');expect(api.status).toBe(400);expect(await api.json()).toEqual({error:'Missing code or state',category:'missing_code_and_state'});
  });
  it('preserves anonymous return-to and shows code entry, unknown, denied, expired and completed states',async()=>{
    const c=await human(),p=await challenge();
    const anon=await req('/device?code='+p.user_code);expect(anon.status).toBe(302);expect(anon.headers.get('Location')).toContain(encodeURIComponent('/device?code='+p.user_code));
    expect(await (await req('/device','GET',undefined,c)).text()).toContain('name="code"');
    const unknown=await req('/device?code=NO-SUCH-CODE','GET',undefined,c);expect(unknown.status).toBe(404);expect(await unknown.text()).toContain('Code not found');
    const pending=await req('/device?code='+p.user_code,'GET',undefined,c);const html=await pending.text();expect(html).toContain('id="device-confirm"');expect(html).toContain('id="device-review" hidden');expect(html).not.toContain(csrf);expect(html).not.toContain(p.device_code);expect(html).not.toContain('device_code_hash');
    expect((await req('/api/device/deny/'+p.user_code,'POST',undefined,c)).status).toBe(200);
    const denied=await req('/device?code='+p.user_code,'GET',undefined,c);expect(await denied.text()).toContain('Request denied');
    const stale=await challenge();
    await runInDurableObject((env as any).HUB.get((env as any).HUB.idFromName('hub')),(h:Hub)=>{h.sql.exec('UPDATE device_pairings SET expires_at=? WHERE user_code=?',new Date(Date.now()-1000).toISOString(),stale.user_code);});
    expect(await (await req('/device?code='+stale.user_code,'GET',undefined,c)).text()).toContain('Code expired');
    const done=await challenge();expect((await req('/api/device/approve/'+done.user_code,'POST',{granted_scopes:['bus:read']},c)).status).toBe(200);
    const approved=await req('/device?code='+done.user_code,'GET',undefined,c);expect(await approved.text()).toContain('Device authorized');expect(approved.headers.get('Cache-Control')).toContain('no-store');
    // Approval is not token consumption: an approved code can still expire before polling.
    await runInDurableObject((env as any).HUB.get((env as any).HUB.idFromName('hub')),(h:Hub)=>{h.sql.exec('UPDATE device_pairings SET expires_at=? WHERE user_code=?',new Date(Date.now()-1000).toISOString(),done.user_code);});
    expect(await (await req('/device?code='+done.user_code,'GET',undefined,c)).text()).toContain('Code expired');
  });
});

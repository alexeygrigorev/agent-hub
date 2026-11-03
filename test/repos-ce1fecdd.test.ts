import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import type { Hub } from '../src/hub';
import type { Env } from '../src/env';

const admin = {caller:{kind:'human',isAdmin:true},scopes:['admin']};
const reader = {caller:{kind:'agent'},scopes:['bus:read']};
const repo = {name:'demo',machine:'linux',dir:'/srv/repos/demo',origin:'Example/demo',purpose:'Repository purpose',stack:'Python',workflows:'ci.yml'};
const request = (path:string,method='GET',body?:any) => new Request('https://hub.test/api/repos'+path,
  {method,...(body === undefined ? {} : {body:JSON.stringify(body)})});
async function fixture(callback:(hub:Hub)=>Promise<void>) {
  const binding = env as unknown as Env;
  const stub = binding.HUB.get(binding.HUB.idFromName('repos-ce1fecdd-'+crypto.randomUUID()));
  await runInDurableObject(stub,async (hub:Hub)=> {
    expect((await hub.repoRegistry.handle(request('/seed','POST',{repos:[repo]}),admin)).status).toBe(200);
    await callback(hub);
  });
}
function advertise(hub:Hub,id:string,caps:any) {
  const at = new Date().toISOString();
  hub.sql.exec('INSERT INTO machines (id,name,hostname,registered_at,last_checkin_at,capabilities) VALUES (?,?,?,?,?,?)',id,id,id,at,at,JSON.stringify(caps));
}

describe('ce1fecdd repository API',()=> {
  it('discovers an existing Windows checkout from the enrolled host without registry edits',()=>fixture(async hub=> {
    advertise(hub,'tabo',{workspaces:{demo:'C:\\repos\\demo'}});
    const data = await (await hub.repoRegistry.handle(request('/demo'),reader)).json() as any;
    expect(data.repo.checkouts_by_host.tabo.path).toBe('C:\\repos\\demo');
    expect(data.repo.hosts).toContain('tabo');
    const result = hub.repoRegistry.resolveLaunchRepo({workspace:'demo',machine:'tabo',prompt:'Work'});
    expect(result.error).toBeUndefined();
    expect(result.input.machine).toBe('tabo');
    expect(result.input.repo_context.repo_CheckoutsByHost.tabo.path).toBe('C:\\repos\\demo');
    expect(result.input.repo_context.clone_policy).toBe('clone_on_demand');
  }));
  it('does not infer a clone from a generic workspace root',()=>fixture(async hub=> {
    advertise(hub,'cold',{workspaces:['C:\\repos'],workspace_roots:['C:\\repos']});
    expect(hub.repoRegistry.get('demo').checkouts_by_host.cold).toBeUndefined();
  }));
  it('adds metadata for raw Windows paths while preserving the exact workspace and empty prompts',()=>fixture(async hub=> {
    advertise(hub,'tabo',{workspaces:['C:\\repos\\demo']});
    const result = hub.repoRegistry.resolveLaunchRepo({workspace:'c:/repos/DEMO',machine:'tabo',prompt:'Work'}).input;
    expect(result.workspace).toBe('c:/repos/DEMO');
    expect(result.repo_context.explicit_path).toBe('c:/repos/DEMO');
    for (const text of [repo.purpose,repo.stack,repo.workflows]) expect(result.prompt).toContain(text);
    expect(hub.repoRegistry.resolveLaunchRepo(result).input.prompt).toBe(result.prompt);
    expect(hub.repoRegistry.resolveLaunchRepo({...result,prompt:''}).input.prompt).toBe('');
    expect(hub.repoRegistry.resolveLaunchRepo({workspace:'/unregistered/project',prompt:'Work'}).input)
      .toEqual({workspace:'/unregistered/project',prompt:'Work'});
  }));
  it('allows only operators to restrict or reset the host set and rejects malformed policy',()=>fixture(async hub=> {
    const registry = hub.repoRegistry;
    expect((await registry.handle(request('/demo','PATCH',{allowed_machines:['tabo']}),reader)).status).toBe(403);
    expect((await registry.handle(request('/demo','PATCH',{allowed_machines:['tabo']}),admin)).status).toBe(200);
    expect(registry.resolveLaunchRepo({workspace:'demo',prompt:'Work'}).input.repo_context.allowed_machines).toEqual(['tabo']);
    for (const allowed_machines of ['tabo',['tabo','tabo'],['../bad'],[3]]) {
      expect((await registry.handle(request('/demo','PATCH',{allowed_machines}),admin)).status).toBe(400);
    }
    expect((await registry.handle(request('/demo','PATCH',{allowed_machines:[]}),admin)).status).toBe(200);
    expect(registry.get('demo').allowed_machines).toEqual([]);
    expect((await registry.handle(request('/demo','PATCH',{allowed_machines:null}),admin)).status).toBe(200);
    expect(registry.resolveLaunchRepo({workspace:'demo',prompt:'Work'}).input.repo_context.allowed_machines).toBeNull();
  }));
  it('resolves duplicate paths through their canonical repo and refuses conflicting identities',()=>fixture(async hub=> {
    const registry=hub.repoRegistry;
    expect((await registry.handle(request('/seed','POST',{repos:[{...repo,name:'old-demo',status:'duplicate',canonical:'demo'}]}),admin)).status).toBe(200);
    expect(registry.resolveLaunchRepo({workspace:repo.dir,machine:'linux',prompt:'Work'}).input.repo_context.name).toBe('demo');
    expect((await registry.handle(request('/seed','POST',{repos:[{...repo,name:'other',origin:'Example/other'}]}),admin)).status).toBe(200);
    expect(registry.resolveLaunchRepo({workspace:repo.dir,machine:'linux',prompt:'Work'}).error?.error).toBe('ambiguous_repo');
  }));
});

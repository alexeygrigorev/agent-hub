import {test} from 'node:test';import assert from 'node:assert/strict';import {DatabaseSync} from 'node:sqlite';import {ControlDO} from '../src/control-do';
import {githubRegisterPublicKey, structurallyValidShared, validSharedFiles} from '../src/shared-credentials';

// Hub-shared host credentials (intakes 54d5b703 + 0043dadb): central sealed
// authority in the ControlDO, machine-scoped distribution with acks, bounded
// gh auth reports, rejections superseded by newer generations, GitHub key
// registration idempotency. Synthetic credentials only.
const admin={id:'human:admin',kind:'operator',scopes:['operator:admin'],family_id:'operator-family',client_id:'phone'};
const host={id:'machine:rmthz',kind:'machine',scopes:['machine:heartbeat','machine:offer'],resource_id:'rmthz'};
const GH_TOKEN='gho_SYNTHETICTESTTOKEN000000000000';
const ghFiles=()=>({'gh/hosts.yml':'github.com:\n    git_protocol: https\n    oauth_token: '+GH_TOKEN+'\n    user: agent-hub-bot\n'});
const pubBlob='AAAAC3NzaC1lZDI1NTE5AAAAISYNTHETICNOTAREALKEY0123456789';
const sshFiles=()=>({'git-ssh/id_ed25519':'-----BEGIN OPENSSH PRIVATE KEY-----\nb3BlbnNzaC1rZXkSYNTHETICNOTAREALKEY\n-----END OPENSSH PRIVATE KEY-----\n','git-ssh/id_ed25519.pub':'ssh-ed25519 '+pubBlob+' agent-hub-shared\n'});

function fixture(){
  const database=new DatabaseSync(':memory:');
  const state:any={storage:{async setAlarm(){},async deleteAlarm(){},sql:{exec(q:string,...a:any[]){const s=database.prepare(q);const rows=s.columns().length?s.all(...a):(s.run(...a),[]);return{toArray:()=>rows};}},transactionSync(fn:()=>unknown){database.exec('BEGIN');try{const v=fn();database.exec('COMMIT');return v;}catch(e){database.exec('ROLLBACK');throw e;}}},blockConcurrencyWhile:(fn:()=>unknown)=>Promise.resolve(fn())};
  const control=new ControlDO(state,{HUB2_CREDENTIAL_KEY:'ab'.repeat(32)} as any);
  const call=(p:string,m='GET',b?:unknown,a:unknown=host)=>control.fetch(new Request('https://control.test'+p,{method:m,headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(a)},...(b===undefined?{}:{body:JSON.stringify(b)})}));
  return{control,database,call};
}
const data=async(r:Response)=>await r.json() as any;
const publish=(c:ReturnType<typeof fixture>['call'],kind:string,files:unknown,base?:number,actor?:unknown)=>c('/api/shared-credentials/'+kind,'POST',{files,...(base!==undefined?{base_generation:base}:{})},actor??admin);

test('shared kinds accept only their exact file namespace and a usable structure',()=>{
  assert.equal(validSharedFiles('gh',ghFiles()),true);
  assert.equal(validSharedFiles('git-ssh',sshFiles()),true);
  assert.equal(validSharedFiles('gh',sshFiles()),false);   // kind isolation
  assert.equal(validSharedFiles('git-ssh',ghFiles()),false);
  assert.equal(validSharedFiles('gh',{'engine/.codex/auth.json':'{}'}),false);
  assert.equal(validSharedFiles('gh','nope'),false);
  assert.equal(structurallyValidShared('gh',ghFiles()),true);
  assert.equal(structurallyValidShared('gh',{'gh/hosts.yml':'github.com:\n    git_protocol: ssh\n    oauth_token: x\n'}),false);
  const truncated=sshFiles();truncated['git-ssh/id_ed25519']='not a key';
  assert.equal(structurallyValidShared('git-ssh',truncated),false);
  const badPub=sshFiles();badPub['git-ssh/id_ed25519.pub']='ssh-rsa AAAA agent-hub-shared';
  assert.equal(structurallyValidShared('git-ssh',badPub),false);
});

test('publish, machine distribution, ack with bounded gh auth report; sealed bytes never contain the token',async()=>{
  const{database,call}=fixture();
  const res=await publish(call,'gh',ghFiles());assert.equal(res.status,200);
  const{generation}=await data(res);
  const fetched=await data(await call('/api/shared-credentials'));
  assert.equal(fetched.kinds.gh.generation,generation);
  assert.equal(fetched.kinds.gh.files['gh/hosts.yml'],ghFiles()['gh/hosts.yml']);
  const sealedRow=database.prepare('SELECT sealed FROM shared_credentials WHERE kind=\'gh\'').get() as {sealed:string};
  assert.ok(!sealedRow.sealed.includes(GH_TOKEN));
  const ack=await call('/api/shared-credentials/gh/ack','POST',{generation,gh_auth:{installed:true,logged_in:true,account:'agent-hub-bot',scopes:['gist','repo','workflow'],checked_at:new Date().toISOString()}});
  assert.equal(ack.status,200);
  const status=await data(await call('/api/shared-credentials/status', 'GET', undefined, admin));
  assert.equal(status.credentials.length,1);
  assert.equal(status.acks[0].machine_id,'rmthz');
  assert.equal(status.acks[0].gh_auth.logged_in,true);
  assert.ok(!JSON.stringify(status).includes(GH_TOKEN));
  // An actor without the machine scope cannot fetch; a bad gh auth report is refused.
  assert.equal((await call('/api/shared-credentials','GET',undefined,{id:'agent:x',kind:'agent',scopes:[]})).status,403);
  assert.equal((await call('/api/shared-credentials/gh/ack','POST',{generation:1,gh_auth:{installed:true,logged_in:true,account:'bad account!',scopes:null,checked_at:new Date().toISOString()}})).status,400);
  assert.equal((await call('/api/shared-credentials/gh/ack','POST',{generation:generation+5})).status,200);
});

test('invalid publishes are refused and the publish-time CAS names the current generation',async()=>{
  const{call}=fixture();
  assert.equal((await publish(call,'gh',{'gh/hosts.yml':'oauth_token: x'})).status,400);
  assert.equal((await publish(call,'gh',ghFiles())).status,200);
  await publish(call,'gh',ghFiles()); // current generation is now 2
  const stale=await publish(call,'gh',ghFiles(),1);
  assert.equal(stale.status,409);
  assert.equal((await data(stale)).generation,2);
  assert.equal((await publish(call,'gh',ghFiles(),2)).status,200);
});

test('rejections are recorded for the current generation and cleared by a newer one',async()=>{
  const{call}=fixture();
  await publish(call,'gh',ghFiles());
  const rej=await call('/api/shared-credentials/gh/rejection','POST',{generation:1,reason:'gh_auth_missing'});
  assert.equal(rej.status,200);
  await publish(call,'gh',ghFiles()); // generation 2 supersedes
  const old=await call('/api/shared-credentials/gh/rejection','POST',{generation:1,reason:'gh_auth_missing'});
  assert.equal((await data(old)).status,'superseded');
  const status=await data(await call('/api/shared-credentials/status','GET',undefined,admin));
  assert.equal(status.rejections.length,0);
});

test('publish, status and registration are operator-only; content never reaches non-admin callers',async()=>{
  const{call}=fixture();
  await publish(call,'gh',ghFiles());
  const worker={id:'machine:rmthz',kind:'machine',scopes:['machine:heartbeat'],resource_id:'rmthz'};
  assert.equal((await publish(call,'gh',ghFiles(),undefined,worker)).status,403);
  assert.equal((await call('/api/shared-credentials/status','GET',undefined,worker)).status,403);
  assert.equal((await call('/api/shared-credentials/git-ssh/register-github','POST',{},worker)).status,403);
  assert.equal((await call('/api/shared-credentials/status','GET',undefined,{id:'human:op',kind:'operator',scopes:['repos:read'],family_id:'f',client_id:'c'})).status,403);
});

test('GitHub key registration is idempotent by key blob and maps failures to fixed reason codes',async()=>{
  const calls:({url:string;init?:RequestInit})[]=[];
  const fetchFn=async(url:string,init?:RequestInit)=>{
    calls.push({url,init});
    if(url.endsWith('/user/keys?per_page=100')&&calls.length===1)return new Response(JSON.stringify([{id:7,key:'ssh-ed25519 someotherkey older'}]),{status:200});
    if(url.endsWith('/user/keys?per_page=100'))return new Response(JSON.stringify([{id:7,key:'ssh-ed25519 someotherkey older'},{id:9,key:'ssh-ed25519 '+pubBlob+' agent-hub-shared'}]),{status:200});
    return new Response(JSON.stringify({id:9}),{status:201});
  };
  const files={gh:ghFiles(),'git-ssh':sshFiles()} as any;
  const first=await githubRegisterPublicKey(files,fetchFn);
  assert.deepEqual(first,{ok:true,already_registered:false,key_id:9});
  const again=await githubRegisterPublicKey(files,fetchFn);
  assert.deepEqual(again,{ok:true,already_registered:true,key_id:9});
  const bodies=calls.filter(c=>c.init?.method==='POST').map(c=>JSON.parse(String(c.init!.body)));
  assert.deepEqual(bodies,[{title:'agent-hub-shared-git',key:'ssh-ed25519 '+pubBlob}]);
  assert.ok(!JSON.stringify(bodies).includes(GH_TOKEN));
  assert.deepEqual(await githubRegisterPublicKey(files,async()=>new Response('{}',{status:401})),{ok:false,reason:'github_unauthorized'});
  assert.deepEqual(await githubRegisterPublicKey(files,async()=>{throw new Error('down');}),{ok:false,reason:'github_unreachable'});
  assert.deepEqual(await githubRegisterPublicKey({gh:{'gh/hosts.yml':'github.com:\n'},'git-ssh':sshFiles()} as any,async()=>new Response('[]',{status:200})),{ok:false,reason:'gh_credential_missing'});
});

test('v1 databases gain the shared tables through the schema migration',async()=>{
  const{database}=fixture();
  for(const table of ['shared_credentials','shared_credential_acks','shared_credential_rejections']){
    assert.ok(database.prepare('SELECT name FROM sqlite_master WHERE type=\'table\' AND name=?').get(table));
  }
  const version=(database.prepare('SELECT version FROM hub2_schema WHERE singleton=1').get() as {version:number}).version;
  assert.equal(version,2);
});

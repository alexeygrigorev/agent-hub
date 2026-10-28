// Loopback-only public HTTP + browser + actual read-only native bridge qualification.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import {randomBytes} from 'node:crypto';
import { launchBrowser } from './playwright.mjs';
import {readFile,mkdir,mkdtemp,rm} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
import {execFile} from 'node:child_process';
import {promisify} from 'node:util';
const exec=promisify(execFile),root=resolve(import.meta.dirname,'..');
const nativeWorkspace=process.env.PAIRING_NATIVE_WORKSPACE;
let own;
if(nativeWorkspace) {
 own=JSON.parse((await exec('a',['--json','whoami'],{cwd:nativeWorkspace})).stdout);
 assert.equal(own.workspace,nativeWorkspace);assert.ok(own.id && own.tag && own.engine);
}
const tag=own?.tag||'browser-worker',actor='pair-local:'+tag;
const key=randomBytes(32).toString('hex'),secret=randomBytes(32).toString('hex');
const bundled=await build({entryPoints:[resolve(root,'test/machine-pairing-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const mime={'.css':'text/css','.js':'text/javascript','.woff2':'font/woff2'};
const mf=new Miniflare(convertV4MiniflareOptions({host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{
 name:'machine-pairing-local',script:bundled.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_compat'],
 durableObjects:{HUB:{className:'Hub',useSQLite:true}},bindings:{SESSION_SECRET:secret,HUB_ADMINS:'admin@fake.local',LOCAL_FIXTURE_KEY:key},
 serviceBindings:{ASSETS:async request=>{const file=resolve(root,'public','.'+new URL(request.url).pathname);assert.ok(file.startsWith(resolve(root,'public')+'/'));try{return new Response(await readFile(file),{headers:{'Content-Type':mime[extname(file)]||'application/octet-stream'}});}catch{return new Response(null,{status:404});}}}
}]}));
let browser;
const cliRoot=await mkdtemp(resolve(process.env.TMPDIR,'pairing-cli-'));
const children=new Set();
try {
 const base=(await mf.ready).origin;
 const fixture=await (await fetch(base+'/__pairing_fixture',{method:'POST',headers:{Authorization:'Bearer '+key}})).json();
 const challengeResponse=await fetch(base+'/api/device/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_kind:'agent',name:actor,requested_scopes:['bus:read','bus:send','health:report']})});
 assert.equal(challengeResponse.status,200);const challenge=await challengeResponse.json();
 const poll=()=>fetch(base+'/api/device/token',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:challenge.device_code})});
 assert.equal((await (await poll()).json()).error,'authorization_pending');
 browser=await launchBrowser({args:['--single-process','--in-process-gpu','--disable-gpu','--disable-dev-shm-usage','--no-sandbox','--js-flags=--thread-pool-size=1','--num-raster-threads=1','--disable-threaded-animation','--disable-threaded-scrolling']});
 const context=await browser.newContext();const [name,value]=fixture.cookie.split('=');await context.addCookies([{name,value,url:base,httpOnly:true,sameSite:'Lax'}]);
 const page=await context.newPage();page.setDefaultTimeout(15000);
 const reviewCode=async code=>{await page.goto(base+'/device?code='+code);await page.getByRole('button',{name:'Continue',exact:true}).click();};
 await reviewCode(challenge.user_code);
 assert.ok((await page.textContent('body')).includes('Project: pair-local / Tag: '+tag));
 assert.equal(await page.locator('#agentMachine').getAttribute('class'),'dk-select');
 assert.equal(await page.locator('link[rel=stylesheet]').first().getAttribute('href'),'/vendor/dakit/dakit.css');
 await page.locator('#agentMachine').selectOption('pair-local-machine');
 page.on('dialog',dialog=>dialog.accept());
 const approved=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/device/approve/'+challenge.user_code);
 await page.getByRole('button',{name:'Approve',exact:true}).click();
 const approval=await approved;assert.equal(approval.status(),200);assert.equal((await approval.json()).machine_id,'pair-local-machine');
 console.log('PASS browser uses signed session, displayed project/tag, official dakit machine selector and CSRF approval');
 const tokenResponse=await poll();assert.equal(tokenResponse.status,200);const tokens=await tokenResponse.json();assert.equal(tokens.machine_id,'pair-local-machine');
 const request=async(token,path,method='GET',body)=>fetch(base+path,{method,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});
 const participants=await (await request(tokens.access_token,'/api/conversations/participants')).json();
 assert.equal(participants.self.actor,actor);assert.equal(participants.self.kind,'agent');
 assert.equal(participants.participants.find(p=>p.actor===actor).machine_id,'pair-local-machine');
 for(const path of ['/api/bus/inbox?actor=machine:pair-local-machine','/api/bus/inbox?actor=role:ops','/api/jobs'])assert.equal((await request(tokens.access_token,path)).status,403);
 console.log('PASS public-issued agent credential authenticates exact registered machine and cannot read machine/role inboxes or jobs');
 const refreshed=await fetch(base+'/api/token/refresh',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({grant_type:'refresh_token',refresh_token:tokens.refresh_token})});
 assert.equal(refreshed.status,200);const next=await refreshed.json();assert.equal(next.scope,'bus:read bus:send health:report');assert.equal(next.machine_id,'pair-local-machine');
 assert.equal((await request(tokens.access_token,'/api/conversations/participants')).status,401);
 const current=await (await request(next.access_token,'/api/conversations/participants')).json();assert.equal(current.self.actor,actor);assert.equal(current.participants.find(p=>p.actor===actor).machine_id,'pair-local-machine');
 console.log('PASS public refresh retains exact agent/registration/machine/scopes, old access token denied');
 // Real CLI subprocess: unmodified cli.main -> shared polling -> api.safe_http_json -> config.update_config.
 const pyEnv=(file)=>({...process.env,HUB_CONFIG_FILE:file,HUB_URL:'',HUB_BASE_URL:'',HUB_TOKEN:'',HUB_JOB_TOKEN:'',PYTHONPATH:resolve(root,'host'),PYTHONDONTWRITEBYTECODE:'1',...(process.env.DTC_AUTH_HOME?{DTC_AUTH_HOME:process.env.DTC_AUTH_HOME}:{})});
 async function python(script,file,extra={}) {
  return exec('python3',['-c',script],{cwd:nativeWorkspace||root,env:{...pyEnv(file),...extra},timeout:30000});
 }
 async function login(suffix,mid,replaceOldBinding=false) {
  const cliActor=suffix==='bound'?actor:'pair-local:cli-'+suffix;
  const dir=resolve(cliRoot,suffix);await mkdir(dir,{mode:0o700});const file=resolve(dir,'host.json');
  if(replaceOldBinding)await python("from agent_hub_host import config; config.update_config(machine_id='wrong-local-machine')",file);
  const child=execFile('python3',['-u','-m','agent_hub_host.cli','login',base,'--kind','agent','--name',cliActor],{cwd:nativeWorkspace||root,env:pyEnv(file),timeout:45000});children.add(child);
  let output='',errorOutput='';
  const done=new Promise((ok,fail)=>{child.on('error',fail);child.on('close',code=>{children.delete(child);ok({code,output,errorOutput});});});
  child.stderr.on('data',d=>errorOutput+=d);
  const verification=await new Promise((ok,fail)=>{
   const timer=setTimeout(()=>fail(new Error('CLI verification prompt missing')),15000);
   child.stdout.on('data',d=>{output+=d;const match=output.match(/Open in a browser:\s+(http[^\s]+)/);if(match){clearTimeout(timer);ok(match[1]);}});
   child.on('close',()=>{clearTimeout(timer);fail(new Error('CLI exited before verification prompt'));});
  });
  await page.goto(verification);await page.getByRole('button',{name:'Continue',exact:true}).click();await page.locator('#agentMachine').selectOption(mid||'');
  const approved=page.waitForResponse(r=>new URL(r.url()).pathname.startsWith('/api/device/approve/'));
  await page.getByRole('button',{name:'Approve',exact:true}).click();assert.equal((await approved).status(),200);
  const result=await done;
  // A completed authenticated exchange replaces the previous binding atomically.
  // config._TokenExchange separately rejects concurrent config writes.
  assert.equal(result.code,0);assert.ok(result.output.includes('Login successful.'));
  await python(`import os
from agent_hub_host import config,auth,conversations
c=config.load_config()
expected=os.environ['EXPECTED_MACHINE'] or None
assert c.get('machine_id')==expected and c['client_kind']=='agent' and c['client_name']==os.environ['EXPECTED_ACTOR']
s=conversations.request('GET','/api/conversations/participants')
assert s['self']['actor']==os.environ['EXPECTED_ACTOR']
row=next(p for p in s['participants'] if p['actor']==os.environ['EXPECTED_ACTOR'])
assert row['machine_id']==expected
old=c['access_token'];auth.refresh_token();c=config.load_config()
assert c['access_token']!=old and c.get('machine_id')==expected
assert conversations.request('GET','/api/conversations/participants')['self']['actor']==os.environ['EXPECTED_ACTOR']
if os.environ.get('OWN_UUID'):
 from agent_hub_host.conversation_bridge import ConversationBridge
 # The actual public CLI config supplies the token and must satisfy execute()'s machine precondition.
 assert c.get('machine_id')==expected and expected
 b=ConversationBridge(actor=os.environ['EXPECTED_ACTOR'],project='pair-local',workspace=os.environ['OWN_WORKSPACE'],machine_id=expected,conversation_ids=['local-qualification'],origin=c['base_url'])
 binding=b.qualify()
 assert binding.session_id==os.environ['OWN_UUID'] and binding.tag==os.environ['OWN_TAG']
 assert not b.path.exists() and not b.path.with_name(b.path.name+'.lock').exists()
`,file,{EXPECTED_MACHINE:mid||'',EXPECTED_ACTOR:cliActor,...(suffix==='bound'&&own?{OWN_UUID:own.id,OWN_TAG:own.tag,OWN_WORKSPACE:nativeWorkspace}:{})});
  console.log('PASS real public CLI '+suffix+' login persists authoritative binding automatically; real HTTP refresh and participants'+(suffix==='bound'&&own?'; genuine own UUID native qualify, no journal or inbox writes':''));
 }
 await login('bound','pair-local-machine');await login('null',null);await login('replace-old-binding','pair-local-machine',true);
 // A displayed machine-less choice stays null even if an admin registers a machine while the page is open.
 const stale=await (await fetch(base+'/api/device/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_kind:'agent',name:'pair-local:stale-ui',requested_scopes:['bus:read']})})).json();
 await reviewCode(stale.user_code);
 const registered=await fetch(base+'/api/agents',{method:'POST',headers:{Cookie:fixture.cookie,'Content-Type':'application/json','X-CSRF-Token':key},body:JSON.stringify({project:'pair-local',tag:'stale-ui',machine_id:'pair-local-machine'})});assert.equal(registered.status,201);
 const dialogs=[];page.on('dialog',d=>dialogs.push(d.message()));
 const denied=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/device/approve/'+stale.user_code);
 await page.getByRole('button',{name:'Approve',exact:true}).click();const rejection=await denied;assert.equal(rejection.status(),409);
 assert.equal((await rejection.json()).error,'agent_machine_conflict');await page.locator('#device-error:not([hidden])').waitFor();assert.ok((await page.locator('#device-error').textContent()).length > 0);
 console.log('PASS browser sends explicit null and truthfully reports rejection after concurrent registration');
 const bare=await (await fetch(base+'/api/device/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_kind:'agent',name:'bare-ui',requested_scopes:['bus:read']})})).json();
 await reviewCode(bare.user_code);assert.ok((await page.textContent('body')).includes('Project: (projectless) / Tag: bare-ui'));
 assert.equal((await fetch(base+'/api/agents',{method:'POST',headers:{Cookie:fixture.cookie,'Content-Type':'application/json','X-CSRF-Token':key},body:JSON.stringify({project:'pair-local',tag:'bare-ui',machine_id:'pair-local-machine'})})).status,201);
 const changed=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/device/approve/'+bare.user_code);
 await page.getByRole('button',{name:'Approve',exact:true}).click();const changedResponse=await changed;assert.equal(changedResponse.status(),400);assert.equal((await changedResponse.json()).error,'invalid_approval');await page.locator('#device-error:not([hidden])').waitFor();assert.ok((await page.locator('#device-error').textContent()).length > 0);
 console.log('PASS browser rejects changed bare-tag canonical identity rather than approving the new project');
 const expired=await (await fetch(base+'/api/device/code',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({client_kind:'agent',name:'pair-local:expired-ui',requested_scopes:['bus:read']})})).json();
 await reviewCode(expired.user_code);
 await fetch(base+'/__pairing_fixture',{method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json'},body:JSON.stringify({action:'expire',code:expired.user_code})});
 const expiration=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/device/approve/'+expired.user_code);
 await page.getByRole('button',{name:'Approve',exact:true}).click();assert.equal((await expiration).status(),400);await page.getByRole('heading',{name:'Code expired',exact:true}).waitFor();
 const missing=await page.goto(base+'/device?code=NOPE-NOPE');assert.equal(missing.status(),404);assert.ok((await page.textContent('body')).includes('Code not found'));
 console.log('PASS browser pending/approved/expired/error paths use actual public responses');
} finally {for(const child of children)child.kill();if(browser)await browser.close();await mf.dispose();await rm(cliRoot,{recursive:true,force:true});}


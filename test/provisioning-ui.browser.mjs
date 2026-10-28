// Real Worker + SQLite browser test for provider consent relay. No provider/model calls.
import assert from 'node:assert/strict';
import {build} from 'esbuild';
import {Miniflare,Log,LogLevel,convertV4MiniflareOptions} from 'miniflare';
import { launchBrowser } from './playwright.mjs';
import {readFile,mkdir} from 'node:fs/promises';
import {resolve,extname} from 'node:path';
const root=resolve(import.meta.dirname,'..');
const bundle=await build({entryPoints:[resolve(root,'test/provisioning-docker-worker.ts')],bundle:true,write:false,format:'esm',platform:'browser',external:['cloudflare:workers']});
const mime={'.html':'text/html','.css':'text/css','.js':'text/javascript','.woff2':'font/woff2'};
const mf=new Miniflare(convertV4MiniflareOptions({host:'127.0.0.1',port:0,log:new Log(LogLevel.ERROR),workers:[{
 name:'provisioning-ui-local',script:bundle.outputFiles[0].text,modules:true,compatibilityDate:'2024-04-05',compatibilityFlags:['nodejs_compat'],durableObjects:{HUB:{className:'Hub',useSQLite:true}},bindings:{SESSION_SECRET:'synthetic-browser-only-session-secret-32',HUB_ADMINS:'admin@fake.local',AUTH_BASE_URL:'https://auth.fake.local',AUTH_CALLBACK_URL:'https://auth.fake.local/auth/callback',AUTH_CLIENT_ID:'local-only'},
 serviceBindings:{ASSETS:async r=>{const f=resolve(root,'public','.'+new URL(r.url).pathname);assert.ok(f.startsWith(resolve(root,'public')+'/'));try{return new Response(await readFile(f),{headers:{'Content-Type':mime[extname(f)]||'application/octet-stream'}});}catch{return new Response(null,{status:404});}}}
}]}));
let browser;
try {
 const base=(await mf.ready).origin;
 const fixture=await fetch(base+'/__docker_fixture',{method:'POST',headers:{Authorization:'Bearer local-docker-fixture-only','Content-Type':'application/json'},body:'{}'}).then(r=>r.json());
 const admin={Cookie:fixture.cookie,'X-CSRF-Token':'docker-csrf'};
 async function api(path,method='GET',data,headers=admin){const r=await fetch(base+path,{method,headers:{'Content-Type':'application/json',...headers},...(data?{body:JSON.stringify(data)}:{})});assert.equal(r.status,200,path);return r.json();}
 assert.equal((await fetch(base+'/engines.html',{redirect:'manual'})).status,303);
 const code=await api('/api/device/code','POST',{client_kind:'host',name:'Browser test host',hostname:'local',requested_scopes:['host']});
 await api('/api/device/approve/'+code.user_code,'POST',{granted_scopes:['host']});
 const token=await api('/api/device/token','POST',{grant_type:'urn:ietf:params:oauth:grant-type:device_code',device_code:code.device_code});
 const host={Authorization:'Bearer '+token.access_token};
 await api('/api/machines/register','POST',{hostname:'local',os:'linux',network:'public',roles:[],capabilities:{}},host);
 const added=await api('/api/engines/my-google','PUT',{label:'my-google',provider:'gemini',version:'0.64.0',auth_method:'machine-login',files:{},env:{},args:[]});
 await api('/api/engines/my-google','POST',{revision:added.revision,authenticated:false},host);
 browser=await launchBrowser({args:['--no-sandbox','--disable-dev-shm-usage']});
 const context=await browser.newContext({viewport:{width:1000,height:800}});
 const index=fixture.cookie.indexOf('=');await context.addCookies([{name:fixture.cookie.slice(0,index),value:fixture.cookie.slice(index+1),url:base}]);
 const page=await context.newPage();await page.goto(base+'/engines.html');
 await page.getByRole('button',{name:'Sign in with Google'}).click();
 let logins=(await api('/api/engines')).logins;assert.equal(logins.length,1);
 await api('/api/engines/logins/'+logins[0].id,'POST',{status:'waiting',url:'https://accounts.google.com/o/oauth2/v2/auth?state=synthetic-ui-state'},host);
 await page.getByRole('link',{name:'Open Google sign-in'}).waitFor();
 await mkdir(resolve(root,'.local/ui-evidence'),{recursive:true});await page.screenshot({path:resolve(root,'.local/ui-evidence/engine-consent.png'),fullPage:true});
 await page.getByLabel('Authorization code').fill('synthetic-onetime-code');await page.getByRole('button',{name:'Complete sign-in'}).click();
 await page.getByText('submitted',{exact:true}).waitFor();
 assert.equal((await api('/api/engines/logins/'+logins[0].id,'GET',undefined,host)).code,'synthetic-onetime-code');
 assert.ok(!JSON.stringify(await api('/api/engines')).includes('synthetic-onetime-code'));
 await api('/api/engines/logins/'+logins[0].id,'POST',{status:'authenticated'},host);
 await page.getByText('authenticated',{exact:true}).waitFor();
 console.log('PASS: real browser native-login request, provider link, private code submission and authenticated state; screenshot .local/ui-evidence/engine-consent.png');
} finally {await browser?.close();await mf.dispose();}

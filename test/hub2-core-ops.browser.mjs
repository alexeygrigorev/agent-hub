// Exercise dashboard controls and receipt verification; no live credentials or sessions.
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import {launchBrowser} from './playwright.mjs';
const html=await readFile(new URL('../hub2/public/index.html',import.meta.url),'utf8');
const browser=await launchBrowser({headless:true});
try{
 const page=await browser.newPage(),requests=[];let item={id:'intake-smoke',title:'Smoke intake',status:'open',updated_at:1},command=null,fail=false;
 await page.route('https://hub.test/**',async route=>{
  const r=route.request(),url=new URL(r.url()),body=r.postDataJSON();requests.push({path:url.pathname,method:r.method(),body});
  if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
  let data={};let status=200;
  if(url.pathname==='/auth/session'){status=401;}
  else if(url.pathname==='/api/repos')data={items:[{canonical_name:'owner/repo'}],next:null};
  else if(url.pathname==='/api/intakes'&&r.method()==='GET')data={items:[item],next:null};
  else if(url.pathname==='/api/intakes'&&r.method()==='POST'){data={intake:item};status=201;}
  else if(url.pathname==='/api/intakes/intake-smoke'&&r.method()==='POST'){
   if(fail){data={error:'intake_version_conflict'};status=409;}
   else{item={...item,...body,status:body.status==='done'?'archived':item.status,updated_at:item.updated_at+1};data={intake:item};}
  }
  else if(url.pathname==='/api/intakes/intake-smoke')data={intake:item};
  else if(url.pathname.endsWith('/append'))data={appended:true};
  else if(url.pathname==='/api/quota')data={pools:[{provider:'codex',account:'test',state:'fresh',remaining_percent:80,observed_at:1,reset_at:null}]};
  else if(url.pathname==='/api/fleet')data={items:[{id:'host',connected:true}],next:null};
  else if(url.pathname==='/api/fleet/routing/machines')data={machines:[{machine_id:'host',capabilities:['goal:codex']}]};
  else if(url.pathname==='/api/sessions'&&r.method()==='POST')data={id:'session-smoke',state:'queued'};
  else if(url.pathname==='/api/sessions')data={items:[{id:'session-smoke',state:'running',engine:'codex'}],next:null};
  else if(url.pathname==='/api/sessions/session-smoke')data={session:{id:'session-smoke',native_id:'native-smoke',state:'running'}};
  else if(url.pathname.endsWith('/commands'))data={commands:[command]};
  else if(/\/(send|capture|stop)$/.test(url.pathname)){const kind=url.pathname.split('/').at(-1);command={id:'command-smoke',kind,state:'acked',ack:kind==='capture'?{capture:'CORE_OPS_ACK'}:kind==='stop'?{outcome:'delivered',confirmed_exit:true}:{outcome:'delivered',correlation_id:'real-fixture-correlation'}};data={command:{id:command.id}};}
  else if(url.pathname.endsWith('/append'))data={appended:true};
  else throw new Error('Unexpected request '+r.method()+' '+url.pathname);
  await route.fulfill({status,contentType:'application/json',body:JSON.stringify(data)});
 });
 await page.goto('https://hub.test/');await page.locator('#token').fill('test-token-'.repeat(4));await page.getByRole('button',{name:'Connect',exact:true}).click();
 await page.getByRole('button',{name:'Open intake'}).click();await page.locator('#detail input[name=title]').fill('Updated smoke intake');await page.getByRole('button',{name:'Update intake'}).click();await page.getByText('Intake updated.',{exact:true}).waitFor();
 await page.locator('#detail textarea[name=note]').fill('Evidence note');await page.getByRole('button',{name:'Append note'}).click();await page.getByText('Note appended.',{exact:true}).waitFor();
 fail=true;await page.getByRole('button',{name:'Update intake'}).click();await page.getByText(/409 · intake_version_conflict/).waitFor();fail=false;
 await page.locator('#detail input[name=reason]').fill('Disposable smoke complete');await page.getByRole('button',{name:'Close intake'}).click();await page.getByText('Intake closed.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Fleet',exact:true}).click();await page.getByRole('button',{name:'Routing machines'}).click();await page.locator('#detail').getByText(/goal:codex/).waitFor();
 await page.getByRole('button',{name:'Quota',exact:true}).click();await page.getByText(/80% remaining/).waitFor();
 await page.getByRole('button',{name:'Sessions',exact:true}).click();await page.locator('#startSession summary').click();await page.locator('#sessionForm select').selectOption('goal');await page.locator('#sessionForm textarea').fill('Disposable goal smoke');await page.getByRole('button',{name:'Start session',exact:true}).click();await page.getByRole('button',{name:'Capture session'}).waitFor();
 const start=requests.find(r=>r.path==='/api/sessions'&&r.method==='POST').body;assert.equal(start.with_goal,true);assert.equal(start.mode,'interactive');assert.equal(start.brief,'Disposable goal smoke');assert(!('engine'in start));assert(!('model'in start));
 await page.locator('#detail textarea[name=body]').fill('Authorized smoke steer');await page.getByRole('button',{name:'Send steer'}).click();await page.getByText('send acknowledged by host.',{exact:true}).waitFor();
 await page.getByRole('button',{name:'Refresh view'}).click(); // requires machine context; select Fleet host as actual users do.
 await page.getByRole('button',{name:'Fleet',exact:true}).click();await page.getByRole('button',{name:'View sessions'}).click();
 await page.getByRole('button',{name:'Open session'}).click();await page.getByRole('button',{name:'Capture session'}).click();await page.getByText('capture acknowledged by host.',{exact:true}).waitFor();await page.getByText(/CORE_OPS_ACK/).waitFor();
 await page.getByRole('button',{name:'Refresh view'}).click();await page.getByRole('button',{name:'Open session'}).click();await page.getByRole('button',{name:'Stop session'}).click();await page.getByText('stop acknowledged by host.',{exact:true}).waitFor();
 console.log('PASS Hub2 dashboard intake edits, notes, close, visible failure, routing, quota, default goal start, steer ack, capture, confirmed stop');
}finally{await browser.close();}

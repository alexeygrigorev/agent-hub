import assert from 'node:assert/strict';
import { launchBrowser } from './playwright.mjs';
import { readFile, mkdir } from 'node:fs/promises';
import { resolve, extname } from 'node:path';

const browser = await launchBrowser({ args: ['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--num-raster-threads=1'] });
const end = Date.parse('2026-10-24T15:00:00Z'), start = end - 86400000;
const series = [];
for (const [kind, entity, machine_id, metric, value] of [
 ['host','host-a','host-a','memory_used_gib',22],['host','host-a','host-a','memory_available_gib',40],['reservation','host-a','host-a','reserved_gib',4],
 ['host','host-a','host-a','load1',1.2],['host','host-a','host-a','load15',2.1],['disk','/','host-a','percent',82],['disk','/data','host-a','percent',20],
 ['sessions','fleet','host-a','running',40],['sessions','fleet','host-a','queued',0],['sessions','fleet','host-a','failed',3],
 ['account','account-a','','used',8],['account','account-a','','max',26],['quota','account-a','','remaining_percent',85],
]) series.push({ kind,entity,machine_id,metric, points: Array.from({length:12},(_,i)=>({at:start+i*7200000,value:i===5?null:value,observed_at:i===5?null:new Date(start+i*7200000).toISOString()})) });
const history = {range:'24h',start:new Date(start).toISOString(),end:new Date(end).toISOString(),series,note:'Observations only. Gaps are unknown.'};
await mkdir('.local/host-charts', {recursive:true});
try {
 for (const file of ['fleet','metrics']) for (const [width,theme] of [[1440,'light'],[1440,'dark'],[390,'light'],[390,'dark']]) {
  const context=await browser.newContext({viewport:{width,height:1000},colorScheme:theme,hasTouch:width===390});
  let range='24h', mode='ok';
  await context.route('**/*',async route=>{
   const u=new URL(route.request().url());
   assert.equal(u.origin,'http://history.fixture');
   if(u.pathname==='/auth/me')return route.fulfill({json:{email:'fixture@example.invalid',csrf_token:'fixture'}});
   if(u.pathname==='/api/monitoring/history'){
    if(file==='metrics') assert.equal(route.request().headers()['x-csrf-token'],'fixture'); range=u.searchParams.get('range');
    return mode==='error'?route.fulfill({status:503}):route.fulfill({json:{...history,range}});
   }
   if(u.pathname==='/api/fleet')return route.fulfill({json:{machines:[],accounts:[],routes:[],agents:[],sessions:[],subscriptions:[],launches:[]}});
   if(u.pathname==='/api/liveness')return route.fulfill({json:{subjects:[],alerts:[]}});
   if(u.pathname==='/api/checks')return route.fulfill({json:{checks:[]}});
   if(u.pathname==='/api/metrics')return route.fulfill({json:{items:[],next_cursor:null}});
   if(u.pathname.startsWith('/api/'))return route.fulfill({json:{machines:[],hosts:[],items:[]}});
   try{return route.fulfill({body:await readFile(resolve('public','.'+u.pathname)),contentType:({'.html':'text/html','.js':'application/javascript','.css':'text/css','.woff2':'font/woff2'})[extname(u.pathname)]||'text/plain'});}catch{return route.fulfill({status:404});}
  });
  const page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://history.fixture/${file}.html#host-health`);
  await page.waitForSelector('#host-history svg');
  assert.equal(await page.locator('#host-history svg').count(),6);
  assert.equal(await page.locator('#host-history .metrics-line').count(),14);
  const path=await page.locator('#host-history .metrics-line').first().getAttribute('d');
  assert.equal((path.match(/M/g)||[]).length,2,'missing observations break line');
  assert.ok(path.includes('L'),'real LINE chart');
  assert.ok(await page.locator('#host-history [data-value="0"]').count()>0,'measured zeros shown');
  assert.match(await page.locator('#host-history [data-series="MemAvailable floor · 10 GiB"] .metrics-point').first().getAttribute('aria-label'),/reference threshold/);
  const point=page.locator('#host-history .metrics-point').first();
  await point.focus();assert.match(await page.locator('#host-history figcaption').first().textContent(),/UTC.*GiB/);
  if(width===390){await point.tap();assert.match(await page.locator('#host-history figcaption').first().textContent(),/UTC/);}
  for(const value of ['1h','7d','30d','24h']){
   await page.getByLabel('Resource history range',{exact:true}).selectOption(value);
   await page.waitForFunction(v=>document.querySelector('#host-history [role="status"]').textContent.startsWith(v+' ·'),value);
   assert.equal(range,value);
  }
  await page.evaluate(t=>document.documentElement.dataset.theme=t,theme);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'no horizontal overflow');
  await page.screenshot({path:`.local/host-charts/${file}-${width}-${theme}.png`,fullPage:false});
  mode='error';await page.getByLabel('Resource history range',{exact:true}).selectOption('1h');
  await page.waitForFunction(()=>document.querySelector('#host-history [role="status"]').textContent.includes('unavailable'));
  assert.equal(await page.locator('#host-history svg').count(),0,'failed reload clears stale charts');
  assert.deepEqual(errors,[]);
  await context.close();
 }
 console.log('Host history browser matrix passed: both pages, desktop/mobile, light/dark, gaps, zero, touch, ranges, errors.');
}finally{await browser.close();}

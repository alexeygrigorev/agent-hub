import { test } from 'node:test';
import assert from 'node:assert/strict';
import { pickModelRoute, weightedChoice } from '../src/model-picker.ts';

const candidates=[
 {id:'codex',engine:'codex',model:'gpt-6.1-sol',effort:'medium',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'claude',engine:'claude',model:'sonnet',effort:'medium',account_id:'claude',weight:1,capabilities:[],enabled:true},
 {id:'luna',engine:'codex',model:'gpt-6-luna',effort:'max',account_id:'codex',weight:1,capabilities:[],enabled:true},
 {id:'sol-high',engine:'codex',model:'gpt-6.1-sol',effort:'max',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'astra-high',engine:'codex',model:'gpt-6-astra',effort:'max',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'argon-high',engine:'gemini',model:'gemini-4-argon',effort:'max',account_id:'gemini',weight:100,capabilities:[],enabled:true},
 {id:'luna-medium',engine:'codex',model:'gpt-6-luna',effort:'medium',account_id:'codex',weight:100,capabilities:[],enabled:true},
 {id:'zai-central',engine:'zcodex',model:'glm-flash',account_id:'zai-central',weight:100,capabilities:[],enabled:true},
 {id:'haiku',engine:'claude',model:'haiku',effort:'medium',account_id:'claude',weight:100,capabilities:[],enabled:true},
 {id:'claude-sonnet-high',engine:'claude',model:'sonnet',effort:'max',account_id:'claude',weight:100,capabilities:[],enabled:true},
 {id:'claude-fable-5-2',engine:'claude',model:'fable',effort:'medium',account_id:'claude',weight:100,capabilities:[],enabled:true},
];

function envFor(quota:Record<string,number>,credentialUsable=false){
 const requests:string[]=[];
 const namespace=(kind:string)=>({idFromName:(id:string)=>id,get:(id:string)=>({fetch:async(url:string)=>{
  const parsed=new URL(url);requests.push(kind+':'+parsed.pathname+parsed.search);
  if(kind==='control'&&parsed.pathname==='/_routing-policy')return Response.json({version:1,candidates});
  if(kind==='control'&&parsed.pathname==='/api/directory')return Response.json({items:[{id:'h1'}],next:null});
  if(kind==='accounts'){
   return Response.json({account:{enabled:true},quota:{remaining:quota[id]??0},hosts:['h1'],credential_usable:credentialUsable});
  }
  if(kind==='machines')return Response.json({machine_id:'h1',engines:['codex','claude','zcodex']});
  return Response.json({}, {status:404});
 }})});
 return {env:{CONTROL:namespace('control'),ACCOUNTS:namespace('accounts'),MACHINES:namespace('machines')} as any,requests};
}

test('weighted choice follows quota headroom and can select a non-default engine',async()=>{
 const {env,requests}=envFor({codex:10,claude:90},false);
 const route=await pickModelRoute(env,{interactive:true},()=>0.5);
 assert.equal(route.policy_id,'claude');
 assert.equal(route.engine,'claude');
 assert.equal(route.effort,'medium');
 assert.ok(requests.some(value=>value.includes('/_routing-policy')));
});

test('unhealthy credentials and missing quota do not become placement refusals',async()=>{
 const {env}=envFor({},false);
 const route=await pickModelRoute(env,{interactive:true,model:'gpt-6.1-sol',override_reason:'test explicit model'},()=>0);
 assert.equal(route.policy_id,'codex');
 assert.equal(route.quota_remaining,0);
});

test('Fable is excluded from automatic picks and available only when explicitly pinned',async()=>{
 const {env}=envFor({codex:10,claude:90},false);
 const automatic=await pickModelRoute(env,{interactive:true},()=>0.999999);
 assert.notEqual(automatic.policy_id,'claude-fable-5-2');
 const explicit=await pickModelRoute(env,{interactive:true,model:'claude-fable-5-2',override_reason:'Explicit request for Claude Fable 5.2'},()=>0);
 assert.equal(explicit.policy_id,'claude-fable-5-2');
 assert.equal(explicit.model,'fable');
 assert.equal(explicit.effort,'medium');
});

test('fallback can exclude failed candidates while enforcing model policy',async()=>{
 const {env}=envFor({codex:10,claude:90},false);
 const route=await pickModelRoute(env,{interactive:true,model:'gpt-6.1-sol',override_reason:'test explicit model',exclude_policy_ids:['claude']},()=>0);
 assert.equal(route.policy_id,'codex');
 assert.equal(route.model,'gpt-6.1-sol');
 assert.notEqual(route.policy_id,'zai-central');
 assert.notEqual(route.policy_id,'haiku');
 assert.notEqual(route.policy_id,'sol-high');
 assert.notEqual(route.policy_id,'astra-high');
 assert.notEqual(route.policy_id,'argon-high');
 assert.notEqual(route.policy_id,'luna-medium');
 assert.notEqual(route.policy_id,'claude-sonnet-high');
});

test('weightedChoice honors weights deterministically',()=>{
 assert.equal(weightedChoice([{value:'small',weight:1},{value:'large',weight:3}],()=>0.9),'large');
});

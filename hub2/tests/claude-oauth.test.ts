import {test} from 'node:test';
import assert from 'node:assert/strict';
import {CLAUDE_OAUTH,claudeAuthorization,claudeExchange,loginRefusal} from '../src/claude-oauth';

const now=1_700_000_000_000;
test('PKCE authorization binds independent verifier and state to the maintained endpoint and scopes',async()=>{
 let draw=0;
 const result=await claudeAuthorization(length=>new Uint8Array(length).fill(++draw));
 const url=new URL(result.url);
 assert.equal(draw,2);assert.notEqual(result.verifier,result.state);
 assert.equal(url.origin+url.pathname,CLAUDE_OAUTH.authorize);
 assert.equal(url.searchParams.get('client_id'),CLAUDE_OAUTH.client);
 assert.equal(url.searchParams.get('redirect_uri'),CLAUDE_OAUTH.redirect);
 assert.equal(url.searchParams.get('scope'),CLAUDE_OAUTH.scopes.join(' '));
 assert.equal(url.searchParams.get('response_type'),'code');assert.equal(url.searchParams.get('code'),'true');
 assert.equal(url.searchParams.get('state'),result.state);
 assert.equal(url.searchParams.get('code_challenge_method'),'S256');
 const expected=Buffer.from(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(result.verifier))).toString('base64url');
 assert.equal(url.searchParams.get('code_challenge'),expected);
 assert.equal(url.searchParams.has('code_verifier'),false);
});

test('exchange preserves request contract, rotated token, expiry, scopes and profile metadata',async()=>{
 for(const [organization_type,subscriptionType] of [['claude_max','max'],['claude_pro','pro']]){
  const calls:{url:string;init:RequestInit|undefined}[]=[];
  const result=await claudeExchange(' code#state ','verifier','state',async(url,init)=>{
   calls.push({url,init});
   return calls.length===1?Response.json({access_token:'synthetic-access',refresh_token:'synthetic-refresh',expires_in:3600,scope:'user:profile user:inference'}):Response.json({organization:{organization_type,rate_limit_tier:'synthetic-tier'}});
  },now);
  assert.equal(result.ok,true);if(!result.ok)throw new Error('expected exchange');
  assert.deepEqual(calls.map(call=>call.url),[CLAUDE_OAUTH.token,CLAUDE_OAUTH.profile]);
  assert.equal(calls[0].init?.method,'POST');assert.equal(calls[0].init?.redirect,'manual');
  assert.deepEqual(JSON.parse(String(calls[0].init?.body)),{grant_type:'authorization_code',code:'code',state:'state',redirect_uri:CLAUDE_OAUTH.redirect,client_id:CLAUDE_OAUTH.client,code_verifier:'verifier'});
  assert.equal(new Headers(calls[0].init?.headers).get('Content-Type'),'application/json');
  assert.equal(new Headers(calls[1].init?.headers).get('Authorization'),'Bearer synthetic-access');
  assert.equal(new Headers(calls[1].init?.headers).get('anthropic-beta'),'oauth-2025-04-20');
  assert(calls.every(call=>call.init?.signal instanceof AbortSignal));
  assert.deepEqual(JSON.parse(result.files['.claude/.credentials.json']),{claudeAiOauth:{accessToken:'synthetic-access',refreshToken:'synthetic-refresh',expiresAt:now+3600_000,scopes:['user:profile','user:inference'],subscriptionType,rateLimitTier:'synthetic-tier'}});
 }
});

test('state mismatch refuses before provider access and code-only paste remains compatible',async()=>{
 let calls=0;
 const fetcher=async()=>{calls++;return Response.json({access_token:'synthetic'});};
 assert.deepEqual(await claudeExchange('code#wrong','verifier','state',fetcher,now),{ok:false,invalid:true,reason:'login_state_mismatch'});
 assert.deepEqual(await claudeExchange('','verifier','state',fetcher,now),{ok:false,invalid:true,reason:'login_state_mismatch'});
 assert.equal(calls,0);
 const result=await claudeExchange('code','verifier','state',fetcher,now);
 assert.equal(result.ok,true);assert.equal(calls,2);
});

test('profile outage does not discard a successfully exchanged credential or invent subscription metadata',async()=>{
 let calls=0;
 const result=await claudeExchange('code#state','verifier','state',async()=>{
  if(++calls===1)return Response.json({access_token:'synthetic'});
  throw new Error('private-provider-body');
 },now);
 assert.equal(result.ok,true);if(!result.ok)throw new Error('expected exchange');
 assert.deepEqual(JSON.parse(result.files['.claude/.credentials.json']),{claudeAiOauth:{scopes:CLAUDE_OAUTH.scopes,accessToken:'synthetic'}});
});

test('token refusal and transport errors retain bounded sanitized classifications',async()=>{
 for(const [status,body,expected] of [
  [403,'<html>private-challenge</html>',{invalid:false,detail:'egress_blocked',blocked:true}],
  [400,JSON.stringify({error:'invalid_grant',error_description:'private-description'}),{invalid:true,detail:'invalid_grant',blocked:false}],
  [401,JSON.stringify({error:'private secret with spaces'}),{invalid:false,detail:'egress_blocked',blocked:true}],
  [503,'private-provider-error',{invalid:false,detail:'provider_unavailable',blocked:false}],
 ] as const){
  const result=await claudeExchange('code#state','verifier','state',async()=>new Response(body,{status}),now);
  assert.deepEqual(result,{ok:false,reason:'login_code_rejected',providerStatus:status,...expected});
  assert(!JSON.stringify(result).includes('private'));
 }
 assert.deepEqual(await claudeExchange('code','verifier','state',async()=>Response.json({refresh_token:'private'}),now),{ok:false,invalid:false,reason:'login_invalid_response',providerStatus:200,detail:'invalid_token_response'});
 assert.deepEqual(await claudeExchange('code','verifier','state',async()=>{throw new Error('private-network-error');},now),{ok:false,invalid:false,reason:'login_unreachable',detail:'egress_unreachable',blocked:true});
});

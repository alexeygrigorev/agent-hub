import {test} from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import {startServer} from '../node/server.ts';
import '../node/compat.ts';
import worker from '../src/worker.ts';

test('Node restores configured HTTPS public origin without trusting forwarded headers',async()=>{
 const seen=[];const responses=[];
 const server=await startServer({async dispose(){},fetch:async request=>{seen.push({url:request.url,origin:request.headers.get('origin')});return worker.fetch(request,{CONTROL:{idFromName:id=>id,get:()=>({fetch:async()=>Response.json({error:'unauthorized'},{status:401})})}});}},
  {host:'127.0.0.1',port:0,publicOrigin:'https://hub.test'});
 async function call(headers){await new Promise((resolve,reject)=>{
  const r=http.request({host:'127.0.0.1',port:server.port,path:'/api/intakes?project=owner%2Frepo',method:'POST',headers:{authorization:'invalid',...headers}},response=>{let text='';response.on('data',chunk=>text+=chunk);response.on('end',()=>{responses.push({status:response.statusCode,body:JSON.parse(text)});resolve();});});r.on('error',reject);r.end('{}');
 });}
 try{
  await call({host:'hub.test',origin:'https://hub.test'});
  await call({host:'hub.test',origin:'https://evil.test','x-forwarded-host':'evil.test','x-forwarded-proto':'http'});
  await call({host:'localhost',origin:'https://hub.test','x-forwarded-host':'hub.test','x-forwarded-proto':'https'});
  assert.equal(seen[0].url,'https://hub.test/api/intakes?project=owner%2Frepo');
  assert.equal(seen[1].url,seen[0].url);assert.equal(seen[1].origin,'https://evil.test');
  assert.equal(seen[2].url,seen[0].url);
  assert.equal(responses[0].status,401,'same-origin reaches authentication');assert.equal(responses[1].status,403);assert.equal(responses[1].body.error,'csrf_refused');
 }finally{await server.close();}
});

test('Node keeps loopback origin when no canonical public origin is configured',async()=>{
 let observed;
 const server=await startServer({async dispose(){},async fetch(request){observed=request.url;return Response.json({ok:true});}},{host:'127.0.0.1',port:0});
 try{const response=await fetch(`http://127.0.0.1:${server.port}/health`);assert.equal(response.status,200);assert.equal(observed,`http://127.0.0.1:${server.port}/health`);}finally{await server.close();}
});

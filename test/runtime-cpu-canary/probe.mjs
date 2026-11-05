// Isolated runtime measurement only. Never mount on Hub production namespaces.
const small=()=>burn(10000);
function burn(iterations=1000000000){let value=17;for(let i=0;i<iterations;i++)value=Math.imul(value^i,1664525)+1013904223|0;return value;}
export class CPUProbe {
 constructor(ctx,env){this.ctx=ctx;this.env=env;}
 async ticket(kind){const key='burn:'+kind;return this.ctx.storage.transaction(async tx=>{if(await tx.get(key))return false;await tx.put(key,1);return true;});}
 async fetch(request){try{return await this.handle(request);}catch(error){return Response.json({probe_error:String(error?.message||error).slice(0,240)},{status:503});}}
 async handle(request){
  const path=new URL(request.url).pathname;
  if(path==='/smoke')return Response.json({ok:true,checksum:small(),source:this.env.SOURCE_SHA});
  if(path==='/status'){const keys=['burn:worker','burn:do','burn:ws','alarm:started','alarm:completed','alarm:recovered','ws:completed'];return Response.json(Object.fromEntries(await this.ctx.storage.get(keys)));}
  if(path==='/ticket')return Response.json({allowed:await this.ticket('worker')});
  if(path==='/burn'){if(!await this.ticket('do'))return new Response('already used',{status:409});const checksum=burn();return Response.json({completed:true,checksum});}
  if(path==='/arm'){if(await this.ctx.storage.get('alarm:started'))return new Response('already used',{status:409});await this.ctx.storage.setAlarm(Date.now()+200);return Response.json({armed:true});}
  if(path==='/ws'&&request.headers.get('Upgrade')==='websocket'){
   for(const socket of this.ctx.getWebSockets('probe'))socket.close(1000,'replace probe');
   const pair=new WebSocketPair();this.ctx.acceptWebSocket(pair[1],['probe']);
   return new Response(null,{status:101,webSocket:pair[0]});
  }
  return new Response('not found',{status:404});
 }
 async alarm(){
  if(await this.ctx.storage.get('alarm:started')){await this.ctx.storage.put('alarm:recovered',1);await this.ctx.storage.deleteAlarm();return;}
  await this.ctx.storage.put('alarm:started',1);burn();await this.ctx.storage.put('alarm:completed',1);
 }
 async webSocketMessage(socket,message){
  if(message==='smoke'){socket.send(JSON.stringify({ok:true,checksum:small()}));return;}
  if(message!=='burn'||!await this.ticket('ws')){socket.close(1000,'probe complete');return;}
  burn();await this.ctx.storage.put('ws:completed',1);socket.send('unexpected completion');
 }
 webSocketError(socket){socket.close();}
}
export default {async fetch(request,env){
 if(!env.CANARY_KEY||request.headers.get('Authorization')!=='Bearer '+env.CANARY_KEY)return Response.json({error:'forbidden',secret_bound:typeof env.CANARY_KEY==='string',source:env.SOURCE_SHA},{status:403});
 const path=new URL(request.url).pathname,stub=env.PROBE.get(env.PROBE.idFromName('single-synthetic-probe'));
 if(path==='/smoke')return Response.json({ok:true,checksum:small(),source:env.SOURCE_SHA});
 if(path==='/worker-burn'){
  const response=await stub.fetch('https://probe/ticket');
  if(!response.ok)return response;
  const ticket=await response.json();
  if(!ticket.allowed)return new Response('already used',{status:409});
  return Response.json({completed:true,checksum:burn()});
 }
 if(['/do-smoke','/do-burn','/do-status','/do-arm','/ws'].includes(path)){
  const target={'/do-smoke':'/smoke','/do-burn':'/burn','/do-status':'/status','/do-arm':'/arm','/ws':'/ws'}[path];
  try{return await stub.fetch(new Request('https://probe'+target,request));}
  catch(error){return Response.json({error:String(error?.message||error).slice(0,240)},{status:503});}
 }
 return new Response('not found',{status:404});
}};

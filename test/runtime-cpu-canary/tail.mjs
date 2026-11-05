// Cloudflare TraceItem fields: cpuTime/wallTime, not JS clocks.
// Never forward headers, logs, request bodies, exceptions, URLs or credentials.
export default {async tail(events,env){
 if(!env.PROBE)return;
 const records=[];
 for(const trace of events.slice(0,16)){
  if(trace.scriptName!==env.PRODUCER_NAME)continue;
  const event=trace.event;let path='',kind='';
  if(event?.request){try{path=new URL(event.request.url).pathname;}catch{continue;}
   if(!['/worker-burn','/do-burn','/burn'].includes(path))continue;
   kind=trace.executionModel==='durableObject'?'do_http':'worker_http';
  }else if(event?.scheduledTime)kind='alarm';
  else if(event?.getWebSocketEvent)kind='websocket';
  else continue;
  if(![trace.cpuTime,trace.wallTime].every(v=>typeof v==='number'&&Number.isFinite(v)&&v>=0))continue;
  records.push({kind,path,outcome:String(trace.outcome).slice(0,40),cpu_ms:trace.cpuTime,wall_ms:trace.wallTime});
 }
 if(records.length)await env.PROBE.get(env.PROBE.idFromName('single-synthetic-probe')).fetch('https://probe/metrics',{method:'POST',body:JSON.stringify({records})});
}};

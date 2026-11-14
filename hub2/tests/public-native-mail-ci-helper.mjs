import fs from 'node:fs';
import {join, resolve, dirname, isAbsolute, delimiter} from 'node:path';
import {createHash} from 'node:crypto';
import {spawn} from 'node:child_process';
import {performance} from 'node:perf_hooks';

export const PINS = Object.freeze({
  supervisor:'bae18a769104e6358e87bece0e80789c02d09721934a7ed238d6c13211649369',
  backend:'6eafb410b9ff4426b4aca4126969f3c120c87960c1ce43307252f237ba58e4fb',
  driver:'c4255e1c6f6558a4cfa6b6b387105585c444d932cd2540f0b141adcfb7ec1ddc',
  adapter:'e56b2c5bb41b46c69caac7d1553082ad461683f02de991fea704ec25a80bede1',
  factory:'54f844e6254ef31e72d9d2eabee931ef6a0be997cd283afb4ebdfc773cb1cd68',
  tests:'c0c363cd9f89189c48322aad4cd67e2d3f79a92e8ba88f61f0b27c78b86b31bc',
});
export const SOURCES = Object.freeze(['hub2/tests/node-fixture.mjs','hub2/tests/core-integration-worker.ts',
  'hub2/node/compat.ts','hub2/node/runtime.ts','hub2/node/server.ts','hub2/host/agent_hub_host/agent.py',
  'hub2/host/agent_hub_host/messages.py','hub2/host/agent_hub_host/client.py','hub2/host/agent_hub_host/transport.py',
  'host/agent_hub_host/cli.py','host/agent_hub_host/msg_cli.py','host/agent_hub_host/config.py','host/agent_hub_host/bus.py']);
export const PROBE = 'import json,sys,websockets; print(json.dumps({"python":list(sys.version_info[:2]),"websockets":websockets.__version__}))';
export function need(value, reason) {if(!value)throw new Error(reason);}
export function digest(raw) {return createHash('sha256').update(raw).digest('hex');}
export function readSource(path, max=2097152) {
  const fd=fs.openSync(path,fs.constants.O_RDONLY|fs.constants.O_NOFOLLOW|fs.constants.O_NONBLOCK);
  try {const m=fs.fstatSync(fd);need(m.isFile()&&m.size<=max,'source_shape');const raw=fs.readFileSync(fd);need(raw.length<=max,'source_cap');return raw;}
  finally {fs.closeSync(fd);}
}
export function bareEnv(home, inherited={}) {
  const env={PATH:'/usr/bin:/bin',HOME:home,TMPDIR:home,LC_ALL:'C',PYTHONDONTWRITEBYTECODE:'1',PYTHONNOUSERSITE:'1'};
  if(inherited.AGENT_HUB_HEAVY_TEST_HELD)env.AGENT_HUB_HEAVY_TEST_HELD=inherited.AGENT_HUB_HEAVY_TEST_HELD;
  return env;
}
export function pythonPath(env, exists=path=>{try{fs.accessSync(path,fs.constants.X_OK);return fs.statSync(path).isFile();}catch{return false;}}) {
  if(env.HUB_PUBLIC_MAIL_TEST_PYTHON!==undefined) {
    need(isAbsolute(env.HUB_PUBLIC_MAIL_TEST_PYTHON)&&exists(env.HUB_PUBLIC_MAIL_TEST_PYTHON),'explicit_python_required');
    return env.HUB_PUBLIC_MAIL_TEST_PYTHON;
  }
  for(const part of (env.PATH||'').split(delimiter))if(isAbsolute(part)) {
    const path=join(part,'python3.12');if(exists(path))return path;
  }
  throw new Error('python312_missing');
}
export function checkProbe(raw) {
  const value=JSON.parse(raw);
  need(value&&Object.keys(value).sort().join(',')==='python,websockets'&&Array.isArray(value.python)
    &&value.python.length===2&&value.python[0]===3&&value.python[1]===12&&value.websockets==='16.0','python_transport_mismatch');
}
export function checkResult(raw, parent) {
  const lines=raw.trim().split('\n');need(lines.length===1,'scenario_output_shape');const r=JSON.parse(lines[0]);
  const uuid=/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  need(r.status==='observed_public_progression'&&r.native_effect_fake===true&&r.full63_retirement===false&&r.complete===false
    &&uuid.test(r.first)&&uuid.test(r.later)&&r.first!==r.later&&typeof r.evidence_directory==='string'
    &&dirname(resolve(r.evidence_directory))===resolve(parent)&&/^public-native-mail-[A-Za-z0-9]+$/.test(r.evidence_directory.split('/').at(-1)),'scenario_not_proved');
  return r;
}
// The direct child is a trusted subreaper supervisor, not the scenario leader.
// Only its strict kernel-ECHILD receipt establishes descendant closure. Missing
// receipt/early supervisor death is UNKNOWN; never signal a recycled PGID.
export function decodeSupervision(raw) {
  const r=JSON.parse(raw);
  need(r&&Object.keys(r).sort().join(',')==='schema,stderr_b64,stdout_b64,supervision'&&r.schema===1,'supervisor_receipt_shape');
  const p=r.supervision;
  need(p&&Object.keys(p).sort().join(',')==='capture_complete,leader_exit_code,leader_status,owned_subtree_reaped,residual_children,signals,success,unknown','supervisor_proof_shape');
  need(['owned_subtree_reaped','unknown','residual_children','success','capture_complete'].every(k=>typeof p[k]==='boolean')
    &&(p.leader_exit_code===null||Number.isInteger(p.leader_exit_code))
    &&(p.leader_status===null||Number.isInteger(p.leader_status))&&Array.isArray(p.signals)
    &&p.signals.length<=2&&p.signals.every((v,i)=>v===(i===0?'INT':'KILL')),'owned_subtree_unknown');
  const decode=(value,cap)=>{need(typeof value==='string'&&/^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value),'supervisor_base64');
    const bytes=Buffer.from(value,'base64');need(bytes.length<=cap&&bytes.toString('base64')===value,'supervisor_capture_cap');return bytes;};
  return {stdout:decode(r.stdout_b64,65536),stderr:decode(r.stderr_b64,16384),proof:p};
}
export function runChild(executable,args,{cwd,env,input='',deadline,cap=65536,retain=()=>{},python,supervisor}, deps={}) {
  const launch=deps.spawn||spawn,now=deps.now||(()=>performance.now()),timer=deps.timer||setTimeout,clear=deps.clear||clearTimeout;
  need(now()<deadline&&isAbsolute(python)&&isAbsolute(supervisor),'child_deadline_or_supervisor');
  return new Promise((resolvePromise,reject)=>{
    let child,closed=false,exited=false,settled=false,fault=null,stopped=false,exitCode=null,exitSignal=null;
    const buffers={out:[],err:[]},sizes={out:0,err:0};let limit,hard;
    const finish=(error,value)=>{if(settled)return;settled=true;clear(limit);clear(hard);
      let out=Buffer.concat(buffers.out),err=Buffer.concat(buffers.err),proof=null;
      try{const decoded=decodeSupervision(out.toString('utf8'));proof=decoded.proof;out=decoded.stdout;err=decoded.stderr;
        need(out.length<=cap,'child_capture_cap');
        if(!proof.owned_subtree_reaped||proof.unknown)error=new Error('owned_subtree_unknown');
        else if(!proof.capture_complete)error=new Error('capture_incomplete_unknown');
        else if(proof.residual_children)error=new Error('residual_child_cleanup_failed');
        else if(!proof.success||proof.leader_exit_code!==0||proof.leader_status!==0)error??=new Error('child_failed');
        value={stdout:out.toString('utf8'),stderr:err.toString('utf8'),closed:true,group_reaped:true};
      }catch{error=new Error('owned_subtree_unknown');}
      try{retain({stdout:out,stderr:err,closed,fault,exit_code:exitCode,signal:exitSignal,supervision:proof});}
      catch{error??=new Error('capture_retention_unknown');}
      error?reject(error):resolvePromise(value);
    };
    // ChildProcess.kill uses its owned libuv process handle. Never negative
    // numeric PID/group signalling from this parent, even after leader exit.
    const send=sig=>{if(!exited&&!closed)try{child?.kill(sig);}catch{fault='supervisor_cleanup_unknown';}};
    const stop=()=>{if(stopped||closed)return;stopped=true;send('SIGTERM');hard=timer(()=>{
      send('SIGKILL');hard=timer(()=>finish(new Error('supervisor_reap_unknown')),200);
    },200);};
    try {
      const remaining=Math.floor(Math.min(49000,deadline-now()));need(remaining>0,'child_deadline');
      child=launch(python,['-I','-B',supervisor,'--parent-pid',String(process.pid),'--remaining-ms',String(remaining),'--',executable,...args],
        {cwd,env,detached:false,stdio:['pipe','pipe','pipe']});
      child.on('error',()=>{fault='supervisor_spawn_unknown';stop();});
      child.on('exit',()=>{exited=true;});
      for(const [stream,key,max] of [[child.stdout,'out',114688],[child.stderr,'err',1024]])stream.on('data',chunk=>{
        if(settled)return;sizes[key]+=chunk.length;if(sizes[key]>max){fault='supervisor_capture_cap';stop();return;}buffers[key].push(Buffer.from(chunk));
      });
      child.on('close',(code,sig)=>{closed=true;exited=true;exitCode=code;exitSignal=sig;
        if(fault||code!==0||sig)finish(new Error(fault||'supervisor_failed_unknown'));
        else finish(null,null);});
      child.stdin.on('error',()=>{fault='supervisor_stdin_unknown';stop();});
      limit=timer(()=>{fault='child_deadline';stop();},Math.max(0,deadline-now()-500));
      child.stdin.end(input);
    }catch{fault='supervisor_spawn_unknown';if(child)stop();else finish(new Error(fault));}
  });
}
export async function executeCi(root, env=process.env) {
  const end=performance.now()+49000;
  need(process.platform==='linux'&&Number(process.versions.node.split('.')[0])===24,'linux_node24_required');
  root=resolve(root);const parent=join(root,'.local/tmp');fs.mkdirSync(parent,{recursive:true});
  const own=fs.mkdtempSync(join(parent,'public-mail-ci-'));fs.chmodSync(own,0o700);
  let success=false, closureKnown=true;
  try {
    const childEnv=bareEnv(own,env), python=pythonPath(env);
    const supervisor=join(root,'hub2/tests/public_native_mail_ci_supervisor.py');
    need(digest(readSource(supervisor,65536))===PINS.supervisor,'supervisor_source_changed');
    const retain=name=>value=>{
      for(const key of ['stdout','stderr'])fs.writeFileSync(join(own,name+'.'+key+'.private'),value[key],{mode:0o600,flag:'wx'});
      fs.writeFileSync(join(own,name+'.closure.private.json'),JSON.stringify({closed:value.closed,fault:value.fault,exit_code:value.exit_code,signal:value.signal,supervision:value.supervision})+'\n',{mode:0o600,flag:'wx'});
    };
    const probe=await runChild(python,['-I','-B','-c',PROBE],{cwd:root,env:childEnv,deadline:Math.min(end,performance.now()+1500),cap:1024,retain:retain('probe'),python,supervisor});
    checkProbe(probe.stdout);
    const tests=join(root,'hub2/tests'), originals=join(root,'hub2/host/tests');
    const backend=join(tests,'public-native-mail-fixture.mjs'), driver=join(tests,'public-native-mail-fairness.mjs');
    need(digest(readSource(backend,131072))===PINS.backend&&digest(readSource(driver,131072))===PINS.driver,'scenario_component_changed');
    const names={'native_mail_public_fixture.py':PINS.adapter,'native_mail_restart_fixture.py':PINS.factory,'test_native_mail_public_fixture.py':PINS.tests};
    const capsule=join(own,'capsule');fs.mkdirSync(capsule,{mode:0o700});const manifest={};
    for(const [name,hash] of Object.entries(names)) {const raw=readSource(join(originals,name),131072);need(digest(raw)===hash,'fixture_component_changed');fs.writeFileSync(join(capsule,name),raw,{mode:0o600,flag:'wx'});manifest[name]=hash;}
    // Manifest is metadata only: no interpreter caches or donor trees copied.
    manifest.machine_source_sha256=digest(readSource(join(root,'hub2/src/machine-do.ts')));
    fs.writeFileSync(join(capsule,'MANIFEST.json'),JSON.stringify(manifest)+'\n',{mode:0o600,flag:'wx'});
    const source_pins=Object.fromEntries(SOURCES.map(name=>[name,digest(readSource(join(root,name)))]));
    const cfg={root,scratch_parent:parent,python,adapter:{path:join(capsule,'native_mail_public_fixture.py'),sha256:PINS.adapter,factory_sha256:PINS.factory},source_pins};
    // Exactly one external 45s INT +2s KILL envelope; no retry or resampling.
    need(end-performance.now()>=47500,'preflight_budget_exhausted');
    const outcome=await runChild('/usr/bin/timeout',['--signal=INT','--kill-after=2s','45s',process.execPath,'--max-old-space-size=512','--experimental-transform-types',driver,'--execute-reviewed','--backend-sha256',PINS.backend],
      {cwd:root,env:childEnv,input:JSON.stringify(cfg),deadline:end,retain:retain('scenario'),python,supervisor});
    checkResult(outcome.stdout,parent);success=true;
  } catch(error) {if(error.message.includes('unknown'))closureKnown=false;throw error;}
  finally {
    // Failed scenario DB/logs remain under backend's own sibling leaf. Our
    // tiny capsule is also retained on failure/unknown; no foreign cleanup.
    if(success&&closureKnown)fs.rmSync(own,{recursive:true});
  }
}

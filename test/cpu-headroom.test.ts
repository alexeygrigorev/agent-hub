import {describe,it,expect} from 'vitest';
import {cpuVerdict,sanitizeCpuUsage,sanitizeCpuAdmission} from '../src/cpu-headroom';
import {freshCpu} from './cpu-fixture';
import wireFixture from './fixtures/cpu-admission-v2.json';
const now=Date.now();
const cfg={cpu_target_utilization:1,cpu_psi_hard_cap_pct:50,cpu_default_cores:1,cpu_estimate_multiplier:1.25,cpu_psi_full_hard_cap_pct:5,memory_psi_full_hard_cap_pct:20};
const usage=(busy=2,extra={})=>({...freshCpu(new Date(now).toISOString(),12,busy),...extra});
describe('heavy utilization without choking',()=>{
 it('accepts the exact cross-repository numeric diagnostic fixture',()=>{
  expect(sanitizeCpuAdmission(wireFixture.cpu)).toEqual(wireFixture.cpu);
  expect(sanitizeCpuAdmission({...wireFixture.cpu,host_oom_kills_delta:1})).toMatchObject({host_oom_kills_delta:1});
  expect(sanitizeCpuAdmission({...wireFixture.cpu,host_oom_kills_delta:1.5})).toBeNull();
  expect(sanitizeCpuAdmission({...wireFixture.cpu,psi_full_avg10:Infinity})).toBeNull();
  expect(sanitizeCpuAdmission({...wireFixture.cpu,private_text:'withheld'})).toBeNull();
 });
 it('admits idle or fully busy CPU; expected load and startup remainder stay explainable',()=>{
  expect(cpuVerdict(usage(),12,3,0,cfg,now)).toMatchObject({admitted:true,limit_cpu_cores:12});
  expect(cpuVerdict(usage(12,{psi_avg10:95}),12,3,2,cfg,now)).toMatchObject({admitted:true,reason:null,predicted_over_target:true,current_cpu_cores:12,expected_cpu_cores:3,ramping_cpu_cores:2});
 });
 it('uses MemAvailable first and only holds on high sustained memory PSI as a secondary signal',()=>{
  expect(cpuVerdict({...usage(12,{psi_avg10:99}),load:[100,100,100]},12,2,0,cfg,now).admitted).toBe(true);
  expect(cpuVerdict(usage(1,{psi_full_avg10:6}),12,2,0,cfg,now).reason).toBe('cpu_pressure');
  expect(cpuVerdict(usage(1),12,2,0,cfg,now,2047).reason).toBe('memory_pressure');
  expect(cpuVerdict(usage(1),12,2,0,cfg,now,2048).reason).toBeNull();
  expect(cpuVerdict(null,12,2,0,cfg,now,2047,2048,new Date(now).toISOString()).reason).toBe('memory_pressure');
  expect(cpuVerdict(null,12,2,0,cfg,now,2047,2048,new Date(now-180001).toISOString()).reason).toBeNull();
  expect(cpuVerdict(usage(1,{memory_psi_full_avg10:2}),12,2,0,cfg,now,32000).reason).toBeNull();
  expect(cpuVerdict(usage(1,{memory_psi_full_avg10:20}),12,2,0,cfg,now,32000).reason).toBe('memory_pressure');
  expect(cpuVerdict(usage(1,{host_oom_kills_delta:1}),12,2,0,cfg,now)).toMatchObject({reason:'host_oom',host_oom_kills_delta:1});
 });
 it('holds on fresh host load above N cores and reports the configured limit',()=>{
  const host={load1:24.1,swap_used_percent:0,observed_at:new Date(now).toISOString(),active_sessions:2};
  // Host admission state is the 10th argument now that the memory gate
  // (memAvailableMb, memoryFloorMb, memObservedAt) owns positions 7-9.
  expect(cpuVerdict(usage(),12,1,0,{...cfg,cpu_load_factor:2},now,undefined,undefined,undefined,host)).toMatchObject({
   admitted:false,reason:'host_load_high',host_load1:24.1,host_load_limit:24,
  });
  expect(cpuVerdict(usage(),12,1,0,{...cfg,cpu_load_factor:2},now,undefined,undefined,undefined,{...host,load1:24})).toMatchObject({admitted:true,reason:null});
  expect(cpuVerdict(usage(),12,1,0,{...cfg,cpu_load_factor:2},now,undefined,undefined,undefined,{...host,load1:100,observed_at:new Date(now-180001).toISOString()})).toMatchObject({admitted:true,reason:null});
 });
 it('holds on host swap pressure and reserves a bounded concurrent session slot',()=>{
  const host={load1:1,swap_used_percent:80.1,observed_at:new Date(now).toISOString(),active_sessions:0};
  expect(cpuVerdict(usage(),12,1,0,{...cfg,swap_max_used_percent:80},now,undefined,undefined,undefined,host)).toMatchObject({admitted:false,reason:'memory_pressure',swap_used_percent:80.1});
  expect(cpuVerdict(usage(),12,1,0,{...cfg,host_max_sessions:2},now,undefined,undefined,undefined,{...host,swap_used_percent:0,active_sessions:2})).toMatchObject({
   admitted:false,reason:'machine_capacity',host_session_count:2,host_max_sessions:2,
  });
 });
 it('never treats the unscoped legacy vmstat counter as host OOM',()=>{
  expect(cpuVerdict(usage(1,{oom_kills_delta:20}),12,2,0,cfg,now)).toMatchObject({admitted:true,reason:null});
  expect(cpuVerdict(usage(1,{oom_kills_delta:20,host_oom_kills_delta:0}),12,2,0,cfg,now).admitted).toBe(true);
 });
 it('keeps missing/stale/malformed readings visibly unknown and nonblocking',()=>{
  for(const u of [null,usage(2,{busy_cores:NaN}),usage(2,{observed_at:new Date(now).toISOString().replace('Z','')}),usage(2,{cores:6}),usage(2,{busy_percent:90}),usage(2,{observed_at:new Date(now-180001).toISOString()}),usage(2,{observed_at:new Date(now+30001).toISOString()})])
   expect(cpuVerdict(u,12,1,0,cfg,now)).toMatchObject({admitted:true,reason:null,telemetry_state:'unknown',current_cpu_cores:null});
  expect(sanitizeCpuUsage({...usage(),secret:'excluded'})).not.toHaveProperty('secret');
 });
 it('carries io PSI full through the sanitized sample and verdict without gating admission',()=>{
  expect(sanitizeCpuUsage(usage(1,{io_psi_full_avg10:3.5}))).toMatchObject({io_psi_full_avg10:3.5});
  expect(sanitizeCpuUsage(usage(1,{io_psi_full_avg10:101}))?.io_psi_full_avg10).toBeNull();
  expect(sanitizeCpuAdmission({...wireFixture.cpu,io_psi_full_avg10:0.9})).toMatchObject({io_psi_full_avg10:0.9});
  expect(sanitizeCpuAdmission({...wireFixture.cpu,io_psi_full_avg10:100.1})).toBeNull();
  // IO pressure is evidence only: it never refuses a route by itself.
  expect(cpuVerdict(usage(1,{io_psi_full_avg10:99}),12,2,0,cfg,now)).toMatchObject({admitted:true,reason:null,io_psi_full_avg10:99});
 });
 it('preserves each independent fresh choking signal when advisory CPU or another metric is unknown',()=>{
  expect(cpuVerdict(usage(2,{busy_cores:null,busy_percent:null,psi_full_avg10:6}),12,1,0,cfg,now)).toMatchObject({reason:'cpu_pressure',current_cpu_cores:null});
  expect(cpuVerdict(usage(2,{busy_cores:NaN,psi_full_avg10:NaN,memory_psi_full_avg10:20}),12,1,0,cfg,now).reason).toBe('memory_pressure');
  expect(cpuVerdict(usage(2,{cores:null,psi_full_avg10:'invalid',host_oom_kills_delta:1}),12,1,0,cfg,now).reason).toBe('host_oom');
  expect(cpuVerdict(usage(2,{busy_cores:null,psi_full_avg10:6,observed_at:new Date(now-180001).toISOString()}),12,1,0,cfg,now).admitted).toBe(true);
 });
 it('measures telemetry on hosts without PSI (Windows) and holds only on >=98% busy CPU averaged over >=10s',()=>{
  const win=(pct:number,span:number)=>usage(12*pct/100,{busy_percent_window:pct,window_s:span,psi_avg10:null,psi_full_avg10:null,memory_psi_full_avg10:null,io_psi_full_avg10:null});
  expect(cpuVerdict(win(40,30),12,1,0,cfg,now)).toMatchObject({admitted:true,reason:null,telemetry_state:'measured',cpu_saturated:false});
  expect(cpuVerdict(win(97.9,30),12,1,0,cfg,now)).toMatchObject({admitted:true,cpu_saturated:false});
  expect(cpuVerdict(win(98,30),12,1,0,cfg,now)).toMatchObject({admitted:false,reason:'cpu_pressure',cpu_saturated:true,busy_percent_window:98});
  // A window shorter than 10s is unknown, never full; an instantaneous 100% alone never holds.
  expect(cpuVerdict(win(100,5),12,1,0,cfg,now)).toMatchObject({admitted:true,reason:null,cpu_saturated:null});
  expect(cpuVerdict({...usage(12),load:[100,100,100]},12,1,0,cfg,now)).toMatchObject({admitted:true,reason:null});
 });
});

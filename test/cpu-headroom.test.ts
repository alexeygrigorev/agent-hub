import {describe,it,expect} from 'vitest';
import {cpuVerdict,sanitizeCpuUsage,sanitizeCpuAdmission} from '../src/cpu-headroom';
import {freshCpu} from './cpu-fixture';
import wireFixture from './fixtures/cpu-admission-v2.json';
const now=Date.now();
const cfg={cpu_target_utilization:1,cpu_psi_hard_cap_pct:50,cpu_default_cores:1,cpu_estimate_multiplier:1.25,cpu_psi_full_hard_cap_pct:5,memory_psi_full_hard_cap_pct:1};
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
 it('ignores loadavg and some PSI but holds on known CPU/memory full or OOM',()=>{
  expect(cpuVerdict({...usage(12,{psi_avg10:99}),load:[100,100,100]},12,2,0,cfg,now).admitted).toBe(true);
  expect(cpuVerdict(usage(1,{psi_full_avg10:6}),12,2,0,cfg,now).reason).toBe('cpu_pressure');
  expect(cpuVerdict(usage(1,{memory_psi_full_avg10:2}),12,2,0,cfg,now).reason).toBe('memory_pressure');
  expect(cpuVerdict(usage(1,{host_oom_kills_delta:1}),12,2,0,cfg,now)).toMatchObject({reason:'host_oom',host_oom_kills_delta:1});
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
 it('preserves each independent fresh choking signal when advisory CPU or another metric is unknown',()=>{
  expect(cpuVerdict(usage(2,{busy_cores:null,busy_percent:null,psi_full_avg10:6}),12,1,0,cfg,now)).toMatchObject({reason:'cpu_pressure',current_cpu_cores:null});
  expect(cpuVerdict(usage(2,{busy_cores:NaN,psi_full_avg10:NaN,memory_psi_full_avg10:2}),12,1,0,cfg,now).reason).toBe('memory_pressure');
  expect(cpuVerdict(usage(2,{cores:null,psi_full_avg10:'invalid',host_oom_kills_delta:1}),12,1,0,cfg,now).reason).toBe('host_oom');
  expect(cpuVerdict(usage(2,{busy_cores:null,psi_full_avg10:6,observed_at:new Date(now-180001).toISOString()}),12,1,0,cfg,now).admitted).toBe(true);
 });
});

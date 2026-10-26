import {describe,it,expect} from 'vitest';
import {cpuVerdict,sanitizeCpuUsage} from '../src/cpu-headroom';
const now=Date.now();
const cfg={cpu_target_utilization:.9,cpu_psi_hard_cap_pct:50,cpu_default_cores:1,cpu_estimate_multiplier:1.25};
const usage=(busy=2,extra={})=>({busy_percent:busy/12*100,busy_cores:busy,cores:12,psi_avg10:5,observed_at:new Date(now).toISOString(),interval_s:.2,...extra});
describe('real CPU headroom',()=>{
 it('admits idle CPU and rejects busy CPU plus heavy task with explainable numbers',()=>{
  expect(cpuVerdict(usage(),12,3,0,cfg,now)).toMatchObject({admitted:true,reason:null,limit_cpu_cores:10.8});
  expect(cpuVerdict(usage(8),12,3,0,cfg,now)).toMatchObject({admitted:false,reason:'cpu_headroom',current_cpu_cores:8,expected_cpu_cores:3,ramping_cpu_cores:0});
 });
 it('reserves not-yet-ramped demand and accepts the exact target boundary',()=>{
  expect(cpuVerdict(usage(8),12,2,1,cfg,now).reason).toBe('cpu_headroom');
  expect(cpuVerdict(usage(8),12,2,.8,cfg,now).admitted).toBe(true);
 });
 it('ignores loadavg and retains the PSI hard safety cap',()=>{
  expect(cpuVerdict({...usage(1),load:[100,100,100]},12,2,0,cfg,now).admitted).toBe(true);
  expect(cpuVerdict(usage(1,{psi_avg10:51}),12,2,0,cfg,now).reason).toBe('cpu_pressure');
 });
 it('fails closed on absent, stale, future, inconsistent or nonfinite CPU telemetry',()=>{
  for(const u of [null,usage(2,{busy_cores:NaN}),usage(2,{cores:6}),usage(2,{busy_percent:90}),usage(2,{observed_at:new Date(now-180001).toISOString()}),usage(2,{observed_at:new Date(now+30001).toISOString()})])
   expect(cpuVerdict(u,12,1,0,cfg,now).reason).toBe('cpu_usage_unknown');
  expect(sanitizeCpuUsage({...usage(),secret:'excluded'})).not.toHaveProperty('secret');
 });
});

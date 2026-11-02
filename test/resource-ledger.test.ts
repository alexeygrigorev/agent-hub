import {describe,it,expect} from 'vitest';
import {projectLabel,percentile,effectiveFloorMb,memoryVerdict,resourceConfig} from '../src/resource-management';

describe('resource ledger project labels',()=>{
 it('normalizes workspace paths on every OS to the repository name',()=>{
  expect(projectLabel('C:\\Users\\User\\work\\1e0bc141\\agent-hub')).toBe('agent-hub');
  expect(projectLabel('/home/alexey/git/agent-hub-wt-resource-ledger')).toBe('agent-hub');
  expect(projectLabel('C:\\Users\\User\\git\\agent-browser')).toBe('agent-browser');
  expect(projectLabel('/home/box/work/dataops/')).toBe('dataops');
 });
 it('keeps registered names and never invents one',()=>{
  expect(projectLabel('agent-hub')).toBe('agent-hub');
  expect(projectLabel('')).toBeNull();
  expect(projectLabel(null)).toBeNull();
  expect(projectLabel(42)).toBeNull();
 });
 it('p90 picks the ledger upper tail',()=>{
  expect(percentile([1,2,3,4,5,6,7,8,9,10],90)).toBe(9);
  expect(percentile([],90)).toBeNull();
 });
});

describe('host-proportional memory floor and robust estimate defaults',()=>{
 const config=resourceConfig();
 it('sizes reservations from the measured per-engine p90 and caps outliers by default',()=>{
  // Intake 189f6cd6: reservations reflect what an engine really needs at p90
  // (codex implement ~125 MiB, claude implement p90 ~943 MiB), so a host with
  // ~1.6 GiB free admits light sessions until MemAvailable actually drops
  // under the floor; the cap keeps a single heavy cohort from reserving gigabytes.
  expect(config.estimate_percentile).toBe(90);
  expect(config.estimate_cap_mb).toBe(1024);
  expect(()=>resourceConfig({estimate_cap_mb:10})).toThrow();
 });
 it('scales the floor to host RAM with a 512 MiB minimum and the configured ceiling',()=>{
  expect(effectiveFloorMb(config,32279)).toBe(968);   // 32 GiB laptop
  expect(effectiveFloorMb(config,15980)).toBe(512);   // 16 GiB tablet
  expect(effectiveFloorMb(config,64223)).toBe(1927);  // 64 GiB server
  expect(effectiveFloorMb(config,256000)).toBe(config.memory_floor_mb);
  expect(effectiveFloorMb(config,null)).toBe(config.memory_floor_mb);
  expect(effectiveFloorMb(config,0)).toBe(config.memory_floor_mb);
 });
 it('lets the 32 GiB laptop with ~1.6 GiB free accept a few-hundred-MiB session',()=>{
  expect(memoryVerdict(1600,0,414,config,32279)).toBe(true);
  expect(memoryVerdict(1300,0,414,config,32279)).toBe(false);
  expect(memoryVerdict(1600,0,414,config)).toBe(false); // legacy unknown-total keeps the ceiling floor
 });
});

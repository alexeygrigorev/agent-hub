import {describe,it,expect} from 'vitest';
import {projectLabel,percentile} from '../src/resource-management';

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

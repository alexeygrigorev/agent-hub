import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeWorkspaceBinding,boundWorkspace} from '../src/workspace-binding';

test('host can bind a label or absolute checkout to its intake worktree and keep continuation binding',()=>{
 for(const requested of ['agent-hub','/work/agent-hub','C:\\work\\agent-hub']){
  const resolved=requested.startsWith('C:')?requested+'\\.local\\hub-worktrees\\'+'a'.repeat(64):'/work/agent-hub/.local/hub-worktrees/'+'a'.repeat(64);
  const binding={version:1,requested_workspace:requested,resolved_workspace:resolved};
  assert.deepEqual(mergeWorkspaceBinding(requested,undefined,binding),binding);
  assert.equal(boundWorkspace({workspace:requested},binding),resolved);
  assert.throws(()=>mergeWorkspaceBinding(requested,binding,{...binding,resolved_workspace:resolved.replace(/a{64}$/,'b'.repeat(64))}),/workspace_binding_conflict/);
 }
});
test('absolute worktree redirection refuses arbitrary paths, traversal and malformed intake names',()=>{
 for(const resolved of ['/foreign','/work/repo/.local/hub-worktrees/../foreign','/work/repo/.local/hub-worktrees/'+ 'a'.repeat(63),'/work/repo/.local/hub-worktrees/'+ 'a'.repeat(64)+'/child'])
  assert.throws(()=>mergeWorkspaceBinding('/work/repo',undefined,{version:1,requested_workspace:'/work/repo',resolved_workspace:resolved}),/invalid_workspace_binding/);
});

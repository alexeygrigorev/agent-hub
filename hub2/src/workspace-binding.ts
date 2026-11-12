import {HttpError} from './http';
import type {JsonRecord} from './types';

/** Host-authenticated resolution metadata, never part of the immutable request. */
export type WorkspaceBinding = {
  version: 1;
  requested_workspace: string;
  resolved_workspace: string;
};

export const WORKSPACE_BINDING_FIELD = '_hub_workspace_binding';
export const WORKSPACE_REPAIR_FIELD = '_hub_workspace_repair_pending';
export const WORKSPACE_RECOVERY_TURN_FIELD = '_hub_workspace_recovery_turn';

function absolute(value:string):boolean {
  // These are target-host strings. Never resolve them on the server filesystem.
  return value.startsWith('/') || /^[A-Za-z]:\\/.test(value) || /^\\\\[^\\]+\\[^\\]+/.test(value);
}

// Host-authenticated redirection of an absolute enrolled checkout is limited
// to the deterministic intake-worktree namespace beneath that checkout.
function intakeWorktree(requested:string,resolved:string):boolean {
  const separator=requested.includes('\\')?'\\':'/';
  const prefix=requested.replace(/[\\/]+$/,'')+separator+'.local'+separator+'hub-worktrees'+separator;
  return resolved.startsWith(prefix)&&/^[a-f0-9]{64}$/.test(resolved.slice(prefix.length));
}

function validate(requested:unknown,value:unknown):WorkspaceBinding {
  const binding=value as Partial<WorkspaceBinding>|null;
  if(typeof requested!=='string'||!binding||typeof binding!=='object'||Array.isArray(binding)
    ||Object.keys(binding).length!==3||binding.version!==1||binding.requested_workspace!==requested
    ||typeof binding.resolved_workspace!=='string'||binding.resolved_workspace.length>1024
    ||/[\u0000-\u001f]/.test(binding.resolved_workspace)||!absolute(binding.resolved_workspace)
    ||(!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(requested)&&binding.resolved_workspace!==requested
      && !intakeWorktree(requested,binding.resolved_workspace))) {
    throw new HttpError('invalid_workspace_binding',409);
  }
  return {version:1,requested_workspace:requested,resolved_workspace:binding.resolved_workspace};
}

export function mergeWorkspaceBinding(requested:unknown,stored:unknown,incoming?:unknown):WorkspaceBinding|undefined {
  const prior=stored===undefined?undefined:validate(requested,stored);
  if(incoming===undefined)return prior;
  const next=validate(requested,incoming);
  if(prior&&prior.resolved_workspace!==next.resolved_workspace)throw new HttpError('workspace_binding_conflict',409);
  return prior??next;
}

export function boundWorkspace(launch:JsonRecord,stored:unknown=launch[WORKSPACE_BINDING_FIELD]):unknown {
  return mergeWorkspaceBinding(launch.workspace,stored)?.resolved_workspace??launch.workspace;
}

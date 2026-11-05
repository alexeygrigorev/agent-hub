import { HttpError } from './http';
import { admitLaunch } from './resource-manager';
import type { AdmissionRequest, AdmissionPlan } from './resource-manager';
import type { Hub2Env } from './types';

export type RoutingRequest=AdmissionRequest;
export type RoutingDecision=AdmissionPlan;

/** Advisory selection only: Account reservation and Machine CAS remain authoritative.
 * Delegates to the Resource Manager facade; typed holds surface as the same
 * wire codes the saga and API already handle. */
export async function selectRoute(env:Hub2Env,input:RoutingRequest):Promise<RoutingDecision>{
  const outcome=await admitLaunch(env,input);
  if(outcome.ok)return outcome.plan;
  const code=outcome.code;
  if(code==='quota_unknown'||code==='quota_stale'||code==='quota_exhausted'||code==='account_saturated')throw new HttpError('quota_unavailable',409);
  if(code==='host_resources_stale')throw new HttpError('machine_unavailable',409);
  throw new HttpError(code,409);
}

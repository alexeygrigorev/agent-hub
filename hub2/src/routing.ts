import { HttpError } from './http';
import { admitLaunch, holdWireCode } from './resource-manager';
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
  throw new HttpError(holdWireCode(outcome.code),409);
}

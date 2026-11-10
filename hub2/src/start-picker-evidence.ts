import type {PickedRoute, PickDiagnostics} from './model-picker';

export const MAX_PICKER_BYTES = 24_000;
export type PickerPoolRow = {
  policy_id: string; model: string; engine: string; effort: string | null;
  weight: number; share: number;
};
export type PickerAttempt = {
  policy_id: string; policy_version: number | string | null; machine_id: string;
  attempt: 0 | 1; model: string; engine: string; effort: string | null;
};
export type StartPickerEvidence = PickerAttempt & {pool: PickerPoolRow[]; history: PickerAttempt[]; pool_degraded?: boolean};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}
function text(value: unknown, max: number): value is string {
  return typeof value === 'string' && value.trim().length > 0 && !/[\x00-\x1f\x7f-\x9f]/.test(value)
    && new TextEncoder().encode(value).length <= max;
}
function version(value: unknown): value is number | string | null {
  return value === null || typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 || text(value,80);
}
function choice(value: unknown): PickerAttempt | null {
  const v=record(value);
  if(!v||!text(v.policy_id,120)||!version(v.policy_version)||!text(v.machine_id,80)
    ||v.attempt!==0&&v.attempt!==1||!text(v.model,120)||!text(v.engine,120)
    ||v.effort!==null&&!text(v.effort,16))return null;
  return {policy_id:v.policy_id,policy_version:v.policy_version,machine_id:v.machine_id,
    attempt:v.attempt,model:v.model,engine:v.engine,effort:v.effort};
}

/** Validate the bounded snapshot and construct a fresh public whitelist. Never
 * copy raw route/account/quota/credential/exclusion fields into the projection.
 */
export function normalizeStartPickerEvidence(value: unknown): StartPickerEvidence | null {
  const v=record(value),selected=choice(value);
  if(!v||!selected||!Array.isArray(v.pool)||!v.pool.length||v.pool.length>32
    ||!Array.isArray(v.history)||!v.history.length||v.history.length>2)return null;
  const pool:PickerPoolRow[]=[],ids=new Set<string>();
  for(const item of v.pool){
    const row=record(item);
    if(!row||!text(row.policy_id,120)||!text(row.model,120)||!text(row.engine,120)
      ||row.effort!==null&&!text(row.effort,16)||typeof row.weight!=='number'
      ||!Number.isFinite(row.weight)||row.weight<=0||typeof row.share!=='number'
      ||!Number.isFinite(row.share)||row.share<=0||row.share>1+1e-9||ids.has(row.policy_id))return null;
    ids.add(row.policy_id);
    pool.push({policy_id:row.policy_id,model:row.model,engine:row.engine,effort:row.effort,weight:row.weight,share:row.share});
  }
  const totalWeight=pool.reduce((total,row)=>total+row.weight,0);
  if(Math.abs(pool.reduce((total,row)=>total+row.share,0)-1)>1e-6
    ||pool.some(row=>Math.abs(row.share-row.weight/totalWeight)>1e-6)
    ||!pool.some(row=>row.policy_id===selected.policy_id&&row.model===selected.model
      &&row.engine===selected.engine&&row.effort===selected.effort))return null;
  const history:PickerAttempt[]=[];
  for(const item of v.history){
    const attempt=choice(item);
    if(!attempt||history.length&&attempt.attempt<=history[history.length-1].attempt)return null;
    history.push(attempt);
  }
  if(JSON.stringify(history[history.length-1])!==JSON.stringify(selected))return null;
  const degraded=new Set(pool.map(row=>row.model)).size<3;
  if(v.pool_degraded!==undefined&&(typeof v.pool_degraded!=='boolean'||v.pool_degraded!==degraded))return null;
  const result={...selected,pool,history,...(v.pool_degraded===undefined?{}:{pool_degraded:degraded})};
  return new TextEncoder().encode(JSON.stringify(result)).length<=MAX_PICKER_BYTES?result:null;
}

/** Called with the route and diagnostics returned by the very same draw. */
export function buildStartPickerEvidence(route: PickedRoute, diagnostics: PickDiagnostics,
  attempt: 0 | 1 = 0, previous?: unknown): StartPickerEvidence {
  const selected:PickerAttempt={policy_id:route.policy_id,policy_version:version(route.policy_version)?route.policy_version:null,
    machine_id:route.machine_id,attempt,model:route.model,engine:route.engine,effort:route.effort??null};
  const prior=normalizeStartPickerEvidence(previous);
  const history=prior?prior.history.filter(row=>row.attempt<attempt):[];
  const evidence=normalizeStartPickerEvidence({...selected,pool:diagnostics.pool,
    pool_degraded:new Set(diagnostics.pool.map(row=>row.model)).size<3,history:[...history,selected].slice(-2)});
  if(!evidence)throw new Error('invalid_start_picker_evidence');
  return evidence;
}

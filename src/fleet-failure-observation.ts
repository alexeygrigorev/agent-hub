import type { SqlStorage } from '@cloudflare/workers-types';

const RAW_FAILURE_LIMIT=128;
type FailureRow={machine_id:string;exit_reason:string|null;updated_at:string};
export type AccountFailurePrefix={rows:FailureRow[];limited:boolean};

/** The deployed account/time partial index bounds work before machine and
 * reason predicates. A sparse route never walks another host's history. */
export function accountFailurePrefix(sql:SqlStorage,account:string,since:string):AccountFailurePrefix {
  const rows=sql.exec(`SELECT machine_id,exit_reason,updated_at FROM fleet_launches INDEXED BY fleet_launch_account_failed_recent
    WHERE account=? AND state='failed' AND updated_at>? ORDER BY updated_at DESC LIMIT 129`,account,since).toArray() as FailureRow[];
  return {rows,limited:rows.length>RAW_FAILURE_LIMIT};
}
export function routeFailureObservation(prefix:AccountFailurePrefix,machine:string,neutral:ReadonlySet<string>,cap:number):{n:number;launcher:string[];limited:boolean} {
  const rows=prefix.rows.filter(row=>row.machine_id===machine && row.exit_reason!==null && !neutral.has(row.exit_reason)).slice(0,cap);
  return {n:rows.length,launcher:rows.filter(row=>row.exit_reason==='launcher_command_failed').map(row=>row.updated_at),limited:prefix.limited&&rows.length<cap};
}
/** A known newer good outcome reopens a route. Absence in an incomplete
 * prefix cannot establish either health or quarantine. */
export function routeQuarantineObservation(sql:SqlStorage,account:string,machine:string,third:string):boolean|null {
  const rows=sql.exec(`SELECT account,machine_id,state,exit_reason FROM fleet_launches INDEXED BY fleet_launch_updated
    WHERE updated_at>? ORDER BY updated_at DESC LIMIT 129`,third).toArray() as any[];
  if(rows.some(row=>row.account===account && row.machine_id===machine && (row.state!=='failed'||row.exit_reason!==null&&row.exit_reason!=='launcher_command_failed')))return false;
  return rows.length>RAW_FAILURE_LIMIT?null:true;
}
/** The already-deployed refusal index supplies a complete raw route prefix.
 * Model/profile/effort/time are postfilters, never an unbounded SQL walk. */
export function unsupportedModelObservation(sql:SqlStorage,route:{machine_id:string;engine:string;account:string;profile?:string|null},mode:string,since:string,model:string,effort:unknown):boolean|null {
  const rows=sql.exec(`SELECT profile,mode,finished_at,updated_at,model_selection_json FROM fleet_launches INDEXED BY fleet_launch_model_unsupported
    WHERE machine_id=? AND engine=? AND account=? AND exit_reason='model_unsupported' ORDER BY rowid DESC LIMIT 129`,route.machine_id,route.engine,route.account).toArray() as any[];
  let unreadable=false;
  const positive=rows.some(row=>{
    if((row.profile??null)!==(route.profile??null)||row.mode!==mode||String(row.finished_at??row.updated_at)<=since)return false;
    let selection:any;try{selection=JSON.parse(row.model_selection_json||'null');}catch{unreadable=true;return false;}
    if(!selection||typeof selection!=='object'||typeof selection.model!=='string'){unreadable=true;return false;}
    return selection.model===model && (selection?.effort??null)===(effort??null);
  });
  return positive?true:unreadable||rows.length>RAW_FAILURE_LIMIT?null:false;
}

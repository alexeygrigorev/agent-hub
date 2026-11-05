// Explicit opt-in sizing fixture, never a production envelope or provider bill.
// CPU/duration allocations stay configured assumptions, not measured strict
// runtime bounds. SQL includes cold owners and the 64R/8W settlement allowance.
export const CANONICAL_COST_METRICS=['worker_requests','worker_cpu_ms','do_requests','do_ws_messages','do_gb_ms','background_invocations','sqlite_rows_read','sqlite_rows_written','sqlite_byte_days','kv_reads','kv_writes','kv_deletes','kv_lists','kv_byte_days'];
export function canonicalSizingEnvelopes(metrics=CANONICAL_COST_METRICS){
 const units=Object.fromEntries(metrics.map(k=>[k,0]));
 Object.assign(units,{worker_requests:1,worker_cpu_ms:500,do_requests:19,do_ws_messages:1,do_gb_ms:2048,background_invocations:1,sqlite_rows_read:256,sqlite_rows_written:160,sqlite_byte_days:65536});
 const plan=(calls,wall,cpu,gbms,writes=160,reads=256)=>({
  units:{...units,worker_cpu_ms:cpu,do_requests:calls+3,do_gb_ms:gbms,sqlite_rows_written:writes,sqlite_rows_read:reads},
  max_do_calls:calls,max_wall_ms:wall,
  provenance:'Test-only canonical fixture; configured CPU/duration assumptions, not verified provider upper bounds.',
 });
 const normal=plan(16,750,500,2048),auth=plan(3,500,200,768,384);
 return JSON.stringify({version:1,normal:{...normal,by_kind:{
  public_request:plan(8,250,250,512),public_read:plan(3,250,200,192),
  public_write:plan(4,300,250,320),fleet_read:plan(6,250,250,384),
  project_request:plan(4,250,250,256),claim_request:plan(3,250,250,192),
  message_request:plan(4,750,250,1536),account_request:plan(2,250,250,128),
  placement_request:normal,connection_request:normal,provider_refresh:normal,
  auth_request:auth,
 }},safety:{...normal,by_kind:{operator_stop:normal,native_exit:plan(16,750,500,2048,112,160),capacity_reconcile:plan(16,750,500,2048,96,160),auth_revoke:auth,alert_delivery:normal,budget_recovery:plan(16,750,500,2048,80,128)}}});
}

// Test-only financial envelopes. These are not production sizing/billing claims.
import {METRICS} from '../src/budget-do';
import {SQL_PROBE_HEADER,SQL_RECEIPT_HEADER} from '../src/sql-receipt';
const units=Object.fromEntries(METRICS.map(k=>[k,0]));
Object.assign(units,{worker_requests:1,worker_cpu_ms:100,do_requests:67,do_gb_ms:1000,sqlite_rows_read:4096,sqlite_rows_written:256,sqlite_byte_days:1});
export const fixtureBudgetEnvelopes=JSON.stringify({version:1,normal:{units,max_do_calls:64,max_wall_ms:60000,provenance:'Synthetic test-only cost envelope; native SQL counters reported separately'},safety:{units,max_do_calls:64,max_wall_ms:60000,provenance:'Synthetic test-only safety envelope'}});
// The semantic stub below executes NO SQLite. Its known zero SQL receipt is
// unit-fixture protocol data, never Cloudflare measurement or billing evidence.
export function semanticNoSqlResponse(request:Request,value:unknown,status=200):Response{
 return Response.json(value,{status,headers:{[SQL_RECEIPT_HEADER]:JSON.stringify({v:1,valid:status<400,reads:0,writes:0,operation_id:request.headers.get(SQL_PROBE_HEADER)||'semantic-no-probe'})}});
}
// Semantic tests isolate quota/mail races. Actual ledger enforcement is tested by run-budget-edge.mjs.
export function semanticBudgetEnv(env:any={}):any{
 const BUDGET={idFromName:(v:string)=>v,get:()=>({fetch:async(input:RequestInfo,init?:RequestInit)=>semanticNoSqlResponse(new Request(input,init),{allowed:true,settled:true})})};
 env.BUDGET??=BUDGET;env.HUB2_BUDGET_ENVELOPES??=fixtureBudgetEnvelopes;return env;
}

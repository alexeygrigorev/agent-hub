// Test-only financial envelopes. These are not production sizing/billing claims.
import {METRICS} from '../src/budget-do';
const units=Object.fromEntries(METRICS.map(k=>[k,0]));
Object.assign(units,{worker_requests:1,worker_cpu_ms:100,do_requests:67,do_gb_ms:1000,sqlite_rows_read:4096,sqlite_rows_written:64,sqlite_byte_days:1});
export const fixtureBudgetEnvelopes=JSON.stringify({version:1,normal:{units,max_do_calls:64,max_wall_ms:60000,provenance:'Synthetic test-only cost envelope; native SQL counters reported separately'},safety:{units,max_do_calls:64,max_wall_ms:60000,provenance:'Synthetic test-only safety envelope'}});
// Semantic tests isolate quota/mail races. Actual ledger enforcement is tested by run-budget-edge.mjs.
export function semanticBudgetEnv(env:any={}):any{
 const BUDGET={idFromName:(v:string)=>v,get:()=>({fetch:async()=>Response.json({allowed:true,settled:true})})};
 env.BUDGET??=BUDGET;env.HUB2_BUDGET_ENVELOPES??=fixtureBudgetEnvelopes;return env;
}

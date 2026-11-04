/** Offline AC1 replay of exact SQL from the disabled deployment's source:
 * git show f2a0dd3a1aea:src/jobs.ts (getAlarmTime/processAlarms, lines510–598)
 * git show f2a0dd3a1aea:src/host-history.ts (sample fleet census)
 * git show f2a0dd3a1aea:src/hub.ts (alarm, lines2536–2578): it invoked both
 * helpers, then unconditionally set the next alarm at <=now+60s, even idle.
 *
 * These are native SQLite fixture measurements, NOT live production billing
 * or a replay of the entire legacy handler. Current indexes are retained:
 * the legacy queries still read history linearly even with those indexes.
 */
import { describe, expect, it } from 'vitest';
import { env, runInDurableObject } from 'cloudflare:test';
import { JobsService } from '../src/jobs';
import { HostHistoryService } from '../src/host-history';
import { RowsMeter } from '../src/rows-meter';

const REVISION = 'f2a0dd3a1aea';
const NOW = Date.parse('2026-11-03T21:00:00Z');
const ISO = new Date(NOW).toISOString();
const FUTURE = new Date(NOW + 3_600_000).toISOString();

// Verbatim statement text from the source revision above. No legacy handler
// is imported, deployed, or sent to the live Hub.
const LEGACY_ALARM_QUERIES = [
  "SELECT * FROM jobs WHERE kind = 'root-run' AND status = 'offered' AND offer_expires_at IS NOT NULL",
  'SELECT * FROM leases WHERE holder_job_id IS NOT NULL',
  "SELECT leases.* FROM leases JOIN jobs ON leases.holder_job_id = jobs.id WHERE jobs.started_at IS NULL AND jobs.status = 'claimed'",
  "SELECT offer_expires_at FROM jobs WHERE status = 'offered' AND offer_expires_at IS NOT NULL",
  'SELECT expires_at FROM leases WHERE holder_job_id IS NOT NULL AND expires_at IS NOT NULL',
  "SELECT leases.acquired_at FROM leases JOIN jobs ON leases.holder_job_id = jobs.id WHERE jobs.status = 'claimed' AND jobs.started_at IS NULL AND leases.acquired_at IS NOT NULL",
];
const LEGACY_HISTORY_CENSUS = `SELECT ids.machine_id,
      (SELECT COUNT(*) FROM fleet_launches f WHERE f.machine_id=ids.machine_id AND f.state='queued') AS queued,
      (SELECT COUNT(*) FROM fleet_launches f WHERE f.machine_id=ids.machine_id AND f.state='failed') AS failed
      FROM (SELECT id AS machine_id FROM machines UNION SELECT DISTINCT machine_id FROM fleet_launches) ids LIMIT 129`;

function fixture(fn: (hub: any, state: any, raw: any) => void) {
  const stub = (env as any).HUB.get((env as any).HUB.idFromName('legacy-cost-replay-' + crypto.randomUUID()));
  return (runInDurableObject as any)(stub, async (hub: any, state: any) => fn(hub,state,state.storage.sql));
}
function receipt(meter: RowsMeter, scope: string, fixtureRows: number) {
  const totals = meter.snapshot()[scope];
  const statements = meter.statementTop(1500).filter(row => row.scope === scope).map(row => ({
    sql: row.sql, calls: row.calls, rows_read: row.rows_read, rows_written: row.rows_written,
    rows_read_per_call: row.rows_read / row.calls,
  }));
  // RowsMeter's scope.max_read is the maximum WHOLE scope invocation, not the
  // maximum single statement. The separate statement receipts prevent mixing
  // those two units in the incident report.
  console.log(JSON.stringify({ evidence: 'offline_native_sql_replay', legacy_revision: REVISION, scope,
    fixture_rows: fixtureRows, invocation_rows_read: totals.rows_read,
    invocation_rows_written: totals.rows_written, invocation_calls: totals.calls, statements }));
  return totals;
}

describe('deployed SQL cost root cause: native before/after replay', () => {
  it('legacy alarm lease probes grow linearly; current due-index probes stay bounded', () => fixture((_hub,state,raw) => {
    const meter = new RowsMeter(() => NOW), sql = meter.wrap(raw);
    const jobs = new JobsService(sql,state,{now:()=>new Date(NOW)});
    // Isolate steady-state alarm work after the independently bounded migration
    // backfill completed; jobs below already started, so no claim deadlines.
    raw.exec('UPDATE job_claim_backfill SET done=1 WHERE id=1');
    let seeded = 0;
    const oldReads: number[] = [];
    for (const count of [500,2000]) {
      for (let i=seeded;i<count;i++) {
        raw.exec(`INSERT INTO jobs(id,kind,idempotency_key,payload,status,created_at,deadline_at,actor,started_at)
          VALUES(?,'writer-run',?,'{}','running',?,?,'test',?)`,'replay-job-'+i,'replay-key-'+i,ISO,FUTURE,ISO);
        raw.exec(`INSERT INTO leases(name,holder_job_id,holder_machine_id,epoch,acquired_at,expires_at)
          VALUES(?,?,'replay-host',1,?,?)`,'replay-lease-'+i,'replay-job-'+i,ISO,FUTURE);
      }
      seeded=count;
      const legacyScope='legacy:alarm_lease_sql:'+count;
      meter.runSync(legacyScope,()=>{ for (const query of LEGACY_ALARM_QUERIES) sql.exec(query).toArray(); });
      const old=receipt(meter,legacyScope,count);oldReads.push(old.rows_read);
      expect(old.rows_read).toBeGreaterThanOrEqual(count*2);
      const currentScope='current:processAlarms:'+count;
      const next=meter.runSync(currentScope,()=>jobs.processAlarms());
      const current=receipt(meter,currentScope,count);
      expect(next).toBe(NOW+3_600_000);
      expect(current.rows_read).toBeLessThan(60);
      expect(current.rows_written).toBe(0);
    }
    expect(oldReads[1]).toBeGreaterThan(oldReads[0]*3);
  }));

  it('legacy history census counts retained failures every call; current projection is bounded and marks unknown', () => fixture((_hub,_state,raw) => {
    const meter = new RowsMeter(() => NOW), sql = meter.wrap(raw);
    const history = new HostHistoryService(sql,{hostResources:()=>[],cachedAccountStatus:()=>({})} as any,()=>NOW);
    history.init();
    raw.exec("INSERT INTO machines(id,name,hostname,registered_at,last_checkin_at) VALUES('replay-host','replay','replay',?,?)",ISO,ISO);
    let seeded=0;
    const oldReads:number[]=[];
    for(const count of [500,2000]) {
      for(let i=seeded;i<count;i++) raw.exec(`INSERT INTO fleet_launches(id,actor,requester_kind,summary,request_key,request_json,machine_id,engine,account,mode,workspace,prompt,state,created_at,updated_at)
        VALUES(?,'test','human','replay',?,'{}','replay-host','codex','test','headless','/repo','','failed',?,?)`,
        'replay-launch-'+i,'replay-launch-'+i,ISO,ISO);
      seeded=count;
      const legacyScope='legacy:history_census:'+count;
      const census=meter.runSync(legacyScope,()=>sql.exec(LEGACY_HISTORY_CENSUS).toArray()) as any[];
      const old=receipt(meter,legacyScope,count);oldReads.push(old.rows_read);
      expect(census.find(row=>row.machine_id==='replay-host').failed).toBe(count);
      expect(old.rows_read).toBeGreaterThanOrEqual(count);
      const currentScope='current:history_census:'+count;
      const projection=meter.runSync(currentScope,()=>(history as any).historyMachines().map((machine:string)=>({
        machine_id:machine,queued:(history as any).launchCount(machine,'queued'),failed:(history as any).launchCount(machine,'failed'),
      }))) as any[];
      const current=receipt(meter,currentScope,count);
      expect(projection.find(row=>row.machine_id==='replay-host').failed).toBeNull();
      expect(current.rows_read).toBeLessThan(160);
      expect(raw.exec("SELECT value FROM resource_history_meta WHERE key='sample_truncated'").toArray()).toHaveLength(1);
    }
    expect(oldReads[1]).toBeGreaterThan(oldReads[0]*3);
  }));
});

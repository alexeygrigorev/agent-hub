/** Extra visibility for placement refusals. Admission remains owned by FleetService. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { FleetService } from './fleet';
import type { Subject } from './liveness';
export function placementPressureSubjects(sql: SqlStorage, fleet: FleetService, now: number): Subject[] {
  const out: Subject[] = [];
  const numbers = () => {
    const memory = fleet.hostResources().map(h => `${h.machine_id}: mem_avail_mb=${h.mem_avail_mb ?? 'unknown'} reserved_mb=${h.reserved_mb ?? 'unknown'} floor_mb=${h.floor_mb} headroom_mb=${h.headroom_mb ?? 'unknown'}`).join('; ');
    const accounts = sql.exec('SELECT * FROM fleet_accounts ORDER BY label').toArray().map(a => {
      const s = fleet.accountStatus(a); return `${s.label}: counted=${s.active} native_live=${s.native_live ?? 'unknown'} max=${s.max_concurrency}`;
    }).join('; ');
    return ` [${memory}${accounts ? '; ' + accounts : ''}]`.slice(0, 4000);
  };
  for (const q of sql.exec("SELECT id,created_at FROM fleet_launches WHERE state='queued' AND created_at <= ? ORDER BY created_at LIMIT 128", new Date(now - 300000).toISOString()).toArray() as any[]) {
    // Memory owner already emits the exact refusal episode and numbers.
    const memoryHold = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_memory_holds'").toArray().length
      && sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?', q.id).toArray().length;
    if (memoryHold) {
      const row:any=sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?',q.id).toArray()[0];
      let rejected:any[]=[]; try {rejected=JSON.parse(row.rejected_json);} catch {}
      if(rejected.some(r=>r.reason==='launcher_memory_reserve')) continue;
      out.push({kind:'launch',id:`placement-queue:${q.id}`,state:'stuck',detail:`Launch held queued >5 min: ${JSON.stringify(rejected).slice(0,3000)}`,age_s:Math.max(0,Math.floor((now-Date.parse(q.created_at))/1000))});
      continue;
    }
    out.push({ kind: 'launch', id: `placement-queue:${q.id}`, state: 'stuck', detail: `Launch held queued >5 min for placement admission${numbers()}`, age_s: Math.max(0, Math.floor((now - Date.parse(q.created_at)) / 1000)) });
  }
  // Rejections and terminal failures are retained as history. They do not
  // describe a live queued intent and must not open placement-pressure alerts.
  return out;
}

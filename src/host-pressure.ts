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
      && sql.exec('SELECT launch_id FROM fleet_memory_holds WHERE launch_id=?', q.id).toArray().length;
    if (memoryHold) continue;
    out.push({ kind: 'launch', id: `placement-queue:${q.id}`, state: 'stuck', detail: `Launch held queued >5 min for memory/capacity${numbers()}`, age_s: Math.max(0, Math.floor((now - Date.parse(q.created_at)) / 1000)) });
  }
  for (const r of sql.exec('SELECT id,created_at,detail FROM placement_rejections WHERE created_at_ms>=? ORDER BY created_at_ms LIMIT 128', now - 86400000).toArray() as any[]) {
    out.push({ kind: 'launch', id: `placement-reject:${r.id}`, state: 'stuck', detail: `Launch rejected for memory/capacity: ${r.detail}${numbers()}` });
  }
  for (const f of sql.exec("SELECT id,machine_id,exit_reason FROM fleet_launches WHERE state='failed' AND updated_at>=? AND exit_reason IN ('account_capacity','machine_capacity') LIMIT 128", new Date(now - 86400000).toISOString()).toArray() as any[]) {
    out.push({ kind:'launch',id:`placement-failed:${f.id}`,state:'stuck',machine_id:f.machine_id,detail:`Launch failed: ${f.exit_reason}${numbers()}` });
  }
  return out;
}

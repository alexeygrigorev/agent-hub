/** Extra visibility for placement refusals. Admission remains owned by FleetService. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { Subject } from './liveness';

/** Render the rejection-time evidence persisted with a hold: the route
 * reasons plus MemAvailable and PSI cpu/memory/io full captured from the
 * heartbeat that drove the refusal. Warnings must show these recorded
 * values, never a later observation (intake 1bc3add1 AC1). */
export function rejectionEvidence(rejected: any[], evidenceByMachine: Record<string, any> | null): string {
  const parts = (Array.isArray(rejected) ? rejected : []).slice(0, 24).map((r) => {
    const e = r && typeof r === 'object' && evidenceByMachine && typeof evidenceByMachine === 'object'
      ? evidenceByMachine[String(r.machine)] ?? {} : {};
    const numbers = `mem_avail_mb=${e.mem_avail_mb ?? 'unknown'} psi_cpu_full=${e.psi_cpu_full_avg10 ?? 'unknown'}% psi_memory_full=${e.psi_memory_full_avg10 ?? 'unknown'}% psi_io_full=${e.psi_io_full_avg10 ?? 'unknown'}%${e.observed_at ? ` observed_at=${e.observed_at}` : ''}`;
    return `${r?.machine ?? 'unknown'}/${r?.engine ?? 'unknown'} ${r?.reason ?? 'unknown'} (${numbers})`;
  });
  return parts.join('; ').slice(0, 3000);
}

export function placementPressureSubjects(sql: SqlStorage, now: number): Subject[] {
  const out: Subject[] = [];
  for (const q of sql.exec("SELECT id,created_at FROM fleet_launches WHERE state='queued' AND created_at <= ? ORDER BY created_at LIMIT 128", new Date(now - 300000).toISOString()).toArray() as any[]) {
    // Memory owner already emits the exact refusal episode and numbers.
    const memoryHold = sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name='fleet_memory_holds'").toArray().length
      && sql.exec('SELECT rejected_json FROM fleet_memory_holds WHERE launch_id=?', q.id).toArray().length;
    if (memoryHold) {
      const row:any=sql.exec('SELECT rejected_json, evidence_json FROM fleet_memory_holds WHERE launch_id=?',q.id).toArray()[0];
      let rejected:any[]=[]; try {rejected=JSON.parse(row.rejected_json);} catch {}
      let evidence:Record<string,any>|null=null; try {evidence=JSON.parse(row.evidence_json||'null');} catch {}
      if(rejected.some(r=>r.reason==='launcher_memory_reserve')) continue;
      out.push({kind:'launch',id:`placement-queue:${q.id}`,state:'stuck',detail:`Launch held queued >5 min: ${rejectionEvidence(rejected, evidence)}`,age_s:Math.max(0,Math.floor((now-Date.parse(q.created_at))/1000))});
      continue;
    }
    // No rejection has been recorded for this queue yet: say so instead of
    // attaching current host numbers to an episode that may predate them.
    out.push({ kind: 'launch', id: `placement-queue:${q.id}`, state: 'stuck', detail: 'Launch held queued >5 min for placement admission; no recorded rejection evidence yet', age_s: Math.max(0, Math.floor((now - Date.parse(q.created_at)) / 1000)) });
  }
  // Rejections and terminal failures are retained as history. They do not
  // describe a live queued intent and must not open placement-pressure alerts.
  return out;
}

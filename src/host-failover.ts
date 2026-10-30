/** Host-offline failover (intake 0f12a181). A host whose effective check-in is
 * missing for >= 60 min is `temporarily_dead`: its running sessions that carry
 * open intake work are re-placed by a fresh session (ordinary Fleet placement,
 * so only live eligible hosts qualify). When the host checks in again, the old
 * sessions are fenced with durable stop commands and the host leaves the dead
 * state. Reuses SessionControl start/stop custody and intake notes; the
 * per-session unique request keys make a repeated tick idempotent. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { SessionControlService } from './session_control';
import { IntakeService } from './intake';
import { linkedIntake, hasOpenWork, parseObject } from './intake-session';
import { effectiveCheckin } from './registry';

export const HOST_DEAD_AFTER_MS = 60 * 60 * 1000;
const MAX_PER_TICK = 25;

export class HostFailoverService {
  private busy = false;
  constructor(private sql: SqlStorage, private tx: <T>(f: () => T) => T,
    private sessions: SessionControlService, private now = Date.now) {}

  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS host_failover(machine_id TEXT PRIMARY KEY, state TEXT NOT NULL,
      dead_since INTEGER NOT NULL, marked_at INTEGER NOT NULL, returned_at INTEGER)`);
    this.sql.exec(`CREATE TABLE IF NOT EXISTS host_failover_sessions(session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL,
      intake_task_id TEXT NOT NULL, replacement_key TEXT NOT NULL, replacement_id TEXT, state TEXT NOT NULL,
      fence_key TEXT, fence_command_id TEXT, created_at INTEGER NOT NULL)`);
  }

  /** Dead hosts with their reassignment counts, for hub status. */
  summary(): any[] {
    return (this.sql.exec(`SELECT h.machine_id,h.dead_since,h.marked_at,
      (SELECT COUNT(*) FROM host_failover_sessions s WHERE s.machine_id=h.machine_id AND s.replacement_id IS NOT NULL) AS reassigned_sessions,
      (SELECT COUNT(*) FROM host_failover_sessions s WHERE s.machine_id=h.machine_id AND s.replacement_id IS NULL) AS pending_sessions
      FROM host_failover h WHERE h.state='temporarily_dead' ORDER BY h.machine_id`).toArray() as any[]).map(r => ({
      machine_id: r.machine_id, state: 'temporarily_dead', dead_since: new Date(r.dead_since).toISOString(),
      reassigned_sessions: r.reassigned_sessions, pending_sessions: r.pending_sessions }));
  }

  private note(task: any, key: string, text: string): boolean {
    return new IntakeService(this.sql, this.tx).serviceRecordEvent(task.id, {
      event: { kind: 'note', text, actor: 'hub:service:host-failover', sourceRef: key, authority: 'untrusted_task_data' }, now: this.now(),
    }).ok;
  }

  private async command(path: string, input: any) {
    const result = await this.sessions.handle(new Request('https://hub.internal/api/sessions' + path,
      { method: 'POST', body: JSON.stringify(input) }), {
      caller: { isAdmin: true, kind: 'human', actor: 'hub:service:host-failover' }, scopes: ['admin'],
    });
    return { status: result.status, body: await result.json() as any };
  }

  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      const now = this.now();
      for (const m of this.sql.exec('SELECT id,last_checkin_at,drained FROM machines').toArray() as any[]) {
        try { await this.machine(m, now); } catch { console.warn('host_failover_machine_failed', m.id); }
      }
    } finally { this.busy = false; }
  }

  private async machine(m: any, now: number) {
    const at = Date.parse(effectiveCheckin(String(m.id), m.last_checkin_at) ?? '');
    const state: any = this.sql.exec('SELECT * FROM host_failover WHERE machine_id=?', m.id).toArray()[0];
    // A host with no check-in ever, or one an operator drained, is not "temporarily" dead.
    if (!Number.isFinite(at) || m.drained) return;
    const fresh = now - at < 180000;
    if (state && fresh) {
      if (state.state === 'temporarily_dead') this.sql.exec("UPDATE host_failover SET state='recovered',returned_at=? WHERE machine_id=?", now, m.id);
      return this.fence(m.id, now);
    }
    if (state?.state === 'recovered' && !fresh) this.sql.exec('DELETE FROM host_failover WHERE machine_id=?', m.id);
    if (fresh || now - at < HOST_DEAD_AFTER_MS) return;
    if (state?.state !== 'temporarily_dead') {
      this.sql.exec(`INSERT INTO host_failover VALUES(?,'temporarily_dead',?,?,NULL)
        ON CONFLICT(machine_id) DO UPDATE SET state='temporarily_dead',dead_since=excluded.dead_since,marked_at=excluded.marked_at,returned_at=NULL`, m.id, at, now);
    }
    await this.reassign(m.id);
  }

  private async reassign(machineId: string) {
    const rows = this.sql.exec(`SELECT * FROM session_control_sessions WHERE machine_id=? AND state IN ('running','starting')
      AND id NOT IN (SELECT session_id FROM host_failover_sessions WHERE replacement_id IS NOT NULL) LIMIT ?`, machineId, MAX_PER_TICK).toArray() as any[];
    for (const row of rows) {
      const request = parseObject(row.request_json);
      if (request.keep === true || this.sql.exec('SELECT 1 FROM session_control_protections WHERE session_id=?', row.id).toArray().length) continue;
      const task = linkedIntake(this.sql, row);
      if (!hasOpenWork(task)) continue;
      const key = 'failover-' + row.id;
      this.sql.exec(`INSERT OR IGNORE INTO host_failover_sessions(session_id,machine_id,intake_task_id,replacement_key,replacement_id,state,created_at)
        VALUES(?,?,?,?,NULL,'pending',?)`, row.id, machineId, task.id, key, this.now());
      const ac = task.acceptance_criteria || '';
      const result = await this.command('', { key, intake_task_id: task.id, workspace: request.workspace || task.project,
        ...(request.access ? { access: request.access } : {}),
        parent_id: row.id, mode: 'interactive', with_goal: true,
        brief: `Host ${machineId} has been offline over 60 minutes (temporarily_dead); session ${row.id} is being replaced. Continue intake ${task.id}: read hub intake show ${task.id}, find its worktree branch / last pushed state on origin, and finish all open acceptance criteria:\n${ac}\nKeep intake OPEN. Append DONE only with accepted evidence. The old session is fenced and stopped when its host returns; do not rely on its local files.` });
      if (!result.body?.id) continue;
      this.tx(() => {
        this.sql.exec("UPDATE host_failover_sessions SET replacement_id=?,state='reassigned' WHERE session_id=?", result.body.id, row.id);
        this.note(task, 'host-failover:' + row.id, `Host ${machineId} temporarily_dead (no check-in >= 60 min). Session ${row.id} reassigned to ${result.body.id}; the old session is fenced and stopped if the host returns.`);
      });
    }
  }

  /** The host is back: stop every superseded session (durable, idempotent). */
  private async fence(machineId: string, now: number) {
    const rows = this.sql.exec("SELECT * FROM host_failover_sessions WHERE machine_id=? AND replacement_id IS NOT NULL AND state<>'fenced'", machineId).toArray() as any[];
    for (const r of rows) {
      const live: any = this.sql.exec('SELECT state FROM session_control_sessions WHERE id=?', r.session_id).toArray()[0];
      if (!live || ['stopped', 'cancelled', 'failed', 'exited'].includes(live.state)) {
        this.sql.exec("UPDATE host_failover_sessions SET state='fenced' WHERE session_id=?", r.session_id);
        continue;
      }
      const attempt = r.fence_command_id ? (this.sql.exec('SELECT state FROM session_control_commands WHERE id=?', r.fence_command_id).toArray()[0] as any)?.state : null;
      if (attempt && ['recorded', 'transported', 'claimed', 'acked'].includes(attempt)) continue;
      // A failed attempt gets a fresh key; the host is fresh so delivery is possible.
      const key = 'failover-fence-' + r.session_id + '-' + (r.fence_command_id ? now : 0);
      const result = await this.command('/' + r.session_id + '/stop', { key });
      this.sql.exec("UPDATE host_failover_sessions SET fence_key=?,fence_command_id=?,state='fencing' WHERE session_id=?", key, result.body?.command_id ?? null, r.session_id);
    }
  }
}

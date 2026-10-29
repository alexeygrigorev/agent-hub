/** Recovery of linked unfinished work. Native effects use SessionControl's
 * durable command/ack fences and Fleet's ordinary placement admission. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { SessionControlService } from './session_control';
import { IntakeService } from './intake';
import { nativeObservationFresh } from './session_stall';
import { linkedIntake, hasOpenWork, parseObject } from './intake-session';

export interface IdleRecoveryOwner {
  disposition(sessionId: string): { mode: 'generic' | 'managed' | 'skip'; nudgeMinutes?: number };
  replaceOwned(sessionId: string, episodeKey: string): Promise<boolean>;
}

export class IdleWorkService {
  private busy = false;
  private owner?: IdleRecoveryOwner;
  setRecoveryOwner(owner: IdleRecoveryOwner) { this.owner = owner; }
  constructor(private sql: SqlStorage, private tx: <T>(f: () => T) => T,
    private sessions: SessionControlService, private env: any = {}, private now = Date.now) {}
  init() {
    this.sql.exec(`CREATE TABLE IF NOT EXISTS session_idle_episodes(session_id TEXT PRIMARY KEY,
      episode TEXT NOT NULL, detected_at INTEGER NOT NULL, active INTEGER NOT NULL,
      nudges_json TEXT NOT NULL, last_nudge_at INTEGER, replacement_key TEXT, replacement_id TEXT, binding_identity TEXT)`);
    const cols = this.sql.exec('PRAGMA table_info(session_idle_episodes)').toArray() as any[];
    if (!cols.some(c => c.name === 'binding_identity')) this.sql.exec('ALTER TABLE session_idle_episodes ADD COLUMN binding_identity TEXT');
  }
  private note(task: any, key: string, text: string): boolean {
    return new IntakeService(this.sql, this.tx).serviceRecordEvent(task.id, {
      event: { kind: 'note', text, actor: 'hub:service:idle-work', sourceRef: key, authority: 'untrusted_task_data' }, now: this.now(),
    }).ok;
  }
  private async command(path: string, input: any) {
    const result = await this.sessions.handle(new Request('https://hub.internal/api/sessions' + path,
      { method: 'POST', body: JSON.stringify(input) }), {
      caller: { isAdmin: true, kind: 'human', actor: 'hub:service:idle-work' }, scopes: ['admin'],
    });
    return { status: result.status, body: await result.json() as any };
  }
  async tick() {
    if (this.busy) return;
    this.busy = true;
    try {
      for (const item of this.sessions.attention()) {
        try {
        const row: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id=?', item.session_id).toArray()[0];
        const task = row && linkedIntake(this.sql, row);
        if (!hasOpenWork(task)) continue;
        const request = parseObject(row.request_json);
        const ownership = this.owner?.disposition(row.id) || { mode: 'generic' as const };
        if (ownership.mode === 'skip') continue;
        if (item.waiting_until && Date.parse(item.waiting_until) > this.now()) continue;
        if (request.keep === true || this.sql.exec('SELECT 1 FROM session_control_protections WHERE session_id=?', row.id).toArray().length) continue;
        const binding = parseObject(row.binding_json);
        const identity = JSON.stringify([row.machine_id, binding.native_id, binding.native_created_at_ms, binding.generation]);
        let episode: any = this.sql.exec('SELECT * FROM session_idle_episodes WHERE session_id=?', row.id).toArray()[0];
        if (!episode?.active || episode.binding_identity !== identity) {
          const key = crypto.randomUUID();
          this.tx(() => {
            if (!this.note(task, 'idle-detected:' + key, `Hub detected ${item.state} for session ${row.id}; intake ${task.id}; idle_for=${item.idle_for}s; last_activity_at=${item.last_activity_at}. Episode ${key}.`)) return;
            this.sql.exec(`INSERT INTO session_idle_episodes VALUES(?,?,?,1,'[]',?,NULL,NULL,?)
              ON CONFLICT(session_id) DO UPDATE SET episode=excluded.episode,detected_at=excluded.detected_at,active=1,nudges_json='[]',replacement_key=NULL,replacement_id=NULL,binding_identity=excluded.binding_identity`,
              row.id, key, this.now(), episode?.last_nudge_at ?? null, identity);
          });
          episode = this.sql.exec('SELECT * FROM session_idle_episodes WHERE session_id=?', row.id).toArray()[0];
          if (!episode?.active) continue;
        }
        const host: any = this.sql.exec('SELECT observed_at FROM fleet_catalog_seen WHERE machine_id=?', row.machine_id).toArray()[0];
        // Uncertain host observations are attention, never new native effects.
        const hostAt = Date.parse(host?.observed_at || '');
        if (!Number.isFinite(hostAt) || hostAt > this.now() + 60000 || this.now() - hostAt > 180000) continue;
        const observedNative: any = binding.native_id && this.sql.exec('SELECT progress_json FROM fleet_sessions WHERE machine_id=? AND native_id=?',row.machine_id,binding.native_id).toArray()[0];
        const sampledAt = Date.parse(parseObject(observedNative?.progress_json).sampled_at || '');
        // Fast census reports may reuse a slow sampler's old enrichment.
        // Fresh transport alone cannot authorize a nudge or replacement.
        if (!Number.isFinite(sampledAt) || sampledAt > this.now() + 60000 || this.now() - sampledAt > 180000) continue;
        const nudges = JSON.parse(episode.nudges_json);
        for (const n of nudges) {
          if (!n.command_id || n.failed !== undefined || this.now() - n.at < 1800000) continue;
          const cmd: any = n.command_id && this.sql.exec('SELECT state,updated_at FROM session_control_commands WHERE id=?', n.command_id).toArray()[0];
          const progress = Date.parse(item.last_activity_at || '');
          n.failed = cmd?.state !== 'acked' || Date.parse(cmd.updated_at) > n.at + 1800000 || !(progress > n.at && progress <= n.at + 1800000);
        }
        this.sql.exec('UPDATE session_idle_episodes SET nudges_json=? WHERE session_id=?', JSON.stringify(nudges), row.id);
        if (episode.replacement_id) {
          const replacement: any = this.sql.exec('SELECT * FROM session_control_sessions WHERE id=?', episode.replacement_id).toArray()[0];
          const rb = parseObject(replacement?.binding_json);
          const native: any = rb.native_id && this.sql.exec('SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?', replacement.machine_id, rb.native_id).toArray()[0];
          if (replacement?.state === 'running' && rb.native_id && rb.native_id !== binding.native_id && native?.phase === 'running' && native.created_at_ms === rb.native_created_at_ms && native.tag === rb.tag &&
              native.workspace === rb.workspace && nativeObservationFresh(this.sql, native, this.now())) {
            await this.command('/' + row.id + '/stop', { key: 'idle-stop-' + episode.episode });
          }
          continue;
        }
        const ac = task.acceptance_criteria || '';
        const lastNote = [...(task.history || [])].reverse().find((e: any) => e.kind === 'note')?.text || '(no intake note)';
        if (nudges.filter((n: any) => n.failed === true).length >= 2) {
          const key = episode.replacement_key || 'idle-replace-' + episode.episode;
          if (ownership.mode === 'managed') {
            // The controller owns generation fencing and lineage, but eligibility
            // comes exclusively from this shared verified-nudge episode.
            await this.owner!.replaceOwned(row.id, key);
            continue;
          }
          this.sql.exec('UPDATE session_idle_episodes SET replacement_key=? WHERE session_id=?', key, row.id);
          const result = await this.command('', { key, intake_task_id: task.id, workspace: binding.workspace,
            ...(request.cwd ? { cwd: request.cwd } : {}), ...(request.access ? { access: request.access } : {}),
            parent_id: row.id, mode: 'interactive', with_goal: true,
            brief: `Replace stalled session ${row.id} for intake ${task.id}. Read hub intake show ${task.id}. Complete all open acceptance criteria:\n${ac}\nKeep intake OPEN. Append DONE only with accepted evidence. Preserve other sessions and the existing workspace.` });
          if (result.body?.id) this.tx(() => {
            this.sql.exec('UPDATE session_idle_episodes SET replacement_id=? WHERE session_id=?', result.body.id, row.id);
            if (!this.note(task, 'idle-replacement:' + episode.episode, `Replacement ${result.body.id} submitted for ${row.id}; lineage parent=${row.id}. Old session stays until replacement is running.`)) throw new Error('idle_lineage_note_failed');
          });
          continue;
        }
        const configured = Number(ownership.nudgeMinutes ?? this.env.HUB_IDLE_OPEN_AC_NUDGE_MINUTES);
        const delay = Number.isFinite(configured) && configured > 0 && configured <= 10080 ? configured * 60000 : 1800000;
        let n = nudges.find((n: any) => !n.command_id);
        if (item.state !== 'idle_with_open_ac' || !(Number.isFinite(item.idle_for) && item.idle_for * 1000 >= delay) ||
            (!n && episode.last_nudge_at !== null && this.now() - episode.last_nudge_at < 3600000)) continue;
        // Persist the deterministic command key before awaiting transport. An
        // interrupted tick retries that same command, never emits another prompt.
        if (!n) { n = { key: 'idle-nudge-' + episode.episode + '-' + nudges.length, at: this.now() }; nudges.push(n); }
        if (typeof n.body !== 'string') {
          // Recover pre-upgrade attempts too: their command may be durable even
          // when the response was lost before command_id reached the episode.
          const prior: any = this.sql.exec("SELECT payload_json FROM session_control_commands WHERE session_id=? AND actor='hub:service:idle-work' AND request_key=?", row.id, n.key).toArray()[0];
          n.body = parseObject(prior?.payload_json).body ?? `${(request.with_goal || item.goal_status) && item.goal_status !== 'unregistered' ? '/goal resume\n' : ''}Resume intake ${task.id}. Open acceptance criteria:\n${ac}\nLast intake note:\n${lastNote}\nRead current intake, acknowledge this message, then continue authorized work.`;
        }
        this.sql.exec('UPDATE session_idle_episodes SET nudges_json=?,last_nudge_at=? WHERE session_id=?', JSON.stringify(nudges), n.at, row.id);
        const result = await this.command('/' + row.id + '/send', { key: n.key, body: n.body });
        if (result.body.command_id) {
          n.command_id = result.body.command_id;
          this.sql.exec('UPDATE session_idle_episodes SET nudges_json=? WHERE session_id=?', JSON.stringify(nudges), row.id);
        }
        } catch { console.warn('idle_work_session_failed', item.session_id); }
      }
      const active = new Set(this.sessions.attention().map(s => s.session_id));
      for (const e of this.sql.exec('SELECT session_id FROM session_idle_episodes WHERE active=1').toArray() as any[]) {
        if (!active.has(e.session_id)) this.sql.exec('UPDATE session_idle_episodes SET active=0 WHERE session_id=?', e.session_id);
      }
    } finally { this.busy = false; }
  }
}

/** Durable host bring-up goals. Installation alone never proves readiness. */
import type { SqlStorage } from '@cloudflare/workers-types';
import type { FleetService } from './fleet';
import { effectiveCheckin } from './registry';
import { PACKAGES } from './provisioning';

export class HostGoals {
  constructor(private sql: SqlStorage, private fleet: FleetService, private now = Date.now) {}
  init() {
    this.sql.exec('CREATE TABLE IF NOT EXISTS host_goals(machine_id TEXT PRIMARY KEY, generation TEXT NOT NULL, engines_json TEXT NOT NULL, workspace TEXT NOT NULL, sha TEXT NOT NULL, set_at TEXT NOT NULL)');
    this.sql.exec('CREATE TABLE IF NOT EXISTS host_goal_proofs(generation TEXT NOT NULL,label TEXT NOT NULL,revision TEXT NOT NULL,launch_id TEXT NOT NULL,verified_at TEXT,native_id TEXT,PRIMARY KEY(generation,label,revision))');
    this.sql.exec('CREATE TABLE IF NOT EXISTS host_goal_holds(generation TEXT NOT NULL,label TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(generation,label))');
    this.sql.exec('CREATE INDEX IF NOT EXISTS host_goal_proof_launch ON host_goal_proofs(launch_id)');
    // Routing initializes before fleet/provisioning on a fresh database.
    // Hub calls init again after both schemas exist.
    if (this.sql.exec("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('fleet_sessions','engine_installations')").toArray().length !== 2) return;
    // Capture positive evidence as it arrives. A short real session may exit
    // between maintenance ticks; its observed running state must not be lost.
    for (const event of ['INSERT', 'UPDATE']) this.sql.exec(`
      CREATE TRIGGER IF NOT EXISTS host_goal_running_${event.toLowerCase()}
      AFTER ${event} ON fleet_sessions WHEN NEW.phase='running'
      BEGIN
        UPDATE host_goal_proofs SET verified_at=NEW.observed_at,native_id=NEW.native_id
        WHERE verified_at IS NULL AND launch_id IN (
          SELECT id FROM fleet_launches WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id
        ) AND EXISTS (
          SELECT 1 FROM fleet_launches l
          JOIN host_goals g ON g.generation=host_goal_proofs.generation AND g.machine_id=l.machine_id
          JOIN host_release_targets r ON r.machine_id=g.machine_id
          JOIN engine_installations i ON i.machine_id=g.machine_id AND i.label=host_goal_proofs.label AND i.revision=host_goal_proofs.revision
          WHERE l.id=host_goal_proofs.launch_id AND l.state IN ('starting','running')
            AND l.account=NEW.account AND l.engine=NEW.engine AND l.profile IS NEW.profile AND l.workspace=NEW.workspace
            AND l.created_at>=g.set_at AND NEW.observed_at>=l.created_at
            AND julianday(NEW.observed_at) BETWEEN julianday('now','-180 seconds') AND julianday('now','+30 seconds')
            AND r.current_sha=g.sha AND r.state IN ('current','updated') AND r.reported_at>=g.set_at
        );
      END`);
  }
  set(machine: string, input: any): { error: string } | any {
    if (!input || Object.keys(input).some(k => !['engines', 'workspace', 'sha'].includes(k)) ||
      !Array.isArray(input.engines) || !input.engines.length || input.engines.length > 16 ||
      new Set(input.engines).size !== input.engines.length || input.engines.some((e: any) => typeof e !== 'string' || !/^[a-z][a-z0-9_-]{0,47}$/.test(e)) ||
      typeof input.workspace !== 'string' || !input.workspace || input.workspace.length > 512 ||
      typeof input.sha !== 'string' || !/^[a-f0-9]{40}$/.test(input.sha)) return { error: 'invalid_host_goal' };
    const host: any = this.sql.exec('SELECT capabilities FROM machines WHERE id=?', machine).toArray()[0];
    if (!host) return { error: 'machine_not_found' };
    if (!(JSON.parse(host.capabilities || '{}').workspaces || []).includes(input.workspace)) return { error: 'workspace_not_advertised' };
    for (const label of input.engines) if (!this.sql.exec('SELECT 1 FROM engine_bundles WHERE label=?', label).toArray().length) return { error: 'engine_bundle_missing' };
    const previous: any = this.sql.exec('SELECT * FROM host_goals WHERE machine_id=?', machine).toArray()[0];
    if (previous && previous.sha === input.sha && previous.workspace === input.workspace && previous.engines_json === JSON.stringify(input.engines)) return this.view(machine);
    const at = new Date(this.now()).toISOString();
    this.sql.exec('INSERT OR REPLACE INTO host_goals VALUES(?,?,?,?,?,?)', machine, crypto.randomUUID(), JSON.stringify(input.engines), input.workspace, input.sha, at);
    this.sql.exec('INSERT INTO host_release_targets(machine_id,sha,set_by,set_at) VALUES(?,?,?,?) ON CONFLICT(machine_id) DO UPDATE SET sha=excluded.sha,set_by=excluded.set_by,set_at=excluded.set_at', machine, input.sha, 'host-goal', at);
    return this.view(machine);
  }
  view(machine: string): any {
    const goal: any = this.sql.exec('SELECT * FROM host_goals WHERE machine_id=?', machine).toArray()[0];
    if (!goal) return null;
    const host: any = this.sql.exec('SELECT last_checkin_at,drained FROM machines WHERE id=?', machine).toArray()[0];
    const seen = Date.parse(effectiveCheckin(machine, host?.last_checkin_at) || '');
    const online = !host?.drained && Number.isFinite(seen) && this.now() - seen <= 180000;
    const release: any = this.sql.exec('SELECT * FROM host_release_targets WHERE machine_id=?', machine).toArray()[0];
    const releaseReady = release?.current_sha === goal.sha && ['current','updated'].includes(release?.state) && release.reported_at >= goal.set_at;
    const engines = JSON.parse(goal.engines_json).map((label: string) => {
      const bundle: any = this.sql.exec('SELECT provider,revision FROM engine_bundles WHERE label=?', label).toArray()[0];
      const installed = !!bundle && !!this.sql.exec('SELECT 1 FROM engine_installations WHERE machine_id=? AND label=? AND revision=?', machine, label, bundle.revision).toArray().length;
      const proof: any = bundle && this.sql.exec('SELECT * FROM host_goal_proofs WHERE generation=? AND label=? AND revision=?', goal.generation, label, bundle.revision).toArray()[0];
      const launch: any = proof && this.sql.exec('SELECT * FROM fleet_launches WHERE id=?', proof.launch_id).toArray()[0];
      const native: any = launch?.native_id && this.sql.exec("SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=? AND phase='running' AND account=?", machine, launch.native_id, label).toArray()[0];
      const running = installed && online && releaseReady && launch?.state === 'running' && launch.created_at >= goal.set_at && native && native.engine === PACKAGES[bundle.provider]?.engine && native.profile === launch.profile && native.workspace === launch.workspace && this.now() - Date.parse(native.observed_at) <= 180000;
      const hold: any = this.sql.exec('SELECT reason FROM host_goal_holds WHERE generation=? AND label=?', goal.generation, label).toArray()[0];
      return { label, revision: bundle?.revision ?? null, installed, placement_reason: hold?.reason ?? null, launch_id: proof?.launch_id ?? null, native_id: proof?.native_id ?? native?.native_id ?? null, verified_at: proof?.verified_at ?? null,
        running_now: !!running, state: installed && (proof?.verified_at || running) ? 'running_proven' : !installed ? 'installing' : !proof ? 'awaiting_placement' : launch?.state === 'running' ? 'verifying_native' : launch?.state ?? 'unknown' };
    });
    return { machine_id: machine, generation: goal.generation, desired_sha: goal.sha, workspace: goal.workspace, online, release_ready: releaseReady, release_state: release?.state ?? 'report_missing', autostart_state: release?.autostart_state ?? null, release_error: release?.error ?? null, engines, state: !online ? 'waiting_for_host' : !releaseReady ? 'updating_release' : engines.every((e: any) => e.state === 'running_proven') ? 'done' : 'bringing_up' };
  }
  /** Called by Hub maintenance. Existing provisioning sync installs bundles in user scope.
   * The normal fleet admission path retains all quota/resource/claim checks. */
  reconcile(machine?: string) {
    for (const goal of this.sql.exec(`SELECT * FROM host_goals ${machine ? 'WHERE machine_id=?' : ''} ORDER BY machine_id LIMIT 128`, ...(machine ? [machine] : [])).toArray() as any[]) {
      const view = this.view(goal.machine_id);
      if (!view.online || !view.release_ready) continue;
      for (const engine of view.engines) {
        if (engine.running_now && !engine.verified_at) this.sql.exec('UPDATE host_goal_proofs SET verified_at=?,native_id=? WHERE generation=? AND label=? AND revision=?', new Date(this.now()).toISOString(), engine.native_id, goal.generation, engine.label, engine.revision);
        if (!engine.installed || engine.launch_id) continue;
        const bundle: any = this.sql.exec('SELECT provider FROM engine_bundles WHERE label=?', engine.label).toArray()[0];
        const result = this.fleet.reserve({ machine: goal.machine_id, engine: PACKAGES[bundle.provider].engine, account: engine.label, workspace: goal.workspace,
          prompt: 'Verify this host engine starts and can respond. Read only; do not modify files or send messages. Report the host and engine, then finish.',
          access: { mode: 'read' }, idempotency_key: `host-goal-${goal.generation}-${engine.revision}-${engine.label}` },
          { caller: { isAdmin: true, actor: 'hub-host-goal', kind: 'human' }, scopes: ['admin'] });
        if (result.status < 300) {
          this.sql.exec('INSERT OR IGNORE INTO host_goal_proofs(generation,label,revision,launch_id) VALUES(?,?,?,?)', goal.generation, engine.label, engine.revision, result.value.id);
          this.sql.exec('DELETE FROM host_goal_holds WHERE generation=? AND label=?', goal.generation, engine.label);
        } else {
          const reason = typeof result.value?.error === 'string' && /^[a-z_]{1,64}$/.test(result.value.error) ? result.value.error : 'admission_refused';
          this.sql.exec('INSERT OR REPLACE INTO host_goal_holds VALUES(?,?,?)', goal.generation, engine.label, reason);
        }
      }
    }
  }
}

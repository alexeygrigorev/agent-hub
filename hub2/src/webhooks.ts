/** Hub2 adapter for the existing Hub1 webhook authentication, renderer and queue.
 * Job routes reuse session-start's durable dispatch saga, never a second launcher.
 */
import type { SqlStorage } from '@cloudflare/workers-types';
import { WebhookService, WEBHOOK_SCHEMA } from '../../src/webhooks';
import { MeteredDb } from './db';
import { actorFrom, bodyObject, error, hasScope, json } from './http';
import { startSession } from './worker';
import type { Actor, Hub2Env, JsonRecord } from './types';

type Launch = { project: string; workspace: string; machine: string };
type Delivery = { id: string; source_id: string; body: string; status: string; attempts: number; intake_id: string | null; session_id: string | null; launch_json: string; created_at: string };
export const HUB2_WEBHOOK_SCHEMA = [
  ...WEBHOOK_SCHEMA.split(';').filter(s => s.trim()),
  'ALTER TABLE session_deliveries ADD COLUMN intake_id TEXT',
  'ALTER TABLE webhook_routes ADD COLUMN prompt_id TEXT',
  'ALTER TABLE webhook_routes ADD COLUMN prompt_vars TEXT',
  'CREATE TABLE webhook_jobs(route_id TEXT PRIMARY KEY,launch_json TEXT NOT NULL)',
  'CREATE TABLE webhook_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,actor TEXT,action TEXT,resource TEXT,target TEXT,detail TEXT,at TEXT)',
];

// Legacy queries stay metered and their audit cannot collide with Control's audit.
function legacySql(db: MeteredDb): SqlStorage {
  return { exec(query: string, ...bindings: unknown[]) {
    const rows = db.all(query.replace(/\baudit\b/g, 'webhook_audit'), ...bindings);
    return { toArray: () => rows };
  } } as unknown as SqlStorage;
}

export class Hub2Webhooks extends WebhookService {
  private pumpingJobs = false;
  constructor(private readonly ctx2: DurableObjectState, private readonly db2: MeteredDb, private readonly env2: Hub2Env, private readonly starter: typeof startSession = startSession) {
    super(legacySql(db2), fn => db2.transaction(ctx2, fn), Date.now, () => null, null);
  }

  async route(request: Request): Promise<Response | null> {
    const path = new URL(request.url).pathname;
    if (this.db2.schemaVersion > 0 && this.db2.schemaVersion < 9) {
      return path.startsWith('/api/webhooks') || path.startsWith('/webhooks/') ? error('webhook_schema_not_ready', 503) : null;
    }
    const receipt = path.match(/^\/webhooks\/([0-9a-f-]{36})\/deliveries\/([0-9a-f-]{36})$/);
    if (receipt) return this.receipt(request, receipt[1], receipt[2]);
    if (/^\/webhooks\/[0-9a-f-]{36}$/.test(path)) {
      const response = await this.handleIngest(request, path);
      await this.wake();
      return response;
    }
    if (path !== '/api/webhooks' && !path.startsWith('/api/webhooks/')) return null;
    const actor = actorFrom(request);
    if (!actor || !hasScope(actor, 'operator:admin')) return error('forbidden', 403);
    if (path === '/api/webhooks' && request.method === 'POST') return this.createJob(request, actor);
    const response = await this.handle(request, { caller: { kind: 'human', isAdmin: true, actor: actor.id }, scopes: ['admin'] });
    await this.wake();
    return response;
  }

  private async createJob(request: Request, actor: Actor): Promise<Response> {
    const input = await bodyObject(request);
    const launch = input.launch as Launch | undefined;
    if (!launch || !/^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(launch.project)
      || typeof launch.workspace !== 'string' || !launch.workspace.startsWith('/') || launch.workspace.length > 1024
      || typeof launch.machine !== 'string' || !/^[A-Za-z0-9_.-]{1,100}$/.test(launch.machine)) return error('invalid_launch', 400);
    if (input.auth_mode && input.auth_mode !== 'token') return error('hub2_job_requires_token', 400);
    const response = await this.handle(new Request(request.url, { method: 'POST', body: JSON.stringify({ ...input, target_label: 'job:' + String(input.name), auth_mode: 'token' }) }),
      { caller: { kind: 'human', isAdmin: true, actor: actor.id }, scopes: ['admin'] });
    if (!response.ok) return response;
    const created = await response.json() as JsonRecord;
    this.db2.run('INSERT INTO webhook_jobs(route_id,launch_json) VALUES(?,?)', created.id, JSON.stringify({ project: launch.project, workspace: launch.workspace, machine: launch.machine }));
    await this.wake();
    return json(created, 201);
  }

  private jobActor(project: string, route: string): Actor {
    return { id: 'webhook:' + route, kind: 'agent', scopes: ['session:start', 'intake:write', 'intake:read', 'project:' + project, 'worker_project:' + project] };
  }

  override async pump(): Promise<number | undefined> {
    if (this.pumpingJobs) return;
    this.pumpingJobs = true;
    try {
      const rows = this.db2.all<Delivery>(`SELECT d.*,j.launch_json FROM session_deliveries d JOIN webhook_jobs j ON j.route_id=d.source_id
        WHERE d.status='pending' AND d.next_attempt_at<=? ORDER BY d.next_attempt_at,d.id LIMIT 8`, new Date().toISOString());
      for (const row of rows) await this.dispatch(row);
    } finally { this.pumpingJobs = false; }
    return this.wake();
  }

  private async dispatch(row: Delivery): Promise<void> {
    const launch = JSON.parse(row.launch_json) as Launch;
    const key = 'webhook-' + row.id;
    // Payload is rendered data inside an operator-authored template; it cannot
    // choose project, machine, permissions or launch parameters.
    const request = new Request('https://hub.internal/api/sessions', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify({ ...launch, brief: draftBrief(row), purpose: 'implement', engine: 'claude', model: 'opus', effort: 'high',
        engine_override_reason: 'workshop-followup-materials requires Opus for workshop drafts', with_goal: false, name: 'Webhook draft ' + row.id }) });
    try {
      const response = await this.starter(request, this.env2, this.jobActor(launch.project, row.source_id), true);
      const started = await response.json() as JsonRecord;
      if (!response.ok || typeof started.id !== 'string' || typeof started.intake_id !== 'string') {
        this.retry(row, String(started.error || 'dispatch_refused'));
        return;
      }
      this.db2.run("UPDATE session_deliveries SET status='queued',session_id=?,intake_id=?,attempts=attempts+1,last_error=NULL,updated_at=? WHERE id=? AND status='pending'",
        started.id, started.intake_id, new Date().toISOString(), row.id);
    } catch {
      // Transport failures retain the same durable start key for alarm retry.
      this.retry(row, 'dispatch_unavailable');
    }
  }

  private retry(row: Delivery, code: string): void {
    const failed = row.attempts >= 4;
    this.db2.run('UPDATE session_deliveries SET status=?,attempts=attempts+1,last_error=?,next_attempt_at=?,updated_at=? WHERE id=? AND status=?',
      failed ? 'failed' : 'pending', code.slice(0, 100), new Date(Date.now() + 60_000).toISOString(), new Date().toISOString(), row.id, 'pending');
  }

  async wake(): Promise<number | undefined> {
    if (this.db2.schemaVersion > 0 && this.db2.schemaVersion < 9) return;
    const next = this.db2.one<{ next_attempt_at: string }>("SELECT next_attempt_at FROM session_deliveries WHERE status='pending' ORDER BY next_attempt_at,id LIMIT 1");
    if (!next) return;
    const due = Math.max(Date.now() + 1000, Date.parse(next.next_attempt_at));
    const current = await this.ctx2.storage.getAlarm();
    if (current === null || current > due) { this.db2.alarmOperation(); await this.ctx2.storage.setAlarm(due); }
    return due;
  }

  private async receipt(request: Request, routeId: string, deliveryId: string): Promise<Response> {
    if (request.method !== 'GET') return error('method_not_allowed', 405);
    const route = this.db2.one<Record<string, unknown>>('SELECT * FROM webhook_routes WHERE id=? AND enabled=1', routeId);
    if (!route) return error('not_found', 404);
    const refused = await this.verifyRouteAuth(request, route, '');
    if (refused) return refused;
    const row = this.db2.one<Delivery>('SELECT d.*,j.launch_json FROM session_deliveries d JOIN webhook_jobs j ON j.route_id=d.source_id WHERE d.id=? AND d.source_id=?', deliveryId, routeId);
    if (!row) return error('not_found', 404);
    if (!row.intake_id) return json({ delivery_id: row.id, status: row.status, session_id: row.session_id, last_error: (row as unknown as JsonRecord).last_error });
    return this.jobReceipt(row);
  }

  private async jobReceipt(row: Delivery): Promise<Response> {
    const launch = JSON.parse(row.launch_json) as Launch;
    const actor = this.jobActor(launch.project, row.source_id);
    const stub = this.env2.PROJECTS.get(this.env2.PROJECTS.idFromName(launch.project));
    const read = async (suffix: string): Promise<JsonRecord | null> => {
      const response = await stub.fetch(new Request('https://project.internal/api/intakes/' + row.intake_id + suffix
        + '?project=' + encodeURIComponent(launch.project), { headers: { 'x-hub2-actor': JSON.stringify(actor) } }));
      return response.ok ? await response.json() as JsonRecord : null;
    };
    const view = await read('');
    if (!view) return error('job_observation_unavailable', 503);
    const intake = view.intake as JsonRecord;
    const stats = (await read('/stats'))?.stats as JsonRecord | undefined;
    const startedAt = stats?.first_session_started_at;
    const started = typeof startedAt === 'number' && Number.isFinite(startedAt);
    // accepted is Hub2's independently verified task terminal, not process exit.
    const finished = intake.status === 'accepted';
    const failed = ['failed','blocked','cancelled','archived'].includes(String(intake.status));
    const context = await read('/context');
    const artifacts = draftArtifacts(context?.events, row.id);
    return json({ delivery_id: row.id, session_id: row.session_id,
      status: finished ? 'finished' : failed ? 'failed' : started ? 'running' : 'queued',
      started_at: started ? new Date(startedAt as number).toISOString() : null,
      finished_at: finished && typeof intake.updated_at === 'number' ? new Date(intake.updated_at).toISOString() : null,
      artifacts, last_error: failed ? String(intake.status) : null });
  }
}

/** Only the job's authenticated intake append can publish this manifest. A
 * finished receipt without both outputs stays unverifiable at the producer.
 */
function draftArtifacts(events: unknown, deliveryId: string): JsonRecord[] {
  if (!Array.isArray(events)) return [];
  for (const event of events.slice().reverse()) {
    if (event.kind !== 'worker_append') continue;
    const evidence = event.body?.evidence;
    if (evidence?.kind !== 'recording_preparation_drafts' || evidence.delivery_id !== deliveryId || !Array.isArray(evidence.artifacts)) continue;
    const items: JsonRecord[] = [];
    for (const item of evidence.artifacts) {
      if (!item || !['video_chapters','workshops_raw_summary'].includes(item.kind)
        || typeof item.path !== 'string' || !item.path.startsWith('.local/') || item.path.includes('..') || item.path.length > 500
        || typeof item.sha256 !== 'string' || !/^[0-9a-f]{64}$/.test(item.sha256)) return [];
      items.push({ kind: item.kind, path: item.path, sha256: item.sha256 });
    }
    return new Set(items.map(item => item.kind)).size === 2 ? items : [];
  }
  return [];
}

function draftBrief(row: Delivery): string {
  const manifest = { kind: 'recording_preparation_drafts', delivery_id: row.id, artifacts: [
    { kind: 'video_chapters', path: '.local/<unique>/chapters.md', sha256: '<actual SHA256>' },
    { kind: 'workshops_raw_summary', path: '.local/<unique>/<dated-workshop>/README.md', sha256: '<actual SHA256>' },
  ] };
  return row.body + '\n\nTrusted Hub adapter footer: delivery UUID ' + row.id
    + '\nAll outputs must remain local review drafts. Do not publish or send email. '
    + 'Write the following manifest shape as JSON using actual file paths/hashes, then use hub intake append '
    + '<your intake id> --event-file <manifest file>. Your task completion proof must let the independent verifier read both files.\n'
    + JSON.stringify(manifest);
}

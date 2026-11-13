import type { MeteredDb } from './db';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, boundedText, error, hasScope, HttpError, json } from './http';
import type { Actor, JsonRecord } from './types';

/**
 * Intake prerequisites (`hub intake depend`) and reviewed PNG artifacts (`hub task-artifact`).
 *
 * Prerequisites live in the existing event journal (rollback-safe, no schema bump); the
 * prerequisite may belong to another project, so the edge router resolves and authorizes it
 * and passes the canonical id/project in the trusted `x-hub2-prerequisite` header.
 * Artifacts live in a lazily created side table so the large payloads never enter history pages.
 *
 * Every refusal names the failed check (error slug plus a short detail).
 */
export const ARTIFACT_MAX_BYTES = 6 * 1024 * 1024;
export const ARTIFACT_MAX_PER_INTAKE = 16;
export const ARTIFACT_MAX_TOTAL_BYTES = 48 * 1024 * 1024;
// base64 of the largest PNG plus a small JSON envelope.
export const ARTIFACT_BODY_LIMIT = Math.ceil(ARTIFACT_MAX_BYTES / 3) * 4 + 16_384;
export const MAX_PREREQUISITES = 32;
export const PREREQUISITE_HEADER = 'x-hub2-prerequisite';
const CLOSED = ['accepted', 'archived'];
const ARTIFACT_TABLE = 'CREATE TABLE IF NOT EXISTS intake_artifacts(intake_id TEXT NOT NULL,name TEXT NOT NULL,media_type TEXT NOT NULL,bytes INTEGER NOT NULL,sha256 TEXT NOT NULL,data_base64 TEXT NOT NULL,actor_id TEXT NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(intake_id,name)) WITHOUT ROWID';
const NAME = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;
const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

type Intake = { id: string; project: string; status: string; updated_at: number };
export type Prerequisite = { intake_id: string; project: string; added_at: number; added_by: string };
type Hooks = {
  transaction: <T>(callback: () => T) => T;
  append: (taskId: string, eventKey: string, actorId: string, kind: string, body: JsonRecord, at: number) => void;
  canRead: (actor: Actor | undefined, intake: Intake) => boolean;
  canWrite: (actor: Actor | undefined, intake: Intake) => boolean;
};

export class IntakeRelations {
  private tableReady = false;
  constructor(private readonly db: MeteredDb, private readonly hooks: Hooks) {}

  /** Current prerequisite set, folded from the bounded dependency journal. */
  prerequisites(intakeId: string): Prerequisite[] {
    const current = new Map<string, Prerequisite>();
    const rows = this.db.all<{ kind: string; body_json: string; actor_id: string; at: number }>(
      "SELECT kind, body_json, actor_id, at FROM intake_events WHERE task_id=? AND kind IN ('dependency_added','dependency_removed') ORDER BY seq LIMIT 1024",
      intakeId,
    );
    for (const row of rows) {
      let body: JsonRecord;
      try { body = JSON.parse(row.body_json) as JsonRecord; } catch { continue; }
      if (typeof body.on !== 'string') continue;
      if (row.kind === 'dependency_removed') current.delete(body.on);
      else current.set(body.on, { intake_id: body.on, project: String(body.on_project ?? ''), added_at: row.at, added_by: row.actor_id });
    }
    return [...current.values()];
  }

  artifacts(intakeId: string): JsonRecord[] {
    if (!this.hasTable()) return [];
    return this.db.all<JsonRecord>('SELECT name, media_type, bytes, sha256, created_at FROM intake_artifacts WHERE intake_id=? ORDER BY created_at, name LIMIT ?',
      intakeId, ARTIFACT_MAX_PER_INTAKE);
  }

  /** Routes `/api/intakes/:id/(depend|links|dependencies|artifacts[/name])`; undefined when not ours. */
  async fetch(request: Request, intakeId: string, action: string, name?: string): Promise<Response | undefined> {
    if (action === 'depend' && request.method === 'POST') return await this.depend(request, intakeId);
    if (action === 'links' && request.method === 'GET') return this.links(request, intakeId);
    if (action === 'artifacts' && !name && request.method === 'POST') return await this.upload(request, intakeId);
    if (action === 'artifacts' && !name && request.method === 'GET') return this.listArtifacts(request, intakeId);
    if (action === 'artifacts' && name && request.method === 'GET') return this.download(request, intakeId, name);
    return undefined;
  }

  /** Edge-internal: the raw prerequisite ids, for the cross-project cycle walk. */
  internalPrerequisites(intakeId: string): Response {
    return json({ intake_id: intakeId, depends_on: this.prerequisites(intakeId).map(p => ({ intake_id: p.intake_id, project: p.project })) });
  }

  private intake(intakeId: string): Intake | undefined {
    return this.db.one<Intake>('SELECT id, project, status, updated_at FROM intakes WHERE id=?', intakeId);
  }

  private links(request: Request, intakeId: string): Response {
    const actor = actorFrom(request), intake = this.intake(intakeId);
    if (!intake) return error('intake_not_found', 404, `intake ${intakeId} does not exist in this project`);
    if (!this.hooks.canRead(actor, intake)) return error('forbidden', 403, 'caller cannot read this intake');
    return json({ intake_id: intake.id, project: intake.project, depends_on: this.prerequisites(intake.id), artifacts: this.artifacts(intake.id) });
  }

  private async depend(request: Request, intakeId: string): Promise<Response> {
    const actor = actorFrom(request), body = await bodyObject(request, 4096);
    const unknown = Object.keys(body).filter(k => !['key', 'expected_version', 'on', 'remove'].includes(k));
    if (unknown.length) return error('unknown_depend_field', 400, 'unsupported field(s): ' + unknown.slice(0, 4).join(', '));
    if (typeof body.key !== 'string' || !/^[A-Za-z0-9_.:-]{1,160}$/.test(body.key)) return error('idempotency_key_required', 400, 'key must be 1-160 of A-Z a-z 0-9 _ . : -');
    if (body.remove !== undefined && typeof body.remove !== 'boolean') return error('invalid_remove', 400, 'remove must be true or false');
    if (body.expected_version !== undefined && !Number.isSafeInteger(body.expected_version)) return error('invalid_expected_version', 400, 'expected_version must be an integer (task.control.version)');
    let prerequisite: { id: string; project: string };
    try {
      prerequisite = JSON.parse(request.headers.get(PREREQUISITE_HEADER) || 'null');
    } catch { prerequisite = null as never; }
    if (!prerequisite || typeof prerequisite.id !== 'string' || typeof prerequisite.project !== 'string')
      return error('prerequisite_unresolved', 400, 'the prerequisite (--on) was not resolved by the Hub router');
    const remove = body.remove === true, key = body.key;
    const digest = await sha256Hex(JSON.stringify([prerequisite.id, remove]));
    const eventKey = 'depend:' + intakeId + ':' + key;
    return this.hooks.transaction(() => {
      const intake = this.intake(intakeId);
      if (!intake) return error('intake_not_found', 404, `intake ${intakeId} does not exist in this project`);
      if (!this.hooks.canWrite(actor, intake)) return error('forbidden', 403, 'caller lacks intake:write on the dependent intake project');
      const previous = this.db.one<{ body_json: string }>('SELECT body_json FROM intake_events WHERE event_key=?', eventKey);
      if (previous) {
        const prior = JSON.parse(previous.body_json) as JsonRecord;
        return prior.request_digest === digest
          ? json({ ...this.result(intake, remove ? 'removed' : 'added', prerequisite), duplicate: true })
          : error('idempotency_conflict', 409, 'this key was already used for a different prerequisite change');
      }
      if (CLOSED.includes(intake.status)) return error('intake_closed', 409, `dependent intake is ${intake.status}`);
      if (body.expected_version !== undefined && body.expected_version !== intake.updated_at)
        return error('intake_version_conflict', 409, `expected_version ${body.expected_version} is stale; current task.control.version is ${intake.updated_at}`);
      if (prerequisite.id === intake.id) return error('dependency_self', 409, 'an intake cannot depend on itself');
      const current = this.prerequisites(intake.id);
      const exists = current.some(p => p.intake_id === prerequisite.id);
      if (remove && !exists) return error('dependency_not_found', 404, `intake ${intake.id} does not depend on ${prerequisite.id}`);
      if (!remove && exists) return json({ ...this.result(intake, 'unchanged', prerequisite), duplicate: true });
      if (!remove && current.length >= MAX_PREREQUISITES) return error('dependency_limit', 409, `at most ${MAX_PREREQUISITES} prerequisites per intake`);
      const at = Math.max(Date.now(), intake.updated_at + 1);
      this.hooks.append(intake.id, eventKey, actor!.id, remove ? 'dependency_removed' : 'dependency_added',
        { on: prerequisite.id, on_project: prerequisite.project, request_digest: digest }, at);
      this.db.run('UPDATE intakes SET updated_at=? WHERE id=?', at, intake.id);
      return json(this.result({ ...intake, updated_at: at }, remove ? 'removed' : 'added', prerequisite), remove ? 200 : 201);
    });
  }

  private result(intake: Intake, action: string, prerequisite: { id: string; project: string }): JsonRecord {
    return { intake_id: intake.id, action, prerequisite: { intake_id: prerequisite.id, project: prerequisite.project },
      depends_on: this.prerequisites(intake.id), task: { id: intake.id, updated_at: intake.updated_at, control: { version: intake.updated_at } } };
  }

  private hasTable(): boolean {
    if (this.tableReady) return true;
    this.tableReady = !!this.db.one("SELECT name FROM sqlite_master WHERE type='table' AND name='intake_artifacts'");
    return this.tableReady;
  }

  private ensureTable(): void {
    if (this.hasTable()) return;
    this.db.run(ARTIFACT_TABLE);
    this.tableReady = true;
  }

  private async upload(request: Request, intakeId: string): Promise<Response> {
    const actor = actorFrom(request);
    let raw: string;
    try { raw = await boundedText(request, ARTIFACT_BODY_LIMIT); }
    catch (caught) {
      if (caught instanceof HttpError && caught.code === 'body_too_large')
        return error('artifact_too_large', 413, `size check failed: PNG over ${ARTIFACT_MAX_BYTES} bytes (6 MiB limit)`);
      throw caught;
    }
    let body: JsonRecord;
    try { body = JSON.parse(raw) as JsonRecord; } catch { return error('invalid_json', 400, 'request body is not JSON'); }
    if (!body || typeof body !== 'object' || Array.isArray(body)) return error('invalid_json_object', 400, 'request body must be a JSON object');
    const unknown = Object.keys(body).filter(k => !['key', 'name', 'media_type', 'data_base64', 'sanitized'].includes(k));
    if (unknown.length) return error('unknown_artifact_field', 400, 'unsupported field(s): ' + unknown.slice(0, 4).join(', '));
    if (typeof body.name !== 'string' || !NAME.test(body.name)) return error('invalid_artifact_name', 400, 'name check failed: use 1-64 of A-Z a-z 0-9 _ . - starting with a letter or digit');
    if (body.sanitized !== true) return error('sanitized_attestation_required', 400, 'sanitized check failed: pass --sanitized after reviewing pixels and metadata');
    if (body.media_type !== 'image/png') return error('unsupported_media_type', 415, 'type check failed: only image/png is accepted');
    if (typeof body.data_base64 !== 'string' || !body.data_base64) return error('invalid_png', 400, 'type check failed: data_base64 is missing');
    let bytes: Uint8Array;
    try { bytes = Uint8Array.from(atob(body.data_base64), c => c.charCodeAt(0)); }
    catch { return error('invalid_png', 400, 'type check failed: data_base64 is not valid base64'); }
    if (bytes.byteLength > ARTIFACT_MAX_BYTES) return error('artifact_too_large', 413, `size check failed: ${bytes.byteLength} bytes exceeds the ${ARTIFACT_MAX_BYTES}-byte (6 MiB) limit`);
    if (bytes.byteLength < 33 || PNG_SIGNATURE.some((v, i) => bytes[i] !== v)) return error('invalid_png', 400, 'type check failed: missing PNG signature');
    if (String.fromCharCode(...bytes.subarray(12, 16)) !== 'IHDR') return error('invalid_png', 400, 'type check failed: PNG header chunk (IHDR) missing');
    // sha256 of the PNG bytes, so `sha256sum file.png` matches.
    const sha256 = [...new Uint8Array(await crypto.subtle.digest('SHA-256', bytes))].map(b => b.toString(16).padStart(2, '0')).join('');
    const name = body.name, dataBase64 = body.data_base64, size = bytes.byteLength;
    return this.hooks.transaction(() => {
      const intake = this.intake(intakeId);
      if (!intake) return error('intake_not_found', 404, `intake ${intakeId} does not exist in this project`);
      if (!this.canUpload(actor, intake)) return error('forbidden', 403, 'caller is neither an operator, a project writer nor the session bound to this intake');
      this.ensureTable();
      const existing = this.db.one<{ sha256: string }>('SELECT sha256 FROM intake_artifacts WHERE intake_id=? AND name=?', intake.id, name);
      if (existing) return existing.sha256 === sha256
        ? json({ intake_id: intake.id, name, media_type: 'image/png', bytes: size, sha256, duplicate: true })
        : error('artifact_name_conflict', 409, `artifact name ${name} already holds different content`);
      const totals = this.db.one<{ n: number; total: number }>('SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS total FROM intake_artifacts WHERE intake_id=?', intake.id)!;
      if (totals.n >= ARTIFACT_MAX_PER_INTAKE) return error('artifact_limit', 409, `count check failed: at most ${ARTIFACT_MAX_PER_INTAKE} artifacts per intake`);
      if (totals.total + size > ARTIFACT_MAX_TOTAL_BYTES) return error('artifact_limit', 409, `total size check failed: at most ${ARTIFACT_MAX_TOTAL_BYTES} bytes per intake`);
      const at = Date.now();
      this.db.run('INSERT INTO intake_artifacts(intake_id,name,media_type,bytes,sha256,data_base64,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?)',
        intake.id, name, 'image/png', size, sha256, dataBase64, actor!.id, at);
      this.hooks.append(intake.id, 'artifact:' + intake.id + ':' + name, actor!.id, 'artifact_added', { name, media_type: 'image/png', bytes: size, sha256 }, at);
      return json({ intake_id: intake.id, name, media_type: 'image/png', bytes: size, sha256 }, 201);
    });
  }

  private canUpload(actor: Actor | undefined, intake: Intake): boolean {
    if (!actor) return false;
    if (this.hooks.canWrite(actor, intake)) return true;
    return actor.resource_id === intake.id && hasScope(actor, 'intake:append');
  }

  private listArtifacts(request: Request, intakeId: string): Response {
    const actor = actorFrom(request), intake = this.intake(intakeId);
    if (!intake) return error('intake_not_found', 404, `intake ${intakeId} does not exist in this project`);
    if (!this.hooks.canRead(actor, intake)) return error('forbidden', 403, 'caller cannot read this intake');
    return json({ intake_id: intake.id, artifacts: this.artifacts(intake.id) });
  }

  private download(request: Request, intakeId: string, name: string): Response {
    const actor = actorFrom(request), intake = this.intake(intakeId);
    if (!intake) return error('intake_not_found', 404, `intake ${intakeId} does not exist in this project`);
    if (!this.hooks.canRead(actor, intake)) return error('forbidden', 403, 'caller cannot read this intake');
    const row = this.hasTable() ? this.db.one<JsonRecord>('SELECT name, media_type, bytes, sha256, data_base64 FROM intake_artifacts WHERE intake_id=? AND name=?', intake.id, name) : undefined;
    return row ? json({ intake_id: intake.id, ...row }) : error('artifact_not_found', 404, `no artifact named ${name} on this intake`);
  }
}

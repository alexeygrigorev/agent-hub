import {SessionWakes,wakeProof,strictWakeProof,type WakeRecord,type WakeSession} from './session-wakes';
import { linkSession } from './session-intake-link';
import {MachineNativeCommands,NATIVE_COMMAND_SCHEMA} from './machine-native-commands';
import {boundWorkspace,mergeWorkspaceBinding,WORKSPACE_BINDING_FIELD,WORKSPACE_REPAIR_FIELD,WORKSPACE_RECOVERY_TURN_FIELD} from './workspace-binding';
import {MachineNativeInventory, NATIVE_INVENTORY_SCHEMA, validNativeAbsenceEvidence, nativeAbsenceObservation, type Meta, type NativeAbsenceEvidence} from './machine-native-inventory';
import { nativeReturncode } from './native-exit';
import { ENGINE_PREFLIGHT_FAILURES, ENGINE_UNHEALTHY_MS, isFastStartRefusal } from './task-completion';
import { normalizeStartPickerEvidence } from './start-picker-evidence';
import { MeteredDb, initializeSchema, schemaMigrationCeiling } from './db';
import { SqlReceiptMeter, SQL_PROBE_HEADER, wrapSqlNamespaces } from './sql-receipt';
import { sha256Hex } from './crypto';
import { actorFrom, bodyObject, boundedString, error, hasScope, HttpError, json } from './http';
import { CLAIMED_WITHOUT_NATIVE_GRACE_MS, HOST_INVENTORY_LOST_MS, inactiveReason, isLiveActiveSession, LIVE_SNAPSHOT_TTL_MS, SESSION_REAP_INTERVAL_MS, type LiveSession } from './session-liveness';
import type { Actor, Hub2Env, JsonRecord } from './types';

const CAPTURE_OUTPUT_BYTES = 64 * 1024;
// Six JSON bytes per raw control byte, plus the existing bounded binding.
const CAPTURE_RESULT_JSON_BYTES = 400 * 1024;
const CAPTURE_ACK_BODY_BYTES = 401 * 1024;
const COMMAND_HISTORY_BYTES = 8 * 1024 * 1024;
const HOST_START_DIAGNOSTIC_KEY = '_hub_host_start_diagnostic';
const HOST_FAILURE_DETAILS:Readonly<Record<string,string>> = {
  "missed_wake": "Scheduled wake was not verified within its deadline.",
  "native_dispatch_not_started": "Native launch did not start.",
  "host_operation_failed": "Host operation failed.",
  "workspace_unavailable": "path does not exist; Hub will not create it",
  "workspace_outside_allowlist": "workspace is outside enrolled roots",
  "invalid_workspace": "invalid workspace path",
  "workspace_identity_unavailable": "Workspace identity is unavailable.",
  "workspace_label_unavailable": "Workspace label is unavailable.",
  "workspace_label_ambiguous": "Workspace label is ambiguous.",
  "native_launch_route_missing": "Native launch route is unavailable.",
  "codex_launch_wrapper_unavailable": "Codex launch wrapper is unavailable.",
  "engine_binary_unavailable": "Engine binary or managed launcher is unavailable on this host.",
  "claude_workspace_trust_unverified": "Claude folder-trust prompt could not be answered.",
  "launcher_capability_unavailable_headless": "Engine is not advertised as headless-capable on this host.",
  "launcher_capability_unavailable_interactive": "Engine is not advertised as interactive-capable on this host (for example shell supports headless only).",
  "launcher_capability_unavailable_goal": "Engine does not advertise goal mode on this host.",
  "launcher_capability_unavailable_effort": "Engine does not advertise effort selection on this host.",
  "launcher_capability_unavailable": "Launcher capability is unavailable.",
  "managed_exec_unavailable": "Managed execution is unavailable.",
  "invalid_offer_identity": "Offer identity is invalid.",
  "invalid_offer_epoch": "Offer epoch is invalid.",
  "offer_expired": "Offer expired.",
  "launch_specification_required": "Launch specification is required.",
  "engine_not_allowed": "Engine is not allowed.",
  "invalid_prompt": "Prompt is invalid.",
  "invalid_model": "Model is invalid.",
  "invalid_task_timeout": "Task timeout is invalid.",
  "invalid_goal_mode": "Goal mode is invalid.",
  "invalid_effort": "Effort is invalid.",
  "invalid_managed_credential_route": "Managed credential route is invalid.",
  "native_reconciliation_required": "Native reconciliation is required.",
  "offer_fence_conflict": "Offer fence conflicts.",
  "native_dispatch_fence_conflict": "Native dispatch fence conflicts.",
  "child_context_path_untrusted": "Child context path is untrusted.",
  "child_context_unverified": "Child context is unverified.",
  "child_context_binding_unverified": "Child context binding is unverified.",
  "host_identity_required": "Host identity is required.",
  "claim_ack_unverified": "Claim acknowledgement is unverified.",
  "launch_outcome_uncertain": "Launch outcome is uncertain.",
  "prompt_delivery_uncertain": "Prompt delivery is uncertain.",
  "native_abort_pending": "Native abort is pending.",
  "native_mail_outcome_uncertain": "Native mail outcome is uncertain.",
  "native_outcome_unconfirmed": "Native outcome is unconfirmed.",
  "native_observation_unverified": "Native observation is unverified.",
  "native_command_outcome_uncertain": "Native command outcome is uncertain.",
  "native_command_failed": "Native command failed.",
  "invalid_native_status": "Native status is invalid.",
  "native_identity_mismatch": "Native identity does not match.",
  "task_output_unavailable": "Task output is unavailable.",
  "provider_auth_failed": "Provider credential preparation was refused.",
  "provider_unauthorized": "Provider credential preparation was refused.",
  "provider_route_mismatch": "Provider credential preparation was refused.",
  "provider_account_required": "Provider credential preparation was refused.",
  "provider_materialization_refused": "Provider credential preparation was refused.",
  "provider_generation_ack_unverified": "Provider credential preparation was refused.",
  "credential_generation_rejected": "Provider credential preparation was refused.",
  "credential_generation_conflict": "Provider credential preparation was refused.",
  "credentials_unavailable": "Provider credential preparation was refused.",
  "account_unconfigured": "Provider credential preparation was refused.",
  "access_lifetime_insufficient": "Provider credential preparation was refused.",
  "provider_access_lifetime_insufficient": "Provider credential preparation was refused.",
  "child_credential_unavailable": "Provider credential preparation was refused.",
  "child_credential_unverified": "Provider credential preparation was refused.",
  "credential_key_unavailable": "Provider credential preparation was refused.",
  "credential_decryption_failed": "Provider credential preparation was refused.",
  "credential_missing": "Provider credential preparation was refused.",
  "refresh_refused": "Provider credential preparation was refused.",
  "refresh_invalid": "Provider credential preparation was refused.",
  "refresh_in_progress": "Provider credential preparation was refused.",
  "refresh_outcome_uncertain": "Provider credential preparation was refused.",
  "generation_conflict": "Provider credential preparation was refused.",
  "host_not_approved": "Provider credential preparation was refused.",
  "native_response_too_large": "Launch preparation was refused.",
  "legacy_launch_identity_unverified": "Launch preparation was refused.",
  "active_native_bound_exceeded": "Launch preparation was refused.",
  "capture_fence_conflict": "Launch preparation was refused.",
  "invalid_command_id": "Launch preparation was refused.",
  "command_not_allowed": "Launch preparation was refused.",
  "command_fence_conflict": "Launch preparation was refused.",
  "headless_send_refused": "Launch preparation was refused.",
  "invalid_native_command": "Launch preparation was refused.",
  "capture_identity_unavailable": "Launch preparation was refused.",
  "claimed_offer_without_local_intent": "Launch preparation was refused.",
  "command_outcome_uncertain": "Launch preparation was refused.",
  "invalid_command_text": "Launch preparation was refused.",
  "command_id_conflict": "Launch preparation was refused.",
  "duplicate_terminal_field": "Launch preparation was refused.",
  "host_launch_queue_full": "Launch preparation was refused.",
  "host_command_queue_full": "Launch preparation was refused.",
  "antigravity_first_run_blocked": "Antigravity is blocked on first-run onboarding or Terms of Service.",
  "engine_auth_check_unavailable": "Engine authentication check failed before the session started.",
  "engine_auth_expired": "Engine login expired before the session started.",
  "antigravity_settings_unverified": "Launch preparation was refused.",
  "invalid_native_send": "Launch preparation was refused.",
  "native_prompt_invalid": "Launch preparation was refused.",
  "native_goal_objective_invalid": "Launch preparation was refused.",
  "glm_model_route_refused": "Launch preparation was refused.",
  "provider_route_unsupported": "Launch preparation was refused.",
  "interactive_explicit_model_required": "Launch preparation was refused.",
  "interactive_provider_unsupported": "Launch preparation was refused.",
  "native_goal_provider_unsupported": "Launch preparation was refused.",
  "interactive_adapter_shape_unverified": "Launch preparation was refused.",
  "interactive_model_unverified": "Launch preparation was refused.",
  "antigravity_workspace_trust_unverified": "Launch preparation was refused.",
  "goal_brief_pointer_not_single_line": "Launch preparation was refused.",
  "goal_brief_path_untrusted": "Launch preparation was refused.",
  "goal_brief_too_large": "Launch preparation was refused.",
  "goal_brief_pointer_too_large": "Launch preparation was refused.",
  "native_first_action_unverified": "Launch preparation was refused.",
  "native_goal_registration_unverified": "Launch preparation was refused.",
  "native_retirement_unverified": "Launch preparation was refused.",
  "unsupported_opencode_model": "Launch preparation was refused.",
  "headless_effort_unsupported": "Launch preparation was refused.",
  "native_target_identity_mismatch": "Launch preparation was refused.",
  "native_census_stale": "Launch preparation was refused.",
  "native_target_ownership_unavailable": "Launch preparation was refused.",
  "native_target_managed": "Launch preparation was refused.",
  "native_command_deadline": "Launch preparation was refused.",
  "native_target_workspace_unavailable": "Launch preparation was refused.",
  "native_target_unavailable": "Launch preparation was refused.",
  "native_runtime_deadline_unverified": "Launch preparation was refused.",
  "native_start_refused": "Launch preparation was refused.",
  "native_start_identity_invalid": "Launch preparation was refused.",
  "native_model_mismatch": "Launch preparation was refused.",
  "native_effort_mismatch": "Launch preparation was refused.",
  "native_profile_command_mismatch": "Launch preparation was refused.",
  "native_interactive_command_mismatch": "Launch preparation was refused.",
  "prompt_path_untrusted": "Launch preparation was refused.",
  "native_goal_identity_unverified": "Launch preparation was refused.",
  "native_goal_delivery_unverified": "Launch preparation was refused.",
  "native_recovery_evidence_too_large": "Launch preparation was refused.",
  "native_launch_recovery_unverified": "Launch preparation was refused.",
  "interactive_effort_unsupported": "Launch preparation was refused.",
  "unverified_receipt": "Launch preparation was refused.",
  "native_start_uncertain": "Launch preparation was refused.",
  "native_start_ambiguous": "Launch preparation was refused.",
  "native_goal_objective_conflict": "Launch preparation was refused.",
  "native_recovery_mode_unverified": "Launch preparation was refused.",
  "duplicate_receipt_field": "Launch preparation was refused.",
  "native_managed_engine_unverified": "Launch preparation was refused.",
  "implement_worktree_path_untrusted": "Launch preparation was refused.",
  "implement_worktree_branch_ambiguous": "Launch preparation was refused.",
  "implement_worktree_branch_changed": "Launch preparation was refused.",
  "implement_worktree_owner_invalid": "Launch preparation was refused.",
  "implement_worktree_identity_required": "Launch preparation was refused.",
  "implement_worktree_owner_conflict": "Launch preparation was refused.",
  "implement_worktree_provenance_invalid": "Launch preparation was refused.",
  "implement_worktree_ref_mismatch": "Launch preparation was refused.",
  "implement_worktree_tip_mismatch": "Launch preparation was refused.",
  "invalid_session_purpose": "Launch preparation was refused.",
  "implement_worktree_preparation_busy": "Launch preparation was refused.",
  "implement_worktree_owner_durability_failed": "Launch preparation was refused.",
  "implement_worktree_branch_missing": "Launch preparation was refused.",
  "implement_worktree_git_failed": "Workspace git operation failed.",
  "implement_worktree_locked": "Workspace intake worktree is locked.",
  "implement_worktree_branch_in_use": "Workspace intake branch is in use by another worktree.",
  "provider_engine_bundle_unavailable": "Launch preparation was refused.",
  "provider_engine_bundle_invalid": "Launch preparation was refused.",
  "provider_engine_version_report_unverified": "Launch preparation was refused.",
  "provider_sdk_native_binary_missing": "Launch preparation was refused.",
  "provider_fable_smoke_failed": "Launch preparation was refused.",
  "provider_sdk_install_refused": "Launch preparation was refused."
};
const HOST_EVENT_REASONS=new Set([...Object.keys(HOST_FAILURE_DETAILS),"host_launch_refused","native_process_absent","launch_timeout_no_native","stop_confirmed","native_exit","stop_unconfirmed","host_inventory_lost","host_operation_failed","native_mail_outcome_uncertain","connected","dead","idle","host_no_response","host_inventory_stale","native_inventory_absent","legacy_launch_unobserved","machine_not_enrolled"]);
function closedHostReason(value:unknown):string {
  return typeof value==='string' && HOST_EVENT_REASONS.has(value) ? value : 'host_operation_failed';
}
function closedLaunchReason(value:unknown):string {
  return typeof value==='string' && Object.hasOwn(HOST_FAILURE_DETAILS,value) ? value : 'native_dispatch_not_started';
}
function fixedHostDetail(reason:unknown):string {
  return HOST_FAILURE_DETAILS[closedLaunchReason(reason)];
}
// Hosts may append closed facts to the fixed detail, e.g.
// "Workspace git operation failed. (operation=rev-parse exit_code=128)".
// Only these shapes pass; paths, argv and stderr never reach session detail.
const HOST_DETAIL_FACT='(?:operation=(?:rev-parse|fetch|worktree|for-each-ref|symbolic-ref|ls-remote|other)|exit_code=-?[0-9]{1,6}|reason=[a-z][a-z_]{0,39}|status=[a-z][a-z_]{0,39}|error_class=(?:timeout|os_error|runtime_error|value_error|key_error|unclassified))';
const HOST_DETAIL_FACTS=new RegExp(`^ \\(${HOST_DETAIL_FACT}(?: ${HOST_DETAIL_FACT}){0,3}\\)$`);
function hostDetail(reason:unknown,reported:unknown):string {
  const fixed=fixedHostDetail(reason);
  return typeof reported==='string'&&reported.length<=256&&reported.startsWith(fixed)
    &&HOST_DETAIL_FACTS.test(reported.slice(fixed.length))?reported:fixed;
}

function isJsonRecord(value: unknown): value is JsonRecord {
  return typeof value==='object'&&value!==null&&!Array.isArray(value);
}

const LABEL_RECOVERY = `state='running' AND native_id IS NOT NULL
  AND CASE WHEN json_valid(launch_json) THEN
  (json_type(launch_json,'$._hub_workspace_binding') IS NULL
       OR json_extract(launch_json,'$._hub_workspace_repair_pending')=1)
  AND length(json_extract(launch_json,'$.workspace')) BETWEEN 1 AND 128
  AND json_extract(launch_json,'$.workspace') GLOB '[A-Za-z0-9]*'
  AND json_extract(launch_json,'$.workspace') NOT GLOB '*[^A-Za-z0-9._-]*'
  ELSE 0 END`;

const RECOVERY_TURN = `CASE WHEN json_valid(launch_json) THEN CASE
  WHEN json_type(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}') IN ('integer','real')
   AND json_extract(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}') BETWEEN 0 AND 9007199254740991
   AND json_extract(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}')=CAST(json_extract(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}') AS INTEGER)
  THEN json_extract(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}') ELSE 0 END ELSE 0 END`;

const SCHEMA = [
  'CREATE TABLE IF NOT EXISTS machine_capabilities(machine_id TEXT PRIMARY KEY,engines_json TEXT NOT NULL,capabilities_json TEXT NOT NULL,resources_json TEXT) WITHOUT ROWID',
  'CREATE TABLE IF NOT EXISTS machine_liveness(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at INTEGER NOT NULL)',
  'CREATE TABLE IF NOT EXISTS native_sessions(session_id TEXT PRIMARY KEY,native_id TEXT,phase TEXT NOT NULL,last_activity_at INTEGER,exit_code INTEGER,containment_empty INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
  `CREATE TABLE machines (
    machine_id TEXT PRIMARY KEY, alias TEXT NOT NULL, os TEXT NOT NULL, arch TEXT NOT NULL,
    labels_json TEXT NOT NULL, last_seen INTEGER NOT NULL, next_epoch INTEGER NOT NULL DEFAULT 0,
    active_session_id TEXT
  )`,
  `CREATE TABLE sessions (
    id TEXT PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, request_digest TEXT NOT NULL,
    dispatch_id TEXT NOT NULL, attempt INTEGER NOT NULL, account_id TEXT,
    project_id TEXT NOT NULL, intake_id TEXT NOT NULL, machine_id TEXT NOT NULL,
    epoch INTEGER NOT NULL, title TEXT NOT NULL, acceptance_criteria_json TEXT NOT NULL,
    launch_json TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT UNIQUE, offer_expires_at INTEGER,
    evidence_json TEXT, evidence_digest TEXT, exit_code INTEGER,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX machine_session_offer ON sessions(machine_id, state, created_at, id)',
  'CREATE INDEX machine_session_deadline ON sessions(offer_expires_at, id) WHERE state = \'offered\' AND offer_expires_at IS NOT NULL',
  'CREATE INDEX machine_session_intake ON sessions(intake_id, created_at, id)',
  `CREATE TABLE machine_session_operations (
    operation_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    request_digest TEXT NOT NULL, state TEXT NOT NULL
  )`,
  'CREATE TABLE launch_admissions(operation_id TEXT PRIMARY KEY,session_id TEXT NOT NULL,epoch INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
  `CREATE TABLE session_commands (
    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, epoch INTEGER NOT NULL,
    request_key TEXT NOT NULL, request_digest TEXT NOT NULL, kind TEXT NOT NULL, text TEXT,
    state TEXT NOT NULL, result_json TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL,
    UNIQUE(session_id, request_key)
  )`,
  'CREATE INDEX command_pending_machine ON session_commands(machine_id, state, created_at, id)',
  `CREATE TABLE native_mail (
    message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,machine_id TEXT NOT NULL,recipient TEXT NOT NULL,body TEXT NOT NULL,
    digest TEXT NOT NULL,state TEXT NOT NULL,receipt_digest TEXT,created_at INTEGER NOT NULL,
    PRIMARY KEY(message_id,epoch)
  )`,
  'CREATE INDEX mail_pending_machine ON native_mail(machine_id,state,created_at,message_id,epoch)',
  'CREATE TABLE native_mail_fences(message_id TEXT NOT NULL,epoch INTEGER NOT NULL,operation_id TEXT NOT NULL,PRIMARY KEY(message_id,epoch)) WITHOUT ROWID',
  'CREATE TABLE attention(session_id TEXT PRIMARY KEY,reason TEXT NOT NULL,operation_id TEXT,message_id TEXT,observed_at INTEGER NOT NULL)',
  'CREATE INDEX attention_recent ON attention(observed_at DESC,session_id)',
  `CREATE TABLE machine_outbox (
    id TEXT PRIMARY KEY, kind TEXT NOT NULL, operation_id TEXT NOT NULL,
    project_id TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL,
    attempt INTEGER NOT NULL, next_action_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL
  )`,
  'CREATE INDEX machine_outbox_due ON machine_outbox(state, next_action_at, id)',
];
// These identifiers are fixed source names; preserve every row and index on upgrade.
const COMPACT_KEYS=['machine_outbox','machine_session_operations','sessions'].flatMap(table=>{
  const create=SCHEMA.find(sql=>sql.startsWith(`CREATE TABLE ${table} (`))!;
  return [create.replace(`TABLE ${table}`,`TABLE ${table}_compact`)+' WITHOUT ROWID',
    `INSERT INTO ${table}_compact SELECT * FROM ${table}`,`DROP TABLE ${table}`,
    `ALTER TABLE ${table}_compact RENAME TO ${table}`,
    ...SCHEMA.filter(sql=>sql.startsWith('CREATE INDEX ')&&sql.includes(` ON ${table}(`))];
});
// Historical migration for slot accounting; migration 6 removes the obsolete table.
const MACHINE_ACTIVE_MIGRATION=[
  'CREATE TABLE IF NOT EXISTS machine_active(session_id TEXT PRIMARY KEY,machine_id TEXT NOT NULL) WITHOUT ROWID',
  'CREATE INDEX IF NOT EXISTS machine_active_machine ON machine_active(machine_id)',
  'INSERT OR IGNORE INTO machine_active(session_id,machine_id) SELECT active_session_id,machine_id FROM machines WHERE active_session_id IS NOT NULL',
  'UPDATE machines SET active_session_id=NULL',
];

const LATEST_SCHEMA=SCHEMA.map(sql=>{
  const latest=sql.replace(',resources_json TEXT','').replace(',\n    active_session_id TEXT','');
  return ['machine_outbox','machine_session_operations','sessions'].some(table=>latest.startsWith(`CREATE TABLE ${table} (`))?latest+' WITHOUT ROWID':latest;
});

type MachineRow = { machine_id: string; alias: string; os: string; arch: string; labels_json: string; last_seen: number; next_epoch: number };
type SessionRow = {
  id: string; operation_id: string; request_digest: string; dispatch_id: string; attempt: number; account_id: string | null;
  project_id: string; intake_id: string;
  machine_id: string; epoch: number; title: string; acceptance_criteria_json: string;
  launch_json: string;
  state: string; native_id: string | null; offer_expires_at: number | null;
  evidence_json: string | null; evidence_digest: string | null; exit_code: number | null;
  created_at: number; updated_at: number;
};
// The server speaks version 1 of the host WebSocket protocol.
const HOST_PROTOCOL_VERSION = 1;
const MANAGED_CLAUDE_CAPABILITY = 'managed:hub-claude:opus:medium:v1';
// AC2 of intake 41ef7048 (t1801u, systems fail loudly): a host that is running
// (still sending HTTP heartbeats, or holding live sessions) but has had no
// socket for more than 15 minutes raises a developer alert. The episode lives in
// Durable Object KV storage so no schema migration or rollback fence is needed.
export const HOST_DISCONNECT_ALERT_MS = 15 * 60_000;
const HOST_DISCONNECT_RECHECK_MS = 5 * 60_000;
const HOST_RUNNING_EVIDENCE_MS = 10 * 60_000;
const HOST_DISCONNECT_WATCH_MS = 7 * 24 * 3_600_000;
const HOST_PRESENCE_KEY = 'host_presence';
export const HOST_ALERT_RECIPIENT = 'dev@dtcdev.click';
type HostPresence = { machine_id: string; disconnected_since: number; running_seen_at?: number; checked_at?: number; alerted_at?: number; evidence?: string };
type PresenceStore = { get(key: string): Promise<unknown>; put(key: string, value: unknown): Promise<void>; delete(key: string): Promise<unknown> };

type HostRelease = { host_version: string; protocol_version: number };
type SocketAttachment = { machine_id: string; actor_id: string; generation: string } & Partial<HostRelease>;

function hostRelease(hostVersion: unknown, protocolVersion: unknown): HostRelease | undefined {
  if (typeof hostVersion !== 'string' || hostVersion.length !== 40 || !/^[0-9a-f]+$/.test(hostVersion)
      || typeof protocolVersion !== 'number' || !Number.isSafeInteger(protocolVersion) || protocolVersion < 1) return undefined;
  return { host_version: hostVersion, protocol_version: protocolVersion };
}

function reportedHostRelease(query: URLSearchParams): HostRelease | undefined {
  const versions = query.getAll('host_version');
  const protocols = query.getAll('protocol_version');
  if (versions.length !== 1 || protocols.length !== 1 || !/^[1-9][0-9]{0,15}$/.test(protocols[0])) return undefined;
  return hostRelease(versions[0], Number(protocols[0]));
}

type OutboxRow = { id: string; kind: string; operation_id: string; project_id: string; payload_json: string; state: string; attempt: number; next_action_at: number; created_at: number };
type CommandRow = { id: string; session_id: string; machine_id: string; epoch: number; request_key: string; request_digest: string; kind: string; text: string | null; capture_mode: string | null; state: string; result_json: string | null; created_at: number; updated_at: number };
type NativeMail = {message_id:string;epoch:number;operation_id:string;session_id:string;machine_id:string;recipient:string;body:string;digest:string;state:string;receipt_digest:string|null};
const AUTH_FALLBACK_REASONS=new Set(['provider_auth_failed','provider_unauthorized','provider_route_mismatch','provider_account_required',
  'provider_materialization_refused','provider_generation_ack_unverified','credential_generation_rejected','credential_generation_conflict',
  'credentials_unavailable','account_unconfigured','access_lifetime_insufficient','provider_access_lifetime_insufficient',
  'child_credential_unavailable','child_credential_unverified','credential_key_unavailable','credential_decryption_failed',
  'credential_missing','refresh_refused','refresh_invalid','refresh_in_progress','refresh_outcome_uncertain','generation_conflict',
  'host_not_approved']);

const ENGINE_UPDATE_JOBS_SCHEMA = [
  'CREATE TABLE IF NOT EXISTS engine_update_jobs(id TEXT PRIMARY KEY,engine TEXT NOT NULL,binary TEXT NOT NULL,version_command TEXT NOT NULL,update_command TEXT NOT NULL,state TEXT NOT NULL,old_version TEXT,new_version TEXT,exit_code INTEGER,error TEXT,output TEXT,created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL) WITHOUT ROWID',
];

/** Per-host presence, native-session evidence and durable completion outbox. */
export class MachineDO {
  private alarmQueue:Promise<void>=Promise.resolve();
  private connectQueue:Promise<void>=Promise.resolve();
  private readonly db: MeteredDb;
  private readonly sqlReceipts: SqlReceiptMeter;
  private readonly env: Hub2Env;
  private readonly constructorMs: number;
  private readonly ready:Promise<void>;
  private readonly nativeInventory:MachineNativeInventory;
  private readonly nativeCommands:MachineNativeCommands;
  private readonly wakes:SessionWakes;
  private readonly preflightWaiters = new Map<string, (result: JsonRecord) => void>();

  constructor(private readonly ctx: DurableObjectState, env: Hub2Env) {
    const started = performance.now();
    this.db = new MeteredDb(ctx.storage.sql);
    this.sqlReceipts = new SqlReceiptMeter(this.db);
    this.env = wrapSqlNamespaces(env, this.sqlReceipts) as Hub2Env;
    this.nativeInventory=new MachineNativeInventory(this.db,ctx,(snapshot,previous)=>this.observeNativeAbsences(snapshot,previous));
    this.nativeCommands=new MachineNativeCommands(this.db,ctx,this.nativeInventory);
    this.wakes=new SessionWakes(ctx.storage,{
      session:id=>this.wakeSession(id),enqueue:w=>this.enqueueWake(w),
      command:w=>{const c=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=? AND session_id=? AND epoch=?',w.command_id,w.session_id,w.epoch);return c?{state:c.state==='wake_pending'?'pending':c.state==='wake_failed'?'failed':c.state,result:this.parse(c.result_json) as JsonRecord,updated_at:c.updated_at}:undefined;},
      missed:async w=>{this.wakeNotification(w);},
    });
    this.ready=initializeSchema(ctx, this.db, 10, [...LATEST_SCHEMA, ...ENGINE_UPDATE_JOBS_SCHEMA], [[SCHEMA[0]],COMPACT_KEYS,['ALTER TABLE machine_capabilities ADD COLUMN resources_json TEXT'],MACHINE_ACTIVE_MIGRATION,[
      'CREATE TABLE IF NOT EXISTS machine_liveness(singleton INTEGER PRIMARY KEY CHECK(singleton=1),observed_at INTEGER NOT NULL)',
      'CREATE TABLE IF NOT EXISTS native_sessions(session_id TEXT PRIMARY KEY,native_id TEXT,phase TEXT NOT NULL,last_activity_at INTEGER,exit_code INTEGER,containment_empty INTEGER NOT NULL,observed_at INTEGER NOT NULL) WITHOUT ROWID',
      'DROP TABLE IF EXISTS machine_active','ALTER TABLE machine_capabilities DROP COLUMN resources_json','ALTER TABLE machines DROP COLUMN active_session_id',
    ],['ALTER TABLE session_commands ADD COLUMN capture_mode TEXT'],NATIVE_INVENTORY_SCHEMA,NATIVE_COMMAND_SCHEMA,ENGINE_UPDATE_JOBS_SCHEMA],async()=>{await this.wakes.load();await this.scheduleNext();},6,schemaMigrationCeiling(env.HUB2_SCHEMA_MIGRATIONS,8,10));
    this.constructorMs = performance.now() - started;
  }

  async fetch(request: Request): Promise<Response> {
    return this.sqlReceipts.measure(() => this.handleFetch(request), request.headers.get(SQL_PROBE_HEADER));
  }

  private async handleFetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    try {
      await this.ready;
      if (url.pathname === '/_internal/reserve' && request.method === 'POST') return await this.reserveInternal(request);
      if (url.pathname === '/_internal/activate' && request.method === 'POST') return await this.activateInternal(request);
      if (url.pathname === '/_internal/mail' && request.method === 'POST') return await this.queueMail(request);
      if (url.pathname === '/_internal/mail-ack' && request.method === 'POST') return await this.settleMail(request, false);
      if (url.pathname === '/_internal/mail-cancel' && request.method === 'POST') return await this.settleMail(request, true);
      if (url.pathname === '/_internal/engine-update' && request.method === 'POST') return await this.queueEngineUpdate(request);
      const internalEngineUpdate = url.pathname.match(/^\/_internal\/engine-updates\/([0-9a-f-]{36})$/i);
      if (internalEngineUpdate && request.method === 'GET') return this.getEngineUpdateJob(internalEngineUpdate[1]);
      const engineUpdateAck = url.pathname.match(/^\/api\/engine-updates\/([0-9a-f-]{36})\/ack$/i);
      if (engineUpdateAck && request.method === 'POST') return await this.ackEngineUpdateJob(request, engineUpdateAck[1]);
      if (url.pathname === '/_internal/mail-authorize' && request.method === 'POST') {
        const b=await bodyObject(request,4096);
        if(!boundedString(b.message_id,180)||!Number.isSafeInteger(b.epoch)||!boundedString(b.operation_id,180)||!boundedString(b.recipient,180)||!boundedString(b.machine_id,180))return error('invalid_delivery_binding',400);
        const row=this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?',b.message_id,b.epoch);
        if(!row||!(['pending','acked'].includes(row.state)||row.state==='outcome_uncertain'&&b.phase==='receipt')||row.machine_id!==b.machine_id||row.operation_id!==b.operation_id||row.recipient!==b.recipient)return error('delivery_binding_not_found',404);
        return json({authorized:true,message_id:row.message_id,epoch:row.epoch,operation_id:row.operation_id,recipient:row.recipient,machine_id:row.machine_id,session_id:row.session_id});
      }
      if (url.pathname === '/_internal/status' && request.method === 'GET') return this.machineView();
      if (url.pathname === '/_internal/routing-snapshot' && request.method === 'GET') return await this.routingView(true);
      if (url.pathname === '/_internal/routing' && request.method === 'GET') return await this.routingView();
      if (url.pathname === '/_internal/workspace-preflight' && request.method === 'POST') return await this.workspacePreflight(request);
      if (url.pathname === '/_internal/live-sessions' && request.method === 'GET') return this.liveSessionView();
      if (url.pathname === '/_internal/reap' && request.method === 'POST') return await this.reap(request);
      if (url.pathname === '/_metrics' && request.method === 'GET') return json({ counts: this.db.snapshot(), constructor_ms: this.constructorMs });
      if (url.pathname === '/api/connect' && request.method === 'GET') return await this.connect(request);
      if (url.pathname === '/api/heartbeat' && request.method === 'POST') return await this.heartbeat(request);
      if (url.pathname === '/api/native-inventory' && request.method === 'POST') {
        const machineId=this.machineActor(actorFrom(request),'machine:heartbeat');
        const registered=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
        if (!machineId||!registered||registered.machine_id!==machineId) return error('forbidden',403);
        if (this.db.schemaVersion<8) return error('inventory_schema_pending',503);
        return json(await this.nativeInventory.receive(await bodyObject(request,65_536)));
      }
      const nativeCommand=url.pathname.match(/^\/api\/native\/([^/]+)\/commands(?:\/([^/]+)(\/ack)?)?$/);
      if(nativeCommand){
        const response=await this.nativeCommands.handle(request,nativeCommand[1],nativeCommand[2],!!nativeCommand[3]);
        if(response.ok&&request.method==='POST'){
          await this.scheduleNext();
          const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
          if(machine)await this.notifyPendingCommands(machine.machine_id);
        }
        return response;
      }
      if (url.pathname === '/api/offers' && request.method === 'GET') return this.offers(request);
      if (url.pathname === '/api/sessions' && request.method === 'GET') return this.sessionList(request);
      const sessionView = url.pathname.match(/^\/api\/sessions\/([^/]+)$/);
      if (sessionView && request.method === 'GET') return this.sessionDetail(request, decodeURIComponent(sessionView[1]));
      const wake=url.pathname.match(/^\/api\/sessions\/([^/]+)\/(wake|wakes)$/);
      if(wake)return await this.sessionWake(request,decodeURIComponent(wake[1]),wake[2]);
      const sessionEvents = url.pathname.match(/^\/api\/sessions\/([^/]+)\/events$/);
      if (sessionEvents && request.method === 'GET') return this.sessionEvents(request, decodeURIComponent(sessionEvents[1]));
      const commandAck = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands\/([^/]+)\/ack$/);
      if (commandAck && request.method === 'POST') return await this.ackCommand(request, decodeURIComponent(commandAck[1]), decodeURIComponent(commandAck[2]));
      const link = url.pathname.match(/^\/api\/sessions\/([^/]+)\/link$/);
      if (link && request.method === 'POST') return await linkSession(request, this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', decodeURIComponent(link[1])), this.env);
      const legacy = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(stop|send|capture)$/);
      if (legacy && request.method === 'POST') return await this.legacyCommand(request, decodeURIComponent(legacy[1]), legacy[2]);
      const commandList = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands$/);
      if (commandList && request.method === 'GET') return this.commandListing(request, decodeURIComponent(commandList[1]));
      const commandSend = url.pathname.match(/^\/api\/sessions\/([^/]+)\/commands$/);
      if (commandSend && request.method === 'POST') return await this.createCommand(request, decodeURIComponent(commandSend[1]));
      const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/(started|finish|admission|credentials)$/);
      if (match && request.method === 'POST') {
        const sessionId = decodeURIComponent(match[1]);
        if (match[2] === 'started') return await this.started(request, sessionId);
        if (match[2] === 'admission') return await this.admission(request, sessionId);
        if (match[2] === 'credentials') return await this.childCredentials(request,sessionId);
        return await this.finishObserved(request, sessionId);
      }
      return error('not_found', 404);
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('internal_error', 500);
    }
  }

  private async queueEngineUpdate(request: Request): Promise<Response> {
    if (this.db.schemaVersion < 10) return error('schema_upgrade_required', 503);
    const body = await bodyObject(request, 8192);
    const jobId = typeof body.job_id === 'string' ? body.job_id : '';
    const engine = typeof body.engine === 'string' ? body.engine : '';
    const binary = typeof body.binary === 'string' ? body.binary : '';
    const versionCommand = typeof body.version_command === 'string' ? body.version_command : '';
    const updateCommand = typeof body.update_command === 'string' ? body.update_command : '';
    if (!jobId || !engine || !binary || !versionCommand || !updateCommand) return error('invalid_engine_update_job', 400);
    const now = Date.now();
    const machine = this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const sockets = machine ? this.machineSockets(machine.machine_id) : [];
    if (!sockets.length) {
      this.db.run(
        'INSERT OR REPLACE INTO engine_update_jobs(id,engine,binary,version_command,update_command,state,old_version,new_version,exit_code,error,output,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
        jobId, engine, binary, versionCommand, updateCommand, 'failed', null, null, null, 'machine_offline', null, now, now,
      );
      return json({ id: jobId, engine, state: 'failed', error: 'machine_offline' });
    }
    this.db.run(
      'INSERT OR REPLACE INTO engine_update_jobs(id,engine,binary,version_command,update_command,state,old_version,new_version,exit_code,error,output,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)',
      jobId, engine, binary, versionCommand, updateCommand, 'pending', null, null, null, null, null, now, now,
    );
    const frame = JSON.stringify({
      type: 'engine.update',
      job: { id: jobId, engine, binary, version_command: versionCommand, update_command: updateCommand },
    });
    let delivered = false;
    for (const socket of sockets) {
      try {
        socket.send(frame);
        delivered = true;
        break;
      } catch {}
    }
    if (!delivered) {
      this.db.run(
        "UPDATE engine_update_jobs SET state='failed',error='machine_socket_send_failed',updated_at=? WHERE id=?",
        Date.now(), jobId,
      );
      return json({ id: jobId, engine, state: 'failed', error: 'machine_socket_send_failed' });
    }
    return json({ id: jobId, engine, state: 'pending' });
  }

  private getEngineUpdateJob(jobId: string): Response {
    if (this.db.schemaVersion < 10) return error('schema_upgrade_required', 503);
    const row = this.db.one<any>('SELECT * FROM engine_update_jobs WHERE id=?', jobId);
    if (!row) return error('not_found', 404);
    return json(row);
  }

  private async ackEngineUpdateJob(request: Request, jobId: string): Promise<Response> {
    if (this.db.schemaVersion < 10) return error('schema_upgrade_required', 503);
    const actor = actorFrom(request);
    if (!actor || actor.kind !== 'machine' || !actor.resource_id) return error('forbidden', 403);
    const row = this.db.one<any>('SELECT * FROM engine_update_jobs WHERE id=?', jobId);
    if (!row) return error('not_found', 404);
    const body = await bodyObject(request, 16_384);
    const ok = body.ok === true;
    const oldVersion = typeof body.old_version === 'string' ? body.old_version.slice(0, 120) : null;
    const newVersion = typeof body.new_version === 'string' ? body.new_version.slice(0, 120) : null;
    const exitCode = Number.isSafeInteger(body.exit_code) ? Number(body.exit_code) : null;
    const errText = typeof body.error === 'string' ? body.error.slice(0, 500) : null;
    const output = typeof body.output === 'string' ? body.output.slice(0, 2000) : null;
    const state = ok ? 'completed' : 'failed';
    this.db.run(
      'UPDATE engine_update_jobs SET state=?,old_version=?,new_version=?,exit_code=?,error=?,output=?,updated_at=? WHERE id=?',
      state, oldVersion, newVersion, exitCode, errText, output, Date.now(), jobId,
    );
    return json({ ok: true, id: jobId, state });
  }

  async alarm(): Promise<void> {
    await this.ready;
    const now = Date.now();
    this.nativeCommands.expire(now);
    await this.wakes.sweep();
    const wakeMachine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');if(wakeMachine)await this.notifyPendingCommands(wakeMachine.machine_id);
    const expired = this.db.all<SessionRow>(
      "SELECT * FROM sessions WHERE state IN ('staged','offered','claimed') AND native_id IS NULL AND created_at<=? ORDER BY created_at,id LIMIT 100",
      now-CLAIMED_WITHOUT_NATIVE_GRACE_MS);
    for (const session of expired) await this.recordLaunchTimeout(session, now);
    this.db.run("UPDATE session_commands SET state='failed',result_json=?,updated_at=? WHERE kind='stop' AND state='pending' AND created_at<=?",
      JSON.stringify({stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed',stage:'host_stop'}),now,now-60_000);
    await this.reapInactive(false);
    await this.checkHostPresence(now);
    await this.scheduleNext(true);
    await this.flushOutbox(now);
    await this.scheduleNext(true);
  }

  private async heartbeat(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:heartbeat');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 262_144);
    const os = typeof body.os === 'string' && body.os.length <= 40 ? body.os : 'unknown';
    const arch = typeof body.arch === 'string' && body.arch.length <= 40 ? body.arch : 'unknown';
    const labels = Array.isArray(body.labels) ? body.labels : [];
    const engines=body.engines??[],capabilities=body.capabilities??[];
    if(!Array.isArray(engines)||engines.length>32||!engines.every(v=>boundedString(v,80))
      ||!Array.isArray(capabilities)||capabilities.length>2080||!capabilities.every(v=>boundedString(v,80)))return error('invalid_capabilities',400);
    // Repository inventory has an independent allowance so adding clones cannot
    // consume the 32 engine/resource observations needed for placement.
    const workspaces=capabilities.filter(v=>typeof v==='string'&&/^workspace:[a-z0-9][a-z0-9._-]{0,63}$/.test(v));
    if(workspaces.length>2048||capabilities.length-workspaces.length>32)return error('invalid_capabilities',400);
    const enginesJson=JSON.stringify([...new Set(engines as string[])].sort()),capabilitiesJson=JSON.stringify([...new Set(capabilities as string[])].sort());
    if (labels.length > 64 || !labels.every(value => typeof value === 'string' && value.length > 0 && value.length <= 80)) return error('invalid_capabilities', 400);
    const now = Date.now();
    const existing = this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?', machineId);
    if (!existing) {
      this.db.run('INSERT INTO machines(machine_id, alias, os, arch, labels_json, last_seen, next_epoch) VALUES (?, ?, ?, ?, ?, ?, 0)',
        machineId, machineId, os, arch, JSON.stringify([...new Set(labels)]), now);
    } else if (now - existing.last_seen >= 60_000 || existing.os !== os || existing.arch !== arch || existing.labels_json !== JSON.stringify([...new Set(labels)])) {
      this.db.run('UPDATE machines SET os=?, arch=?, labels_json=?, last_seen=? WHERE machine_id=?', os, arch, JSON.stringify([...new Set(labels)]), now, machineId);
    }
    const prior=this.db.one<{engines_json:string;capabilities_json:string}>('SELECT engines_json,capabilities_json FROM machine_capabilities WHERE machine_id=?',machineId);
    if(!prior||prior.engines_json!==enginesJson||prior.capabilities_json!==capabilitiesJson)this.db.run('INSERT INTO machine_capabilities(machine_id,engines_json,capabilities_json) VALUES(?,?,?) ON CONFLICT(machine_id) DO UPDATE SET engines_json=excluded.engines_json,capabilities_json=excluded.capabilities_json',machineId,enginesJson,capabilitiesJson);
    const nativeSessions=body.native_sessions;
    if(nativeSessions!==undefined&&(!Array.isArray(nativeSessions)||nativeSessions.length>100||nativeSessions.some(value=>{
      if(!value||typeof value!=='object'||Array.isArray(value))return true;
      const row=value as JsonRecord;
      return !boundedString(row.session_id,180)||(row.native_id!==null&&!boundedString(row.native_id,180))
        ||!boundedString(row.phase,40)||(row.last_activity_at!==null&&!Number.isSafeInteger(row.last_activity_at))
        ||(row.exit_code!==null&&(!Number.isInteger(row.exit_code)||Number(row.exit_code)<-2147483648||Number(row.exit_code)>2147483647))
        ||typeof row.containment_empty!=='boolean';
    })))return error('invalid_native_session_snapshot',400);
    if(Array.isArray(nativeSessions))this.db.transaction(this.ctx,()=>{
      this.db.run('DELETE FROM native_sessions');
      for(const value of nativeSessions as JsonRecord[]){
        this.db.run('INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at) VALUES(?,?,?,?,?,?,?)',
          value.session_id,value.native_id,value.phase,value.last_activity_at,value.exit_code,Number(value.containment_empty),now);
      }
      this.db.run('INSERT INTO machine_liveness(singleton,observed_at) VALUES(1,?) ON CONFLICT(singleton) DO UPDATE SET observed_at=excluded.observed_at',now);
    });
    // A heartbeat with no socket (daemon cannot connect, or the DO restarted)
    // starts a disconnect episode; the next socket ends it.
    if(!this.hostSocketOpen(machineId))await this.hostDisconnected(machineId,now,'heartbeat_without_socket');
    await this.scheduleNext();
    return json({ ok: true, machine_id: machineId, last_seen: now });
  }

  /** An engine that refused to start on its auth/preflight check is unhealthy
   * on this machine for a bounded time; the picker routes around it. */
  private async markEngineUnhealthy(engine:string,reason:string,sessionId:string,now:number):Promise<void>{
    const health=await this.engineHealth(now);
    health[engine]={reason,session_id:sessionId,failed_at:now,until:now+ENGINE_UNHEALTHY_MS};
    if(typeof this.ctx.storage.put==='function')await this.ctx.storage.put('engine_health',health);
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    console.error('[hub2] ENGINE_UNHEALTHY',JSON.stringify({machine_id:machine?.machine_id??null,engine,reason,session_id:sessionId,until:now+ENGINE_UNHEALTHY_MS}));
  }

  private async engineHealth(now:number):Promise<Record<string,{reason:string;session_id:string;failed_at:number;until:number}>>{
    // SQL-only storage (no KV) has recorded no engine health.
    if(typeof this.ctx.storage.get!=='function')return {};
    const stored=await this.ctx.storage.get<Record<string,{reason:string;session_id:string;failed_at:number;until:number}>>('engine_health')??{};
    return Object.fromEntries(Object.entries(stored).filter(([,entry])=>Number.isSafeInteger(entry?.until)&&entry.until>now));
  }

  private async routingView(passive=false):Promise<Response>{
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    if(!machine)return json({available:false,reason:'machine_not_enrolled'});
    let connected=false;
    let release:HostRelease|undefined;
    for(const socket of this.machineSockets(machine.machine_id,!passive)){
      if(passive){try{if(socket.deserializeAttachment()?.machine_id!==machine.machine_id)continue;}catch{continue;}}
      if(socket.readyState!==1||this.stopOnly(socket))continue;
      const attachment=await this.authorizedAttachment(socket,'machine:offer',!passive,this.env,!passive);
      if(attachment?.machine_id!==machine.machine_id)continue;
      connected=true;
      // The running daemon reports these values. They are not an attestation.
      release=hostRelease(attachment.host_version,attachment.protocol_version);
      break;
    }
    const capabilities=this.db.one<{engines_json:string;capabilities_json:string}>('SELECT engines_json,capabilities_json FROM machine_capabilities WHERE machine_id=?',machine.machine_id);
    // Report protocol skew for operators without changing socket admission or dispatch.
    const versionMismatch = release && release.protocol_version !== HOST_PROTOCOL_VERSION;
    const visibleCapabilities=(capabilities?JSON.parse(capabilities.capabilities_json):[]) as string[];
    const currentCapabilities=visibleCapabilities.filter(cap=>cap!==MANAGED_CLAUDE_CAPABILITY
      ||connected&&this.managedClaudeAvailable(machine.machine_id));
    const presence=connected?undefined:await this.hostPresence();
    const unhealthy=Object.entries(await this.engineHealth(Date.now())).map(([engine,entry])=>({engine,reason:entry.reason,session_id:entry.session_id,failed_at:entry.failed_at,until:entry.until}));
    const hostAlert=presence?.alerted_at?{kind:'host_disconnected',recipient:HOST_ALERT_RECIPIENT,
      disconnected_since:presence.disconnected_since,alerted_at:presence.alerted_at,evidence:presence.evidence??null}:undefined;
    return json({machine_id:machine.machine_id,connected,...release,version_mismatch:versionMismatch,
      last_seen:machine.last_seen,os:machine.os,arch:machine.arch,labels:JSON.parse(machine.labels_json),
      engines:capabilities?JSON.parse(capabilities.engines_json):[],capabilities:currentCapabilities,
      ...(unhealthy.length?{unhealthy_engines:unhealthy}:{}),
      ...(presence?{disconnected_since:presence.disconnected_since}:{}),...(hostAlert?{host_alert:hostAlert}:{})});
  }

  private managedClaudeSocket(socket:WebSocket):boolean {
    try{return socket.readyState===1&&!this.stopOnly(socket)&&socket.deserializeAttachment()?.managed_claude_ready===true;}
    catch{return false;}
  }

  private managedClaudeAvailable(machineId:string):boolean {
    const row=this.db.one<{capabilities_json:string}>('SELECT capabilities_json FROM machine_capabilities WHERE machine_id=?',machineId);
    const caps=this.parse(row?.capabilities_json);
    return Array.isArray(caps)&&caps.includes(MANAGED_CLAUDE_CAPABILITY)
      &&this.machineSockets(machineId).some(socket=>this.managedClaudeSocket(socket));
  }

  private managedClaudeLaunch(launch:unknown):boolean {
    return !!launch&&typeof launch==='object'&&!Array.isArray(launch)&&(launch as JsonRecord).managed_credential_label!==undefined;
  }

  private offers(request: Request): Response {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:offer');
    if (!machineId) return error('forbidden', 403);
    const now = Date.now();
    // The selector is delivered only on the readiness-qualified socket. An
    // older REST poller sharing this machine identity cannot silently ignore it.
    const rows = this.offerRows(machineId,now,false).filter(row=>!this.managedClaudeLaunch(this.parse(row.launch_json)));
    return json({ offers: rows.map(row => this.offerView(row)) });
  }

  private offerRows(machineId:string,now:number,allowManaged=true):SessionRow[]{
    // Filter before the shared ten-row budget. Held opt-in attempts must not
    // hide ordinary starts during a profile withdrawal or daemon rollback.
    const delivery=allowManaged?'':` AND CASE WHEN json_valid(launch_json) THEN json_type(launch_json,'$.managed_credential_label') IS NULL ELSE 1 END`;
    const rows=this.db.all<SessionRow>(
      `SELECT * FROM sessions WHERE machine_id=? AND ((state='offered' AND offer_expires_at>?) OR state='claimed')${delivery} ORDER BY created_at,id LIMIT 10`,machineId,now);
    // Historical repair fills spare offer capacity; it cannot displace starts.
    if(rows.length===10)return rows;
    let recovery:SessionRow[]=[];
    try{
      recovery=this.db.all<SessionRow>(
        `SELECT * FROM sessions WHERE machine_id=? AND (${LABEL_RECOVERY})${delivery} ORDER BY ${RECOVERY_TURN},created_at,id LIMIT ?`,machineId,10-rows.length);
      if(recovery.length)this.db.transaction(this.ctx,()=>{
        let turn=this.db.one<{turn:number|null}>(
          `SELECT MAX(${RECOVERY_TURN}) AS turn FROM sessions WHERE machine_id=? AND (${LABEL_RECOVERY})`,machineId)?.turn??0;
        if(turn>Number.MAX_SAFE_INTEGER-recovery.length){
          // Rare counter exhaustion: preserve queue order with a bounded
          // renumber, rather than wrap older rows back ahead of unseen rows.
          const queue=this.db.all<SessionRow>(
            `SELECT * FROM sessions WHERE machine_id=? AND (${LABEL_RECOVERY}) ORDER BY ${RECOVERY_TURN},created_at,id LIMIT 1001`,machineId);
          if(queue.length>1000)throw new Error('workspace_recovery_rotation_bound');
          for(const [index,row] of queue.entries())this.db.run(
            `UPDATE sessions SET launch_json=json_set(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}',CAST(? AS INTEGER)) WHERE id=?`,index,row.id);
          turn=queue.length;
        }
        for(const row of recovery)this.db.run(
          `UPDATE sessions SET launch_json=json_set(launch_json,'$.${WORKSPACE_RECOVERY_TURN_FIELD}',CAST(? AS INTEGER)) WHERE id=? AND operation_id=? AND epoch=? AND state='running'`,
          ++turn,row.id,row.operation_id,row.epoch);
      });
    }catch{
      // Bookkeeping failure must not become a start gate. The selected rows
      // remain retryable; durable fairness is unavailable until checkpointing
      // recovers, and must not be inferred from a successful HTTP response.
      console.warn('[hub2] workspace recovery rotation checkpoint unavailable');
    }
    rows.push(...recovery);
    return rows;
  }

  private async reserveInternal(request: Request): Promise<Response> {
    // Diagnostics travel separately, so a full policy pool never consumes the
    // user launch-spec allowance. Both fields retain their own bounded checks.
    const body = await bodyObject(request, 65_536);
    const required = ['operation_id', 'dispatch_id', 'session_id', 'project_id', 'intake_id', 'machine_id', 'request_digest', 'title'];
    if (required.some(key => typeof body[key] !== 'string') || !Number.isSafeInteger(body.attempt) || Number(body.attempt) < 0) return error('invalid_reservation', 400);
    if (typeof body.machine_id !== 'string' || typeof body.operation_id !== 'string' || typeof body.session_id !== 'string') return error('invalid_reservation', 400);
    if (!Array.isArray(body.acceptance_criteria) || body.acceptance_criteria.length < 1) return error('acceptance_criteria_required', 409);
    const launch = body.launch;
    if (!launch || typeof launch !== 'object' || Array.isArray(launch)
      || typeof (launch as JsonRecord).workspace !== 'string' || typeof (launch as JsonRecord).engine !== 'string'
      || typeof (launch as JsonRecord).prompt !== 'string' || typeof (launch as JsonRecord).interactive !== 'boolean') return error('invalid_launch_spec', 400);
    if (new TextEncoder().encode(JSON.stringify(launch)).byteLength > 12_000) return error('launch_spec_too_large', 413);
    if (Object.hasOwn(launch, '_hub_picker') || Object.hasOwn(launch,WORKSPACE_BINDING_FIELD)
      ||Object.hasOwn(launch,WORKSPACE_REPAIR_FIELD)||Object.hasOwn(launch,WORKSPACE_RECOVERY_TURN_FIELD)) return error('reserved_launch_field', 400);
    const managed=(launch as JsonRecord).managed_credential_label;
    if(managed!==undefined){
      if(managed!=='hub-claude'||(launch as JsonRecord).engine!=='claude'||(launch as JsonRecord).model!=='opus'
        ||(launch as JsonRecord).effort!=='medium'||(launch as JsonRecord).interactive!==false
        ||(launch as JsonRecord).with_goal===true||body.account_id!=='claude')return error('invalid_managed_credential_route',400);
      if(!this.managedClaudeAvailable(String(body.machine_id)))return error('managed_exec_unavailable',409);
    }
    const picker = normalizeStartPickerEvidence(body.picker);
    if (body.picker !== undefined && (!picker || picker.machine_id !== body.machine_id || picker.attempt !== body.attempt
      || picker.engine !== (launch as JsonRecord).engine || picker.model !== (launch as JsonRecord).model
      || picker.effort !== ((launch as JsonRecord).effort ?? null))) return error('invalid_picker_evidence', 400);
    // Use an existing JSON column; keep evidence out of the native launch wire.
    const storedLaunch = { ...(launch as JsonRecord), ...(picker ? { _hub_picker: picker } : {}) };
    const now = Date.now();
    const deadline=(launch as JsonRecord).start_deadline_at;
    if(deadline!==undefined&&(!Number.isSafeInteger(deadline)||Number(deadline)>now+120_000))return error('invalid_start_deadline',400);
    if(deadline!==undefined&&Number(deadline)<=now)return error('host_no_response',409);
    const acceptedAt=deadline===undefined?now:Number(deadline)-120_000;
    const priorOperation = this.db.one<{ session_id: string; epoch: number; request_digest: string; state: string }>(
      'SELECT session_id,epoch,request_digest,state FROM machine_session_operations WHERE operation_id=?', body.operation_id,
    );
    const prior = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', body.session_id);
    if (priorOperation && priorOperation.session_id === body.session_id && priorOperation.state !== 'current') {
      return json({ session_id: body.session_id, epoch: priorOperation.epoch, state: priorOperation.state, duplicate: true }, 200);
    }
    if (prior && prior.operation_id === body.operation_id) {
      if (prior.request_digest !== body.request_digest || prior.dispatch_id !== body.dispatch_id || prior.machine_id !== body.machine_id
        || prior.attempt !== body.attempt || prior.account_id !== (typeof body.account_id === 'string' ? body.account_id : null)) return error('idempotency_conflict', 409);
      return json({ session_id: prior.id, epoch: prior.epoch, state: prior.state, duplicate: true }, 200);
    }
    let machine = this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?', body.machine_id);
    if(!machine){
      this.db.run('INSERT INTO machines(machine_id,alias,os,arch,labels_json,last_seen,next_epoch) VALUES(?,?,?,?,?,?,0)',
        body.machine_id,body.machine_id,'unknown','unknown','[]',now);
      machine=this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?',body.machine_id);
    }
    const retrying = prior !== undefined;
    if (retrying && (prior!.state !== 'expired' || prior!.dispatch_id !== body.dispatch_id
      || prior!.request_digest !== body.request_digest || prior!.machine_id !== body.machine_id
      || prior!.attempt + 1 !== body.attempt || body.retry_epoch !== prior!.epoch)) return error('retry_fence_conflict', 409);
    if (!retrying && (Number(body.attempt) !== 0 || body.retry_epoch !== undefined)) return error('retry_without_expired_offer', 409);
    let epoch = 0;
    try {
      this.db.transaction(this.ctx, () => {
        const fresh=this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?',body.machine_id);
        if (!fresh) throw new HttpError('machine_route_unavailable',503);
        epoch=fresh.next_epoch+1;
        this.db.run('UPDATE machines SET next_epoch=? WHERE machine_id=?', epoch, body.machine_id);
        if (retrying) {
          this.db.run("UPDATE session_commands SET state='failed',result_json=?,updated_at=? WHERE session_id=? AND machine_id=? AND epoch=? AND state='pending'",
            JSON.stringify({error:'session_epoch_superseded'}),now,body.session_id,body.machine_id,prior!.epoch);
          this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=?",
            prior!.operation_id, prior!.id, prior!.epoch);
          this.db.run(`UPDATE sessions SET operation_id=?,attempt=?,epoch=?,account_id=?,launch_json=?,state='staged',offer_expires_at=NULL,
            native_id=NULL,evidence_json=NULL,evidence_digest=NULL,exit_code=NULL,updated_at=? WHERE id=? AND state='expired' AND epoch=?`,
            body.operation_id, body.attempt, epoch, typeof body.account_id === 'string' ? body.account_id : null, JSON.stringify(storedLaunch), now, body.session_id, prior!.epoch);
        } else {
          this.db.run(`INSERT INTO sessions(id, operation_id, request_digest, dispatch_id, attempt, account_id, project_id, intake_id, machine_id, epoch, title,
            acceptance_criteria_json, launch_json, state, native_id, offer_expires_at, evidence_json, evidence_digest, exit_code, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'staged', NULL, NULL, NULL, NULL, NULL, ?, ?)`,
            body.session_id, body.operation_id, body.request_digest, body.dispatch_id, body.attempt, typeof body.account_id === 'string' ? body.account_id : null,
            body.project_id, body.intake_id, body.machine_id, epoch, body.title,
            JSON.stringify(body.acceptance_criteria), JSON.stringify(storedLaunch), acceptedAt, now);
        }
        this.db.run("INSERT INTO machine_session_operations(operation_id,session_id,epoch,request_digest,state) VALUES(?,?,?,?, 'current')",
          body.operation_id, body.session_id, epoch, body.request_digest);
      });
    } catch (caught) {
      return error('reservation_failed', 503);
    }
    return json({ session_id: body.session_id, epoch, state: 'staged' }, retrying ? 200 : 201);
  }

  private async activateInternal(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.session_id !== 'string' || !Number.isInteger(body.epoch)) return error('invalid_activation', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE operation_id=? AND id=?', body.operation_id, body.session_id);
    if (!row || row.epoch !== body.epoch) return error('reservation_not_found', 404);
    if (row.state === 'offered' || row.state === 'claimed' || row.state === 'running') return json({ session_id: row.id, epoch: row.epoch, state: row.state }, 200);
    if (row.state !== 'staged') return error('reservation_not_activatable', 409);
    const now = Date.now();
    const expires = row.created_at + CLAIMED_WITHOUT_NATIVE_GRACE_MS;
    if(expires<=now){await this.recordLaunchTimeout(row,now);return error('host_no_response',409);}
    this.db.run("UPDATE sessions SET state='offered', offer_expires_at=?, updated_at=? WHERE id=? AND state='staged'", expires, now, row.id);
    await this.ensureDeadline(expires);
    await this.notifyOffer(row.id);
    return json({ session_id: row.id, epoch: row.epoch, state: 'offered' }, 201);
  }

  private async connect(request: Request): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'machine:offer');
    if (!machineId) return error('forbidden', 403);
    if (request.headers.get('Upgrade')?.toLowerCase() !== 'websocket') return error('websocket_upgrade_required', 426);
    const stopOnly=request.headers.get('x-hub2-safety-mode')==='operator_stop';
    const query=new URL(request.url).searchParams;
    if(query.has('safety')&&!stopOnly)return error('untrusted_safety_mode',403);
    let client!:WebSocket,server!:WebSocket;
    const accepted=this.connectQueue.then(async()=>{
      const control=this.env.CONTROL.get(this.env.CONTROL.idFromName('global'));
      const current=await control.fetch('https://control.hub2.internal/_authorize',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({actor_id:actor!.id,generation:actor!.generation,scope:'machine:offer'}),
      });
      if(!current.ok)return false;
      const pair=new WebSocketPair();[client,server]=Object.values(pair);
      for(const old of this.ctx.getWebSockets('hub2:'+machineId)){
        try{old.close(1000,'connection superseded');}catch{}
      }
      server.serializeAttachment({machine_id:machineId,actor_id:actor!.id,generation:actor!.generation,
        stop_only:stopOnly,connection_id:crypto.randomUUID(),connected_at:Date.now(),
        ...(!stopOnly?reportedHostRelease(query):undefined)});
      this.ctx.acceptWebSocket(server,['hub2:'+machineId]);
      return true;
    });
    this.connectQueue=accepted.then(()=>{},()=>{});
    if(!await accepted)return error('credential_revoked',401);
    if(!stopOnly)await this.hostConnected(machineId);
    try { server.send(JSON.stringify({ type: 'resync', reason: 'connected' })); } catch {}
    await this.sendOffers(server, machineId);
    await this.sendPendingCommands(server, machineId);
    await this.sendPendingMail(server, machineId);
    return new Response(null, { status: 101, webSocket: client });
  }

  async webSocketMessage(socket:WebSocket,message:string|ArrayBuffer):Promise<void>{
    await this.ready;
    if(!this.currentSocket(socket))return;
    await this.webSocketMessageRaw(socket, message, this.env);
  }

  private async webSocketMessageRaw(socket: WebSocket, message: string | ArrayBuffer, env:Hub2Env): Promise<void> {
    if (typeof message !== 'string' || message.length > 8192) {
      try { socket.close(1009, 'message too large'); } catch {}
      return;
    }
    let body: JsonRecord;
    try { body = JSON.parse(message) as JsonRecord; } catch { return; }
    if(this.stopOnly(socket)&&body.type!=='resync'){try{socket.send(JSON.stringify({type:'error',error:'safety_socket_stop_only'}));}catch{}return;}
    if (body.type === 'ping') {
      try { socket.send(JSON.stringify({ type: 'pong', nonce: typeof body.nonce === 'string' ? body.nonce.slice(0, 80) : undefined })); } catch {}
      return;
    }
    if(body.type==='managed.claude.ready'){
      if(Object.keys(body).some(key=>!['type','protocol','ready'].includes(key))
        ||body.protocol!=='hub2-managed-claude-exec-v1'||typeof body.ready!=='boolean'){
        try{socket.send(JSON.stringify({type:'error',error:'invalid_managed_exec_readiness'}));}catch{}return;
      }
      try{
        const attachment=await this.authorizedAttachment(socket,'machine:offer',true,env);
        if(!attachment?.machine_id)return;
        const row=this.db.one<{capabilities_json:string}>('SELECT capabilities_json FROM machine_capabilities WHERE machine_id=?',attachment.machine_id);
        const caps=this.parse(row?.capabilities_json);
        const ready=body.ready&&Array.isArray(caps)&&caps.includes(MANAGED_CLAUDE_CAPABILITY);
        const prior=socket.deserializeAttachment()?.managed_claude_ready===true;
        socket.serializeAttachment({...attachment,managed_claude_ready:ready});
        if(ready&&!prior)await this.sendOffers(socket,attachment.machine_id,env);
      }catch{} // Optional code observation cannot poison the normal socket.
      return;
    }
    if (body.type === 'resync') {
      try {
        const attachment = await this.authorizedAttachment(socket, 'machine:offer',true,env);
        if (attachment?.machine_id) { await this.sendOffers(socket, attachment.machine_id,env); await this.sendPendingCommands(socket, attachment.machine_id,env); await this.sendPendingMail(socket, attachment.machine_id,env); }
      } catch {}
      return;
    }
    if (body.type === 'offer_ack') {
      await this.ackOffer(socket, body,env);
      return;
    }
    if (body.type === 'workspace.preflight.result') {
      if (typeof body.request_id === 'string') {
        const resolve = this.preflightWaiters.get(body.request_id);
        if (resolve) { this.preflightWaiters.delete(body.request_id); resolve(body); }
      }
      return;
    }
    if (body.type === 'host.error') { await this.recordAttention(socket, body,env); return; }
    try { socket.send(JSON.stringify({ type: 'error', error: 'unsupported_message' })); } catch {}
  }

  async webSocketClose(socket:WebSocket,code:number,reason:string,_wasClean:boolean):Promise<void>{
    await this.ready;
    try{socket.close(code===1005||code===1006?1000:code,reason);}catch{}
    let machineId:unknown;
    try{const attachment=socket.deserializeAttachment();machineId=attachment?.stop_only===true?undefined:attachment?.machine_id;}catch{}
    if(typeof machineId==='string'&&!this.hostSocketOpen(machineId,socket)){
      await this.hostDisconnected(machineId,Date.now(),'socket_closed');
      await this.scheduleNext();
    }
  }

  private presenceStore():PresenceStore|undefined{
    const storage=this.ctx.storage as unknown as Partial<PresenceStore>;
    return typeof storage.get==='function'&&typeof storage.put==='function'&&typeof storage.delete==='function'
      ?storage as PresenceStore:undefined;
  }

  private async hostPresence():Promise<HostPresence|undefined>{
    const value=await this.presenceStore()?.get(HOST_PRESENCE_KEY) as HostPresence|undefined;
    return value&&typeof value==='object'&&typeof value.machine_id==='string'&&Number.isSafeInteger(value.disconnected_since)?value:undefined;
  }

  /** True when an ordinary (not stop-only) host socket other than `except` is open. */
  private hostSocketOpen(machineId:string,except?:WebSocket):boolean{
    return this.ctx.getWebSockets('hub2:'+machineId).some(socket=>socket!==except&&socket.readyState===1&&!this.stopOnly(socket));
  }

  private async hostDisconnected(machineId:string,now:number,cause:'socket_closed'|'heartbeat_without_socket'):Promise<void>{
    const store=this.presenceStore(),presence=await this.hostPresence();
    if(!store)return;
    // A heartbeat without a socket proves the daemon runs: only then arm the
    // 15 minute alarm. A powered-off host leaves no pending alarm.
    if(presence){
      if(cause==='heartbeat_without_socket'&&!presence.running_seen_at)await store.put(HOST_PRESENCE_KEY,{...presence,running_seen_at:now} satisfies HostPresence);
      return;
    }
    await store.put(HOST_PRESENCE_KEY,{machine_id:machineId,disconnected_since:now,
      ...(cause==='heartbeat_without_socket'?{running_seen_at:now}:{})} satisfies HostPresence);
    console.error('[hub2] HOST_DISCONNECT_WATCH',JSON.stringify({machine_id:machineId,cause,disconnected_since:now,alert_after_ms:HOST_DISCONNECT_ALERT_MS}));
  }

  private async hostConnected(machineId:string):Promise<void>{
    const store=this.presenceStore(),presence=await this.hostPresence();
    if(!store||!presence)return;
    await store.delete(HOST_PRESENCE_KEY);
    if(presence.alerted_at)console.error('[hub2] HOST_RECONNECTED alert='+HOST_ALERT_RECIPIENT,JSON.stringify({machine_id:machineId,
      disconnected_since:presence.disconnected_since,alerted_at:presence.alerted_at,reconnected_at:Date.now()}));
  }

  /** Next alarm needed by the disconnect watch, or null when nothing is pending. */
  private presenceDue(presence:HostPresence|undefined,now:number):number|null{
    // Live sessions already keep the reaper alarm (which runs this check) armed.
    if(!presence||!presence.running_seen_at||presence.alerted_at||now-presence.disconnected_since>HOST_DISCONNECT_WATCH_MS)return null;
    const first=presence.disconnected_since+HOST_DISCONNECT_ALERT_MS;
    return Math.max(first,(presence.checked_at??0)+HOST_DISCONNECT_RECHECK_MS);
  }

  private async checkHostPresence(now:number):Promise<void>{
    const store=this.presenceStore(),presence=await this.hostPresence();
    if(!store||!presence||presence.alerted_at||now<presence.disconnected_since+HOST_DISCONNECT_ALERT_MS)return;
    if(this.hostSocketOpen(presence.machine_id)){await store.delete(HOST_PRESENCE_KEY);return;}
    const machine=this.db.one<MachineRow>('SELECT * FROM machines WHERE machine_id=?',presence.machine_id);
    const stranded=this.db.one<{total:number}>("SELECT COUNT(*) AS total FROM sessions WHERE machine_id=? AND state IN ('offered','claimed','running')",presence.machine_id)?.total??0;
    const heartbeat=!!machine&&machine.last_seen>=presence.disconnected_since&&now-machine.last_seen<=HOST_RUNNING_EVIDENCE_MS;
    const evidence=heartbeat?'http_heartbeat_without_socket':stranded>0?'sessions_without_socket':undefined;
    if(!evidence){await store.put(HOST_PRESENCE_KEY,{...presence,checked_at:now} satisfies HostPresence);return;}
    await store.put(HOST_PRESENCE_KEY,{...presence,checked_at:now,alerted_at:now,evidence} satisfies HostPresence);
    console.error('[hub2] HOST_DISCONNECTED alert='+HOST_ALERT_RECIPIENT,JSON.stringify({machine_id:presence.machine_id,
      disconnected_since:presence.disconnected_since,disconnected_ms:now-presence.disconnected_since,evidence,
      last_seen:machine?.last_seen??null,live_sessions:stranded}));
  }

  async webSocketError(_socket: WebSocket, _error: unknown): Promise<void> { await this.ready; }

  private async ackOffer(socket: WebSocket, body: JsonRecord,env:Hub2Env): Promise<void> {
    if (typeof body.session_id !== 'string' || !Number.isInteger(body.epoch)) return;
    try {
      const attachment = await this.authorizedAttachment(socket, 'machine:offer',true,env);
      if (!attachment?.machine_id) return;
      const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', body.session_id, attachment.machine_id);
      if (!row || row.epoch !== body.epoch) {
        socket.send(JSON.stringify({ type: 'error', error: 'stale_offer', session_id: body.session_id, epoch: body.epoch }));
        return;
      }
      const launch=this.parse(row.launch_json) as JsonRecord;
      if(this.managedClaudeLaunch(launch)&&(!this.managedClaudeSocket(socket)||!this.managedClaudeAvailable(row.machine_id))){
        socket.send(JSON.stringify({type:'error',error:'managed_exec_unavailable',session_id:body.session_id,epoch:body.epoch}));return;
      }
      const binding=mergeWorkspaceBinding(launch.workspace,launch[WORKSPACE_BINDING_FIELD],body.workspace_binding);
      const newlyBound=binding!==undefined&&launch[WORKSPACE_BINDING_FIELD]===undefined;
      const claim={operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,
        machine_id:row.machine_id,epoch:row.epoch,...(binding?{workspace_binding:binding}:{})};
      if (row.state === 'claimed' || row.state === 'running') {
        if(newlyBound){
          this.db.transaction(this.ctx,()=>{
            this.db.run('UPDATE sessions SET launch_json=? WHERE id=?',JSON.stringify({...launch,[WORKSPACE_BINDING_FIELD]:binding,
              ...(row.state==='running'&&/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(String(launch.workspace))
                ?{[WORKSPACE_REPAIR_FIELD]:true}:{})}),row.id);
            this.insertOutbox(`workspace-claim:${row.operation_id}`,'claim',row.operation_id,row.project_id,claim,Date.now());
          });
        }
        if(binding){
          const id=this.db.one('SELECT id FROM machine_outbox WHERE id=?',`workspace-claim:${row.operation_id}`)
            ?`workspace-claim:${row.operation_id}`:`claim:${row.operation_id}`;
          await this.flushOne(id);
          await this.scheduleNext();
        }
        socket.send(JSON.stringify({ type: 'ack', session_id: row.id, epoch: row.epoch, duplicate: true,
          ...(binding?{workspace_binding:binding}:{}) }));
        return;
      }
      if (row.state !== 'offered' || row.offer_expires_at === null || row.offer_expires_at <= Date.now()) {
        socket.send(JSON.stringify({ type: 'error', error: 'offer_unavailable', session_id: body.session_id, epoch: body.epoch }));
        return;
      }
      const now = Date.now();
      this.db.transaction(this.ctx, () => {
        if(newlyBound)this.db.run('UPDATE sessions SET launch_json=? WHERE id=?',JSON.stringify({...launch,[WORKSPACE_BINDING_FIELD]:binding}),row.id);
        this.db.run("UPDATE sessions SET state='claimed', offer_expires_at=NULL, updated_at=? WHERE id=? AND state='offered' AND epoch=?", now, row.id, row.epoch);
        this.db.run('INSERT OR IGNORE INTO launch_admissions VALUES(?,?,?,?)', row.operation_id,row.id,row.epoch,now);
        this.insertOutbox(`claim:${row.operation_id}`, 'claim', row.operation_id, row.project_id,
          claim, now);
      });
      await this.ensureDeadline(now+30_000);
      await this.flushOne(`claim:${row.operation_id}`);
      await this.scheduleNext();
      await this.sendPendingCommands(socket, attachment.machine_id,env);
      socket.send(JSON.stringify({ type: 'ack', session_id: row.id, epoch: row.epoch,
        ...(binding?{workspace_binding:binding}:{}) }));
    } catch(caught) {
      if(caught instanceof HttpError)try{socket.send(JSON.stringify({type:'error',error:caught.code,session_id:body.session_id,epoch:body.epoch}));}catch{}
    }
  }

  private async sendOffers(socket: WebSocket, machineId: string,env:Hub2Env=this.env): Promise<void> {
    if(this.stopOnly(socket))return;
    if(!this.currentSocket(socket))return;
    const now = Date.now();
    const rows = this.offerRows(machineId,now,this.managedClaudeSocket(socket)&&this.managedClaudeAvailable(machineId));
    for (const row of rows) {
      if(this.managedClaudeLaunch(this.parse(row.launch_json))&&(!this.managedClaudeSocket(socket)||!this.managedClaudeAvailable(machineId)))continue;
      try { socket.send(JSON.stringify({ type: 'session.offer', offer: this.offerView(row) })); } catch { return; }
    }
  }

  private async notifyOffer(sessionId: string): Promise<void> {
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND state=\'offered\'', sessionId);
    if (!row) return;
    const frame = JSON.stringify({ type: 'session.offer', offer: this.offerView(row) });
    for (const socket of this.machineSockets(`${row.machine_id}`)) {
      try {
        if(this.stopOnly(socket)||this.managedClaudeLaunch(this.parse(row.launch_json))&&(!this.managedClaudeSocket(socket)||!this.managedClaudeAvailable(row.machine_id)))continue;
        if(await this.authorizedAttachment(socket,'machine:offer',true,this.env))socket.send(frame);
      } catch {}
    }
  }

  private offerView(row: SessionRow): JsonRecord {
    const { _hub_picker: _picker, [WORKSPACE_BINDING_FIELD]: _binding, [WORKSPACE_REPAIR_FIELD]: _repair,
      [WORKSPACE_RECOVERY_TURN_FIELD]:_turn,...launch } = this.parse(row.launch_json) as JsonRecord;
    return {
      session_id: row.id, operation_id: row.operation_id, intake_id: row.intake_id,
      project_id: row.project_id, account_id: row.account_id, epoch: row.epoch, title: row.title,
      acceptance_criteria: this.parse(row.acceptance_criteria_json),
      ...(launch.managed_credential_label?{request_digest:row.request_digest}:{}),
      launch, expires_at: row.offer_expires_at, claimed: row.state === 'claimed'||row.state==='running',
      ...(row.state==='running'?{recovery_native_id:row.native_id}:{}),
    };
  }

  private async admission(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:admit');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || !Number.isSafeInteger(body.epoch)) return error('invalid_admission', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row || row.operation_id !== body.operation_id || row.epoch !== body.epoch || !['claimed', 'running'].includes(row.state)) return error('stale_or_unclaimed_admission', 409);
    const stub = this.env.PROJECTS.get(this.env.PROJECTS.idFromName(row.project_id));
    const response = await stub.fetch('https://project.internal/_internal/admission', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ operation_id: row.operation_id, session_id: row.id, machine_id: machineId, epoch: row.epoch }),
    });
    if (!response.ok) return error('launch_admission_refused', response.status === 503 ? 503 : 409);
    const result = await response.json() as JsonRecord;
    const launch = this.parse(row.launch_json) as JsonRecord;
    if(this.managedClaudeLaunch(launch)&&!this.managedClaudeAvailable(machineId))return error('managed_exec_unavailable',409);
    if (result.launch_allowed !== true || result.epoch !== row.epoch || result.engine !== launch.engine
      || (result.model ?? null) !== (launch.model ?? null) || !Number.isSafeInteger(result.observed_at)) return error('launch_admission_unverified', 503);
    const recorded = this.db.transaction(this.ctx, () => {
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',row.id);
      if (!current || current.operation_id!==row.operation_id || current.epoch!==row.epoch || !['claimed','running'].includes(current.state)) return false;
      const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=?',row.operation_id);
      if (!admission) this.db.run('INSERT INTO launch_admissions VALUES(?,?,?,?)',row.operation_id,row.id,row.epoch,result.observed_at);
      return admission?.observed_at ?? Number(result.observed_at);
    });
    if (!recorded) return error('stale_admission_result',409);
    const runtimeDeadline=Number(recorded)+Number(launch.timeout_seconds)*1000;
    if(!Number.isSafeInteger(runtimeDeadline)||runtimeDeadline<=Date.now()||runtimeDeadline>Date.now()+7_200_000)return error('runtime_deadline_unavailable',409);
    return json({...result,session_id:row.id,operation_id:row.operation_id,machine_id:machineId,
      account_id:row.account_id??null,runtime_deadline:runtimeDeadline});
  }

  private async childCredentials(request:Request,sessionId:string):Promise<Response>{
    const actor=actorFrom(request),machine=this.machineActor(actor,'session:admit');
    if(!machine||!actor?.generation)return error('forbidden',403);
    const body=await bodyObject(request,4096);
    const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machine);
    if(!row||row.state!=='claimed'||row.operation_id!==body.operation_id||row.epoch!==body.epoch)return error('child_credential_fence',409);
    const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    const timeout=(this.parse(row.launch_json) as JsonRecord).timeout_seconds;
    if(!admission||!Number.isSafeInteger(timeout)||Number(timeout)<1||Number(timeout)>86400)return error('launch_admission_required',409);
    const deadline=admission.observed_at+Number(timeout)*1000;
    if(deadline<=Date.now())return error('child_credential_deadline_passed',409);
    const response=await this.env.CONTROL.get(this.env.CONTROL.idFromName('global')).fetch('https://control.internal/_worker/issue',{
      method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:row.id,operation_id:row.operation_id,
        epoch:row.epoch,machine_id:row.machine_id,machine_generation:actor.generation,intake_id:row.intake_id,project_id:row.project_id,deadline})});
    if(!response.ok)return error('child_credential_unavailable',response.status===409?409:503);
    const result=await response.json() as JsonRecord;
    if(result.session_id!==row.id||result.operation_id!==row.operation_id||result.epoch!==row.epoch||result.machine_id!==row.machine_id||result.intake_id!==row.intake_id||result.project_id!==row.project_id||result.actor!=='session:'+row.id||typeof result.token!=='string'||!Number.isSafeInteger(result.expires_at)||Number(result.expires_at)>deadline||Number(result.expires_at)<=Date.now())return error('child_credential_unverified',503);
    if(!this.db.one('SELECT id FROM sessions WHERE id=? AND operation_id=? AND epoch=? AND state=\'claimed\'',row.id,row.operation_id,row.epoch))return error('child_credential_fence',409);
    return json(result,response.status);
  }

  private async started(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 4096);
    if (typeof body.operation_id !== 'string' || typeof body.native_id !== 'string' || body.native_id.length < 8 || body.native_id.length > 128 || !Number.isSafeInteger(body.epoch)) return error('invalid_native_receipt', 400);
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row) return error('session_not_found', 404);
    if (row.state==='lost' && row.operation_id===body.operation_id && row.native_id===body.native_id && row.epoch===body.epoch) {
      return json({ok:true,duplicate:true,state:'lost'});
    }
    if (row.state === 'running' && row.operation_id === body.operation_id && row.native_id === body.native_id && row.epoch === body.epoch) {
      const sent=this.db.one<{state:string}>('SELECT state FROM machine_outbox WHERE id=?',`started:${row.operation_id}`);
      const forwarded=sent?.state==='delivered'||await this.flushOne(`started:${row.operation_id}`);
      await this.scheduleNext();
      return json({ok:true,duplicate:true,forwarded});
    }
    if (row.state !== 'claimed' || row.operation_id !== body.operation_id || row.epoch !== body.epoch) return error('stale_or_unoffered_session', 409);
    if (!this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch)) return error('launch_admission_required',409);
    const launch=this.parse(row.launch_json) as JsonRecord;
    const binding=mergeWorkspaceBinding(launch.workspace,launch[WORKSPACE_BINDING_FIELD]);
    const now=Date.now();
    this.db.transaction(this.ctx,()=>{
      this.db.run("UPDATE sessions SET state='running', native_id=?, offer_expires_at=NULL, updated_at=? WHERE id=? AND state='claimed'",
        body.native_id,now,sessionId);
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,'running',?,NULL,0,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase='running',last_activity_at=excluded.last_activity_at,exit_code=NULL,containment_empty=0,observed_at=excluded.observed_at`,
        sessionId,body.native_id,now,now);
      this.insertOutbox(`started:${row.operation_id}`,'started',row.operation_id,row.project_id,
        {operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,machine_id:row.machine_id,epoch:row.epoch,native_id:body.native_id,
          ...(binding?{workspace_binding:binding}:{})},now);
    });
    await this.ensureDeadline(now+30_000);
    const forwarded=await this.flushOne(`started:${row.operation_id}`);
    await this.scheduleNext();
    return json({ ok: true, session_id: sessionId, state: 'running',forwarded }, forwarded?201:202);
  }

  private isCanary(session:SessionRow):boolean{
    const launch=this.parse(session.launch_json) as JsonRecord;
    return session.title.startsWith('hub-canary-')&&/(?:^|[/\\])hub-canary$/.test(String(boundWorkspace(launch)));
  }

  private async finishObserved(request:Request,sessionId:string):Promise<Response>{
    let response:Response;
    try{response=await this.finish(request,sessionId);}catch(caught){
      if(!(caught instanceof HttpError))throw caught;
      response=error(caught.code,caught.status);
    }
    if([400,404,409,410,413,422].includes(response.status)){
      const machine=this.machineActor(actorFrom(request),'session:report');
      const row=machine?this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machine):null;
      if(row&&!this.isCanary(row)){
        const detail=await response.clone().json() as JsonRecord;
        await this.queueFinishAlert(row,'finish_dead_letter',String(detail.error??'finish_rejected'),response.status);
      }
    }
    return response;
  }

  private async queueFinishAlert(row:SessionRow,kind:string,reason:string,status:number,extra:JsonRecord={}):Promise<void>{
    const key=`${kind}:${row.operation_id}:${row.epoch}`;
    this.insertOutbox(key,'finish-dead-letter',row.operation_id,row.project_id,{
      operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,
      intake_id:row.intake_id,machine_id:row.machine_id,epoch:row.epoch,kind,reason,status,
      recipient:'dev@dtcdev.click',observed_at:Date.now(),...extra},Date.now());
    await this.flushOne(key);
    await this.scheduleNext();
  }

  private async finishExpired(machineId:string,sessionId:string,body:JsonRecord):Promise<Response>{
    const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machineId);
    if(!row||row.operation_id!==body.operation_id||row.epoch!==body.epoch||row.native_id!==body.native_id)
      return error('stale_expired_launch',409);
    const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    const launch=this.parse(row.launch_json) as JsonRecord;
    const e=body.evidence as JsonRecord|undefined;
    const deadline=admission?admission.observed_at+Number(launch.timeout_seconds)*1000:null;
    if(body.launch_expired!==true||body.native_outcome!=='unconfirmed'||body.native_exit_confirmed!==undefined||body.exit_code!==undefined
      ||!deadline||deadline>Date.now()||body.runtime_deadline!==deadline
      ||!e||e.source!=='aplexer_all_registry_census'||e.dispatch_provenance!=='host_journal'||e.complete!==true||e.match_count!==0
      ||e.workspace!==boundWorkspace(launch)||e.tag!==`task-hub2-${row.id}`
      ||!Number.isSafeInteger(e.started_at_ms)||!Number.isSafeInteger(e.observed_at_ms)
      ||Number(e.started_at_ms)<deadline||Number(e.observed_at_ms)<Number(e.started_at_ms)
      ||Number(e.observed_at_ms)>Date.now()+5000||Number(e.observed_at_ms)-Number(e.started_at_ms)>6000
      ||!Number.isSafeInteger(e.row_count)||Number(e.row_count)<0||Number(e.row_count)>5000
      ||typeof e.sha256!=='string'||!/^[0-9a-f]{64}$/.test(e.sha256))return error('unqualified_expired_launch',409);
    // This records an expired host launch, never confirmed death or success.
    // Hub's independent lost-session/containment accounting remains authoritative.
    await this.queueFinishAlert(row,'launch_expired_unconfirmed','deadline_expired_native_absent',0,{evidence:e,native_id:body.native_id,runtime_deadline:deadline});
    return json({ok:true,native_outcome:'unconfirmed',terminal_report:true});
  }

  private async finish(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    const machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const body = await bodyObject(request, 12_288);
    if(body.launch_expired!==undefined)return await this.finishExpired(machineId,sessionId,body);
    if(body.failure_kind!==undefined||body.native_outcome!==undefined)return await this.finishLegacyUnobserved(machineId,sessionId,body);
    if(body.no_dispatch!==undefined)return await this.finishWithoutDispatch(machineId,sessionId,body);
    const launchFailed=body.launch_failed===true;
    if(body.launch_failed!==undefined&&typeof body.launch_failed!=='boolean')return error('invalid_launch_failure',400);
    if (typeof body.operation_id !== 'string' || !Number.isSafeInteger(body.epoch) || typeof body.exit_code !== 'number'
      || !Number.isInteger(body.exit_code) || body.exit_code < -2147483648 || body.exit_code > 2147483647
      || typeof body.native_id !== 'string' || body.native_exit_confirmed !== true) return error('invalid_finish_evidence', 400);
    const evidence = body.evidence && typeof body.evidence === 'object' && !Array.isArray(body.evidence) ? body.evidence as JsonRecord : {};
    const evidenceJson = JSON.stringify(evidence);
    if (new TextEncoder().encode(evidenceJson).byteLength > 8192) return error('evidence_too_large', 413);
    const evidenceDigest = await sha256Hex(JSON.stringify({ epoch: body.epoch, native_id: body.native_id, exit_code: body.exit_code, evidence,
      ...(launchFailed?{launch_failed:true}:{}) }));
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?', sessionId, machineId);
    if (!row) return error('session_not_found', 404);
    if(row.operation_id!==body.operation_id||row.epoch!==body.epoch)return error('stale_or_unmatched_finish',409);
    const launch = this.parse(row.launch_json) as JsonRecord;
    const identity = evidence.native_identity as JsonRecord | undefined;
    const nativeExit = evidence.native_exit as JsonRecord | undefined;
    if (!['aplexer_status', 'aplexer_retirement'].includes(String(evidence.source)) || evidence.containment_empty !== true
      || !identity || identity.id !== body.native_id || identity.tag !== `task-hub2-${row.id}`
      || identity.workspace !== boundWorkspace(launch) || !nativeExit || nativeReturncode(nativeExit) !== body.exit_code) return error('unqualified_native_exit_evidence', 409);
    const lateLaunchFailure=launchFailed&&row.state==='failed'&&row.native_id===null&&row.evidence_digest===null;
    if (row.state === 'completed-awaiting-review' || (row.state === 'failed'&&!lateLaunchFailure)) {
      return row.evidence_digest === evidenceDigest ? json({ ok: true, duplicate: true, state: row.state }) : error('finish_conflict', 409);
    }
    const nativeDeadline=evidence.native_deadline as JsonRecord|undefined;
    const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    // Native exit timestamps use the host clock. Apply the same bounded skew
    // allowance on both sides; the exact server-issued deadline stays fenced.
    if(!nativeDeadline||!Number.isSafeInteger(nativeDeadline.deadline_ms)||typeof nativeDeadline.exceeded!=='boolean'||!admission
      ||nativeDeadline.deadline_ms!==admission.observed_at+Number(launch.timeout_seconds)*1000
      ||!Number.isSafeInteger(nativeExit.exited_at_ms)||Number(nativeExit.exited_at_ms)<admission.observed_at-5000||Number(nativeExit.exited_at_ms)>Date.now()+5000
      ||(Number(nativeExit.exited_at_ms)>=Number(nativeDeadline.deadline_ms)&&nativeDeadline.exceeded!==true)
      ||(nativeDeadline.exceeded&&Number(nativeDeadline.deadline_ms)>Date.now()))return error('native_deadline_unverified',409);
    // A qualified native exit adds no new lifecycle transition after a confirmed
    // stop. Keep the stop outcome and its Project notification authoritative.
    if(row.state==='stopped'&&row.native_id===body.native_id&&this.stopConfirmed(row)){
      const now=Date.now(),key=`stopped-finish:${row.operation_id}:${row.epoch}`;
      const payload={operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,
        intake_id:row.intake_id,machine_id:row.machine_id,epoch:row.epoch,native_id:body.native_id,
        state:'stopped',exit_code:body.exit_code,native_exit_confirmed:true,evidence,evidence_digest:evidenceDigest};
      this.db.transaction(this.ctx,()=>{
        if(row.evidence_digest!==null)return;
        this.db.run("UPDATE sessions SET evidence_json=?,evidence_digest=?,exit_code=? WHERE id=? AND state='stopped' AND evidence_digest IS NULL",
          JSON.stringify(payload),evidenceDigest,body.exit_code,row.id);
        this.insertOutbox(key,'stopped-finish',row.operation_id,row.project_id,payload,now);
      });
      await this.flushOne(key);
      await this.scheduleNext();
      return json({ok:true,duplicate:true,state:'stopped'});
    }
    const lateLost=row.state==='lost'&&row.native_id===body.native_id&&!launchFailed;
    const admittedAbort=launchFailed&&(row.state==='claimed'||lateLaunchFailure)&&row.native_id===null
      &&/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(body.native_id)
      &&this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',row.operation_id,row.id,row.epoch);
    if (!lateLost&&!admittedAbort&&(launchFailed||row.state!=='running'||row.native_id!==body.native_id))return error('stale_or_unmatched_finish',409);
    const failureNotification=lateLaunchFailure
      ? this.db.one<{id:string}>("SELECT id FROM machine_outbox WHERE operation_id=? AND id IN (?,?,?)",
        row.operation_id,`reaped:${row.operation_id}:${row.epoch}`,`launch-refused:${row.operation_id}:${row.epoch}`,`legacy-unobserved:${row.operation_id}:${row.epoch}`)
      : undefined;
    if(lateLaunchFailure&&!failureNotification)return error('launch_failure_notification_missing',409);
    const now = Date.now();
    const typedFailure = typeof body.failure_reason === 'string' && Object.hasOwn(HOST_FAILURE_DETAILS, body.failure_reason)
      ? body.failure_reason : null;
    const state = !launchFailed&&!typedFailure&&nativeDeadline?.exceeded!==true&&Number(body.exit_code) === 0 ? 'completed-awaiting-review' : 'failed';
    const payload = { operation_id: row.dispatch_id, attempt_operation_id: row.operation_id, session_id: row.id,
      machine_id: row.machine_id,intake_id:row.intake_id, epoch: row.epoch, native_id: body.native_id,
      ...(launch[WORKSPACE_BINDING_FIELD]?{workspace_binding:launch[WORKSPACE_BINDING_FIELD]}:{}),
      ...(launchFailed?{launch_failed:true}:{}),
      ...(typedFailure?{failure_reason:typedFailure,detail:hostDetail(typedFailure,body.detail)}:{}),
      ...(failureNotification?{after_notification:failureNotification.id}:{}),
      // An accepted STOP remains intent even if its ACK failed or timed out.
      // Freeze it before forwarding; this is never proof of STOP confirmation.
      stop_requested:!!this.db.one(`SELECT id FROM session_commands WHERE session_id=? AND machine_id=? AND epoch=?
        AND kind='stop' LIMIT 1`,row.id,row.machine_id,row.epoch),
      exit_code: Number(body.exit_code), native_exit_confirmed: true, evidence, evidence_digest: evidenceDigest };
    if (lateLost) {
      if (row.evidence_digest !== null) return row.evidence_digest===evidenceDigest
        ? json({ok:true,duplicate:true,state:'lost'}) : error('finish_conflict',409);
      this.db.transaction(this.ctx,()=>{
        this.db.run("UPDATE sessions SET evidence_json=?,evidence_digest=?,exit_code=? WHERE id=? AND state='lost' AND evidence_digest IS NULL",
          JSON.stringify({...payload,lost:true}),evidenceDigest,Number(body.exit_code),row.id);
        this.db.run("UPDATE native_sessions SET phase='exited',exit_code=?,containment_empty=1,observed_at=? WHERE session_id=? AND native_id=?",
          Number(body.exit_code),now,row.id,row.native_id);
        // A newly qualified exit can retry the same loss notification if its
        // earlier revocation ran out of retries. Duplicate finishes cannot reset it.
        this.db.run("UPDATE machine_outbox SET state='pending',attempt=0,next_action_at=?,updated_at=? WHERE id=? AND state='dead_letter'",
          now,now,`lost:${row.operation_id}:${row.epoch}`);
      });
      await this.scheduleNext();
      return json({ok:true,state:'lost',native_exit_recorded:true});
    }
    if(typedFailure&&ENGINE_PREFLIGHT_FAILURES.has(typedFailure)&&typeof launch.engine==='string')
      await this.markEngineUnhealthy(launch.engine,typedFailure,row.id,now);
    else if(!typedFailure&&!launchFailed&&!payload.stop_requested&&nativeDeadline?.exceeded!==true&&typeof launch.engine==='string'
      &&launch.engine!=='shell'&&isFastStartRefusal(Number(body.exit_code),row.created_at,now))
      // The launcher refused within seconds (exit 75/127): route around this engine here too.
      await this.markEngineUnhealthy(launch.engine,'native_exit_'+String(body.exit_code),row.id,now);
    this.db.transaction(this.ctx, () => {
      const {[WORKSPACE_REPAIR_FIELD]:_repair,...completedLaunch}=launch;
      this.db.run('UPDATE sessions SET state=?, evidence_json=?, evidence_digest=?, exit_code=?, updated_at=?,native_id=?,launch_json=? WHERE id=? AND state=?',
        state, JSON.stringify(payload), evidenceDigest, Number(body.exit_code), now, body.native_id,JSON.stringify(completedLaunch),row.id, row.state);
      this.db.run("UPDATE native_sessions SET phase='exited',last_activity_at=?,exit_code=?,containment_empty=1,observed_at=? WHERE session_id=?",
        now,Number(body.exit_code),now,row.id);
      if(state==='failed'&&!payload.stop_requested)this.db.run('INSERT OR REPLACE INTO attention(session_id, reason, observed_at) VALUES (?, ?, ?)',
        row.id,nativeDeadline?.exceeded===true?'deadline_exceeded':typedFailure??(launchFailed?'launch_failed':'native_exit_'+String(body.exit_code)),now);
      if(failureNotification)this.db.run("UPDATE machine_outbox SET state='pending',attempt=0,next_action_at=?,updated_at=? WHERE id=? AND state='dead_letter'",
        now,now,failureNotification.id);
      this.insertOutbox(`finish:${row.operation_id}`, 'finish', row.operation_id, row.project_id, payload, now);
    });
    await this.ensureDeadline(now+30_000);
    const forwarded = await this.flushOne(`finish:${row.operation_id}`);
    await this.scheduleNext();
    return json({ ok: true, session_id: sessionId, state, forwarded }, forwarded ? 201 : 202);
  }

  private async finishLegacyUnobserved(machineId:string,sessionId:string,body:JsonRecord):Promise<Response>{
    if(body.failure_kind!=='legacy_census_absent'||body.native_outcome!=='unconfirmed'||body.launch_failed!==true
      ||body.native_id!==null||typeof body.operation_id!=='string'||!Number.isSafeInteger(body.epoch)
      ||Object.keys(body).some(key=>!['failure_kind','native_outcome','launch_failed','native_id','operation_id','epoch','evidence'].includes(key)))
      return error('invalid_legacy_launch_failure',400);
    const value=body.evidence;
    if(!value||typeof value!=='object'||Array.isArray(value))return error('invalid_legacy_census',400);
    const e=value as JsonRecord;
    if(Object.keys(e).some(key=>!['source','started_at_ms','observed_at_ms','row_count','sha256','complete','match_count','workspace','tag','dispatch_provenance'].includes(key))
      ||e.source!=='aplexer_all_registry_census'||e.dispatch_provenance!=='legacy_unknown'||e.complete!==true||e.match_count!==0
      ||!Number.isSafeInteger(e.started_at_ms)||!Number.isSafeInteger(e.observed_at_ms)||Number(e.started_at_ms)<1
      ||Number(e.observed_at_ms)<Number(e.started_at_ms)||Number(e.observed_at_ms)-Number(e.started_at_ms)>6000
      ||Number(e.observed_at_ms)>Date.now()+5000||!Number.isSafeInteger(e.row_count)||Number(e.row_count)<0||Number(e.row_count)>5000
      ||typeof e.sha256!=='string'||!/^[0-9a-f]{64}$/.test(e.sha256)||!boundedString(e.workspace,4096)||e.tag!=='task-hub2-'+sessionId)
      return error('invalid_legacy_census',400);
    const evidence={source:e.source,started_at_ms:e.started_at_ms,observed_at_ms:e.observed_at_ms,row_count:e.row_count,
      sha256:e.sha256,complete:true,match_count:0,workspace:e.workspace,tag:e.tag,dispatch_provenance:e.dispatch_provenance};
    const operation=body.operation_id,epoch=body.epoch as number,key='legacy-unobserved:'+operation+':'+epoch;
    const digest=await sha256Hex(JSON.stringify(evidence));
    this.db.transaction(this.ctx,()=>{
      const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machineId);
      const admission=this.db.one<{observed_at:number}>('SELECT observed_at FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',operation,sessionId,epoch);
      const attempt=this.db.one<{state:string}>('SELECT state FROM machine_session_operations WHERE operation_id=? AND session_id=? AND epoch=?',operation,sessionId,epoch);
      if(!row||!admission||!attempt||Number(evidence.started_at_ms)<admission.observed_at-5000)
        throw new HttpError('stale_legacy_launch_failure',409);
      const prior=this.db.one<{payload_json:string}>('SELECT payload_json FROM machine_outbox WHERE id=?',key);
      if(prior){
        if((this.parse(prior.payload_json) as JsonRecord).evidence_digest!==digest)throw new HttpError('legacy_launch_failure_conflict',409);
        return;
      }
      const current=row.operation_id===operation&&row.epoch===epoch;
      // Only the current attempt retains its launch specification. A prior
      // accepted receipt above is the replay proof for superseded attempts.
      const launch=this.parse(row.launch_json) as JsonRecord|null;
      if(!current||!launch||!boundedString(launch.workspace,1024)||boundWorkspace(launch)!==evidence.workspace)
        throw new HttpError('legacy_launch_workspace_unverified',409);
      // Only a pinned label resolution can carry the full canonical Unicode
      // path. Keep the legacy byte bound for unbound/absolute receipts.
      if((!launch[WORKSPACE_BINDING_FIELD]||!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(String(launch.workspace)))
        &&!boundedString(evidence.workspace,1024))throw new HttpError('invalid_legacy_census',400);
      if(row.native_id!==null)
        throw new HttpError('stale_legacy_launch_failure',409);
      if(current&&!['offered','claimed','expired','failed','stopped'].includes(row.state))
        throw new HttpError('stale_legacy_launch_failure',409);
      const now=Date.now();
      const payload={operation_id:row.dispatch_id,attempt_operation_id:operation,session_id:sessionId,intake_id:row.intake_id,
        machine_id:machineId,epoch,native_id:null,reason:'legacy_launch_unobserved',launch_failed:true,
        native_outcome:'unconfirmed',failure_kind:'legacy_census_absent',historical_only:row.state==='expired',
        evidence,evidence_digest:digest,observed_at:evidence.observed_at_ms};
      const boundPayload={...payload,...(launch[WORKSPACE_BINDING_FIELD]?{workspace_binding:launch[WORKSPACE_BINDING_FIELD]}:{})};
      if(current){
        const active=['offered','claimed'].includes(row.state);
        const previous=this.parse(row.evidence_json) as JsonRecord|null;
        const storedEvidence=active?payload:{...previous,legacy_launch_recovery:payload};
        this.db.run('UPDATE sessions SET state=?,evidence_json=?,updated_at=? WHERE id=?',active?'failed':row.state,JSON.stringify(storedEvidence),now,sessionId);
        if(active){
          this.db.run("UPDATE machine_session_operations SET state='failed' WHERE operation_id=? AND session_id=? AND epoch=?",operation,sessionId,epoch);
          this.settlePendingCommands(row,now,'legacy_launch_unobserved',null);
        }
      }
      this.insertOutbox(key,'legacy-launch-failure',operation,row.project_id,boundPayload,now);
    });
    const forwarded=await this.flushOne(key)||!!this.db.one("SELECT id FROM machine_outbox WHERE id=? AND state='delivered'",key);
    await this.scheduleNext();
    return json({ok:true,launch_failed:true,native_outcome:'unconfirmed',forwarded},forwarded?200:202);
  }

  private async finishWithoutDispatch(machineId:string,sessionId:string,body:JsonRecord):Promise<Response>{
    if(body.no_dispatch!==true||body.launch_failed!==true||body.native_id!==null||body.exit_code!==undefined
      ||body.native_exit_confirmed!==undefined||typeof body.operation_id!=='string'||!Number.isSafeInteger(body.epoch))
      return error('invalid_no_dispatch_receipt',400);
    const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',sessionId,machineId);
    const operationId=body.operation_id,epoch=body.epoch as number;
    const admission=this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',
      operationId,sessionId,epoch);
    const admissionExpired=body.admission_expired===true&&row&&row.native_id===null
      &&Date.now()>=(row.offer_expires_at??row.created_at+60_000);
    if(!row||!admission&&!admissionExpired)return error('stale_no_dispatch_receipt',409);
    const current=row.operation_id===operationId&&row.epoch===epoch;
    if(current&&row.native_id!==null)return error('stale_no_dispatch_receipt',409);
    if(current&&['offered','claimed'].includes(row.state)){
      const failureReason = closedLaunchReason(body.failure_reason);
      const detail = body.detail === undefined ? undefined : hostDetail(failureReason,body.detail);
      await this.recordLaunchRefusal(row, failureReason, true, detail);
    }else{
      const settled=current ? ['failed','stopped','expired'].includes(row.state)
        : epoch<row.epoch&&!!this.db.one("SELECT operation_id FROM machine_session_operations WHERE operation_id=? AND session_id=? AND epoch=? AND state='expired'",
          operationId,sessionId,epoch);
      if(!settled)return error('stale_no_dispatch_receipt',409);
      const refusal=this.db.one("SELECT id FROM machine_outbox WHERE id=? AND json_extract(payload_json,'$.no_dispatch')=1",
        `launch-refused:${operationId}:${epoch}`);
      if(!refusal){
        const now=Date.now(),key=`no-dispatch-revoke:${operationId}:${epoch}`;
        this.insertOutbox(key,'no-dispatch-revoke',operationId,row.project_id,{
          session_id:row.id,attempt_operation_id:operationId,epoch,machine_id:row.machine_id,
          intake_id:row.intake_id,no_dispatch:true},now);
        await this.flushOne(key);
        await this.scheduleNext();
      }
    }
    return json({ok:true,no_dispatch:true});
  }

  private async flushOutbox(now: number): Promise<void> {
    const rows = this.db.all<OutboxRow>(
      `SELECT * FROM machine_outbox WHERE (state='pending' OR (kind='wake' AND state='wake_pending')) AND next_action_at <= ? ORDER BY next_action_at, id LIMIT 20`, now,
    );
    for (const row of rows) await this.flushOne(row.id);
  }

  private async flushOne(id:string):Promise<boolean>{
    return this.flushOneRaw(id,this.env);
  }

  private async flushOneRaw(id: string, env:Hub2Env): Promise<boolean> {
    const row = this.db.one<OutboxRow>("SELECT * FROM machine_outbox WHERE id=? AND (state='pending' OR (kind='wake' AND state='wake_pending'))", id);
    if (!row) return false;
    const payload = this.parse(row.payload_json) as JsonRecord;
    if(row.kind==='finish'&&!payload.intake_id)payload.intake_id=this.db.one<{intake_id:string}>('SELECT intake_id FROM sessions WHERE id=? AND operation_id=?',payload.session_id,payload.attempt_operation_id)?.intake_id;
    // Every finish outbox row was created only after verified public finish evidence.
    if (row.kind === 'finish') payload.native_exit_confirmed = true;
    try {
      if(row.kind==='expiry'){
        const session=this.db.one<SessionRow>("SELECT * FROM sessions WHERE id=? AND operation_id=? AND epoch=? AND state='offered' AND offer_expires_at<=?",
          payload.session_id,payload.operation_id,payload.epoch,Date.now());
        if(session){
          const now=Date.now();
          this.db.transaction(this.ctx, () => {
            this.db.run("UPDATE sessions SET state='expired', offer_expires_at=NULL, updated_at=? WHERE id=? AND state='offered'", now, session.id);
            this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",
              session.operation_id, session.id, session.epoch);
            this.insertOutbox(`requeue:${session.operation_id}`, 'requeue', session.operation_id, session.project_id,
              { operation_id: session.dispatch_id, attempt_operation_id: session.operation_id, session_id: session.id,
                machine_id: session.machine_id, epoch: session.epoch, proof_kind: 'offer-expired-unclaimed',
                offer_expires_at: session.offer_expires_at, observed_at: now }, now);
          });
        }
        this.db.run("UPDATE machine_outbox SET state='delivered',updated_at=? WHERE id=? AND state='pending'",Date.now(),row.id);
        return true;
      }
      const revocationReason = row.kind==='legacy-launch-failure' ? 'legacy_launch_unobserved'
        : payload.no_dispatch===true ? 'native_dispatch_not_started'
        : row.kind==='lost' ? (payload.reason==='native_inventory_absent'?'native_inventory_absent':'host_inventory_stale') : row.kind==='finish' ? 'native_exit_confirmed' : null;
      if(revocationReason){
        const revoked=await env.CONTROL.get(env.CONTROL.idFromName('global')).fetch('https://control.internal/_worker/revoke',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({session_id:payload.session_id,
            operation_id:payload.attempt_operation_id,epoch:payload.epoch,machine_id:payload.machine_id,
            intake_id:payload.intake_id,project_id:row.project_id,reason:revocationReason})});
        if(!revoked.ok||(await revoked.json() as JsonRecord).revoked!==true)throw new Error('child_revocation_pending');
      }
      if(row.kind==='finish'&&typeof payload.after_notification==='string'){
        const notification=this.db.one<{state:string}>('SELECT state FROM machine_outbox WHERE id=?',payload.after_notification);
        if(notification?.state!=='delivered'){
          if(notification?.state==='pending')await this.flushOneRaw(payload.after_notification,env);
          if(this.db.one<{state:string}>('SELECT state FROM machine_outbox WHERE id=?',payload.after_notification)?.state!=='delivered')
            throw new Error('launch_failure_notification_pending');
        }
      }
      // An expired attempt may already have fallen back on another Machine.
      // Its original launch metadata remains here, so retain and revoke its
      // history without requiring the successor Project to adopt that spec.
      if(row.kind==='no-dispatch-revoke'||row.kind==='legacy-launch-failure'&&payload.historical_only===true){
        this.db.run("UPDATE machine_outbox SET state='delivered',updated_at=? WHERE id=? AND state='pending'",Date.now(),row.id);
        return true;
      }
      const stub = env.PROJECTS.get(env.PROJECTS.idFromName(row.project_id));
      const path = row.kind === 'wake' ? '/_internal/session-wake' : row.kind === 'stopped-finish' ? '/_internal/stopped-finish' : row.kind === 'finish-dead-letter' ? '/_internal/finish-dead-letter' : row.kind === 'finish' ? '/_internal/finish' : row.kind === 'started' ? '/_internal/started'
        : row.kind === 'auth-fallback' ? '/_internal/auth-fallback'
        : row.kind === 'stopped' ? '/_internal/stopped' : (row.kind === 'reaped' || row.kind === 'lost' || row.kind === 'legacy-launch-failure') ? '/_internal/reaped'
        : row.kind === 'claim' ? '/_internal/claim' : '/_internal/requeue';
      const response = await stub.fetch(`https://project.internal${path}`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
      });
      if (!response.ok) throw new Error('project_forward_failed');
      this.db.run("UPDATE machine_outbox SET state='delivered', updated_at=? WHERE id=? AND (state='pending' OR (kind='wake' AND state='wake_pending'))", Date.now(), row.id);
      return true;
    } catch {
      const now = Date.now();
      const attempt = row.attempt + 1;
      if(attempt>=10){
        this.db.run("UPDATE machine_outbox SET state='dead_letter',attempt=?,updated_at=? WHERE id=? AND (state='pending' OR (kind='wake' AND state='wake_pending'))",attempt,now,row.id);
        if(typeof payload.session_id==='string')this.db.run('INSERT OR IGNORE INTO attention VALUES(?,?,?,?,?)',payload.session_id,'outbox_retries_exhausted',row.operation_id,null,now);
        console.error('[hub2] MACHINE_OUTBOX_DEAD_LETTER',JSON.stringify({id:row.id,kind:row.kind,operation_id:row.operation_id,attempt}));
        return false;
      }
      const delay = Math.min(15 * 60_000, 1_000 * 2 ** Math.min(attempt, 9));
      this.db.run("UPDATE machine_outbox SET attempt=?, next_action_at=?, updated_at=? WHERE id=? AND (state='pending' OR (kind='wake' AND state='wake_pending'))", attempt, now + delay, now, row.id);
      return false;
    }
  }

  private insertOutbox(id: string, kind: string, operationId: string, projectId: string, payload: JsonRecord, now: number) {
    this.db.run(`INSERT OR IGNORE INTO machine_outbox(id, kind, operation_id, project_id, payload_json, state, attempt, next_action_at, created_at, updated_at)
      VALUES (?, ?, ?, ?, ?, 'pending', 0, ?, ?, ?)`, id, kind, operationId, projectId, JSON.stringify(payload), now+30_000, now, now);
  }

  private async scheduleNext(fromAlarm=false): Promise<void> {
    const pending=this.alarmQueue.catch(()=>{}).then(async()=>{
    this.db.alarmOperation();
    const current = await this.ctx.storage.getAlarm();
    const nativeDue=this.db.schemaVersion>=9?this.db.one<{due:number|null}>("SELECT MIN(deadline_ms) AS due FROM native_commands WHERE state='pending'")?.due:null;
    const presenceDue=this.presenceDue(await this.hostPresence(),Date.now());
    const wakeDue=this.wakes.due();
    const next = this.db.one<{ due: number | null }>(`SELECT MIN(due) AS due FROM (
      SELECT MIN(offer_expires_at) AS due FROM sessions WHERE state='offered' AND offer_expires_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM machine_outbox b WHERE b.id='expiry:'||sessions.id||':'||sessions.epoch AND b.state='pending')
      UNION ALL SELECT MIN(created_at+?) AS due FROM sessions WHERE state IN ('staged','offered','claimed') AND native_id IS NULL
      UNION ALL SELECT MIN(MAX(updated_at,COALESCE((SELECT observed_at FROM machine_liveness WHERE singleton=1),0))+?) AS due
        FROM sessions WHERE state='running' AND native_id IS NOT NULL
      UNION ALL SELECT MIN(created_at+60000) AS due FROM session_commands WHERE kind='stop' AND state='pending'
      UNION ALL SELECT ? AS due
      UNION ALL SELECT ? AS due
      UNION ALL SELECT ? AS due
      UNION ALL SELECT MIN(next_action_at) AS due FROM machine_outbox WHERE (state='pending' OR (kind='wake' AND state='wake_pending'))
      UNION ALL SELECT ? AS due WHERE EXISTS(SELECT 1 FROM sessions WHERE state IN ('claimed','running'))
    )`,CLAIMED_WITHOUT_NATIVE_GRACE_MS,HOST_INVENTORY_LOST_MS,nativeDue??null,presenceDue,wakeDue,Date.now()+SESSION_REAP_INTERVAL_MS);
    if (next?.due !== null && next?.due !== undefined) {
      // Ordinary requests preserve pending wakes, even overdue ones. Only the
      // handler may replace the runtime's retry lease and own the next wake.
      // Rearm even an identical timestamp so the runtime retains our revision.
      if (fromAlarm || (current !== next.due && (current === null || next.due < current))) {
        this.db.alarmOperation(); await this.ctx.storage.setAlarm(next.due);
      }
    } else if (current !== null) {
      this.db.alarmOperation();
      await this.ctx.storage.deleteAlarm();
    }
    });
    this.alarmQueue=pending;
    await pending;
  }

  private async ensureDeadline(due:number):Promise<void>{
    const pending=this.alarmQueue.catch(()=>{}).then(async()=>{
    this.db.alarmOperation();
    const current=await this.ctx.storage.getAlarm();
    if(current===null||current>due){this.db.alarmOperation();await this.ctx.storage.setAlarm(due);}
    });
    this.alarmQueue=pending;
    await pending;
  }

  private retainedCaptureReady(row:SessionRow):boolean {
    const launch=this.parse(row.launch_json) as JsonRecord,proof=this.parse(row.evidence_json) as JsonRecord|null;
    const evidence=proof?.evidence as JsonRecord|undefined,identity=evidence?.native_identity as JsonRecord|undefined;
    return row.state==='completed-awaiting-review'&&launch.interactive===false&&!!row.native_id&&row.exit_code===0
      &&typeof row.evidence_digest==='string'&&/^[0-9a-f]{64}$/.test(row.evidence_digest)
      &&proof?.evidence_digest===row.evidence_digest&&proof.native_exit_confirmed===true&&proof.exit_code===0
      &&proof.session_id===row.id&&proof.epoch===row.epoch&&proof.attempt_operation_id===row.operation_id
      &&proof.operation_id===row.dispatch_id&&proof.intake_id===row.intake_id
      &&proof.machine_id===row.machine_id&&proof.native_id===row.native_id
      &&evidence?.containment_empty===true&&['aplexer_status','aplexer_retirement'].includes(String(evidence.source))
      &&identity?.id===row.native_id&&identity.tag===`task-hub2-${row.id}`&&identity.workspace===boundWorkspace(launch)
      &&nativeReturncode(evidence.native_exit as JsonRecord)===0;
  }

  private async createCommand(request: Request, sessionId: string): Promise<Response> {
    const actor = actorFrom(request);
    if (!actor || actor.kind === 'machine') return error('forbidden', 403);
    const session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
    if (!session) return error('session_not_found', 404);
    const body = await bodyObject(request, 12_288);
    const kind = body.kind;
    if(Object.hasOwn(body,'capture_binding'))return error('reserved_command_field',400);
    if (!['send', 'capture', 'stop'].includes(String(kind))) return error('invalid_command_kind', 400);
    const scope = kind === 'send' ? 'session:send' : kind === 'capture' ? 'session:capture' : 'session:stop';
    if (!hasScope(actor, 'operator:admin') && (!hasScope(actor, scope)
      || (actor.resource_id !== sessionId && actor.resource_id !== session.project_id))) return error('forbidden', 403);
    if (kind === 'send' && (this.parse(session.launch_json) as JsonRecord).interactive !== true) return error('headless_send_refused', 409);
    const captureMode = kind === 'capture' ? body.mode : undefined;
    if (captureMode !== undefined && captureMode !== 'screen') return error('invalid_capture_mode', 400);
    if (kind !== 'capture' && body.mode !== undefined) return error('unexpected_capture_mode', 400);
    // The first bridge must remain wire-compatible with the legacy schema6 reader.
    // Explicit screen capture becomes available after the compatible bridge is the rollback target.
    if (captureMode === 'screen' && this.db.schemaVersion < 7) return error('schema_migration_deferred', 503);
    const commandText = body.text;
    if (kind === 'send' && (typeof commandText !== 'string' || !commandText.trim() || new TextEncoder().encode(commandText).byteLength > 8192)) return error('invalid_command_text', 400);
    if (kind !== 'send' && commandText !== undefined) return error('unexpected_command_text', 400);
    const key = request.headers.get('Idempotency-Key') || body.key;
    if(typeof key==='string'&&key.startsWith('hub-wake:'))return error('reserved_command_key',400);
    if (typeof key !== 'string' || !/^[A-Za-z0-9_.:-]{1,120}$/.test(key)) return error('idempotency_key_required', 400);
    const digest = await sha256Hex(JSON.stringify({ kind, text: commandText ?? null, epoch: session.epoch, ...(captureMode ? {mode:captureMode} : {}) }));
    // Schema6 is the first rollout bridge: keep its legacy command wire and
    // running-only behavior intact until the compatible schema7 bridge is active.
    const retained = this.db.schemaVersion>=7&&kind==='capture'&&captureMode===undefined&&actor.kind==='operator'
      &&(this.parse(session.launch_json) as JsonRecord).interactive===false&&!!session.native_id;
    const binding=retained?{native_id:session.native_id,operation_id:session.operation_id,request_digest:digest}:null;
    const immediateStop = kind === 'stop' && this.unclaimedWithoutNative(session);
    const prior = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
    if (prior) {
      if (prior.request_digest !== digest) return error('idempotency_conflict', 409);
      if (!immediateStop || prior.state !== 'pending') return json({ command: this.commandView(prior), replayed: true });
    }
    if (kind === 'stop' ? !immediateStop && !this.canDispatchStop(session)
      : (session.state !== 'running' || !session.native_id)&&!(retained&&this.retainedCaptureReady(session))) return error('session_not_running', 409);
    const now = Date.now(), id = crypto.randomUUID();
    if (immediateStop) {
      const applied = await this.recordStopped(session, 'stop_confirmed', now, prior ? undefined : {
        id, request_key: key, request_digest: digest,
      });
      if (!applied) {
        const raced = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
        if (raced?.request_digest === digest) return json({ command: this.commandView(raced), replayed: true });
        return error('session_not_running', 409);
      }
      await this.scheduleNext();
      const command = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND request_key=?', sessionId, key);
      return command ? json({ command: this.commandView(command), ...(prior ? { replayed: true } : {}) }, prior ? 200 : 201)
        : error('command_persist_failed', 503);
    }
    try {
      this.db.transaction(this.ctx, () => {
        const current = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
        if (!current || current.epoch !== session.epoch || !(kind==='stop'
          ? this.canDispatchStop(current)
          : current.state==='running'&&!!current.native_id||retained&&this.retainedCaptureReady(current))) throw new HttpError('session_not_running', 409);
        // Preserve the pre-delivery native binding in both schema readers.
        const pendingResult = kind === 'stop' && current.native_id ? JSON.stringify({dispatch_native_id:current.native_id})
          :binding?JSON.stringify({capture_binding:binding}):null;
        if(binding&&(current.native_id!==binding.native_id||current.operation_id!==binding.operation_id))throw new HttpError('stale_command',409);
        if(this.db.schemaVersion>=7){
          this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,capture_mode,state,result_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,'pending',?,?,?)`, id, sessionId, session.machine_id, session.epoch, key, digest, kind,
            commandText ?? null,captureMode ?? null,pendingResult,now,now);
        }else{
          this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,'pending',?,?,?)`, id, sessionId, session.machine_id, session.epoch, key, digest, kind,
            commandText ?? null,pendingResult,now,now);
        }
      });
    } catch (caught) {
      if (caught instanceof HttpError) return error(caught.code, caught.status);
      return error('command_persist_failed', 503);
    }
    await this.notifyPendingCommands(session.machine_id);
    const command = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?', id);
    return command ? json({ command: this.commandView(command) }, 201) : error('command_persist_failed', 503);
  }

  private nativeControlDiagnostic(result: JsonRecord, kind: string): JsonRecord {
    if (result.native_control_stage === undefined && result.native_control_error === undefined) return {};
    const stages = kind === 'capture'
      ? ['capture_binding','capture_status','capture_retained','capture_rpc','capture_qualification']
      : kind === 'stop' ? ['stop_status','stop_kill','stop_verify','stop_qualification'] : [];
    const errors = ['timeout','native_nonzero','invalid_response','identity_mismatch','unavailable','os_error','unqualified','unknown'];
    const valid = typeof result.native_control_stage === 'string' && stages.includes(result.native_control_stage)
      && typeof result.native_control_error === 'string' && errors.includes(result.native_control_error);
    return {native_control_stage:valid ? result.native_control_stage : 'unknown',
      native_control_error:valid ? result.native_control_error : 'unknown'};
  }

  private async ackCommand(request: Request, sessionId: string, commandId: string): Promise<Response> {
    const actor = actorFrom(request), machineId = this.machineActor(actor, 'session:report');
    if (!machineId) return error('forbidden', 403);
    const safety=request.headers.get('x-hub2-safety-mode')==='operator_stop';
    if(new URL(request.url).searchParams.has('safety')&&!safety)return error('untrusted_safety_mode',403);
    let row = this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=? AND session_id=? AND machine_id=?', commandId, sessionId, machineId);
    if (!row) return error('stale_command', 409);
    const capture = row.kind === 'capture';
    const body = await bodyObject(request, capture ? CAPTURE_ACK_BODY_BYTES : 12_288);
    if (!Number.isSafeInteger(body.epoch) || !body.result || typeof body.result !== 'object' || Array.isArray(body.result)) return error('invalid_command_ack', 400);
    const resultLimit = capture && typeof (body.result as JsonRecord).output === 'string' ? CAPTURE_RESULT_JSON_BYTES : 8192;
    if(row.kind==='wake_send'){const current=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=? AND session_id=? AND machine_id=?',commandId,sessionId,machineId);if(!current)return error('stale_command',409);row=current;}
    if (new TextEncoder().encode(JSON.stringify(body.result)).byteLength > resultLimit) return error('command_ack_too_large', 413);
    if (row.epoch !== body.epoch) return error('stale_command', 409);
    if(safety&&row.kind!=='stop')return error('safety_command_refused',403);
    const session = row.kind === 'stop'
      ? this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=? AND epoch=?',sessionId,machineId,row.epoch)
      : null;
    let result={...body.result as JsonRecord};
    const wakeBinding=(this.parse(row.result_json) as JsonRecord|null)?.wake_binding as JsonRecord|undefined;
    if(wakeBinding){
      const current=this.wakeSession(sessionId);
      if(row.kind!=='wake_send'||!current||current.operation_id!==wakeBinding.operation_id||current.epoch!==row.epoch||current.native_id!==wakeBinding.native_id)return error('stale_wake',409);
      const strict=['strict-submit:v1','strict-inbox:v1'].includes(String(wakeBinding.transport));
      if(strict&&result.wake_pending===true){
        if(Object.keys(result).sort().join(',')!=='transport,wake_pending'||result.transport!==wakeBinding.transport)return error('invalid_wake_result',400);
        if(row.state!=='wake_pending')return error('stale_command',409);
        await this.wakes.sweep();await this.scheduleNext();return json({acknowledged:false,pending:true});
      }
      if(!strict&&result.wake_pending===true){
        if(Object.keys(result).sort().join(',')!=='native_message_id,wake_pending'||typeof result.native_message_id!=='string'||!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(result.native_message_id))return error('invalid_wake_result',400);
        if(row.state!=='wake_pending')return error('stale_command',409);
        if(wakeBinding.native_message_id!==undefined&&wakeBinding.native_message_id!==result.native_message_id)return error('wake_receipt_conflict',409);
        if(wakeBinding.ack_deadline_ms===undefined){
          const now=Date.now();
          if(now>=Number(wakeBinding.deadline_ms)){await this.wakes.sweep();await this.scheduleNext();return json({acknowledged:false,expired:true});}
          const upgraded={...wakeBinding,native_message_id:result.native_message_id,ack_deadline_ms:now+600000};
          this.db.run("UPDATE session_commands SET result_json=?,updated_at=? WHERE id=? AND epoch=? AND state='wake_pending' AND json_extract(result_json,'$.wake_binding.ack_deadline_ms') IS NULL",JSON.stringify({wake_binding:upgraded,wake_delivered_at_ms:now}),now,row.id,row.epoch);
        }
        await this.wakes.sweep();await this.scheduleNext();await this.notifyPendingCommands(machineId);
        return json({acknowledged:false,pending:true});
      }
      if(Object.keys(result).some(k=>!['delivery','delivered_at_ms','wake_proof'].includes(k))||result.delivery!=='native_acknowledged'||!Number.isSafeInteger(result.delivered_at_ms)||Number(result.delivered_at_ms)<=0||Number(result.delivered_at_ms)>Date.now()+5000)return error('invalid_wake_result',400);
      if(strict){
        if(!await strictWakeProof(result.wake_proof,wakeBinding,row.id,row.text??''))return error('invalid_wake_proof',409);
        if(wakeBinding.transport==='strict-inbox:v1'){
          const proof=result.wake_proof as JsonRecord|undefined;
          const reused=proof&&this.db.one<{id:string}>("SELECT id FROM session_commands WHERE id<>? AND kind='wake_send' AND json_valid(result_json) AND json_extract(result_json,'$.wake_proof.envelope_id')=? LIMIT 1",row.id,proof.envelope_id);
          if(reused)return error('wake_proof_replayed',409);
        }
        const live=this.wakeSession(sessionId);
        if(!live||live.state!=='running'||live.operation_id!==wakeBinding.operation_id||live.epoch!==row.epoch||live.native_id!==wakeBinding.native_id)return error('stale_wake',409);
        if(Number(result.delivered_at_ms)>Number(wakeBinding.deadline_ms))return error('wake_ack_after_deadline',409);
        result={...result,wake_delivered_at_ms:result.delivered_at_ms,wake_binding:wakeBinding};
      }else{
      if(!wakeProof(result.wake_proof,wakeBinding))return error('invalid_wake_proof',409);
      const reused=this.db.one<{id:string}>("SELECT id FROM session_commands WHERE id<>? AND kind='wake_send' AND json_valid(result_json) AND json_extract(result_json,'$.wake_proof.native_message_id')=? LIMIT 1",row.id,(result.wake_proof as JsonRecord).native_message_id);
      if(reused)return error('wake_proof_replayed',409);
      const firstReceipt=this.parse(row.result_json) as JsonRecord;
      if(!Number.isSafeInteger(firstReceipt.wake_delivered_at_ms)||!Number.isSafeInteger(wakeBinding.ack_deadline_ms))return error('wake_receipt_required',409);
      result={...result,delivered_at_ms:firstReceipt.wake_delivered_at_ms,wake_delivered_at_ms:firstReceipt.wake_delivered_at_ms,wake_binding:wakeBinding};
      }
    }

    const diagnostic=this.nativeControlDiagnostic(result,row.kind);
    delete result.native_control_stage; delete result.native_control_error;
    const captureFailed=row.kind==='capture'&&result.error!==undefined;
    if(captureFailed&&(typeof result.error!=='string'||!/^[a-z_]{1,80}$/.test(result.error)
      ||result.output!==undefined))return error('invalid_command_ack',400);
    if(capture&&!captureFailed&&(typeof result.output!=='string'||new TextEncoder().encode(result.output).length>CAPTURE_OUTPUT_BYTES))return error('invalid_capture_output',400);
    const captureBinding=row.kind==='capture'?(this.parse(row.result_json) as JsonRecord|null)?.capture_binding as JsonRecord|undefined:undefined;
    if(captureBinding){
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=? AND epoch=?',sessionId,machineId,row.epoch);
      if(!current||current.native_id!==captureBinding.native_id||current.operation_id!==captureBinding.operation_id
        ||captureBinding.request_digest!==row.request_digest)return error('stale_command',409);
      const echo=result.capture_binding as JsonRecord|undefined;
      const echoed=!!echo&&typeof echo==='object'&&!Array.isArray(echo)&&Object.keys(echo).length===3
        &&['native_id','operation_id','request_digest'].every(key=>echo[key]===captureBinding[key]);
      if(result.capture_binding!==undefined&&!echoed)return error('capture_fence_conflict',409);
      if(result.error!==undefined){
        if(typeof result.error!=='string'||!/^[a-z_]{1,80}$/.test(result.error))return error('invalid_command_ack',400);
        result={error:result.error,reason:result.error,capture_binding:captureBinding,capture_source:result.capture_source??'legacy_running'};
      }else{
        if(typeof result.output!=='string'||new TextEncoder().encode(result.output).length>CAPTURE_OUTPUT_BYTES)return error('invalid_capture_output',400);
        result={output:result.output,capture_binding:captureBinding,capture_source:result.capture_source??'legacy_running'};
      }
    }
    if (session) {
      const priorResult=this.parse(row.result_json) as JsonRecord|null;
      const failedPrelaunch=row.state==='pending'&&session.state==='failed'&&session.native_id===null;
      const legacyBoundStop = session.state!=='lost' && result.stopped === true && result.evidence === undefined && !!session.native_id
        && (row.state === 'pending' && priorResult?.dispatch_native_id === session.native_id
          || row.state === 'acked' && priorResult?.evidence === 'legacy_bound_exit' && priorResult?.native_id === session.native_id);
      if (legacyBoundStop) {
        result = {...result,evidence:'legacy_bound_exit',native_id:session.native_id};
      } else if (result.evidence === 'legacy_bound_exit' || !this.qualifiedStopResult(session,result)
        ||failedPrelaunch&&result.evidence!=='dispatch_fenced') {
        result = {stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed',stage:'host_stop'};
      }
    }
    if (row.kind==='capture'&&result.error!==undefined || row.kind==='stop'&&result.stopped===false) {
      result={...result,...diagnostic};
    }
    const resultJson = JSON.stringify(result);
    if(new TextEncoder().encode(resultJson).length>resultLimit)return error('command_ack_too_large',413);
    const unconfirmed=row.kind==='stop'&&result.stopped===false&&result.error==='stop_unconfirmed';
    if (row.state === 'acked' || (row.state === 'failed' && (unconfirmed||captureBinding||captureFailed) && row.result_json === resultJson)) {
      if(row.result_json!==resultJson)return error('command_ack_conflict',409);
      if(wakeBinding){await this.wakes.reconcile(sessionId,String(wakeBinding.id));await this.scheduleNext();}
      return json({acknowledged:true,duplicate:true});
    }
    if (row.state !== 'pending'&&!(wakeBinding&&['wake_pending','wake_failed'].includes(row.state))) return error('command_not_pending', 409);
    if(captureBinding){
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=? AND epoch=?',sessionId,machineId,row.epoch);
      const echo=(body.result as JsonRecord).capture_binding as JsonRecord|undefined;
      if(!current||current.state!=='running'&&(!this.retainedCaptureReady(current)||!echo
        ||!captureFailed&&(body.result as JsonRecord).capture_source!=='retained_task_output'))return error('unqualified_retained_capture',409);
    }
    const now=Date.now();
    if (row.kind === 'stop' && result.stopped === true) {
      const nativeId = result.evidence === 'native_exit' ? result.native_id as string : undefined;
      if (!session || !await this.recordStopped(session,'stop_confirmed',now,{id:commandId,result_json:resultJson,native_id:nativeId})) {
        return error('stale_command',409);
      }
    } else {
      this.db.run("UPDATE session_commands SET state=?,result_json=?,updated_at=? WHERE id=? AND (state='pending' OR (?=1 AND state IN ('wake_pending','wake_failed'))) AND epoch=?",
        unconfirmed||captureFailed?'failed':'acked', resultJson, now, commandId,wakeBinding?1:0, row.epoch);
    }
    if(row.kind==='stop'&&result.stopped===true)await this.scheduleNext();
    if(wakeBinding){await this.wakes.reconcile(sessionId,String(wakeBinding.id));await this.wakes.sweep();await this.scheduleNext();}
    await this.notifyPendingCommands(machineId);
    return json({ acknowledged: true });
  }

  private async sendPendingCommands(socket:WebSocket,machineId:string,env:Hub2Env=this.env):Promise<void>{
    const stop=this.stopOnly(socket);
    const wakeCap=this.db.one<{capabilities_json:string}>('SELECT capabilities_json FROM machine_capabilities WHERE machine_id=?',machineId);
    const wakeCaps=wakeCap?JSON.parse(wakeCap.capabilities_json) as string[]:[];
    const inboxWakes=wakeCaps.includes('session-wakes:strict-inbox:v1');
    const strictWakes=inboxWakes||wakeCaps.includes('session-wakes:strict-submit:v1');
    const supportsWakes=inboxWakes||strictWakes||wakeCaps.includes('session-wakes:v1');
    const command=this.db.one<CommandRow>(`
      SELECT c.* FROM session_commands c
      JOIN sessions s ON s.id=c.session_id AND s.machine_id=c.machine_id AND c.epoch=s.epoch
      WHERE c.machine_id=? AND (c.state='pending' OR (c.kind='wake_send' AND c.state='wake_pending')) AND (CASE WHEN json_valid(c.result_json) THEN json_extract(c.result_json,'$.wake_binding.transport') END IS NULL OR (?=1 AND json_extract(c.result_json,'$.wake_binding.transport')='strict-submit:v1') OR (?=1 AND json_extract(c.result_json,'$.wake_binding.transport')='strict-inbox:v1')) AND (CASE WHEN json_valid(c.result_json) THEN json_type(c.result_json,'$.wake_binding') END IS NULL OR (?=1 AND COALESCE(json_extract(c.result_json,'$.wake_binding.ack_deadline_ms'),json_extract(c.result_json,'$.wake_binding.deadline_ms'))>?)) AND (
        s.state IN ('claimed','running') OR (c.kind='capture' AND s.state='completed-awaiting-review' AND s.exit_code=0
          AND CASE WHEN json_valid(c.result_json) THEN json_extract(c.result_json,'$.capture_binding.native_id') END=s.native_id
          AND CASE WHEN json_valid(c.result_json) THEN json_extract(c.result_json,'$.capture_binding.operation_id') END=s.operation_id
        ) OR (c.kind='stop' AND (
          (s.state='lost' AND s.native_id IS NOT NULL) OR
          (s.state='failed' AND s.native_id IS NULL) OR
          (s.state='expired' AND EXISTS(SELECT 1 FROM launch_admissions a
            WHERE a.operation_id=s.operation_id AND a.session_id=s.id AND a.epoch=s.epoch))
        ))
      ) ${stop?"AND c.kind='stop'":''}
      ORDER BY CASE WHEN c.kind='stop' THEN 0 WHEN c.kind='wake_send' THEN 2 ELSE 1 END,c.created_at,c.id LIMIT 1`,machineId,strictWakes?1:0,inboxWakes?1:0,supportsWakes?1:0,Date.now());
    if(!stop){
      const native=this.nativeCommands.next();
      if(native&&await this.authorizedAttachment(socket,'machine:offer',true,env)){
        try{socket.send(JSON.stringify({type:'session.command',command:native}));}catch{}
      }
    }
    if(!command)return;
    if(!await this.authorizedAttachment(socket,'machine:offer',true,env))return;
    try{
      const view=this.commandView(command);
      const session=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',command.session_id,machineId);
      if(command.kind==='capture'&&session&&session.state!=='running'&&session.state!=='claimed'&&!this.retainedCaptureReady(session))return;
      if (session && !session.native_id && (session.state === 'claimed' || session.state === 'expired' || session.state === 'failed')) {
        const launch=this.parse(session.launch_json) as JsonRecord;
        view.pre_native=true;view.workspace=boundWorkspace(launch);view.tag=`task-hub2-${session.id}`;
      }
      socket.send(JSON.stringify({type:'session.command',command:view}));
    }catch{}
  }

  private async notifyPendingCommands(machineId: string): Promise<void> {
    for (const socket of this.machineSockets(`${machineId}`)) {
      try {
        await this.sendPendingCommands(socket, machineId);
      } catch {}
    }
  }

  private async queueMail(request: Request): Promise<Response> {
    const body = await bodyObject(request);
    if (!['message_id','operation_id','recipient','session_id','body'].every(key => boundedString(body[key], key === 'body' ? 12000 : 180))
      || !Number.isSafeInteger(body.epoch) || Number(body.epoch) < 1
      || (body.idempotency_key !== undefined && body.idempotency_key !== body.operation_id)) return error('invalid_native_mail', 400);
    const session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', body.session_id);
    if (!session || session.state !== 'running' || !session.native_id) return error('native_mail_target_unavailable', 409);
    const digest = await sha256Hex(JSON.stringify([body.message_id,body.epoch,body.operation_id,body.session_id,body.recipient,body.body]));
    const row = this.db.transaction(this.ctx, () => {
      if (this.db.one('SELECT operation_id FROM native_mail_fences WHERE message_id=? AND epoch=?', body.message_id, body.epoch)) throw new HttpError('native_mail_fenced', 409);
      const prior = this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?', body.message_id, body.epoch);
      if (prior) {
        if (prior.digest !== digest) throw new HttpError('native_mail_conflict', 409);
        return prior;
      }
      this.db.run("INSERT INTO native_mail(message_id,epoch,operation_id,session_id,machine_id,recipient,body,digest,state,created_at) VALUES(?,?,?,?,?,?,?,?,'pending',?)",
        body.message_id,body.epoch,body.operation_id,session.id,session.machine_id,body.recipient,body.body,digest,Date.now());
      return this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?', body.message_id, body.epoch)!;
    });
    await this.notifyPendingMail(session.machine_id);
    return json({queued:true,state:row.state,message_id:row.message_id,epoch:row.epoch,operation_id:row.operation_id});
  }

  private async settleMail(request: Request, cancel: boolean): Promise<Response> {
    const body = await bodyObject(request, 8192);
    if (!boundedString(body.message_id,180) || !boundedString(body.operation_id,180)
      || !Number.isSafeInteger(body.epoch) || Number(body.epoch)<1) return error('invalid_native_mail_ack',400);
    const proof = body.native_receipt as JsonRecord | undefined;
    if (!cancel && (!proof || proof.provider!=='aplexer' || !boundedString(proof.message_id,180)
      || !boundedString(proof.session_id,180) || !boundedString(proof.recipient,180))) return error('native_mail_receipt_required',400);
    const digest = cancel ? null : await sha256Hex(JSON.stringify([proof!.provider,proof!.message_id,proof!.session_id,proof!.recipient]));
    let machine: string | undefined;
    this.db.transaction(this.ctx, () => {
      const row = this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?',body.message_id,body.epoch);
      if (row && row.operation_id !== body.operation_id) throw new HttpError('native_mail_operation_conflict',409);
      if (cancel) {
        if (row?.state==='acked') throw new HttpError('cannot_cancel_delivered',409);
        const fence = this.db.one<{operation_id:string}>('SELECT operation_id FROM native_mail_fences WHERE message_id=? AND epoch=?',body.message_id,body.epoch);
        if (fence && fence.operation_id!==body.operation_id) throw new HttpError('native_mail_operation_conflict',409);
        if (!fence) this.db.run('INSERT INTO native_mail_fences VALUES(?,?,?)',body.message_id,body.epoch,body.operation_id);
        if (row && row.state!=='cancelled') this.db.run("UPDATE native_mail SET state='cancelled' WHERE message_id=? AND epoch=?",body.message_id,body.epoch);
      } else {
        if (!row) throw new HttpError('native_mail_not_found',404);
        if (row.state==='cancelled' || proof!.session_id!==row.session_id || proof!.recipient!==row.recipient) throw new HttpError('native_mail_fenced',409);
        if (row.receipt_digest && row.receipt_digest!==digest) throw new HttpError('native_mail_receipt_conflict',409);
        if (row.state!=='acked') this.db.run("UPDATE native_mail SET state='acked',receipt_digest=? WHERE message_id=? AND epoch=?",digest,body.message_id,body.epoch);
      }
      machine=row?.machine_id;
    });
    if (machine) await this.notifyPendingMail(machine);
    return json({acknowledged:true,acked:!cancel,cancelled:cancel,state:cancel?'cancelled':'acked',
      message_id:body.message_id,epoch:body.epoch,operation_id:body.operation_id});
  }

  private async sendPendingMail(socket: WebSocket, machine: string,env:Hub2Env=this.env): Promise<void> {
    if(this.stopOnly(socket))return;
    if (!await this.authorizedAttachment(socket,'message:delivery',false,env)) return;
    const row = this.db.one<NativeMail>("SELECT m.* FROM native_mail m JOIN sessions s ON s.id=m.session_id AND s.machine_id=m.machine_id WHERE m.machine_id=? AND s.state='running' AND s.native_id IS NOT NULL AND m.state='pending' ORDER BY m.created_at,m.message_id,m.epoch LIMIT 1",machine);
    if (row) socket.send(JSON.stringify({type:'mailbox.deliver',message:{id:row.message_id,epoch:row.epoch,
      recipient:row.recipient,session_id:row.session_id,operation_id:row.operation_id,body:row.body}}));
  }

  private async notifyPendingMail(machine: string): Promise<void> {
    for (const socket of this.machineSockets(`${machine}`)) {
      try { await this.sendPendingMail(socket,machine); } catch {}
    }
  }

  private async quarantineUncertainMail(socket:WebSocket,body:JsonRecord,session:SessionRow,env:Hub2Env):Promise<void>{
    // MESSAGE route epoch/operation, not the session's launch epoch/operation.
    // Legacy diagnostics cannot mutate durable delivery state.
    if(body.stage!=='host_mailbox'||body.reason!=='native_mail_outcome_uncertain'||this.stopOnly(socket)
      ||!boundedString(body.message_id,180)||!boundedString(body.operation_id,180)
      ||!Number.isSafeInteger(body.message_epoch)||Number(body.message_epoch)<1
      ||!boundedString(body.native_id,180)||session.state!=='running'||session.native_id!==body.native_id)return;
    const attachment=await this.authorizedAttachment(socket,'message:delivery',false,env);
    if(!attachment||attachment.machine_id!==session.machine_id||this.stopOnly(socket))return;
    const changed=this.db.transaction(this.ctx,()=>{
      // Auth awaited: re-read the exact native/session incarnation before commit.
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',session.id,session.machine_id);
      if(!current||current.state!=='running'||current.native_id!==session.native_id
        ||current.epoch!==session.epoch||current.operation_id!==session.operation_id)return false;
      const mail=this.db.one<NativeMail>('SELECT * FROM native_mail WHERE message_id=? AND epoch=?',body.message_id,body.message_epoch);
      if(!mail||mail.state!=='pending'||mail.receipt_digest!==null
        ||this.db.one('SELECT operation_id FROM native_mail_fences WHERE message_id=? AND epoch=?',mail.message_id,mail.epoch)
        ||mail.operation_id!==body.operation_id
        ||mail.session_id!==current.id||mail.machine_id!==current.machine_id)return false;
      this.db.run("UPDATE native_mail SET state='outcome_uncertain' WHERE message_id=? AND epoch=? AND operation_id=? AND session_id=? AND machine_id=? AND state='pending' AND receipt_digest IS NULL",
        mail.message_id,mail.epoch,mail.operation_id,mail.session_id,mail.machine_id);
      return true;
    });
    // Commit quarantine before selecting another pending message. No ACK,
    // receipt, cancellation, deletion or native-effect retry is implied.
    if(changed)await this.notifyPendingMail(session.machine_id);
  }

  private async recordAttention(socket: WebSocket, body: JsonRecord,env:Hub2Env): Promise<void> {
    if (!boundedString(body.session_id,120) || typeof body.reason!=='string' || !/^[a-z_]{1,80}$/.test(body.reason)) return;
    const attachment = await this.authorizedAttachment(socket,'machine:offer',true,env);
    if (!attachment) return;
    const row = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',body.session_id,attachment.machine_id);
    if (!row) return;
    const operation=boundedString(body.operation_id,180)?body.operation_id:null,message=boundedString(body.message_id,180)?body.message_id:null;
    // Only the fully bound new ordinary-send diagnostic is terminal authority.
    // Older hosts' loose host.error remains diagnostic-only; old servers also
    // safely ignore these extra fields instead of treating a NACK as delivery.
    if(body.stage==='host_command'&&body.reason==='command_outcome_uncertain'
      &&body.command_kind==='send'&&body.command_state==='done'&&message!==null
      &&Number.isSafeInteger(body.epoch)&&body.epoch===row.epoch
      &&operation===row.operation_id&&typeof body.native_id==='string'&&body.native_id===row.native_id){
      const changed=this.db.transaction(this.ctx,()=>{
        const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=? AND machine_id=?',row.id,attachment.machine_id);
        if(!current||current.state!=='running'||current.epoch!==body.epoch
          ||current.operation_id!==operation||current.native_id!==body.native_id)return false;
        const command=this.db.one<CommandRow>("SELECT * FROM session_commands WHERE id=? AND session_id=? AND machine_id=? AND epoch=? AND kind='send' AND state='pending'",message,row.id,attachment.machine_id,body.epoch);
        if(!command)return false;
        this.db.run("UPDATE session_commands SET state='failed',result_json=?,updated_at=? WHERE id=? AND state='pending'",
          JSON.stringify({error:'command_outcome_uncertain',reason:'command_outcome_uncertain'}),Date.now(),command.id);
        return true;
      });
      if(changed)await this.notifyPendingCommands(attachment.machine_id);
      return; // Duplicate/stopped commands keep their authoritative settlement.
    }

    body = {...body, reason:closedHostReason(body.reason)};
    const hostLaunchEvent=(body.stage==='host_launch'||body.stage==null)&&message===null;
    const hostLaunchDiagnostic=body.stage==='host_launch'&&message===null;
    // A launch diagnostic is useful only for the exact currently-owned
    // attempt. Operation IDs fence legacy hosts that omit epoch; if epoch is
    // supplied, it must also match. Command and mailbox errors keep their
    // existing looser diagnostic-only behavior below.
    if(hostLaunchEvent&&(operation!==row.operation_id
      ||body.epoch!==undefined&&(!Number.isSafeInteger(body.epoch)||body.epoch!==row.epoch)))return;
    if(hostLaunchDiagnostic&&row.evidence_digest===null){
      const previous=this.parse(row.evidence_json);
      if(previous===null||isJsonRecord(previous)){
        const evidence=isJsonRecord(previous)?{...previous}:{};
        const detail=body.detail === undefined ? undefined : hostDetail(body.reason,body.detail);
        const diagnostic={operation_id:row.operation_id,attempt:row.attempt,epoch:row.epoch,reason:body.reason,stage:'host_launch',
          ...(detail?{detail}:{})};
        if(JSON.stringify(evidence[HOST_START_DIAGNOSTIC_KEY])!==JSON.stringify(diagnostic)){
          evidence[HOST_START_DIAGNOSTIC_KEY]=diagnostic;
          this.db.run('UPDATE sessions SET evidence_json=? WHERE id=? AND operation_id=? AND attempt=? AND epoch=? AND evidence_digest IS NULL',
            JSON.stringify(evidence),row.id,row.operation_id,row.attempt,row.epoch);
        }
      }
    }
    const prior=this.db.one<{reason:string;operation_id:string|null;message_id:string|null}>('SELECT reason,operation_id,message_id FROM attention WHERE session_id=?',row.id);
    if (!prior || prior.reason!==body.reason || prior.operation_id!==operation || prior.message_id!==message) {
      this.db.run('INSERT INTO attention VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=excluded.message_id,observed_at=excluded.observed_at',
        row.id,body.reason,operation,message,Date.now());
      console.error('[hub2] MACHINE_SESSION_ATTENTION',JSON.stringify({session_id:row.id,machine_id:row.machine_id,reason:body.reason,operation_id:operation}));
    }
    await this.quarantineUncertainMail(socket,body,row,env);
    // Unqualified command/mailbox diagnostics never act as launch failures.
    // Older daemons omit stage but include the command or
    // message ID; their launch refusals carry neither.
    if((body.stage!=null&&body.stage!=='host_launch')||body.message_id!=null)return;
    if(AUTH_FALLBACK_REASONS.has(closedHostReason(body.reason))&&['offered','claimed'].includes(row.state)&&!row.native_id&&operation===row.operation_id){
      const now=Date.now(),payload={operation_id:row.dispatch_id,attempt_operation_id:row.operation_id,session_id:row.id,
        machine_id:row.machine_id,epoch:row.epoch,reason:body.reason};
      this.db.transaction(this.ctx,()=>{
        this.db.run("UPDATE sessions SET state='expired',updated_at=? WHERE id=? AND operation_id=? AND epoch=? AND state IN ('offered','claimed')",now,row.id,row.operation_id,row.epoch);
        this.db.run("UPDATE machine_session_operations SET state='expired' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",row.operation_id,row.id,row.epoch);
        this.insertOutbox(`auth-fallback:${row.operation_id}:${row.epoch}`,'auth-fallback',row.operation_id,row.project_id,payload,now);
      });
      await this.ensureDeadline(now+1000);
      await this.flushOne(`auth-fallback:${row.operation_id}:${row.epoch}`);
      await this.scheduleNext();
    } else if(['offered','claimed'].includes(row.state)&&!row.native_id&&operation===row.operation_id){
      await this.recordLaunchRefusal(row,closedHostReason(body.reason));
    }
  }

  private async recordLaunchRefusal(session:SessionRow,failureReason:string,noDispatch=false,detail?:string):Promise<void>{
    failureReason = closedLaunchReason(failureReason);
    detail = detail === undefined ? undefined : hostDetail(failureReason,detail);
    const now=Date.now(),reason='host_launch_refused',payload={operation_id:session.dispatch_id,
      attempt_operation_id:session.operation_id,session_id:session.id,intake_id:session.intake_id,
      machine_id:session.machine_id,epoch:session.epoch,native_id:null,reason,failure_reason:failureReason,
      ...(detail?{detail}:{}),
      launch_failed:true,...(noDispatch?{no_dispatch:true}:{}),observed_at:now};
    const key=`launch-refused:${session.operation_id}:${session.epoch}`;
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||!['offered','claimed'].includes(current.state)||current.native_id!==null||current.operation_id!==session.operation_id||current.epoch!==session.epoch)return false;
      this.db.run("UPDATE sessions SET state='failed',updated_at=? WHERE id=? AND operation_id=? AND epoch=? AND state IN ('offered','claimed')",
        now,session.id,session.operation_id,session.epoch);
      this.db.run("UPDATE machine_session_operations SET state='failed' WHERE operation_id=? AND session_id=? AND epoch=? AND state='current'",
        session.operation_id,session.id,session.epoch);
      this.db.run('INSERT INTO attention(session_id,reason,operation_id,message_id,observed_at) VALUES(?,?,?,NULL,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=NULL,observed_at=excluded.observed_at',
        session.id,failureReason,session.operation_id,now);
      if (detail && current.evidence_digest === null) {
        const previous = this.parse(current.evidence_json);
        if (previous === null || isJsonRecord(previous)) {
          const evidence = isJsonRecord(previous) ? { ...previous } : {};
          evidence[HOST_START_DIAGNOSTIC_KEY] = { operation_id: session.operation_id, attempt: current.attempt, epoch: session.epoch, reason: failureReason, stage: 'host_launch', detail };
          this.db.run('UPDATE sessions SET evidence_json=? WHERE id=? AND operation_id=? AND attempt=? AND epoch=? AND evidence_digest IS NULL',
            JSON.stringify(evidence), session.id, session.operation_id, current.attempt, session.epoch);
        }
      }
      this.settlePendingCommands(current,now,reason,noDispatch?{evidence:'dispatch_fenced'}:null);
      this.insertOutbox(key,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(!applied)return;
    await this.flushOne(key);
    await this.scheduleNext();
    console.error('[hub2] SESSION_LAUNCH_REFUSED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,
      intake_id:session.intake_id,epoch:session.epoch,reason:failureReason}));
    // An untyped host launch failure is not a workspace/request refusal: do not
    // keep re-picking that engine on this machine while it keeps failing.
    const refused=this.parse(session.launch_json) as JsonRecord|null;
    if((failureReason==='host_operation_failed'||ENGINE_PREFLIGHT_FAILURES.has(failureReason))&&typeof refused?.engine==='string'&&refused.engine!=='shell')
      await this.markEngineUnhealthy(refused.engine,failureReason,session.id,now);
  }

  private machineView(): Response {
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const sessions=machine?this.db.all("SELECT id,state,project_id,intake_id,native_id,updated_at FROM sessions WHERE machine_id=? AND state IN ('staged','offered','claimed','running') LIMIT 101",machine.machine_id):[];
    return json({machine:machine?{id:machine.machine_id,alias:machine.alias,os:machine.os,arch:machine.arch,
      labels:this.parse(machine.labels_json),last_seen:machine.last_seen,connected:this.machineSockets(`${machine.machine_id}`).length>0}:null,
      sessions,live_active_count:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,
      attention:this.db.all<JsonRecord>('SELECT * FROM attention ORDER BY observed_at DESC,session_id LIMIT 20').map(row=>({...row,reason:closedHostReason(row.reason)})),
      pending_notifications:this.db.all("SELECT id,kind,attempt,next_action_at FROM machine_outbox WHERE (state='pending' OR (kind='wake' AND state='wake_pending')) ORDER BY next_action_at,id LIMIT 21")});
  }

  private liveSessions():LiveSession[]{
    return this.db.all<Omit<LiveSession,'containment_empty'>&{containment_empty:number}>(
      `SELECT n.session_id,n.native_id,n.phase,s.state AS managed_state,
        n.last_activity_at,n.observed_at,n.containment_empty FROM native_sessions n
        LEFT JOIN sessions s ON s.id=n.session_id ORDER BY n.session_id LIMIT 100`)
      .map(row=>({...row,containment_empty:row.containment_empty===1}));
  }

  private liveSessionView():Response{
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const snapshot=this.db.one<{observed_at:number}>('SELECT observed_at FROM machine_liveness WHERE singleton=1');
    return json({machine_id:machine?.machine_id??null,observed_at:snapshot?.observed_at??null,sessions:this.liveSessions()});
  }

  private async reap(request:Request):Promise<Response>{
    const body=await bodyObject(request,4096);
    if(body.dry_run!==undefined&&typeof body.dry_run!=='boolean')return error('invalid_reap_request',400);
    return json(await this.reapInactive(body.dry_run===true));
  }

  private async reapInactive(dryRun:boolean):Promise<JsonRecord>{
    const now=Date.now(),machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const inventory=this.db.one<{observed_at:number}>('SELECT observed_at FROM machine_liveness WHERE singleton=1');
    const sessions=this.liveSessions(),active=sessions.filter(row=>isLiveActiveSession(row,now)).length;
    const rows=this.db.all<SessionRow>("SELECT * FROM sessions WHERE state IN ('claimed','running') ORDER BY updated_at,id LIMIT 200");
    const confirmedStops=rows.filter(session=>this.stopConfirmed(session));
    const timedOutClaims=rows.filter(session=>session.state==='claimed'&&!session.native_id
      &&now-session.updated_at>=CLAIMED_WITHOUT_NATIVE_GRACE_MS&&!this.stopConfirmed(session));
    if(!machine||!inventory||inventory.observed_at>now+5_000||now-inventory.observed_at>LIVE_SNAPSHOT_TTL_MS){
      const lostSessions=rows.filter(session=>session.state==='running'&&session.native_id&&!this.stopConfirmed(session)
        &&now-Math.max(session.updated_at,inventory?.observed_at??0)>=HOST_INVENTORY_LOST_MS);
      if(dryRun)return {machine_id:machine?.machine_id??null,dry_run:true,active_count:active,reaped:0,stop_requested:0,
        skipped:'host_liveness_stale',candidates:[...confirmedStops.map(session=>({session_id:session.id,native_id:session.native_id,reason:'stop_confirmed'})),
          ...timedOutClaims.map(session=>({session_id:session.id,native_id:null,reason:'launch_timeout_no_native'})),
          ...lostSessions.map(session=>({session_id:session.id,native_id:session.native_id,reason:'host_inventory_stale'}))]};
      let reaped=0;
      for(const session of confirmedStops)if(await this.recordStopped(session,'stop_confirmed',now))reaped++;
      for(const session of rows){
        if(session.state!=='claimed'||session.native_id||this.stopConfirmed(session)
          ||now-session.updated_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)continue;
        if(await this.recordLaunchTimeout(session,now))reaped++;
      }
      let lost=0;
      for(const session of lostSessions)if(await this.recordLost(session,now))lost++;
      return {machine_id:machine?.machine_id??null,dry_run:false,active_count:active,active_count_after:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,
        reaped,lost,stop_requested:0,skipped:'host_liveness_stale'};
    }
    const bySession=new Map(sessions.map(row=>[row.session_id,row]));
    const candidates:Array<{session:SessionRow;reason:'dead'|'idle';live:LiveSession|null}> = [];
    for(const session of rows){
      if(this.stopConfirmed(session))continue;
      const live=bySession.get(session.id)||null;
      if(!session.native_id){
        const starting=live&&['launching','starting'].includes(live.phase);
        if(starting&&now-session.updated_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)continue;
        if(now-session.updated_at>=CLAIMED_WITHOUT_NATIVE_GRACE_MS)candidates.push({session,reason:'dead',live});
        continue;
      }
      if(session.native_id&&!live)continue;  // The host snapshot is bounded.
      if(live){
        const reason=inactiveReason({...live,last_activity_at:live.last_activity_at??session.created_at},now);
        if(reason&&!(reason==='idle'&&this.wakes.holdsIdle(session.id,now)))candidates.push({session,reason,live});
      }
    }
    if(dryRun)return {machine_id:machine.machine_id,dry_run:true,active_count:active,reaped:0,stop_requested:0,
      candidates:[...confirmedStops.map(session=>({session_id:session.id,native_id:session.native_id,reason:'stop_confirmed'})),
        ...candidates.map(({session,reason,live})=>({session_id:session.id,native_id:session.native_id,reason,
          last_activity_at:live?.last_activity_at??null,observed_at:live?.observed_at??inventory.observed_at}))]};
    let reaped=0,stopRequested=0;const actions:JsonRecord[]=[];
    for(const session of confirmedStops){
      if(await this.recordStopped(session,'stop_confirmed',now)){reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason:'stop_confirmed'});}
    }
    for(const candidate of candidates){
      const {session,reason,live}=candidate;
      if(session.state==='claimed'&&!session.native_id&&!live?.native_id){
        if(await this.recordLaunchTimeout(session,live?.observed_at??inventory.observed_at)){
          reaped++;actions.push({session_id:session.id,action:'marked_failed',reason:'launch_timeout_no_native'});
        }
        continue;
      }
      if(!live)continue;
      if(live.phase==='exited'&&live.containment_empty===true&&!!session.native_id&&live.native_id===session.native_id){
        if(await this.recordReaped(session,reason,live.observed_at)){reaped++;actions.push({session_id:session.id,action:'marked_stopped',reason});}
        continue;
      }
      if(session.native_id||live?.native_id||!!(live&&['launching','starting'].includes(live.phase))){
        const pending=this.db.one<{id:string}>("SELECT id FROM session_commands WHERE session_id=? AND machine_id=? AND epoch=? AND kind='stop' AND state='pending' ORDER BY created_at DESC LIMIT 1",
          session.id,session.machine_id,session.epoch);
        if(pending)continue;
        const actor={id:'hub2-session-reaper',kind:'operator',scopes:['operator:admin']};
        const command=await this.createCommand(new Request(`https://machine.internal/api/sessions/${session.id}/commands`,{
          method:'POST',headers:{'Content-Type':'application/json','x-hub2-actor':JSON.stringify(actor)},
          body:JSON.stringify({kind:'stop',key:`reap:${session.id}:${session.epoch}:${Math.floor(now/SESSION_REAP_INTERVAL_MS)}`}),
        }),session.id);
        if(command.status===201){
          stopRequested++;actions.push({session_id:session.id,action:'stop_requested',reason});
          console.info('[hub2] SESSION_REAP_STOP_REQUESTED',JSON.stringify({session_id:session.id,machine_id:machine.machine_id,reason}));
        }else if(!command.ok){
          console.error('[hub2] SESSION_REAP_STOP_FAILED',JSON.stringify({session_id:session.id,machine_id:machine.machine_id,reason,status:command.status}));
        }
        continue;
      }
    }
    return {machine_id:machine.machine_id,dry_run:false,active_count:active,
      active_count_after:this.liveSessions().filter(row=>isLiveActiveSession(row)).length,reaped,stop_requested:stopRequested,actions};
  }

  private qualifiedStopResult(session: SessionRow, result: JsonRecord): boolean {
    if (result.stopped !== true) return false;
    if (result.evidence === 'dispatch_fenced') return !session.native_id && result.native_id === undefined;
    if (result.evidence === 'native_exit') return typeof result.native_id === 'string'
      && /^[A-Za-z0-9-]{1,120}$/.test(result.native_id) && (!session.native_id || session.native_id === result.native_id);
    return result.evidence === 'legacy_bound_exit' && !!session.native_id && result.native_id === session.native_id;
  }

  private stopConfirmed(session:SessionRow):boolean{
    const row=this.db.one<{result_json:string|null}>("SELECT result_json FROM session_commands WHERE session_id=? AND machine_id=? AND epoch=? AND kind='stop' AND state='acked' ORDER BY created_at DESC LIMIT 1",
      session.id,session.machine_id,session.epoch);
    try{return !!row?.result_json&&this.qualifiedStopResult(session,JSON.parse(row.result_json) as JsonRecord);}catch{return false;}
  }

  private hasLaunchAdmission(session: SessionRow): boolean {
    return !!this.db.one('SELECT operation_id FROM launch_admissions WHERE operation_id=? AND session_id=? AND epoch=?',
      session.operation_id,session.id,session.epoch);
  }

  private unclaimedWithoutNative(session: SessionRow): boolean {
    return session.native_id === null && ['offered', 'expired'].includes(session.state) && !this.hasLaunchAdmission(session);
  }

  private canDispatchStop(session: SessionRow): boolean {
    return ['claimed', 'running'].includes(session.state) || (session.state === 'expired' && this.hasLaunchAdmission(session))
      || (session.state==='lost' && !!session.native_id)
      || (session.state==='failed' && session.native_id===null);
  }

  private async recordStopped(session:SessionRow,reason:string,observedAt:number,
    stopCommand?:{id:string;request_key:string;request_digest:string}|{id:string;result_json:string;native_id?:string}):Promise<boolean>{
    const nativeId = session.native_id ?? (stopCommand && 'result_json' in stopCommand ? stopCommand.native_id ?? null : null);
    const now=Date.now(),payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:nativeId,
      reason,observed_at:observedAt};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      const unclaimed=current&&this.unclaimedWithoutNative(current);
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id||current.native_id!==session.native_id
        ||(!this.canDispatchStop(current)&&!unclaimed))return false;
      const failedPrelaunch=current.state==='failed'&&current.native_id===null;
      if(failedPrelaunch&&(!stopCommand||!('result_json' in stopCommand)
        ||(this.parse(stopCommand.result_json) as JsonRecord|null)?.evidence!=='dispatch_fenced'))return false;
      const lost=current.state==='lost';
      if (lost && (!stopCommand || !('result_json' in stopCommand) || stopCommand.native_id!==current.native_id)) return false;
      if (stopCommand && 'result_json' in stopCommand) {
        const pending=this.db.one("SELECT id FROM session_commands WHERE id=? AND session_id=? AND machine_id=? AND epoch=? AND kind='stop' AND state='pending'",
          stopCommand.id,session.id,session.machine_id,session.epoch);
        if(!pending)return false;
        this.db.run("UPDATE session_commands SET state='acked',result_json=?,updated_at=? WHERE id=?",
          stopCommand.result_json,now,stopCommand.id);
      } else if(stopCommand)this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at)
        VALUES(?,?,?,?,?,?, 'stop',NULL,'pending',NULL,?,?)`,stopCommand.id,session.id,session.machine_id,session.epoch,
        stopCommand.request_key,stopCommand.request_digest,now,now);
      if (!lost) {
        this.db.run("UPDATE sessions SET state='stopped',native_id=?,updated_at=? WHERE id=?",nativeId,now,session.id);
        this.db.run("UPDATE machine_session_operations SET state='stopped' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      }
      this.settlePendingCommands(session,now,'stop_confirmed',nativeId
        ? {evidence:'native_exit',native_id:nativeId} : {evidence:'dispatch_fenced'});
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,?, ?,NULL,1,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase=excluded.phase,last_activity_at=excluded.last_activity_at,containment_empty=1,observed_at=excluded.observed_at`,
        session.id,nativeId,nativeId?'exited':'missing',observedAt,now);
      if (!lost) {
        const dispatchFenced=nativeId===null&&!unclaimed;
        this.insertOutbox(`stopped:${session.operation_id}:${session.epoch}`,'stopped',session.operation_id,session.project_id,
          {...payload,...(dispatchFenced?{no_dispatch:true}:{})},now);
      }
      return true;
    });
    if(applied && session.state!=='lost')await this.flushOne(`stopped:${session.operation_id}:${session.epoch}`);
    if(applied && session.state!=='lost')console.info('[hub2] SESSION_REAPED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,reason:'stop_confirmed'}));
    return applied;
  }

  private async observeNativeAbsences(snapshot:Meta,previous:Meta|undefined):Promise<void>{
    const store=this.presenceStore();
    if(!store)return; // No durable observation authority in incomplete adapters.
    const rows=this.db.all<SessionRow>("SELECT * FROM sessions WHERE state='running' AND native_id IS NOT NULL ORDER BY updated_at,id LIMIT 200");
    for(const session of rows){
      const key=`native-absence:${session.id}`;
      const matches=()=>{
        const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
        return current?.state==='running'&&current.operation_id===session.operation_id&&current.epoch===session.epoch
          &&current.native_id===session.native_id&&JSON.stringify(this.nativeInventory.metadata())===JSON.stringify(snapshot);
      };
      const eligible=()=>{
        const now=Date.now();
        const live=this.db.one<{native_id:string;phase:string;observed_at:number}>('SELECT native_id,phase,observed_at FROM native_sessions WHERE session_id=?',session.id);
        return matches()&&now>=snapshot.observed_at_ms&&now-snapshot.observed_at_ms<=LIVE_SNAPSHOT_TTL_MS
          &&snapshot.started_at_ms>=session.updated_at&&!this.stopConfirmed(session)
          &&this.nativeInventory.absentOrNonliving(session.native_id!)
          &&!(live&&live.native_id===session.native_id&&!['missing','dead','exited'].includes(live.phase)
            &&live.observed_at<=now+5000&&now-live.observed_at<=LIVE_SNAPSHOT_TTL_MS);
      };
      const saved=await store.get(key) as {operation_id?:string;native_id?:string;epoch?:number;evidence?:NativeAbsenceEvidence}|undefined;
      if(!matches())continue;
      if(!eligible()){await store.delete(key);continue;}
      const now=Date.now(),old=saved?.evidence;
      const continuous=nativeAbsenceObservation(old)&&JSON.stringify(old.latest)===JSON.stringify(previous)&&saved?.operation_id===session.operation_id&&saved.native_id===session.native_id&&saved.epoch===session.epoch
        &&Number.isSafeInteger(old.last_seen_at)&&now>=old.last_seen_at&&now-old.last_seen_at<=LIVE_SNAPSHOT_TTL_MS
        &&snapshot.started_at_ms>=old.latest.observed_at_ms&&snapshot.observed_at_ms>old.latest.observed_at_ms
        &&snapshot.observed_at_ms-old.latest.observed_at_ms<=LIVE_SNAPSHOT_TTL_MS;
      const evidence:NativeAbsenceEvidence={first:continuous?old!.first:snapshot,latest:snapshot,
        first_seen_at:continuous?old!.first_seen_at:now,last_seen_at:now,observations:continuous?old!.observations+1:1};
      const observation={operation_id:session.operation_id,native_id:session.native_id,epoch:session.epoch,evidence};
      if(!eligible())continue;
      await store.put(key,observation);
      if(!eligible())continue;
      const persisted=await store.get(key);
      if(!eligible()||JSON.stringify(persisted)!==JSON.stringify(observation))continue;
      if(validNativeAbsenceEvidence(evidence,Date.now()))await this.recordLost(session,Date.now(),evidence);
    }
  }

  private async recordLost(session:SessionRow,now:number,absence?:NativeAbsenceEvidence):Promise<boolean>{
    const reason=absence?'native_inventory_absent':'host_inventory_stale',key=`lost:${session.operation_id}:${session.epoch}`;
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      const inventory=this.db.one<{observed_at:number}>('SELECT observed_at FROM machine_liveness WHERE singleton=1');
      if(!current||current.state!=='running'||!current.native_id||current.native_id!==session.native_id
        ||current.epoch!==session.epoch||current.operation_id!==session.operation_id
        ||(!absence&&now-Math.max(current.updated_at,inventory?.observed_at??0)<HOST_INVENTORY_LOST_MS))return false;
      if(absence&&(!validNativeAbsenceEvidence(absence,now)
        ||JSON.stringify(this.nativeInventory.metadata())!==JSON.stringify(absence.latest)
        ||!this.nativeInventory.absentOrNonliving(current.native_id)))return false;
      const payload={operation_id:current.dispatch_id,attempt_operation_id:current.operation_id,session_id:current.id,
        intake_id:current.intake_id,machine_id:current.machine_id,epoch:current.epoch,native_id:current.native_id,
        reason,lost:true,observed_at:now,inventory_observed_at:inventory?.observed_at??null,
        ...(absence?{native_inventory_absence:absence}:{})};
      this.db.run("UPDATE sessions SET state='lost',updated_at=? WHERE id=?",now,current.id);
      this.db.run("UPDATE machine_session_operations SET state='lost' WHERE operation_id=? AND session_id=? AND epoch=?",
        current.operation_id,current.id,current.epoch);
      this.settlePendingCommands(current,now,reason,null);
      this.db.run('INSERT INTO attention(session_id,reason,operation_id,message_id,observed_at) VALUES(?,?,?,NULL,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=NULL,observed_at=excluded.observed_at',
        current.id,reason,current.operation_id,now);
      this.insertOutbox(key,'lost',current.operation_id,current.project_id,payload,now);
      return true;
    });
    if(applied){
      await this.flushOne(key);
      await this.scheduleNext();
    }
    return applied;
  }

  private async recordReaped(session:SessionRow,reason:'dead'|'idle',observedAt:number):Promise<boolean>{
    const now=Date.now(),payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:session.native_id,
      reason,observed_at:observedAt,stopped:true,reaped:true};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id||current.native_id!==session.native_id||!['claimed','running'].includes(current.state))return false;
      this.db.run("UPDATE sessions SET state='stopped',updated_at=? WHERE id=?",now,session.id);
      this.db.run("UPDATE machine_session_operations SET state='reaped' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(session,now,reason,{evidence:'native_exit',native_id:session.native_id});
      this.db.run(`INSERT INTO native_sessions(session_id,native_id,phase,last_activity_at,exit_code,containment_empty,observed_at)
        VALUES(?,?,?,?,NULL,?,?) ON CONFLICT(session_id) DO UPDATE SET native_id=excluded.native_id,
        phase=excluded.phase,last_activity_at=excluded.last_activity_at,containment_empty=excluded.containment_empty,observed_at=excluded.observed_at`,
        session.id,session.native_id,'exited',observedAt,1,now);
      this.insertOutbox(`reaped:${session.operation_id}:${session.epoch}`,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(applied)await this.flushOne(`reaped:${session.operation_id}:${session.epoch}`);
    if(applied)console.info('[hub2] SESSION_REAPED',JSON.stringify({session_id:session.id,machine_id:session.machine_id,reason}));
    return applied;
  }

  private async recordLaunchTimeout(session:SessionRow,observedAt:number):Promise<boolean>{
    const now=Date.now(),reason=session.state==='claimed'?'launch_timeout_no_native':'host_no_response';
    const previousAttention=this.db.one<{reason:string}>('SELECT reason FROM attention WHERE session_id=?',session.id)?.reason??null;
    const payload={operation_id:session.dispatch_id,attempt_operation_id:session.operation_id,session_id:session.id,
      intake_id:session.intake_id,machine_id:session.machine_id,epoch:session.epoch,native_id:null,
      reason,launch_failed:true,observed_at:observedAt};
    const applied=this.db.transaction(this.ctx,()=>{
      const current=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',session.id);
      if(!current||current.epoch!==session.epoch||current.operation_id!==session.operation_id
        ||!['staged','offered','claimed'].includes(current.state)||current.native_id!==null||Date.now()-current.created_at<CLAIMED_WITHOUT_NATIVE_GRACE_MS)return false;
      this.db.run("UPDATE sessions SET state='failed',updated_at=? WHERE id=? AND state IN ('staged','offered','claimed') AND native_id IS NULL",now,session.id);
      this.db.run("UPDATE machine_session_operations SET state='failed' WHERE operation_id=? AND session_id=? AND epoch=?",session.operation_id,session.id,session.epoch);
      this.settlePendingCommands(session,now,reason,null);
      this.db.run('INSERT INTO attention(session_id,reason,operation_id,message_id,observed_at) VALUES(?,?,?,NULL,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=NULL,observed_at=excluded.observed_at',
        session.id,reason,session.operation_id,now);
      this.insertOutbox(`reaped:${session.operation_id}:${session.epoch}`,'reaped',session.operation_id,session.project_id,payload,now);
      return true;
    });
    if(applied)await this.flushOne(`reaped:${session.operation_id}:${session.epoch}`);
    if(applied)console.error('[hub2] SESSION_LAUNCH_TIMEOUT',JSON.stringify({session_id:session.id,machine_id:session.machine_id,
      intake_id:session.intake_id,epoch:session.epoch,reason,previous_attention:previousAttention}));
    return applied;
  }

  private settlePendingCommands(session:SessionRow,now:number,reason:string,stopEvidence:JsonRecord|null):void{
    const stopResult=JSON.stringify({stopped:true,reason,...stopEvidence});
    const unconfirmedStopResult=JSON.stringify({stopped:false,error:'stop_unconfirmed',reason:'stop_unconfirmed',detail:reason});
    const failedResult=JSON.stringify({error:'session_ended_before_command_execution',reason});
    this.db.run(`UPDATE session_commands SET state=CASE WHEN kind='stop' AND ? THEN 'acked' ELSE 'failed' END,
      result_json=CASE WHEN kind='stop' AND ? THEN ? WHEN kind='stop' THEN ? ELSE ? END,updated_at=?
      WHERE session_id=? AND machine_id=? AND epoch=? AND state='pending'`,
      stopEvidence?1:0,stopEvidence?1:0,stopResult,unconfirmedStopResult,failedResult,now,session.id,session.machine_id,session.epoch);
  }

  private sessionList(request: Request): Response {
    const actor=actorFrom(request);
    if (!hasScope(actor,'operator:admin')) return error('forbidden',403);
    const query=new URL(request.url).searchParams,rawAfter=query.get('after')||'',limit=Number(query.get('limit')||25);
    let after=rawAfter;
    if (!Number.isInteger(limit)||limit<1||limit>100||rawAfter.length>1024) return error('invalid_page',400);
    if(this.db.schemaVersion<8){
      const rows=this.db.all<SessionRow>(query.get('live')==='1'
        ? "SELECT * FROM sessions WHERE id>? AND state IN ('staged','offered','claimed','running') ORDER BY id LIMIT ?"
        : 'SELECT * FROM sessions WHERE id>? ORDER BY id LIMIT ?',after,limit+1);
      return json({items:rows.slice(0,limit).map(row=>this.sessionView(row)),next:rows.length>limit?rows[limit-1].id:null,native_inventory:{complete:false,fresh:false,error:'inventory_schema_pending'}});
    }
    const inventory:JsonRecord={machine_id:this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1')?.machine_id??null,...this.nativeInventory.status()},live=query.get('live')==='1';
    const generation=String(inventory.snapshot_id??'none')+':'+(inventory.fresh?'fresh':'stale')+':'+(live?'live':'history');
    if(rawAfter){
      try{
        if(!rawAfter.startsWith('ni1.'))return error('native_inventory_changed',409);
        const token=JSON.parse(decodeURIComponent(rawAfter.slice(4))) as {generation?:unknown;after?:unknown};
        if(token.generation!==generation)return error('native_inventory_changed',409);
        if(typeof token.after!=='string'||token.after.length>180)return error('invalid_page',400);
        after=token.after;
      }catch{return error('invalid_page',400);}
    }
    const rows=this.db.all<{page_key:string;session_id:string|null;native_json:string|null}>(`SELECT * FROM (
      SELECT s.id AS page_key,s.id AS session_id,n.row_json AS native_json FROM sessions s
      LEFT JOIN native_inventory_rows n ON n.native_id=s.native_id
      ${live?"WHERE s.state IN ('staged','offered','claimed','running')":''}
      UNION ALL
      SELECT '~native:'||n.native_id AS page_key,NULL AS session_id,n.row_json AS native_json FROM native_inventory_rows n
      WHERE NOT EXISTS(SELECT 1 FROM sessions s WHERE s.native_id=n.native_id)
      ${live?"AND ?=1 AND json_extract(n.row_json,'$.state') IN ('starting','running','exiting') AND json_extract(n.row_json,'$.worker_alive')=1":''}
    ) WHERE page_key>? ORDER BY page_key LIMIT ?`,...(live?[inventory.fresh?1:0]:[]),after,limit+1);
    const machine=this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    const items=rows.slice(0,limit).map(item=>{
      const native=item.native_json?JSON.parse(item.native_json) as JsonRecord:null;
      if(item.session_id){
        const session=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',item.session_id)!;
        return {...this.sessionView(session),...(native?{native_observation:{...native,observed_at:inventory.observed_at_ms,fresh:inventory.fresh}}:{})};
      }
      return {id:'native:'+machine?.machine_id+':'+native!.id,managed:false,native_id:native!.id,
        machine_id:machine?.machine_id,machine:machine?.machine_id,host:machine?.machine_id,
        name:native!.tag,session_type:'native',mode:'unknown',workspace:native!.workspace,cwd:native!.cwd,engine:native!.engine,
        agent:native!.agent,profile:native!.profile,agent_profile:native!.agent_profile,state:native!.state??'unknown',worker_alive:native!.worker_alive??null,
        phase:native!.phase,last_activity_at:native!.last_activity_ms,created_at:native!.created_at_ms,
        observed_at:inventory.observed_at_ms,inventory_fresh:inventory.fresh};
    });
    return json({items,next:rows.length>limit?'ni1.'+encodeURIComponent(JSON.stringify({generation,after:rows[limit-1].page_key})):null,native_inventory:inventory});
  }

  /** Hub1-shaped `POST /api/sessions/:id/(stop|send|capture)` mapped onto the Hub2 command queue, so the CLI and MCP keep working. */
  private async legacyCommand(request: Request, sessionId: string, kind: string): Promise<Response> {
    const body = await bodyObject(request, 12_288);
    const mapped: JsonRecord = { kind, key: body.key };
    if (kind === 'send') mapped.text = body.body;
    if (kind === 'capture' && body.mode !== undefined) mapped.mode = body.mode;
    const response = await this.createCommand(new Request(request.url, { method: 'POST', headers: request.headers, body: JSON.stringify(mapped) }), sessionId);
    if (!response.ok) return response;
    const data = await response.json() as { command: { id: string } };
    const command=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?',data.command.id);
    const result=this.parse(command?.result_json) as JsonRecord|null;
    const session=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',sessionId);
    const stopped=kind==='stop'&&command?.state==='acked'&&!!result&&!!session
      && this.qualifiedStopResult(session,result);
    return json({ ...data, command_id: data.command.id,
      state: stopped?'stopped':command?.state==='acked'?'acked':command?.state==='failed'?'failed':'queued',
      delivery: stopped?'confirmed':command?.state==='acked'?'delivered':command?.state==='failed'?'unconfirmed':'queued' }, response.status);
  }

  /** Command receipts in the shape `sessions send/capture/stop --wait` polls: ack.capture, ack.outcome, ack.confirmed_exit. */
  private commandListing(request: Request, sessionId: string): Response {
    const actor = actorFrom(request), session = this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?', sessionId);
    if (!session || (!hasScope(actor, 'operator:admin') && !(hasScope(actor, 'session:read') && [session.id, session.project_id].includes(actor?.resource_id || '')))) return error('session_not_found', 404);
    const query = new URL(request.url).searchParams;
    const commandId = query.get('command_id');
    if (commandId !== null && (query.getAll('command_id').length !== 1 || !/^[A-Za-z0-9-]{1,80}$/.test(commandId))) return error('invalid_command_id',400);
    const rows = commandId !== null
      ? this.db.all<CommandRow>('SELECT * FROM session_commands WHERE session_id=? AND id=?',sessionId,commandId)
      : this.db.all<CommandRow>('SELECT * FROM session_commands WHERE session_id=? ORDER BY created_at DESC,id LIMIT 100', sessionId);
    const commands:JsonRecord[]=[];
    let bytes = new TextEncoder().encode(JSON.stringify({commands:[],next_cursor:null})).length;
    for (const row of rows) {
      const result = row.state !== 'pending' && row.result_json ? JSON.parse(row.result_json) as JsonRecord : null;
      const ack = result === null ? null : {
        ...result,
        ...(row.kind === 'capture' && typeof result.output === 'string' ? { capture: result.output } : {}),
        ...(row.kind === 'stop' ? { outcome: row.state === 'failed' ? 'failed' : 'delivered',
          confirmed_exit: row.state==='acked'&&this.qualifiedStopResult(session,result) } : {}),
      };
      const entry={ id: row.id, session_id: row.session_id, kind: row.kind==='wake_send'?'send':row.kind, state: row.state, created_at: row.created_at, ack };
      bytes += new TextEncoder().encode(JSON.stringify(entry)).length + (commands.length ? 1 : 0);
      if(bytes>COMMAND_HISTORY_BYTES)return error('command_history_too_large',413);
      commands.push(entry);
    }
    return json({commands,next_cursor:null});
  }

  private async workspacePreflight(request: Request): Promise<Response> {
    const body = await bodyObject(request, 4096);
    const workspace = typeof body.workspace === 'string' ? body.workspace : '';
    const machine = this.db.one<MachineRow>('SELECT * FROM machines LIMIT 1');
    if (!machine || !workspace) return json({ checked: false });
    const capRow = this.db.one<{ capabilities_json: string }>('SELECT capabilities_json FROM machine_capabilities WHERE machine_id=?', machine.machine_id);
    let caps: string[] = [];
    try { caps = JSON.parse(capRow?.capabilities_json || '[]'); } catch {}
    if (!Array.isArray(caps) || !caps.includes('workspace-preflight:v1')) return json({ checked: false });
    let activeSocket: WebSocket | undefined;
    for (const socket of this.machineSockets(machine.machine_id)) {
      if (socket.readyState !== 1 || this.stopOnly(socket)) continue;
      if (await this.authorizedAttachment(socket, 'machine:offer', false, this.env, false)) {
        activeSocket = socket;
        break;
      }
    }
    if (!activeSocket) return json({ checked: false });
    const requestId = crypto.randomUUID();
    const result = await new Promise<JsonRecord | null>(resolve => {
      const timer = setTimeout(() => { this.preflightWaiters.delete(requestId); resolve(null); }, 1500);
      this.preflightWaiters.set(requestId, reply => { clearTimeout(timer); resolve(reply); });
      try {
        activeSocket!.send(JSON.stringify({ type: 'workspace.preflight', request_id: requestId, workspace }));
      } catch {
        clearTimeout(timer);
        this.preflightWaiters.delete(requestId);
        resolve(null);
      }
    });
    if (!result) return json({ checked: false });
    return json({
      checked: true,
      ok: result.ok === true,
      ...(typeof result.reason === 'string' ? { reason: closedLaunchReason(result.reason) } : {}),
      ...(typeof result.detail === 'string' ? { detail: fixedHostDetail(result.reason) } : {}),
    });
  }

  private wakeSession(id:string):WakeSession|undefined{
    const row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',id);if(!row)return;
    const launch=this.parse(row.launch_json) as JsonRecord;
    return{id:row.id,operation_id:row.operation_id,epoch:row.epoch,native_id:row.native_id,state:row.state,project_id:row.project_id,intake_id:row.intake_id,machine_id:row.machine_id,interactive:launch.interactive===true,workspace:String(boundWorkspace(launch))};
  }
  private enqueueWake(w:WakeRecord):boolean{
    const current=this.wakeSession(w.session_id);
    if(!current||current.operation_id!==w.operation_id||current.epoch!==w.epoch||current.native_id!==w.native_id||current.state!=='running'||!current.interactive)return false;
    const prior=this.db.one<CommandRow>('SELECT * FROM session_commands WHERE id=?',w.command_id);
    if(prior)return prior.session_id===w.session_id&&prior.epoch===w.epoch;
    const native=this.db.one<{row_json:string}>('SELECT row_json FROM native_inventory_rows WHERE native_id=?',w.native_id);
    const observed=native?JSON.parse(native.row_json) as JsonRecord:null;
    if(this.nativeInventory.status().fresh!==true||!observed||!Number.isSafeInteger(observed.created_at_ms)||Number(observed.created_at_ms)<=0||observed.tag!==`task-hub2-${w.session_id}`||observed.workspace!==w.workspace)return false;
    w.native_birth=Number(observed.created_at_ms);
    // Every NEW wake uses strict transport; old/unknown hosts durably hold it.
    const binding={id:w.id,session_id:w.session_id,operation_id:w.operation_id,epoch:w.epoch,native_id:w.native_id,tag:observed.tag,workspace:w.workspace,created_at_ms:w.native_birth,deadline_ms:w.wake_at_ms+600000,transport:'strict-inbox:v1'};
    this.db.transaction(this.ctx,()=>{
      const row=this.wakeSession(w.session_id);if(!row||row.operation_id!==w.operation_id||row.epoch!==w.epoch||row.native_id!==w.native_id||row.state!=='running')throw new HttpError('stale_wake',409);
      this.db.run(`INSERT INTO session_commands(id,session_id,machine_id,epoch,request_key,request_digest,kind,text,state,result_json,created_at,updated_at) VALUES(?,?,?,?,?,?,'wake_send',?,'wake_pending',?,?,?)`,w.command_id,w.session_id,w.machine_id,w.epoch,'hub-wake:'+w.id,w.id,w.prompt,JSON.stringify({wake_binding:binding}),Date.now(),Date.now());
    });return true;
  }
  private wakeNotification(w:WakeRecord){
    const payload={wake_id:w.id,session_id:w.session_id,attempt_operation_id:w.operation_id,epoch:w.epoch,intake_id:w.intake_id,state:w.state,reason:w.reason,missed_at:w.missed_at,wake_at_ms:w.wake_at_ms};
    this.db.transaction(this.ctx,()=>{
      if(w.state==='missed')this.db.run("UPDATE session_commands SET state='wake_failed',updated_at=? WHERE id=? AND kind='wake_send' AND state='wake_pending'",Date.now(),w.command_id);
      if(w.state==='acked')this.db.run("DELETE FROM attention WHERE session_id=? AND reason='missed_wake' AND message_id=?",w.session_id,w.id);
      if(w.missed_at!==null){this.insertOutbox('wake:'+w.id+':missed','wake',w.operation_id,w.project_id,{...payload,state:'missed'},Date.now());}
      if(w.missed_at!==null&&w.state!=='acked')this.db.run('INSERT INTO attention(session_id,reason,operation_id,message_id,observed_at) VALUES(?,?,?,?,?) ON CONFLICT(session_id) DO UPDATE SET reason=excluded.reason,operation_id=excluded.operation_id,message_id=excluded.message_id,observed_at=excluded.observed_at',w.session_id,'missed_wake',w.operation_id,w.id,Date.now());
      this.insertOutbox('wake:'+w.id+':'+(w.state==='acked'?'resolved':w.missed_at!==null?'missed':w.state),'wake',w.operation_id,w.project_id,payload,Date.now());
      for(const phase of new Set(['missed',w.state==='acked'?'resolved':w.missed_at!==null?'missed':w.state]))this.db.run("UPDATE machine_outbox SET state='wake_pending' WHERE id=? AND kind='wake' AND state='pending'",'wake:'+w.id+':'+phase);
    });
  }
  // A maintained worker grant binds its intake, not the SID, and may never hold
  // session:send/read scopes: self wake/history therefore re-prove the exact
  // current attempt at native-worker issuance shape instead of resource scope.
  private workerSelfWake(actor:Actor,session:WakeSession,read:boolean):boolean{
    return actor.kind==='agent'
      &&actor.id==='session:'+session.id
      &&actor.resource_id===session.intake_id
      &&actor.scopes.includes(read?'intake:read':'intake:append')
      &&actor.scopes.includes('worker_project:'+session.project_id)
      &&actor.generation==='worker:'+session.epoch+':'+session.operation_id
      &&actor.family_id==='worker:'+session.id+':'+session.epoch
      &&actor.client_id==='native-worker';
  }
  private async sessionWake(request:Request,id:string,action:string):Promise<Response>{
    const session=this.wakeSession(id),actor=actorFrom(request);
    const read=request.method==='GET';
    if(!session||!actor||!hasScope(actor,'operator:admin')&&!(hasScope(actor,read?'session:read':'session:send')&&[id,session.project_id].includes(actor.resource_id||''))&&!this.workerSelfWake(actor,session,read))return error('session_not_found',404);
    if(action==='wakes'&&read){await this.wakes.refresh();return json({session_id:id,wakes:await this.wakes.history(id),...this.wakes.waiting(id)});}
    if(action!=='wake'||request.method!=='POST')return error('method_not_allowed',405);
    const b=await bodyObject(request,12288);if(Object.keys(b).some(k=>!['key','at','prompt','body'].includes(k)))return error('invalid_wake',400);
    const {wake,replay}=await this.wakes.create(session,actor.id,b.key as string,b.at,b.prompt??b.body);
    this.wakeNotification(wake);await this.flushOne('wake:'+wake.id+':'+(wake.missed_at!==null?'missed':wake.state));await this.wakes.sweep();await this.scheduleNext();
    await this.notifyPendingCommands(session.machine_id);
    const current=await this.wakes.lookup(id,wake.id)??wake;
    return json({...this.wakes.view(current),...this.wakes.waiting(id),replay},replay?200:201);
  }

  private sessionDetail(request: Request, id: string): Response {
    const actor=actorFrom(request),row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',id);
    if (!row || (!hasScope(actor,'operator:admin') && !(hasScope(actor,'session:read') && [row.id,row.project_id].includes(actor?.resource_id||'')))) return error('session_not_found',404);
    return json({session:{...this.sessionView(row), ...(this.stopConfirmed(row) ? { stop_confirmed: true } : {})}});
  }

  private sessionEvents(request: Request, id: string): Response {
    const actor=actorFrom(request),row=this.db.one<SessionRow>('SELECT * FROM sessions WHERE id=?',id);
    if (!row || (!hasScope(actor,'operator:admin') && !(hasScope(actor,'session:read') && [row.id,row.project_id].includes(actor?.resource_id||'')))) return error('session_not_found',404);
    const events: JsonRecord[] = [];
    const attention = this.db.one<{reason:string;operation_id:string|null;observed_at:number}>('SELECT reason,operation_id,observed_at FROM attention WHERE session_id=?', row.id);
    const diagnostic = attention ? this.hostStartDiagnostic(row, attention.reason) : null;
    if (attention) {
      events.push({
        kind: 'attention',
        reason: closedHostReason(attention.reason),
        operation_id: attention.operation_id,
        observed_at: new Date(attention.observed_at).toISOString(),
        ...(diagnostic ?? {}),
      });
    }
    const outbox = this.db.all<OutboxRow>("SELECT * FROM machine_outbox WHERE operation_id=? ORDER BY created_at ASC, id ASC LIMIT 20", row.operation_id);
    for (const item of outbox) {
      const payload = this.parse(item.payload_json) as JsonRecord | null;
      if (payload?.session_id !== row.id) continue;
      events.push({
        kind: item.kind,
        state: item.state,
        operation_id: item.operation_id,
        created_at: new Date(item.created_at).toISOString(),
        ...(typeof payload?.reason === 'string' ? { reason: closedHostReason(payload.reason) } : {}),
        ...(typeof payload?.failure_reason === 'string' ? { failure_reason: closedLaunchReason(payload.failure_reason) } : {}),
        ...(typeof payload?.detail === 'string' ? { detail: hostDetail(payload?.failure_reason,payload.detail) } : {}),
      });
    }
    return json({ session_id: row.id, state: row.state, events });
  }

  private sessionView(row: SessionRow): JsonRecord {
    const launch=this.parse(row.launch_json) as JsonRecord;
    const picker=normalizeStartPickerEvidence(launch._hub_picker);
    const attention=this.db.one<{reason:string;observed_at:number}>('SELECT reason,observed_at FROM attention WHERE session_id=?',row.id);
    const diagnostic=attention?this.hostStartDiagnostic(row,attention.reason):null;
    return {id:row.id,state:row.state,intake_id:row.intake_id,project_id:row.project_id,machine_id:row.machine_id,
      epoch:row.epoch,operation_id:row.operation_id,native_id:row.native_id,engine:launch.engine,model:launch.model,
      managed:true,host:row.machine_id,machine:row.machine_id,workspace:boundWorkspace(launch),requested_workspace:launch.workspace,
      start_deadline_at:launch.start_deadline_at??null,
      account_id:row.account_id,account:row.account_id,effort:launch.effort??null,
      ...(launch.managed_credential_label?{managed_credential_label:launch.managed_credential_label}:{}),
      ...(picker ? { picker, policy_id: picker.policy_id, policy_version: picker.policy_version } : {}),
      mode:launch.interactive===true?'interactive':'headless',
      ...(['running','claimed','offered'].includes(row.state)?this.wakes.waiting(row.id):{}),
      created_at:new Date(row.created_at).toISOString(),updated_at:new Date(row.updated_at).toISOString(),
      last_activity_at:this.db.one<{last_activity_at:number|null}>('SELECT last_activity_at FROM native_sessions WHERE session_id=?',row.id)?.last_activity_at??null,
      interactive:launch.interactive,evidence:this.sessionEvidence(row),
      attention:attention?{...attention,reason:closedHostReason(attention.reason),...(diagnostic??{})}:null};
  }

  private sessionEvidence(row:SessionRow):unknown{
    const evidence=this.parse(row.evidence_json);
    if(!isJsonRecord(evidence)||!(HOST_START_DIAGNOSTIC_KEY in evidence))return evidence;
    const projected={...evidence};delete projected[HOST_START_DIAGNOSTIC_KEY];
    return Object.keys(projected).length?projected:null;
  }

  private hostStartDiagnostic(row:SessionRow,reason:string):JsonRecord|null{
    if(row.evidence_digest!==null)return null;
    const evidence=this.parse(row.evidence_json);
    const diagnostic=isJsonRecord(evidence)?evidence[HOST_START_DIAGNOSTIC_KEY]:null;
    if(!isJsonRecord(diagnostic)||diagnostic.operation_id!==row.operation_id||diagnostic.attempt!==row.attempt||diagnostic.epoch!==row.epoch
      ||diagnostic.reason!==reason||diagnostic.stage!=='host_launch')return null;
    const detail=diagnostic.detail === undefined ? undefined : hostDetail(reason,diagnostic.detail);
    return {stage:'host_launch',...(detail?{detail}:{})};
  }

  /** Hibernation attachments retain selection across eviction; prune legacy duplicates before RPCs. */
  private machineSockets(machineId:string,prune=true):WebSocket[]{
    const sockets=this.ctx.getWebSockets('hub2:'+machineId).filter(socket=>socket.readyState===1);
    const rank=(socket:WebSocket):string=>{
      try{const a=socket.deserializeAttachment();return String(a?.connected_at??0).padStart(16,'0')+':'+String(a?.connection_id??'');}
      catch{return '';}
    };
    sockets.sort((a,b)=>rank(b).localeCompare(rank(a)));
    if(prune)for(const old of sockets.slice(1)){try{old.close(1000,'connection superseded');}catch{}}
    return sockets.slice(0,1);
  }

  private currentSocket(socket:WebSocket,prune=true):boolean{
    try{const machine=socket.deserializeAttachment()?.machine_id;
      return typeof machine==='string'&&this.machineSockets(machine,prune)[0]===socket;
    }catch{return false;}
  }

  private stopOnly(socket:WebSocket):boolean{try{return socket.deserializeAttachment()?.stop_only===true;}catch{return false;}}

  private async authorizedAttachment(socket: WebSocket, scope: string, closeOnRefusal = true,env:Hub2Env=this.env,prune=true): Promise<SocketAttachment | undefined> {
    if(!this.currentSocket(socket,prune))return undefined;
    let attachment: Partial<SocketAttachment> | null;
    try { attachment = socket.deserializeAttachment(); } catch { attachment = null; }
    if (!attachment?.machine_id || !attachment.actor_id || !attachment.generation) return undefined;
    const control = env.CONTROL.get(env.CONTROL.idFromName('global'));
    const response = await control.fetch('https://control.hub2.internal/_authorize', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ actor_id: attachment.actor_id, generation: attachment.generation, scope }),
    });
    if (!response.ok) {
      if (closeOnRefusal) { try { socket.close(1008, 'credential revoked'); } catch {} }
      return undefined;
    }
    if(!this.currentSocket(socket,prune))return undefined;
    return attachment as SocketAttachment;
  }

  private commandView(row: CommandRow): JsonRecord {
    return { id: row.id, session_id: row.session_id, epoch: row.epoch, kind: row.kind==='wake_send'?'send':row.kind,
      ...(row.kind === 'stop' ? {deadline_ms: row.created_at + 60000} : {}),
      ...(row.capture_mode ? {mode:row.capture_mode} : {}),
      ...(row.kind==='capture'&&(this.parse(row.result_json) as JsonRecord|null)?.capture_binding
        ?{capture_binding:(this.parse(row.result_json) as JsonRecord).capture_binding}:{}),
      ...((row.kind==='send'||row.kind==='wake_send')&&row.text!==null ? {text:row.text} : {}),
      ...((this.parse(row.result_json) as JsonRecord|null)?.wake_binding?{wake:(this.parse(row.result_json) as JsonRecord).wake_binding}:{}) };
  }

  private machineActor(actor: Actor | undefined, scope: string): string | undefined {
    if (!actor || actor.kind !== 'machine' || !actor.resource_id || !actor.scopes.includes(scope)) return undefined;
    return actor.resource_id;
  }

  private parse(value: unknown): unknown {
    try { return JSON.parse(String(value ?? 'null')); } catch { return null; }
  }
}

/** Actual deployed USER schema, captured by a bounded read-only operator query
 * on2026-11-04. Schema/defaults audited; no user rows or secrets. Reserved
 * Cloudflare/SQLite objects and implicit indexes are never replayed/dropped. */
export type DeployedSchemaObject={captured_rowid:number;name:string;type:'table'|'index'|'trigger'|'view';table:string;sql:string};
export const deployedSchemaObjectCount=757;
export const deployedUserObjectCount=755;
export const deployedUserSchema:readonly DeployedSchemaObject[] = [
  {
    "captured_rowid": 1,
    "name": "users",
    "type": "table",
    "table": "users",
    "sql": "CREATE TABLE users (\n  sub TEXT PRIMARY KEY,\n  email TEXT NOT NULL,\n  is_admin BOOLEAN NOT NULL,\n  first_seen_at TEXT NOT NULL,\n  last_seen_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 3,
    "name": "credentials",
    "type": "table",
    "table": "credentials",
    "sql": "CREATE TABLE credentials (\n  id TEXT PRIMARY KEY,\n  kind TEXT NOT NULL, -- host|agent|job|human\n  token_hash TEXT UNIQUE NOT NULL,\n  refresh_hash TEXT UNIQUE,\n  family_id TEXT NOT NULL,\n  subject TEXT NOT NULL,\n  machine_id TEXT,\n  actor TEXT,\n  scopes JSON NOT NULL,\n  created_by_sub TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  expires_at TEXT NOT NULL,\n  revoked_at TEXT,\n  last_used_at TEXT\n, registry_actor TEXT, approval_seq INTEGER)"
  },
  {
    "captured_rowid": 7,
    "name": "device_pairings",
    "type": "table",
    "table": "device_pairings",
    "sql": "CREATE TABLE device_pairings (\n  device_code_hash TEXT PRIMARY KEY,\n  user_code TEXT UNIQUE NOT NULL,\n  client_kind TEXT NOT NULL,\n  name TEXT NOT NULL,\n  hostname TEXT NOT NULL,\n  ip_country TEXT,\n  actor TEXT,\n  requested_scopes JSON NOT NULL,\n  status TEXT NOT NULL, -- pending|approved|denied|consumed|expired\n  approved_by_sub TEXT,\n  granted_scopes JSON,\n  machine_id TEXT,\n  interval_s INTEGER NOT NULL,\n  last_poll_at TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  expires_at TEXT NOT NULL\n, approved_seq INTEGER)"
  },
  {
    "captured_rowid": 10,
    "name": "machines",
    "type": "table",
    "table": "machines",
    "sql": "CREATE TABLE machines (\n  id TEXT PRIMARY KEY,\n  name TEXT NOT NULL,\n  hostname TEXT NOT NULL,\n  network TEXT NOT NULL DEFAULT 'home',\n  os TEXT NOT NULL DEFAULT '',\n  roles JSON NOT NULL DEFAULT '[]',\n  capabilities JSON NOT NULL DEFAULT '{}',\n  credential_id TEXT,\n  registered_at TEXT NOT NULL,\n  last_checkin_at TEXT,\n  last_heartbeat JSON,\n  drained INTEGER NOT NULL DEFAULT 0,\n  notes TEXT\n, state TEXT, approval_seq INTEGER, arch TEXT DEFAULT '', revoked_at TEXT, revoked_reason TEXT, revoked_by TEXT)"
  },
  {
    "captured_rowid": 12,
    "name": "login_states",
    "type": "table",
    "table": "login_states",
    "sql": "CREATE TABLE login_states (\n  state_hash TEXT PRIMARY KEY,\n  expires_at INTEGER NOT NULL,\n  consumed INTEGER NOT NULL DEFAULT 0\n)"
  },
  {
    "captured_rowid": 14,
    "name": "health_events",
    "type": "table",
    "table": "health_events",
    "sql": "CREATE TABLE health_events (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  subject_kind TEXT,\n  subject TEXT,\n  state TEXT,\n  detail TEXT,\n  at TEXT\n)"
  },
  {
    "captured_rowid": 16,
    "name": "jobs",
    "type": "table",
    "table": "jobs",
    "sql": "CREATE TABLE jobs (\n  id TEXT PRIMARY KEY,\n  kind TEXT NOT NULL,\n  idempotency_key TEXT UNIQUE NOT NULL,\n  payload JSON NOT NULL,\n  status TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  deadline_at TEXT NOT NULL,\n  actor TEXT NOT NULL,\n  offer_host_id TEXT,\n  offer_machine_id TEXT,\n  offer_expires_at TEXT,\n  offer_attempts JSON NOT NULL DEFAULT '[]',\n  claim_token_hash TEXT,\n  holder_host_id TEXT,\n  holder_machine_id TEXT,\n  started_at TEXT,\n  first_action_at TEXT,\n  evidence_digest TEXT,\n  native_session TEXT,\n  launcher_task_id TEXT,\n  lease_epoch INTEGER,\n  finished_at TEXT,\n  outcome_summary TEXT,\n  outcome_evidence JSON\n, role_name TEXT, role_generation INTEGER, role_host_credential TEXT, role_expected_epoch INTEGER, role_effect TEXT, role_finish TEXT)"
  },
  {
    "captured_rowid": 19,
    "name": "leases",
    "type": "table",
    "table": "leases",
    "sql": "CREATE TABLE leases (\n  name TEXT PRIMARY KEY,\n  holder_job_id TEXT,\n  holder_machine_id TEXT,\n  holder_host_id TEXT,\n  holder_actor TEXT,\n  epoch INTEGER NOT NULL DEFAULT 0,\n  acquired_at TEXT,\n  renewed_at TEXT,\n  expires_at TEXT,\n  released_at TEXT,\n  release_reason TEXT\n)"
  },
  {
    "captured_rowid": 21,
    "name": "offers",
    "type": "table",
    "table": "offers",
    "sql": "CREATE TABLE offers (\n  job_id TEXT,\n  machine_id TEXT,\n  offered_at TEXT,\n  response TEXT,\n  reason TEXT\n)"
  },
  {
    "captured_rowid": 22,
    "name": "audit",
    "type": "table",
    "table": "audit",
    "sql": "CREATE TABLE audit (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  actor TEXT NOT NULL,\n  action TEXT NOT NULL,\n  resource TEXT,\n  target TEXT,\n  detail TEXT,\n  at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 23,
    "name": "handoff_notes",
    "type": "table",
    "table": "handoff_notes",
    "sql": "CREATE TABLE handoff_notes (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  lease_name TEXT NOT NULL,\n  job_id TEXT NOT NULL,\n  host_id TEXT NOT NULL,\n  session_id TEXT,\n  epoch INTEGER NOT NULL,\n  summary TEXT NOT NULL,\n  evidence_refs JSON,\n  created_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 24,
    "name": "root_actions",
    "type": "table",
    "table": "root_actions",
    "sql": "CREATE TABLE root_actions (\n  id INTEGER PRIMARY KEY AUTOINCREMENT,\n  job_id TEXT NOT NULL,\n  host_id TEXT NOT NULL,\n  session_id TEXT,\n  epoch INTEGER NOT NULL,\n  action_name TEXT NOT NULL,\n  action_type TEXT NOT NULL,\n  idempotency_key TEXT NOT NULL,\n  payload_digest TEXT NOT NULL,\n  result JSON NOT NULL,\n  created_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 27,
    "name": "messages",
    "type": "table",
    "table": "messages",
    "sql": "CREATE TABLE messages (\n  id TEXT PRIMARY KEY,\n  from_actor TEXT NOT NULL,\n  to_actor TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  thread_id TEXT NOT NULL,\n  reply_to TEXT,\n  body TEXT NOT NULL,\n  idempotency_key TEXT NOT NULL,\n  lease_epoch INTEGER,\n  created_at INTEGER NOT NULL,\n  delivered_at INTEGER,\n  read_at INTEGER,\n  acked_at INTEGER,\n  accepted_at INTEGER,\n  completed_at INTEGER,\n  expires_at INTEGER NOT NULL, registry_unbound_from INTEGER NOT NULL DEFAULT 0, registry_unbound_to INTEGER NOT NULL DEFAULT 0, delivery TEXT, state TEXT, state_reason TEXT, deliver_by INTEGER, to_session_id TEXT, recipient_seq INTEGER, ref_json TEXT, from_session_id TEXT,\n  UNIQUE(from_actor, idempotency_key)\n)"
  },
  {
    "captured_rowid": 30,
    "name": "claims",
    "type": "table",
    "table": "claims",
    "sql": "CREATE TABLE claims (\n  id TEXT PRIMARY KEY,\n  project TEXT NOT NULL,\n  scope TEXT NOT NULL,\n  holder TEXT NOT NULL,\n  created_at INTEGER NOT NULL,\n  expires_at INTEGER NOT NULL,\n  released_at INTEGER\n, work_key TEXT, claim_mode TEXT, claim_source TEXT, worktree TEXT, session_state TEXT, native_claim_id TEXT, native_session_id TEXT)"
  },
  {
    "captured_rowid": 32,
    "name": "root_scheduling",
    "type": "table",
    "table": "root_scheduling",
    "sql": "CREATE TABLE root_scheduling (\n  id INTEGER PRIMARY KEY,\n  enabled INTEGER NOT NULL DEFAULT 1,\n  updated_at TEXT\n)"
  },
  {
    "captured_rowid": 33,
    "name": "fleet_accounts",
    "type": "table",
    "table": "fleet_accounts",
    "sql": "CREATE TABLE fleet_accounts (label TEXT PRIMARY KEY, provider TEXT NOT NULL, max_concurrency INTEGER NOT NULL, reserve_percent REAL NOT NULL, enabled INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 35,
    "name": "fleet_routes",
    "type": "table",
    "table": "fleet_routes",
    "sql": "CREATE TABLE fleet_routes (machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, PRIMARY KEY(machine_id, engine, account))"
  },
  {
    "captured_rowid": 37,
    "name": "fleet_subscriptions",
    "type": "table",
    "table": "fleet_subscriptions",
    "sql": "CREATE TABLE fleet_subscriptions (machine_id TEXT NOT NULL, account TEXT NOT NULL, service TEXT NOT NULL, plan TEXT, observed_at TEXT NOT NULL, quota_json TEXT NOT NULL, PRIMARY KEY(machine_id, account))"
  },
  {
    "captured_rowid": 39,
    "name": "fleet_sessions",
    "type": "table",
    "table": "fleet_sessions",
    "sql": "CREATE TABLE fleet_sessions (machine_id TEXT NOT NULL, native_id TEXT NOT NULL, tag TEXT NOT NULL, engine TEXT, phase TEXT NOT NULL, agent_state TEXT, observed_at TEXT NOT NULL, workspace TEXT NOT NULL, profile TEXT, account TEXT, last_activity_at TEXT, cwd TEXT, created_at_ms INTEGER, progress_json TEXT, PRIMARY KEY(machine_id, native_id))"
  },
  {
    "captured_rowid": 41,
    "name": "fleet_launches",
    "type": "table",
    "table": "fleet_launches",
    "sql": "CREATE TABLE fleet_launches (id TEXT PRIMARY KEY, actor TEXT NOT NULL, requester_kind TEXT NOT NULL, parent_id TEXT, summary TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT, mode TEXT NOT NULL, workspace TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, native_id TEXT, launcher_task_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, verification_status TEXT, exit_reason TEXT, finished_at TEXT, launch_error_json TEXT, model_selection_json TEXT, retried_launch_id TEXT, claim_key TEXT, runtime_json TEXT, worker_status TEXT, UNIQUE(actor, request_key))"
  },
  {
    "captured_rowid": 44,
    "name": "agent_heartbeats",
    "type": "table",
    "table": "agent_heartbeats",
    "sql": "CREATE TABLE agent_heartbeats (actor TEXT PRIMARY KEY, machine_id TEXT, status TEXT NOT NULL, summary TEXT, at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 46,
    "name": "liveness_alerts",
    "type": "table",
    "table": "liveness_alerts",
    "sql": "CREATE TABLE liveness_alerts (id INTEGER PRIMARY KEY AUTOINCREMENT, subject_kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL, detail TEXT NOT NULL, opened_at TEXT NOT NULL, resolved_at TEXT, notified_at TEXT, notify_error TEXT, delivered_to TEXT NOT NULL DEFAULT '[]')"
  },
  {
    "captured_rowid": 47,
    "name": "liveness_alerts_open",
    "type": "index",
    "table": "liveness_alerts",
    "sql": "CREATE INDEX liveness_alerts_open ON liveness_alerts (subject_kind, subject, resolved_at)"
  },
  {
    "captured_rowid": 48,
    "name": "hub_checks",
    "type": "table",
    "table": "hub_checks",
    "sql": "CREATE TABLE hub_checks (name TEXT PRIMARY KEY, kind TEXT NOT NULL, interval_s INTEGER NOT NULL, params_json TEXT NOT NULL, action_json TEXT NOT NULL, enabled INTEGER NOT NULL, next_due_at TEXT NOT NULL, last_run_at TEXT, last_status TEXT, last_detail TEXT)"
  },
  {
    "captured_rowid": 50,
    "name": "hub_meta",
    "type": "table",
    "table": "hub_meta",
    "sql": "CREATE TABLE hub_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
  },
  {
    "captured_rowid": 52,
    "name": "hub_check_runs",
    "type": "table",
    "table": "hub_check_runs",
    "sql": "CREATE TABLE hub_check_runs (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, at TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL, action TEXT)"
  },
  {
    "captured_rowid": 53,
    "name": "host_quota_snapshots",
    "type": "table",
    "table": "host_quota_snapshots",
    "sql": "CREATE TABLE host_quota_snapshots (\n  host_id TEXT PRIMARY KEY,\n  schema_version TEXT NOT NULL,\n  captured_at TEXT NOT NULL,\n  captured_at_ms INTEGER NOT NULL,\n  received_at TEXT NOT NULL,\n  received_at_ms INTEGER NOT NULL,\n  status TEXT NOT NULL,\n  error TEXT,\n  routes_json TEXT NOT NULL\n, collector_version TEXT)"
  },
  {
    "captured_rowid": 55,
    "name": "agents",
    "type": "table",
    "table": "agents",
    "sql": "CREATE TABLE \"agents\" (\n  tag TEXT NOT NULL,\n  project TEXT,\n  role TEXT,\n  machine_id TEXT,\n  registered_at TEXT,\n  engine TEXT,\n  session_id TEXT,\n  workspace TEXT,\n  team_id TEXT,\n  imported_from TEXT,\n  state TEXT,\n  health_state TEXT,\n  health_detail JSON,\n  detail TEXT,\n  lasthealth TEXT,\n  health_at TEXT,\n  last_seen_at TEXT,\n  last_seen TEXT,\n  updated_at TEXT,\n  pid INTEGER,\n  started_at TEXT\n)"
  },
  {
    "captured_rowid": 56,
    "name": "idx_agents_project_tag_unique",
    "type": "index",
    "table": "agents",
    "sql": "CREATE UNIQUE INDEX idx_agents_project_tag_unique ON agents(COALESCE(project, ''), tag)"
  },
  {
    "captured_rowid": 57,
    "name": "idx_agents_tag_lookup",
    "type": "index",
    "table": "agents",
    "sql": "CREATE INDEX idx_agents_tag_lookup ON agents(tag)"
  },
  {
    "captured_rowid": 58,
    "name": "messages_bind_agents",
    "type": "trigger",
    "table": "messages",
    "sql": "CREATE TRIGGER messages_bind_agents AFTER INSERT ON messages BEGIN\n      UPDATE messages SET from_actor = COALESCE(NULLIF(COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END\n      FROM agents a WHERE (length(CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END) <= 128 AND (length(a.tag) BETWEEN 1 AND 64 AND a.tag NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.tag,1,1) GLOB '[A-Za-z0-9]' AND lower(a.tag) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.tag) NOT LIKE 'root%') AND (COALESCE(a.project,'') = '' OR (length(a.project) BETWEEN 1 AND 64 AND a.project NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.project,1,1) GLOB '[A-Za-z0-9]' AND lower(a.project) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.project) NOT LIKE 'root%'))) AND\n      (CASE WHEN instr(NEW.from_actor,':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = NEW.from_actor\n      AND (instr(NEW.from_actor,':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = NEW.from_actor) = 1)\n      \n      LIMIT 1), ''),''), NEW.from_actor), to_actor = COALESCE(NULLIF(COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END\n      FROM agents a WHERE (length(CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END) <= 128 AND (length(a.tag) BETWEEN 1 AND 64 AND a.tag NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.tag,1,1) GLOB '[A-Za-z0-9]' AND lower(a.tag) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.tag) NOT LIKE 'root%') AND (COALESCE(a.project,'') = '' OR (length(a.project) BETWEEN 1 AND 64 AND a.project NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.project,1,1) GLOB '[A-Za-z0-9]' AND lower(a.project) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.project) NOT LIKE 'root%'))) AND\n      (CASE WHEN instr(NEW.to_actor,':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = NEW.to_actor\n      AND (instr(NEW.to_actor,':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = NEW.to_actor) = 1)\n      \n      LIMIT 1), ''),''), NEW.to_actor),\n        registry_unbound_from = CASE WHEN instr(NEW.from_actor,':') = 0 AND (length(NEW.from_actor) BETWEEN 1 AND 64 AND NEW.from_actor NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(NEW.from_actor,1,1) GLOB '[A-Za-z0-9]' AND lower(NEW.from_actor) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(NEW.from_actor) NOT LIKE 'root%') AND COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END\n      FROM agents a WHERE (length(CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END) <= 128 AND (length(a.tag) BETWEEN 1 AND 64 AND a.tag NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.tag,1,1) GLOB '[A-Za-z0-9]' AND lower(a.tag) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.tag) NOT LIKE 'root%') AND (COALESCE(a.project,'') = '' OR (length(a.project) BETWEEN 1 AND 64 AND a.project NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.project,1,1) GLOB '[A-Za-z0-9]' AND lower(a.project) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.project) NOT LIKE 'root%'))) AND\n      (CASE WHEN instr(NEW.from_actor,':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = NEW.from_actor\n      AND (instr(NEW.from_actor,':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = NEW.from_actor) = 1)\n      \n      LIMIT 1), '') = '' THEN 1 ELSE NEW.registry_unbound_from END,\n        registry_unbound_to = CASE WHEN instr(NEW.to_actor,':') = 0 AND (length(NEW.to_actor) BETWEEN 1 AND 64 AND NEW.to_actor NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(NEW.to_actor,1,1) GLOB '[A-Za-z0-9]' AND lower(NEW.to_actor) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(NEW.to_actor) NOT LIKE 'root%') AND COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END\n      FROM agents a WHERE (length(CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END) <= 128 AND (length(a.tag) BETWEEN 1 AND 64 AND a.tag NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.tag,1,1) GLOB '[A-Za-z0-9]' AND lower(a.tag) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.tag) NOT LIKE 'root%') AND (COALESCE(a.project,'') = '' OR (length(a.project) BETWEEN 1 AND 64 AND a.project NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.project,1,1) GLOB '[A-Za-z0-9]' AND lower(a.project) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.project) NOT LIKE 'root%'))) AND\n      (CASE WHEN instr(NEW.to_actor,':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = NEW.to_actor\n      AND (instr(NEW.to_actor,':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = NEW.to_actor) = 1)\n      \n      LIMIT 1), '') = '' THEN 1 ELSE NEW.registry_unbound_to END\n        WHERE id = NEW.id;\n      END"
  },
  {
    "captured_rowid": 59,
    "name": "credentials_bind_agent",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER credentials_bind_agent AFTER INSERT ON credentials\n      WHEN NEW.kind = 'agent' AND NEW.registry_actor IS NULL BEGIN\n      UPDATE credentials SET registry_actor = COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END\n      FROM agents a WHERE (length(CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END) <= 128 AND (length(a.tag) BETWEEN 1 AND 64 AND a.tag NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.tag,1,1) GLOB '[A-Za-z0-9]' AND lower(a.tag) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.tag) NOT LIKE 'root%') AND (COALESCE(a.project,'') = '' OR (length(a.project) BETWEEN 1 AND 64 AND a.project NOT GLOB '*[^A-Za-z0-9._-]*'\n    AND substr(a.project,1,1) GLOB '[A-Za-z0-9]' AND lower(a.project) NOT IN\n    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')\n    AND lower(a.project) NOT LIKE 'root%'))) AND\n      (CASE WHEN instr(COALESCE(NULLIF(NEW.actor,''), NEW.subject),':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = COALESCE(NULLIF(NEW.actor,''), NEW.subject)\n      AND (instr(COALESCE(NULLIF(NEW.actor,''), NEW.subject),':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = COALESCE(NULLIF(NEW.actor,''), NEW.subject)) = 1)\n      \n      LIMIT 1), '') WHERE id = NEW.id;\n      END"
  },
  {
    "captured_rowid": 60,
    "name": "credentials_keep_binding",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER credentials_keep_binding BEFORE UPDATE OF registry_actor ON credentials\n      WHEN OLD.registry_actor IS NOT NULL AND NEW.registry_actor IS NOT OLD.registry_actor\n      BEGIN SELECT RAISE(ABORT, 'immutable_agent_binding'); END"
  },
  {
    "captured_rowid": 61,
    "name": "agents_revoke_credentials",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_revoke_credentials AFTER DELETE ON agents BEGIN\n      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))\n      WHERE kind = 'agent' AND registry_actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;\n      END"
  },
  {
    "captured_rowid": 62,
    "name": "machines_revoke_agent_credentials",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER machines_revoke_agent_credentials AFTER DELETE ON machines BEGIN\n      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))\n      WHERE kind = 'agent' AND machine_id = OLD.id;\n      END"
  },
  {
    "captured_rowid": 63,
    "name": "agents_revoke_machine_binding",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_revoke_machine_binding AFTER UPDATE OF machine_id ON agents\n      WHEN OLD.machine_id IS NOT NEW.machine_id BEGIN\n      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))\n      WHERE kind = 'agent' AND registry_actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;\n      END"
  },
  {
    "captured_rowid": 64,
    "name": "agents_end_pairing_on_delete",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_end_pairing_on_delete AFTER DELETE ON agents BEGIN\n      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent'\n      AND actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;\n      END"
  },
  {
    "captured_rowid": 65,
    "name": "agents_end_pairing_on_move",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_end_pairing_on_move AFTER UPDATE OF tag, project, machine_id ON agents\n      WHEN OLD.tag IS NOT NEW.tag OR COALESCE(OLD.project,'') != COALESCE(NEW.project,'') OR OLD.machine_id IS NOT NEW.machine_id BEGIN\n      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent'\n      AND actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;\n      END"
  },
  {
    "captured_rowid": 66,
    "name": "machines_end_agent_pairing",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER machines_end_agent_pairing AFTER DELETE ON machines BEGIN\n      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent' AND machine_id = OLD.id;\n      END"
  },
  {
    "captured_rowid": 67,
    "name": "agents_no_legacy_collision_insert",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_no_legacy_collision_insert BEFORE INSERT ON agents\n        WHEN EXISTS (SELECT 1 FROM agents WHERE tag = NEW.tag AND rowid != NEW.rowid AND COALESCE(project,'') != COALESCE(NEW.project,'')\n          AND (COALESCE(project,'') = '' OR COALESCE(NEW.project,'') = ''))\n        BEGIN SELECT RAISE(ABORT, 'agent_exists'); END"
  },
  {
    "captured_rowid": 68,
    "name": "agents_no_legacy_collision_update",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER agents_no_legacy_collision_update BEFORE UPDATE OF tag, project ON agents\n        WHEN EXISTS (SELECT 1 FROM agents WHERE tag = NEW.tag AND rowid != NEW.rowid AND COALESCE(project,'') != COALESCE(NEW.project,'')\n          AND (COALESCE(project,'') = '' OR COALESCE(NEW.project,'') = ''))\n        BEGIN SELECT RAISE(ABORT, 'agent_exists'); END"
  },
  {
    "captured_rowid": 69,
    "name": "intake_meta",
    "type": "table",
    "table": "intake_meta",
    "sql": "CREATE TABLE intake_meta (id INTEGER PRIMARY KEY CHECK(id=1), revision INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 70,
    "name": "intake_tasks",
    "type": "table",
    "table": "intake_tasks",
    "sql": "CREATE TABLE intake_tasks (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 72,
    "name": "intake_grants",
    "type": "table",
    "table": "intake_grants",
    "sql": "CREATE TABLE intake_grants (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 74,
    "name": "intake_rules",
    "type": "table",
    "table": "intake_rules",
    "sql": "CREATE TABLE intake_rules (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 76,
    "name": "intake_receipts",
    "type": "table",
    "table": "intake_receipts",
    "sql": "CREATE TABLE intake_receipts (id TEXT PRIMARY KEY, data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 78,
    "name": "intake_end_registration",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER intake_end_registration AFTER DELETE ON agents BEGIN UPDATE intake_grants SET data=json_set(data,'$.revoked_at',CAST(strftime('%s','now') AS INTEGER)*1000,'$.revocation_reason','registry_binding_changed') WHERE json_extract(data,'$.revoked_at') IS NULL AND json_extract(data,'$.binding.actor')=(CASE WHEN COALESCE(OLD.project,'')='' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END); UPDATE intake_meta SET revision=revision+1 WHERE id=1 AND changes()>0; END"
  },
  {
    "captured_rowid": 79,
    "name": "intake_end_binding",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER intake_end_binding AFTER UPDATE OF tag,project,machine_id ON agents WHEN OLD.tag IS NOT NEW.tag OR COALESCE(OLD.project,'')!=COALESCE(NEW.project,'') OR OLD.machine_id IS NOT NEW.machine_id BEGIN UPDATE intake_grants SET data=json_set(data,'$.revoked_at',CAST(strftime('%s','now') AS INTEGER)*1000,'$.revocation_reason','registry_binding_changed') WHERE json_extract(data,'$.revoked_at') IS NULL AND json_extract(data,'$.binding.actor')=(CASE WHEN COALESCE(OLD.project,'')='' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END); UPDATE intake_meta SET revision=revision+1 WHERE id=1 AND changes()>0; END"
  },
  {
    "captured_rowid": 80,
    "name": "intake_end_machine",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER intake_end_machine AFTER DELETE ON machines BEGIN UPDATE intake_grants SET data=json_set(data,'$.revoked_at',CAST(strftime('%s','now') AS INTEGER)*1000,'$.revocation_reason','registry_binding_changed') WHERE json_extract(data,'$.revoked_at') IS NULL AND json_extract(data,'$.binding.machine_id')=OLD.id; UPDATE intake_meta SET revision=revision+1 WHERE id=1 AND changes()>0; END"
  },
  {
    "captured_rowid": 81,
    "name": "routing_cursor",
    "type": "table",
    "table": "routing_cursor",
    "sql": "CREATE TABLE routing_cursor (singleton INTEGER PRIMARY KEY, job_id TEXT NOT NULL)"
  },
  {
    "captured_rowid": 82,
    "name": "routing_machines",
    "type": "table",
    "table": "routing_machines",
    "sql": "CREATE TABLE routing_machines (machine_id TEXT PRIMARY KEY, needs_json TEXT NOT NULL, max_agents INTEGER NOT NULL, observed_at TEXT NOT NULL, runners_json TEXT NOT NULL DEFAULT '[]')"
  },
  {
    "captured_rowid": 84,
    "name": "routing_artifacts",
    "type": "table",
    "table": "routing_artifacts",
    "sql": "CREATE TABLE routing_artifacts (job_id TEXT NOT NULL, name TEXT NOT NULL, media_type TEXT NOT NULL, data_base64 TEXT NOT NULL, PRIMARY KEY(job_id, name))"
  },
  {
    "captured_rowid": 86,
    "name": "routing_jobs",
    "type": "table",
    "table": "routing_jobs",
    "sql": "CREATE TABLE routing_jobs (id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, launch_id TEXT, state TEXT NOT NULL, report TEXT NOT NULL DEFAULT '', artifacts_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, updated_at TEXT NOT NULL, selection_json TEXT, UNIQUE(actor, request_key))"
  },
  {
    "captured_rowid": 89,
    "name": "routing_queued",
    "type": "index",
    "table": "routing_jobs",
    "sql": "CREATE INDEX routing_queued ON routing_jobs(state, launch_id, id)"
  },
  {
    "captured_rowid": 90,
    "name": "fleet_claim_policy",
    "type": "table",
    "table": "fleet_claim_policy",
    "sql": "CREATE TABLE fleet_claim_policy (workspace TEXT PRIMARY KEY, project TEXT, require_claim INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 92,
    "name": "fleet_offered",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_offered ON fleet_launches(id) WHERE state = 'offered'"
  },
  {
    "captured_rowid": 93,
    "name": "session_control_sessions",
    "type": "table",
    "table": "session_control_sessions",
    "sql": "CREATE TABLE session_control_sessions (id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, request_json TEXT NOT NULL, fleet_launch_id TEXT, machine_id TEXT, engine TEXT, mode TEXT NOT NULL, state TEXT NOT NULL, binding_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, owner_token_id TEXT, owner_family TEXT, host_family TEXT, parent_id TEXT, parent_source TEXT, session_type TEXT, hold_json TEXT, UNIQUE(actor, request_key))"
  },
  {
    "captured_rowid": 96,
    "name": "session_control_commands",
    "type": "table",
    "table": "session_control_commands",
    "sql": "CREATE TABLE session_control_commands (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL, generation INTEGER NOT NULL, correlation TEXT NOT NULL, poll_at TEXT, ack_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, owner_token_id TEXT, owner_family TEXT, holder_machine TEXT, claim_token TEXT, claim_expires_at TEXT, holder_attempt TEXT, attempts INTEGER DEFAULT 0, host_status TEXT, host_reason TEXT, not_before TEXT, recipient_seq INTEGER, UNIQUE(session_id, actor, request_key))"
  },
  {
    "captured_rowid": 99,
    "name": "session_control_proofs",
    "type": "table",
    "table": "session_control_proofs",
    "sql": "CREATE TABLE session_control_proofs (reply_envelope_id TEXT PRIMARY KEY, command_id TEXT NOT NULL, session_id TEXT NOT NULL, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 101,
    "name": "hub_check_slots",
    "type": "table",
    "table": "hub_check_slots",
    "sql": "CREATE TABLE hub_check_slots (name TEXT PRIMARY KEY, slot_key TEXT NOT NULL, next_at INTEGER NOT NULL, check_json TEXT NOT NULL, result_json TEXT, intent_json TEXT, action TEXT, state TEXT NOT NULL, token TEXT, lease_until INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 103,
    "name": "idx_check_slot_effect",
    "type": "index",
    "table": "hub_check_slots",
    "sql": "CREATE INDEX idx_check_slot_effect ON hub_check_slots(('check-' || slot_key))"
  },
  {
    "captured_rowid": 104,
    "name": "idx_check_slot_grant",
    "type": "index",
    "table": "hub_check_slots",
    "sql": "CREATE INDEX idx_check_slot_grant ON hub_check_slots(json_extract(check_json, '$.action.grant_id'))"
  },
  {
    "captured_rowid": 105,
    "name": "automation_roles",
    "type": "table",
    "table": "automation_roles",
    "sql": "CREATE TABLE automation_roles (\n      name TEXT PRIMARY KEY, generation INTEGER NOT NULL, config TEXT NOT NULL,\n      last_clock INTEGER NOT NULL DEFAULT 0)"
  },
  {
    "captured_rowid": 107,
    "name": "jobs_role_retention",
    "type": "index",
    "table": "jobs",
    "sql": "CREATE INDEX jobs_role_retention ON jobs(role_name, finished_at)"
  },
  {
    "captured_rowid": 108,
    "name": "jobs_role_credential",
    "type": "index",
    "table": "jobs",
    "sql": "CREATE INDEX jobs_role_credential ON jobs(role_host_credential, status)"
  },
  {
    "captured_rowid": 109,
    "name": "jobs_role_pending_profile",
    "type": "index",
    "table": "jobs",
    "sql": "CREATE INDEX jobs_role_pending_profile ON jobs(kind,payload) WHERE role_name IS NOT NULL AND role_effect IS NOT NULL AND role_finish IS NULL"
  },
  {
    "captured_rowid": 110,
    "name": "conversations",
    "type": "table",
    "table": "conversations",
    "sql": "CREATE TABLE conversations (\n  id TEXT PRIMARY KEY,\n  kind TEXT NOT NULL,\n  title TEXT NOT NULL,\n  created_by TEXT NOT NULL,\n  idempotency_key TEXT,\n  request_json TEXT,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL,\n  archived_at TEXT,\n  archived_by TEXT,\n  last_seq INTEGER NOT NULL DEFAULT 0,\n  last_message_at TEXT,\n  last_message_preview TEXT,\n  retained_message_count INTEGER NOT NULL DEFAULT 0,\n  retained_message_bytes INTEGER NOT NULL DEFAULT 0,\n  removed_member_count INTEGER NOT NULL DEFAULT 0,\n  UNIQUE(created_by, idempotency_key)\n)"
  },
  {
    "captured_rowid": 113,
    "name": "idx_conversations_updated",
    "type": "index",
    "table": "conversations",
    "sql": "CREATE INDEX idx_conversations_updated ON conversations(updated_at DESC, id DESC)"
  },
  {
    "captured_rowid": 114,
    "name": "idx_conversations_archived",
    "type": "index",
    "table": "conversations",
    "sql": "CREATE INDEX idx_conversations_archived ON conversations(archived_at, id)"
  },
  {
    "captured_rowid": 115,
    "name": "conversation_members",
    "type": "table",
    "table": "conversation_members",
    "sql": "CREATE TABLE conversation_members (\n  conversation_id TEXT NOT NULL,\n  actor TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  role TEXT NOT NULL,\n  added_at TEXT NOT NULL,\n  added_by TEXT NOT NULL,\n  removed_at TEXT,\n  removed_by TEXT,\n  last_read_seq INTEGER NOT NULL DEFAULT 0,\n  last_read_at TEXT,\n  PRIMARY KEY(conversation_id, actor)\n)"
  },
  {
    "captured_rowid": 117,
    "name": "idx_conversation_members_actor",
    "type": "index",
    "table": "conversation_members",
    "sql": "CREATE INDEX idx_conversation_members_actor ON conversation_members(actor, removed_at, conversation_id)"
  },
  {
    "captured_rowid": 118,
    "name": "idx_conversation_members_removed",
    "type": "index",
    "table": "conversation_members",
    "sql": "CREATE INDEX idx_conversation_members_removed ON conversation_members(removed_at, conversation_id, actor)"
  },
  {
    "captured_rowid": 119,
    "name": "conversation_messages",
    "type": "table",
    "table": "conversation_messages",
    "sql": "CREATE TABLE conversation_messages (\n  id TEXT PRIMARY KEY,\n  conversation_id TEXT NOT NULL,\n  seq INTEGER NOT NULL,\n  sender_actor TEXT NOT NULL,\n  sender_kind TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  body TEXT NOT NULL,\n  body_bytes INTEGER NOT NULL DEFAULT 0,\n  idempotency_key TEXT NOT NULL,\n  request_json TEXT NOT NULL,\n  reply_to TEXT,\n  reply_to_seq INTEGER,\n  bridge_ref TEXT,\n  created_at TEXT NOT NULL,\n  UNIQUE(conversation_id, seq),\n  UNIQUE(conversation_id, sender_actor, idempotency_key)\n)"
  },
  {
    "captured_rowid": 123,
    "name": "idx_conversation_messages_conv_seq",
    "type": "index",
    "table": "conversation_messages",
    "sql": "CREATE INDEX idx_conversation_messages_conv_seq ON conversation_messages(conversation_id, seq ASC)"
  },
  {
    "captured_rowid": 124,
    "name": "idx_conversation_messages_created",
    "type": "index",
    "table": "conversation_messages",
    "sql": "CREATE INDEX idx_conversation_messages_created ON conversation_messages(created_at, id)"
  },
  {
    "captured_rowid": 125,
    "name": "conversation_storage_usage",
    "type": "table",
    "table": "conversation_storage_usage",
    "sql": "CREATE TABLE conversation_storage_usage (\n  singleton INTEGER PRIMARY KEY CHECK (singleton = 1),\n  conversation_count INTEGER NOT NULL DEFAULT 0,\n  message_count INTEGER NOT NULL DEFAULT 0,\n  message_bytes INTEGER NOT NULL DEFAULT 0,\n  removed_member_count INTEGER NOT NULL DEFAULT 0,\n  schema_version INTEGER NOT NULL DEFAULT 0\n)"
  },
  {
    "captured_rowid": 126,
    "name": "conversation_automation_grants",
    "type": "table",
    "table": "conversation_automation_grants",
    "sql": "CREATE TABLE conversation_automation_grants (\n  id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL, check_name TEXT NOT NULL,\n  target_actor TEXT NOT NULL, target_credential_id TEXT, owner_sub TEXT NOT NULL,\n  expires_at INTEGER NOT NULL, body_template TEXT NOT NULL, action_on TEXT NOT NULL,\n  revoked_at INTEGER, UNIQUE(conversation_id, check_name)\n)"
  },
  {
    "captured_rowid": 129,
    "name": "conversation_automation_receipts",
    "type": "table",
    "table": "conversation_automation_receipts",
    "sql": "CREATE TABLE conversation_automation_receipts (\n  effect_key TEXT PRIMARY KEY, request_json TEXT NOT NULL, message_id TEXT NOT NULL,\n  conversation_id TEXT NOT NULL, created_at INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 131,
    "name": "idx_automation_grant_expiry",
    "type": "index",
    "table": "conversation_automation_grants",
    "sql": "CREATE INDEX idx_automation_grant_expiry ON conversation_automation_grants(expires_at)"
  },
  {
    "captured_rowid": 132,
    "name": "idx_automation_receipt_parent",
    "type": "index",
    "table": "conversation_automation_receipts",
    "sql": "CREATE INDEX idx_automation_receipt_parent ON conversation_automation_receipts(conversation_id)"
  },
  {
    "captured_rowid": 133,
    "name": "conversation_automation_remove_member",
    "type": "trigger",
    "table": "conversation_members",
    "sql": "CREATE TRIGGER conversation_automation_remove_member AFTER UPDATE OF removed_at ON conversation_members\nWHEN NEW.removed_at IS NOT NULL BEGIN\n UPDATE conversation_automation_grants SET revoked_at = 1 WHERE conversation_id = NEW.conversation_id\n AND (target_actor = NEW.actor OR 'human:' || owner_sub = NEW.actor);\nEND"
  },
  {
    "captured_rowid": 134,
    "name": "conversation_links",
    "type": "table",
    "table": "conversation_links",
    "sql": "CREATE TABLE conversation_links (\n  id TEXT PRIMARY KEY,\n  conversation_id TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  ref TEXT NOT NULL,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  idempotency_key TEXT,\n  UNIQUE(conversation_id, kind, ref),\n  UNIQUE(conversation_id, created_by, idempotency_key)\n)"
  },
  {
    "captured_rowid": 138,
    "name": "idx_conversation_links_conv_created",
    "type": "index",
    "table": "conversation_links",
    "sql": "CREATE INDEX idx_conversation_links_conv_created\n  ON conversation_links(conversation_id, created_at DESC, id DESC)"
  },
  {
    "captured_rowid": 139,
    "name": "idx_conversation_links_kind_ref",
    "type": "index",
    "table": "conversation_links",
    "sql": "CREATE INDEX idx_conversation_links_kind_ref\n  ON conversation_links(kind, ref, id)"
  },
  {
    "captured_rowid": 140,
    "name": "retention_cleanup_cursor",
    "type": "table",
    "table": "retention_cleanup_cursor",
    "sql": "CREATE TABLE retention_cleanup_cursor (\n  kind TEXT PRIMARY KEY,\n  next_index INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 142,
    "name": "session_lifecycle_observations",
    "type": "table",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TABLE session_lifecycle_observations(session_key TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, observed_at TEXT NOT NULL, payload_json TEXT NOT NULL)"
  },
  {
    "captured_rowid": 144,
    "name": "session_lifecycle_decisions",
    "type": "table",
    "table": "session_lifecycle_decisions",
    "sql": "CREATE TABLE session_lifecycle_decisions(session_key TEXT NOT NULL, decision_id TEXT PRIMARY KEY, kind TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL, engine TEXT, target TEXT, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 146,
    "name": "status_observations",
    "type": "table",
    "table": "status_observations",
    "sql": "CREATE TABLE status_observations(\n  id TEXT PRIMARY KEY,\n  machine_id TEXT NOT NULL,\n  hub_session_id TEXT NOT NULL,\n  kind TEXT NOT NULL,\n  observed_at TEXT NOT NULL,\n  payload_json TEXT NOT NULL,\n  reporter TEXT NOT NULL,\n  received_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 148,
    "name": "idx_status_obs_session",
    "type": "index",
    "table": "status_observations",
    "sql": "CREATE INDEX idx_status_obs_session ON status_observations(hub_session_id, kind, observed_at)"
  },
  {
    "captured_rowid": 149,
    "name": "status_baselines",
    "type": "table",
    "table": "status_baselines",
    "sql": "CREATE TABLE status_baselines(\n  caller_key TEXT PRIMARY KEY,\n  snapshot_json TEXT NOT NULL,\n  revision INTEGER NOT NULL,\n  updated_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 151,
    "name": "host_metrics_latest",
    "type": "table",
    "table": "host_metrics_latest",
    "sql": "CREATE TABLE host_metrics_latest (\n  host_id TEXT PRIMARY KEY,\n  as_of TEXT NOT NULL,\n  as_of_ms INTEGER NOT NULL,\n  received_at TEXT NOT NULL,\n  received_at_ms INTEGER NOT NULL,\n  request_key TEXT NOT NULL,\n  request_digest TEXT NOT NULL,\n  credential_id TEXT NOT NULL,\n  schema_version TEXT NOT NULL,\n  payload_json TEXT NOT NULL,\n  payload_bytes INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 153,
    "name": "host_metrics_idempotency",
    "type": "table",
    "table": "host_metrics_idempotency",
    "sql": "CREATE TABLE host_metrics_idempotency (\n  host_id TEXT NOT NULL,\n  request_key TEXT NOT NULL,\n  request_digest TEXT NOT NULL,\n  as_of_ms INTEGER NOT NULL,\n  received_at_ms INTEGER NOT NULL,\n  received_at TEXT,\n  credential_id TEXT NOT NULL,\n  payload_bytes INTEGER NOT NULL,\n  payload_json TEXT,\n  PRIMARY KEY (host_id, request_key)\n)"
  },
  {
    "captured_rowid": 155,
    "name": "host_metrics_idempotency_received",
    "type": "index",
    "table": "host_metrics_idempotency",
    "sql": "CREATE INDEX host_metrics_idempotency_received\n  ON host_metrics_idempotency (received_at_ms, host_id)"
  },
  {
    "captured_rowid": 156,
    "name": "disk_observations",
    "type": "table",
    "table": "disk_observations",
    "sql": "CREATE TABLE disk_observations (\n  id TEXT PRIMARY KEY,\n  machine_id TEXT NOT NULL,\n  observed_at TEXT NOT NULL,\n  observed_at_ms INTEGER NOT NULL,\n  received_at TEXT NOT NULL,\n  payload_json TEXT NOT NULL,\n  reporter TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 158,
    "name": "idx_disk_obs_machine",
    "type": "index",
    "table": "disk_observations",
    "sql": "CREATE INDEX idx_disk_obs_machine ON disk_observations(machine_id, observed_at_ms)"
  },
  {
    "captured_rowid": 159,
    "name": "disk_episodes",
    "type": "table",
    "table": "disk_episodes",
    "sql": "CREATE TABLE disk_episodes (\n  episode_key TEXT PRIMARY KEY,\n  machine_id TEXT NOT NULL,\n  mount_point TEXT NOT NULL,\n  state TEXT NOT NULL,\n  opened_at TEXT NOT NULL,\n  last_updated_at TEXT NOT NULL,\n  resolved_at TEXT,\n  alert_id INTEGER,\n  intake_task_id TEXT,\n  details_json TEXT\n)"
  },
  {
    "captured_rowid": 161,
    "name": "idx_disk_episodes_open",
    "type": "index",
    "table": "disk_episodes",
    "sql": "CREATE INDEX idx_disk_episodes_open ON disk_episodes(resolved_at, machine_id)"
  },
  {
    "captured_rowid": 162,
    "name": "host_health_observations",
    "type": "table",
    "table": "host_health_observations",
    "sql": "CREATE TABLE host_health_observations (\n  machine_id TEXT PRIMARY KEY,\n  observed_at TEXT NOT NULL,\n  observed_at_ms INTEGER NOT NULL,\n  received_at TEXT NOT NULL,\n  payload_json TEXT NOT NULL,\n  reporter TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 164,
    "name": "idx_host_health_obs",
    "type": "index",
    "table": "host_health_observations",
    "sql": "CREATE INDEX idx_host_health_obs ON host_health_observations(machine_id, observed_at_ms)"
  },
  {
    "captured_rowid": 165,
    "name": "mcp_clients",
    "type": "table",
    "table": "mcp_clients",
    "sql": "CREATE TABLE mcp_clients (id TEXT PRIMARY KEY, metadata TEXT NOT NULL, expires INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 167,
    "name": "mcp_codes",
    "type": "table",
    "table": "mcp_codes",
    "sql": "CREATE TABLE mcp_codes (hash TEXT PRIMARY KEY, client TEXT NOT NULL, redirect TEXT NOT NULL, challenge TEXT NOT NULL, resource TEXT NOT NULL, sub TEXT NOT NULL, email TEXT NOT NULL, expires INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 169,
    "name": "mcp_grants",
    "type": "table",
    "table": "mcp_grants",
    "sql": "CREATE TABLE mcp_grants (family TEXT PRIMARY KEY, client TEXT NOT NULL, resource TEXT NOT NULL, expires INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 171,
    "name": "mcp_rate",
    "type": "table",
    "table": "mcp_rate",
    "sql": "CREATE TABLE mcp_rate (bucket TEXT PRIMARY KEY, count INTEGER NOT NULL, expires INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 173,
    "name": "session_control_cancellations",
    "type": "table",
    "table": "session_control_cancellations",
    "sql": "CREATE TABLE session_control_cancellations (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 175,
    "name": "session_control_protections",
    "type": "table",
    "table": "session_control_protections",
    "sql": "CREATE TABLE session_control_protections (session_id TEXT PRIMARY KEY, actor TEXT NOT NULL, request_key TEXT NOT NULL, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 177,
    "name": "supervisor_episodes",
    "type": "table",
    "table": "supervisor_episodes",
    "sql": "CREATE TABLE supervisor_episodes (\n  episode_key TEXT PRIMARY KEY,\n  machine_id TEXT NOT NULL,\n  service_id TEXT NOT NULL,\n  state TEXT NOT NULL,\n  opened_at TEXT NOT NULL,\n  last_updated_at TEXT NOT NULL,\n  resolved_at TEXT,\n  alert_id INTEGER,\n  details_json TEXT\n)"
  },
  {
    "captured_rowid": 179,
    "name": "idx_supervisor_episodes_open",
    "type": "index",
    "table": "supervisor_episodes",
    "sql": "CREATE INDEX idx_supervisor_episodes_open ON supervisor_episodes(resolved_at, machine_id)"
  },
  {
    "captured_rowid": 181,
    "name": "server_quota_snapshots",
    "type": "table",
    "table": "server_quota_snapshots",
    "sql": "CREATE TABLE server_quota_snapshots (account TEXT PRIMARY KEY, provider TEXT NOT NULL, captured_at TEXT NOT NULL, quota_json TEXT NOT NULL)"
  },
  {
    "captured_rowid": 183,
    "name": "engine_bundles",
    "type": "table",
    "table": "engine_bundles",
    "sql": "CREATE TABLE engine_bundles (label TEXT PRIMARY KEY, provider TEXT NOT NULL, version TEXT NOT NULL, revision TEXT NOT NULL, ciphertext TEXT NOT NULL)"
  },
  {
    "captured_rowid": 185,
    "name": "engine_logins",
    "type": "table",
    "table": "engine_logins",
    "sql": "CREATE TABLE engine_logins (id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, label TEXT NOT NULL, status TEXT NOT NULL, url TEXT, code_ciphertext TEXT, expires_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 187,
    "name": "engine_installations",
    "type": "table",
    "table": "engine_installations",
    "sql": "CREATE TABLE engine_installations (machine_id TEXT NOT NULL, label TEXT NOT NULL, revision TEXT NOT NULL, PRIMARY KEY(machine_id,label))"
  },
  {
    "captured_rowid": 189,
    "name": "tbm_quota_pools",
    "type": "table",
    "table": "tbm_quota_pools",
    "sql": "CREATE TABLE tbm_quota_pools (\n    pool_id TEXT PRIMARY KEY,\n    provider TEXT NOT NULL,\n    account TEXT NOT NULL DEFAULT 'default',\n    total_capacity_tokens INTEGER NOT NULL,\n    remaining_tokens INTEGER NOT NULL,\n    headroom_percentage REAL NOT NULL,\n    reset_at_ms INTEGER NOT NULL,\n    hard_floor_percent REAL NOT NULL DEFAULT 5.0,\n    soft_reserve_percent REAL NOT NULL DEFAULT 20.0,\n    convexity_exponent REAL NOT NULL DEFAULT 1.5,\n    current_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,\n    target_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,\n    pacing_ratio REAL NOT NULL DEFAULT 1.0,\n    kill_switch_active INTEGER NOT NULL DEFAULT 0,\n    kill_switch_reason TEXT,\n    kill_switch_updated_at TEXT,\n    last_updated_ms INTEGER NOT NULL\n, cycle_ms INTEGER, window_type TEXT, remaining_percent REAL, capacity_known INTEGER NOT NULL DEFAULT 1, quota_unit TEXT NOT NULL DEFAULT 'tokens')"
  },
  {
    "captured_rowid": 191,
    "name": "tbm_banked_tranches",
    "type": "table",
    "table": "tbm_banked_tranches",
    "sql": "CREATE TABLE tbm_banked_tranches (\n    tranche_id TEXT PRIMARY KEY,\n    pool_id TEXT NOT NULL REFERENCES tbm_quota_pools(pool_id),\n    provider TEXT NOT NULL,\n    label TEXT,\n    allocated_tokens INTEGER NOT NULL,\n    remaining_tokens INTEGER NOT NULL,\n    granted_at_ms INTEGER NOT NULL,\n    expires_at_ms INTEGER NOT NULL,\n    status TEXT CHECK(status IN ('active', 'exhausted', 'expired')) NOT NULL DEFAULT 'active',\n    banked_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,\n    target_banked_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,\n    expired_unused_tokens INTEGER NOT NULL DEFAULT 0,\n    updated_at_ms INTEGER NOT NULL\n, quantity_known INTEGER NOT NULL DEFAULT 1, account TEXT)"
  },
  {
    "captured_rowid": 193,
    "name": "tbm_reward_events",
    "type": "table",
    "table": "tbm_reward_events",
    "sql": "CREATE TABLE tbm_reward_events (\n    event_id TEXT PRIMARY KEY,\n    timestamp_ms INTEGER NOT NULL,\n    pool_id TEXT NOT NULL,\n    intake_task_id TEXT NOT NULL,\n    active_mode TEXT CHECK(active_mode IN ('high', 'super-think', 'mini-swarm')) NOT NULL,\n    swarm_concurrency INTEGER NOT NULL DEFAULT 1,\n    useful_work_reward REAL NOT NULL,\n    pacing_error_penalty REAL NOT NULL,\n    floor_breach_penalty REAL NOT NULL,\n    engine_mix_penalty REAL NOT NULL,\n    waste_at_reset_penalty REAL NOT NULL,\n    banked_waste_penalty REAL NOT NULL,\n    net_reward REAL NOT NULL,\n    token_efficiency_eta REAL NOT NULL\n, tokens_known INTEGER NOT NULL DEFAULT 0, efficiency_known INTEGER NOT NULL DEFAULT 0, tokens_burned INTEGER, work_units REAL, session_id TEXT, event_kind TEXT NOT NULL DEFAULT 'session')"
  },
  {
    "captured_rowid": 195,
    "name": "tbm_metrics_rollup",
    "type": "table",
    "table": "tbm_metrics_rollup",
    "sql": "CREATE TABLE tbm_metrics_rollup (\n    rollup_id TEXT PRIMARY KEY,\n    window_type TEXT CHECK(window_type IN ('1m', '10m', '1h', 'reset_window', '24h')) NOT NULL,\n    start_time_ms INTEGER NOT NULL,\n    end_time_ms INTEGER NOT NULL,\n    pool_id TEXT NOT NULL,\n    avg_burn_rate_vs_target REAL NOT NULL,\n    leftover_quota_at_reset INTEGER NOT NULL DEFAULT 0,\n    soft_floor_breach_count INTEGER NOT NULL DEFAULT 0,\n    hard_floor_breach_count INTEGER NOT NULL DEFAULT 0,\n    total_tokens_burned INTEGER NOT NULL,\n    useful_work_units REAL NOT NULL,\n    token_efficiency_eta REAL NOT NULL,\n    idle_wasted_quota INTEGER NOT NULL DEFAULT 0,\n    engine_mix_free_share REAL NOT NULL,\n    engine_mix_paid_share REAL NOT NULL,\n    engine_mix_grok_share REAL NOT NULL,\n    admission_rejection_rate REAL NOT NULL,\n    no_eligible_route_rate REAL NOT NULL,\n    avg_time_to_outcome_seconds REAL NOT NULL,\n    banked_tokens_burned INTEGER NOT NULL DEFAULT 0,\n    banked_tokens_expired_unused INTEGER NOT NULL DEFAULT 0\n, tokens_known INTEGER NOT NULL DEFAULT 0, efficiency_known INTEGER NOT NULL DEFAULT 0)"
  },
  {
    "captured_rowid": 197,
    "name": "idx_pool_provider",
    "type": "index",
    "table": "tbm_quota_pools",
    "sql": "CREATE INDEX idx_pool_provider ON tbm_quota_pools(provider)"
  },
  {
    "captured_rowid": 198,
    "name": "idx_banked_edf",
    "type": "index",
    "table": "tbm_banked_tranches",
    "sql": "CREATE INDEX idx_banked_edf ON tbm_banked_tranches(status, expires_at_ms ASC)"
  },
  {
    "captured_rowid": 199,
    "name": "idx_reward_timestamp",
    "type": "index",
    "table": "tbm_reward_events",
    "sql": "CREATE INDEX idx_reward_timestamp ON tbm_reward_events(timestamp_ms DESC)"
  },
  {
    "captured_rowid": 200,
    "name": "idx_reward_intake",
    "type": "index",
    "table": "tbm_reward_events",
    "sql": "CREATE INDEX idx_reward_intake ON tbm_reward_events(intake_task_id)"
  },
  {
    "captured_rowid": 201,
    "name": "idx_rollup_query",
    "type": "index",
    "table": "tbm_metrics_rollup",
    "sql": "CREATE INDEX idx_rollup_query ON tbm_metrics_rollup(pool_id, window_type, end_time_ms DESC)"
  },
  {
    "captured_rowid": 202,
    "name": "session_warning_events",
    "type": "table",
    "table": "session_warning_events",
    "sql": "CREATE TABLE session_warning_events (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL, until_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 204,
    "name": "session_warning_events_session",
    "type": "index",
    "table": "session_warning_events",
    "sql": "CREATE INDEX session_warning_events_session ON session_warning_events(session_id, created_at)"
  },
  {
    "captured_rowid": 205,
    "name": "fleet_memory_holds",
    "type": "table",
    "table": "fleet_memory_holds",
    "sql": "CREATE TABLE fleet_memory_holds (launch_id TEXT PRIMARY KEY, since TEXT NOT NULL, rejected_json TEXT NOT NULL, refused INTEGER NOT NULL DEFAULT 0, retry_id TEXT, evidence_json TEXT, host_load_machine TEXT, host_load_factor REAL, host_load_trigger TEXT, host_load_limit REAL)"
  },
  {
    "captured_rowid": 207,
    "name": "fleet_engine_health",
    "type": "table",
    "table": "fleet_engine_health",
    "sql": "CREATE TABLE fleet_engine_health (engine TEXT NOT NULL, machine_id TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, reason TEXT NOT NULL, updated_at TEXT NOT NULL, diagnostics_json TEXT, PRIMARY KEY(engine, machine_id))"
  },
  {
    "captured_rowid": 209,
    "name": "operator_notices",
    "type": "table",
    "table": "operator_notices",
    "sql": "CREATE TABLE operator_notices (notice_key TEXT PRIMARY KEY, engine TEXT NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 211,
    "name": "placement_rejections",
    "type": "table",
    "table": "placement_rejections",
    "sql": "CREATE TABLE placement_rejections (id TEXT PRIMARY KEY, created_at TEXT NOT NULL, created_at_ms INTEGER NOT NULL, actor TEXT NOT NULL, reasons_json TEXT NOT NULL, detail TEXT NOT NULL)"
  },
  {
    "captured_rowid": 213,
    "name": "idx_placement_rejections_recent",
    "type": "index",
    "table": "placement_rejections",
    "sql": "CREATE INDEX idx_placement_rejections_recent ON placement_rejections(created_at_ms)"
  },
  {
    "captured_rowid": 214,
    "name": "resource_history",
    "type": "table",
    "table": "resource_history",
    "sql": "CREATE TABLE resource_history (\n bucket_ms INTEGER PRIMARY KEY,payload_json TEXT NOT NULL\n) WITHOUT ROWID"
  },
  {
    "captured_rowid": 215,
    "name": "resource_history_entities",
    "type": "table",
    "table": "resource_history_entities",
    "sql": "CREATE TABLE resource_history_entities (\n kind TEXT NOT NULL,entity TEXT NOT NULL,machine_id TEXT NOT NULL,\n last_seen_ms INTEGER NOT NULL,PRIMARY KEY(kind,entity,machine_id)\n) WITHOUT ROWID"
  },
  {
    "captured_rowid": 216,
    "name": "resource_history_meta",
    "type": "table",
    "table": "resource_history_meta",
    "sql": "CREATE TABLE resource_history_meta (key TEXT PRIMARY KEY, value INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 218,
    "name": "fleet_catalog_seen",
    "type": "table",
    "table": "fleet_catalog_seen",
    "sql": "CREATE TABLE fleet_catalog_seen (machine_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 220,
    "name": "session_stalls",
    "type": "table",
    "table": "session_stalls",
    "sql": "CREATE TABLE session_stalls (\n    machine_id TEXT NOT NULL, native_id TEXT NOT NULL, incarnation INTEGER NOT NULL,\n    since_ms INTEGER NOT NULL, recovery_ms INTEGER, PRIMARY KEY(machine_id, native_id))"
  },
  {
    "captured_rowid": 222,
    "name": "intake_audit",
    "type": "table",
    "table": "intake_audit",
    "sql": "CREATE TABLE intake_audit (\n        seq INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, project TEXT NOT NULL,\n        kind TEXT NOT NULL, actor TEXT NOT NULL, captured_at INTEGER NOT NULL,\n        from_status TEXT, to_status TEXT, reason TEXT NOT NULL)"
  },
  {
    "captured_rowid": 223,
    "name": "intake_audit_task",
    "type": "index",
    "table": "intake_audit",
    "sql": "CREATE INDEX intake_audit_task ON intake_audit(task_id,seq)"
  },
  {
    "captured_rowid": 224,
    "name": "intake_audit_no_update",
    "type": "trigger",
    "table": "intake_audit",
    "sql": "CREATE TRIGGER intake_audit_no_update\n        BEFORE UPDATE ON intake_audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END"
  },
  {
    "captured_rowid": 225,
    "name": "intake_audit_no_delete",
    "type": "trigger",
    "table": "intake_audit",
    "sql": "CREATE TRIGGER intake_audit_no_delete\n        BEFORE DELETE ON intake_audit BEGIN SELECT RAISE(ABORT,'append_only_audit'); END"
  },
  {
    "captured_rowid": 227,
    "name": "intake_audit_archive",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_audit_archive AFTER UPDATE ON intake_tasks\n        WHEN json_extract(OLD.data,'$.archived_at') IS NULL AND json_extract(NEW.data,'$.archived_at') IS NOT NULL BEGIN\n        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)\n        VALUES(NEW.id,json_extract(NEW.data,'$.project'),'archive',COALESCE(json_extract(NEW.data,'$.lifecycle_actor'),json_extract(NEW.data,'$.history[#-1].actor'),'hub:legacy'),json_extract(NEW.data,'$.archived_at'),json_extract(OLD.data,'$.status'),json_extract(NEW.data,'$.status'),COALESCE(json_extract(NEW.data,'$.lifecycle_reason'),'legacy lifecycle transition')); END"
  },
  {
    "captured_rowid": 228,
    "name": "intake_audit_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_audit_delete AFTER DELETE ON intake_tasks BEGIN\n        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)\n        VALUES(OLD.id,json_extract(OLD.data,'$.project'),'delete','hub:sql',CAST(strftime('%s','now') AS INTEGER)*1000,\n        json_extract(OLD.data,'$.status'),NULL,'direct SQL delete; initiating actor unavailable'); END"
  },
  {
    "captured_rowid": 229,
    "name": "tbm_controller_state",
    "type": "table",
    "table": "tbm_controller_state",
    "sql": "CREATE TABLE tbm_controller_state (\n  pool_id TEXT PRIMARY KEY,\n  deficit_tokens REAL NOT NULL DEFAULT 0.0,\n  integral REAL NOT NULL DEFAULT 0.0,\n  last_rate_error REAL NOT NULL DEFAULT 0.0,\n  last_at_ms INTEGER,\n  last_remaining REAL,\n  last_reset_at_ms INTEGER,\n  last_burn_rate_tpm REAL NOT NULL DEFAULT 0.0,\n  updated_at_ms INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 231,
    "name": "tbm_pool_resets",
    "type": "table",
    "table": "tbm_pool_resets",
    "sql": "CREATE TABLE tbm_pool_resets (\n  pool_id TEXT NOT NULL,\n  reset_at_ms INTEGER NOT NULL,\n  provider TEXT NOT NULL,\n  account TEXT NOT NULL DEFAULT 'default',\n  total_capacity_tokens INTEGER NOT NULL DEFAULT 0,\n  remaining_tokens INTEGER NOT NULL DEFAULT 0,\n  headroom_percentage REAL NOT NULL DEFAULT 0.0,\n  remaining_percent REAL,\n  capacity_known INTEGER NOT NULL DEFAULT 1,\n  hard_floor_percent REAL NOT NULL DEFAULT 5.0,\n  captured_at_ms INTEGER NOT NULL,\n  rewarded INTEGER NOT NULL DEFAULT 0,\n  PRIMARY KEY (pool_id, reset_at_ms)\n)"
  },
  {
    "captured_rowid": 233,
    "name": "tbm_session_tokens",
    "type": "table",
    "table": "tbm_session_tokens",
    "sql": "CREATE TABLE tbm_session_tokens (\n  session_id TEXT PRIMARY KEY,\n  native_id TEXT,\n  tag TEXT,\n  engine TEXT,\n  account TEXT,\n  input_tokens INTEGER NOT NULL DEFAULT 0,\n  output_tokens INTEGER NOT NULL DEFAULT 0,\n  total_tokens INTEGER NOT NULL DEFAULT 0,\n  observed_at_ms INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 235,
    "name": "idx_tbm_session_tokens_native",
    "type": "index",
    "table": "tbm_session_tokens",
    "sql": "CREATE INDEX idx_tbm_session_tokens_native ON tbm_session_tokens(native_id)"
  },
  {
    "captured_rowid": 236,
    "name": "idx_tbm_session_tokens_tag",
    "type": "index",
    "table": "tbm_session_tokens",
    "sql": "CREATE INDEX idx_tbm_session_tokens_tag ON tbm_session_tokens(tag)"
  },
  {
    "captured_rowid": 237,
    "name": "trg_tbm_pool_reset_capture_v3",
    "type": "trigger",
    "table": "tbm_quota_pools",
    "sql": "CREATE TRIGGER trg_tbm_pool_reset_capture_v3\nAFTER UPDATE OF reset_at_ms ON tbm_quota_pools\nWHEN NEW.reset_at_ms > OLD.reset_at_ms AND OLD.pool_id != '*' AND OLD.reset_at_ms > 0 AND NEW.last_updated_ms >= OLD.reset_at_ms\nAND NOT EXISTS (SELECT 1 FROM tbm_pool_resets WHERE pool_id=OLD.pool_id AND reset_at_ms=OLD.reset_at_ms)\nBEGIN\n  INSERT OR IGNORE INTO tbm_pool_resets (\n    pool_id, reset_at_ms, provider, account,\n    total_capacity_tokens, remaining_tokens, headroom_percentage,\n    remaining_percent, capacity_known, hard_floor_percent, captured_at_ms, rewarded\n  ) VALUES (\n    OLD.pool_id, OLD.reset_at_ms, OLD.provider, OLD.account,\n    OLD.total_capacity_tokens, OLD.remaining_tokens, OLD.headroom_percentage,\n    OLD.remaining_percent, OLD.capacity_known, OLD.hard_floor_percent, NEW.last_updated_ms, 0\n  );\nEND"
  },
  {
    "captured_rowid": 238,
    "name": "idx_tbm_reward_pool_time",
    "type": "index",
    "table": "tbm_reward_events",
    "sql": "CREATE INDEX idx_tbm_reward_pool_time ON tbm_reward_events(pool_id,timestamp_ms)"
  },
  {
    "captured_rowid": 239,
    "name": "idx_tbm_binding_native_valid",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX idx_tbm_binding_native_valid ON session_control_sessions(CASE WHEN json_valid(binding_json) THEN json_extract(binding_json, '$.native_id') END)"
  },
  {
    "captured_rowid": 240,
    "name": "token_hours_posted",
    "type": "table",
    "table": "token_hours_posted",
    "sql": "CREATE TABLE token_hours_posted (\n  entity TEXT NOT NULL, hour_ms INTEGER NOT NULL,\n  input_tokens INTEGER NOT NULL, output_tokens INTEGER NOT NULL,\n  revision INTEGER NOT NULL DEFAULT 0,\n  PRIMARY KEY(entity, hour_ms)\n) WITHOUT ROWID"
  },
  {
    "captured_rowid": 241,
    "name": "tbm_tick_state",
    "type": "table",
    "table": "tbm_tick_state",
    "sql": "CREATE TABLE tbm_tick_state (id INTEGER PRIMARY KEY, last_tick_ms INTEGER NOT NULL, summary_json TEXT NOT NULL)"
  },
  {
    "captured_rowid": 242,
    "name": "intake_watchdog_meta",
    "type": "table",
    "table": "intake_watchdog_meta",
    "sql": "CREATE TABLE intake_watchdog_meta(id INTEGER PRIMARY KEY CHECK(id=1),last_tick INTEGER,nonzero_since INTEGER,digest_day TEXT,last_source TEXT,last_result TEXT)"
  },
  {
    "captured_rowid": 243,
    "name": "intake_restart_queue",
    "type": "table",
    "table": "intake_restart_queue",
    "sql": "CREATE TABLE intake_restart_queue(task_id TEXT PRIMARY KEY, state TEXT NOT NULL,\n      reason TEXT NOT NULL, request_key TEXT, request_json TEXT, session_id TEXT, attempts INTEGER NOT NULL DEFAULT 0,\n      next_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, held_since INTEGER)"
  },
  {
    "captured_rowid": 245,
    "name": "repo_registry",
    "type": "table",
    "table": "repo_registry",
    "sql": "CREATE TABLE repo_registry (name TEXT PRIMARY KEY, machine TEXT NOT NULL, data TEXT NOT NULL, updated_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 247,
    "name": "resource_resident_samples",
    "type": "table",
    "table": "resource_resident_samples",
    "sql": "CREATE TABLE resource_resident_samples (launch_id TEXT PRIMARY KEY, resident_mb REAL NOT NULL, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 249,
    "name": "resource_choking_events",
    "type": "table",
    "table": "resource_choking_events",
    "sql": "CREATE TABLE resource_choking_events (machine_id TEXT NOT NULL, reason TEXT NOT NULL, observed_at TEXT NOT NULL, PRIMARY KEY(machine_id,reason))"
  },
  {
    "captured_rowid": 251,
    "name": "resource_cpu_samples",
    "type": "table",
    "table": "resource_cpu_samples",
    "sql": "CREATE TABLE resource_cpu_samples (launch_id TEXT PRIMARY KEY, current_cpu_cores REAL NOT NULL, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 253,
    "name": "resource_cpu_starts",
    "type": "table",
    "table": "resource_cpu_starts",
    "sql": "CREATE TABLE resource_cpu_starts (launch_id TEXT PRIMARY KEY, expected_cpu_cores REAL NOT NULL, expected_cpu_source TEXT NOT NULL)"
  },
  {
    "captured_rowid": 255,
    "name": "session_cpu_usage",
    "type": "table",
    "table": "session_cpu_usage",
    "sql": "CREATE TABLE session_cpu_usage (session_id TEXT PRIMARY KEY, p95_cpu_cores REAL, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 257,
    "name": "session_cpu_finalized",
    "type": "table",
    "table": "session_cpu_finalized",
    "sql": "CREATE TABLE session_cpu_finalized (session_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 259,
    "name": "session_resource_metadata",
    "type": "table",
    "table": "session_resource_metadata",
    "sql": "CREATE TABLE session_resource_metadata (session_id TEXT PRIMARY KEY, workspace TEXT NOT NULL)"
  },
  {
    "captured_rowid": 261,
    "name": "resource_ramps",
    "type": "table",
    "table": "resource_ramps",
    "sql": "CREATE TABLE resource_ramps (launch_id TEXT PRIMARY KEY, started_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 263,
    "name": "resource_settings",
    "type": "table",
    "table": "resource_settings",
    "sql": "CREATE TABLE resource_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 264,
    "name": "session_resource_usage",
    "type": "table",
    "table": "session_resource_usage",
    "sql": "CREATE TABLE session_resource_usage (\n session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL,\n fleet_launch_id TEXT, intake_id TEXT, project TEXT, engine TEXT, model TEXT, tier TEXT, kind TEXT,\n started_at TEXT NOT NULL, finished_at TEXT, state TEXT NOT NULL,\n peak_rss_mb REAL, peak_pss_mb REAL, cpu_seconds REAL, runtime_s REAL, avg_cpu_cores REAL,\n observed_at TEXT NOT NULL, UNIQUE(machine_id,native_id))"
  },
  {
    "captured_rowid": 267,
    "name": "session_resource_estimate",
    "type": "index",
    "table": "session_resource_usage",
    "sql": "CREATE INDEX session_resource_estimate ON session_resource_usage(engine,project,kind,finished_at)"
  },
  {
    "captured_rowid": 268,
    "name": "resource_evidence",
    "type": "table",
    "table": "resource_evidence",
    "sql": "CREATE TABLE resource_evidence (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, native_id TEXT NOT NULL, reason TEXT NOT NULL, saved_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 270,
    "name": "resource_reaps",
    "type": "table",
    "table": "resource_reaps",
    "sql": "CREATE TABLE resource_reaps (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, intake_id TEXT, actor TEXT NOT NULL, reason TEXT NOT NULL, freed_mb REAL, at TEXT NOT NULL, evidence_id TEXT NOT NULL)"
  },
  {
    "captured_rowid": 272,
    "name": "host_forks_breaches",
    "type": "table",
    "table": "host_forks_breaches",
    "sql": "CREATE TABLE host_forks_breaches (\n  machine_id TEXT PRIMARY KEY,\n  since_ms INTEGER NOT NULL,\n  last_value REAL NOT NULL\n)"
  },
  {
    "captured_rowid": 274,
    "name": "intake_task_projection",
    "type": "table",
    "table": "intake_task_projection",
    "sql": "CREATE TABLE intake_task_projection(id TEXT PRIMARY KEY, data TEXT NOT NULL, links TEXT NOT NULL)"
  },
  {
    "captured_rowid": 276,
    "name": "intake_projection_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_projection_insert AFTER INSERT ON intake_tasks BEGIN\n        INSERT INTO intake_task_projection(id,data,links) SELECT NEW.id, json_set(json_remove(NEW.data,'$.history'), '$.history',\n        json((SELECT json_group_array(json(CASE WHEN json_extract(e.value,'$.kind')='intake' THEN\n            json_object('kind','intake','text',CASE WHEN json_type(e.value,'$.text')='null' THEN NULL ELSE '' END,\n              'transcription',json_extract(e.value,'$.transcription'), 'source',json_object('reference',json_extract(e.value,'$.source.reference')))\n            ELSE e.value END)) FROM json_each(NEW.data,'$.history') e\n          WHERE json_extract(e.value,'$.kind') IN ('intake','link','report','commit','followup','blocker','pending_approval','resolution','approval_decision')))),\n        (SELECT json_group_array(json(e.value)) FROM json_each(NEW.data,'$.history') e\n         WHERE json_extract(e.value,'$.kind') IN ('link','report'))\n        ON CONFLICT(id) DO UPDATE SET data=excluded.data,links=excluded.links; END"
  },
  {
    "captured_rowid": 277,
    "name": "intake_projection_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_projection_update AFTER UPDATE ON intake_tasks BEGIN\n        INSERT INTO intake_task_projection(id,data,links) SELECT NEW.id, json_set(json_remove(NEW.data,'$.history'), '$.history',\n        json((SELECT json_group_array(json(CASE WHEN json_extract(e.value,'$.kind')='intake' THEN\n            json_object('kind','intake','text',CASE WHEN json_type(e.value,'$.text')='null' THEN NULL ELSE '' END,\n              'transcription',json_extract(e.value,'$.transcription'), 'source',json_object('reference',json_extract(e.value,'$.source.reference')))\n            ELSE e.value END)) FROM json_each(NEW.data,'$.history') e\n          WHERE json_extract(e.value,'$.kind') IN ('intake','link','report','commit','followup','blocker','pending_approval','resolution','approval_decision')))),\n        (SELECT json_group_array(json(e.value)) FROM json_each(NEW.data,'$.history') e\n         WHERE json_extract(e.value,'$.kind') IN ('link','report'))\n        ON CONFLICT(id) DO UPDATE SET data=excluded.data,links=excluded.links; END"
  },
  {
    "captured_rowid": 278,
    "name": "intake_projection_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_projection_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_task_projection WHERE id=OLD.id; END"
  },
  {
    "captured_rowid": 279,
    "name": "intake_storage_totals",
    "type": "table",
    "table": "intake_storage_totals",
    "sql": "CREATE TABLE intake_storage_totals (\n        kind TEXT PRIMARY KEY, bytes INTEGER NOT NULL, hot INTEGER NOT NULL,\n        events INTEGER NOT NULL, original_requests INTEGER NOT NULL, count INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 281,
    "name": "intake_storage_tasks_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_storage_tasks_insert AFTER INSERT ON intake_tasks BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),hot=hot+(CASE WHEN json_extract(NEW.data,'$.archived_at') IS NULL AND json_extract(NEW.data,'$.status') NOT IN ('done','cancelled') THEN length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)) ELSE 0 END),events=events+(COALESCE(length(CAST(json_extract(NEW.data,'$.history') AS BLOB)),0)),original_requests=original_requests+((SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) FROM json_each(json_extract(NEW.data,'$.history')) e WHERE json_extract(e.value,'$.kind')='intake')),count=count+(1) WHERE kind='tasks'; END"
  },
  {
    "captured_rowid": 282,
    "name": "intake_storage_tasks_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_storage_tasks_update AFTER UPDATE ON intake_tasks BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot+(CASE WHEN json_extract(NEW.data,'$.archived_at') IS NULL AND json_extract(NEW.data,'$.status') NOT IN ('done','cancelled') THEN length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)) ELSE 0 END)-(CASE WHEN json_extract(OLD.data,'$.archived_at') IS NULL AND json_extract(OLD.data,'$.status') NOT IN ('done','cancelled') THEN length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB)) ELSE 0 END),events=events+(COALESCE(length(CAST(json_extract(NEW.data,'$.history') AS BLOB)),0))-(COALESCE(length(CAST(json_extract(OLD.data,'$.history') AS BLOB)),0)),original_requests=original_requests+((SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) FROM json_each(json_extract(NEW.data,'$.history')) e WHERE json_extract(e.value,'$.kind')='intake'))-((SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) FROM json_each(json_extract(OLD.data,'$.history')) e WHERE json_extract(e.value,'$.kind')='intake')),count=count+(1)-(1) WHERE kind='tasks'; END"
  },
  {
    "captured_rowid": 283,
    "name": "intake_storage_tasks_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_storage_tasks_delete AFTER DELETE ON intake_tasks BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot-(CASE WHEN json_extract(OLD.data,'$.archived_at') IS NULL AND json_extract(OLD.data,'$.status') NOT IN ('done','cancelled') THEN length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB)) ELSE 0 END),events=events-(COALESCE(length(CAST(json_extract(OLD.data,'$.history') AS BLOB)),0)),original_requests=original_requests-((SELECT COALESCE(SUM(length(CAST(e.value AS BLOB))),0) FROM json_each(json_extract(OLD.data,'$.history')) e WHERE json_extract(e.value,'$.kind')='intake')),count=count-(1) WHERE kind='tasks'; END"
  },
  {
    "captured_rowid": 284,
    "name": "intake_storage_grants_insert",
    "type": "trigger",
    "table": "intake_grants",
    "sql": "CREATE TRIGGER intake_storage_grants_insert AFTER INSERT ON intake_grants BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),hot=hot+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),events=events+(0),original_requests=original_requests+(0),count=count+(1) WHERE kind='grants'; END"
  },
  {
    "captured_rowid": 285,
    "name": "intake_storage_grants_update",
    "type": "trigger",
    "table": "intake_grants",
    "sql": "CREATE TRIGGER intake_storage_grants_update AFTER UPDATE ON intake_grants BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),events=events+(0)-(0),original_requests=original_requests+(0)-(0),count=count+(1)-(1) WHERE kind='grants'; END"
  },
  {
    "captured_rowid": 286,
    "name": "intake_storage_grants_delete",
    "type": "trigger",
    "table": "intake_grants",
    "sql": "CREATE TRIGGER intake_storage_grants_delete AFTER DELETE ON intake_grants BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),events=events-(0),original_requests=original_requests-(0),count=count-(1) WHERE kind='grants'; END"
  },
  {
    "captured_rowid": 287,
    "name": "intake_storage_rules_insert",
    "type": "trigger",
    "table": "intake_rules",
    "sql": "CREATE TRIGGER intake_storage_rules_insert AFTER INSERT ON intake_rules BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),hot=hot+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),events=events+(0),original_requests=original_requests+(0),count=count+(1) WHERE kind='rules'; END"
  },
  {
    "captured_rowid": 288,
    "name": "intake_storage_rules_update",
    "type": "trigger",
    "table": "intake_rules",
    "sql": "CREATE TRIGGER intake_storage_rules_update AFTER UPDATE ON intake_rules BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),events=events+(0)-(0),original_requests=original_requests+(0)-(0),count=count+(1)-(1) WHERE kind='rules'; END"
  },
  {
    "captured_rowid": 289,
    "name": "intake_storage_rules_delete",
    "type": "trigger",
    "table": "intake_rules",
    "sql": "CREATE TRIGGER intake_storage_rules_delete AFTER DELETE ON intake_rules BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),events=events-(0),original_requests=original_requests-(0),count=count-(1) WHERE kind='rules'; END"
  },
  {
    "captured_rowid": 290,
    "name": "intake_storage_receipts_insert",
    "type": "trigger",
    "table": "intake_receipts",
    "sql": "CREATE TRIGGER intake_storage_receipts_insert AFTER INSERT ON intake_receipts BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),hot=hot+(0),events=events+(0),original_requests=original_requests+(0),count=count+(1) WHERE kind='receipts'; END"
  },
  {
    "captured_rowid": 291,
    "name": "intake_storage_receipts_update",
    "type": "trigger",
    "table": "intake_receipts",
    "sql": "CREATE TRIGGER intake_storage_receipts_update AFTER UPDATE ON intake_receipts BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot+(0)-(0),events=events+(0)-(0),original_requests=original_requests+(0)-(0),count=count+(1)-(1) WHERE kind='receipts'; END"
  },
  {
    "captured_rowid": 292,
    "name": "intake_storage_receipts_delete",
    "type": "trigger",
    "table": "intake_receipts",
    "sql": "CREATE TRIGGER intake_storage_receipts_delete AFTER DELETE ON intake_receipts BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot-(0),events=events-(0),original_requests=original_requests-(0),count=count-(1) WHERE kind='receipts'; END"
  },
  {
    "captured_rowid": 293,
    "name": "intake_storage_audit_insert",
    "type": "trigger",
    "table": "intake_audit",
    "sql": "CREATE TRIGGER intake_storage_audit_insert AFTER INSERT ON intake_audit BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(json_object('seq',NEW.seq,'task_id',NEW.task_id,'project',NEW.project,'kind',NEW.kind,'actor',NEW.actor,'captured_at',NEW.captured_at,'from_status',NEW.from_status,'to_status',NEW.to_status,'reason',NEW.reason) AS BLOB))),hot=hot+(0),events=events+(0),original_requests=original_requests+(0),count=count+(1) WHERE kind='audit'; END"
  },
  {
    "captured_rowid": 294,
    "name": "intake_storage_audit_update",
    "type": "trigger",
    "table": "intake_audit",
    "sql": "CREATE TRIGGER intake_storage_audit_update AFTER UPDATE ON intake_audit BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(json_object('seq',NEW.seq,'task_id',NEW.task_id,'project',NEW.project,'kind',NEW.kind,'actor',NEW.actor,'captured_at',NEW.captured_at,'from_status',NEW.from_status,'to_status',NEW.to_status,'reason',NEW.reason) AS BLOB)))-(length(CAST(json_object('seq',OLD.seq,'task_id',OLD.task_id,'project',OLD.project,'kind',OLD.kind,'actor',OLD.actor,'captured_at',OLD.captured_at,'from_status',OLD.from_status,'to_status',OLD.to_status,'reason',OLD.reason) AS BLOB))),hot=hot+(0)-(0),events=events+(0)-(0),original_requests=original_requests+(0)-(0),count=count+(1)-(1) WHERE kind='audit'; END"
  },
  {
    "captured_rowid": 295,
    "name": "intake_storage_audit_delete",
    "type": "trigger",
    "table": "intake_audit",
    "sql": "CREATE TRIGGER intake_storage_audit_delete AFTER DELETE ON intake_audit BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(json_object('seq',OLD.seq,'task_id',OLD.task_id,'project',OLD.project,'kind',OLD.kind,'actor',OLD.actor,'captured_at',OLD.captured_at,'from_status',OLD.from_status,'to_status',OLD.to_status,'reason',OLD.reason) AS BLOB))),hot=hot-(0),events=events-(0),original_requests=original_requests-(0),count=count-(1) WHERE kind='audit'; END"
  },
  {
    "captured_rowid": 296,
    "name": "session_control_native",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_native ON session_control_sessions(machine_id,\n      CASE WHEN json_valid(binding_json) THEN CASE WHEN json_type(binding_json, '$.native_id') = 'text'\n        THEN json_extract(binding_json, '$.native_id') END END)"
  },
  {
    "captured_rowid": 297,
    "name": "lifecycle_decision_latest",
    "type": "index",
    "table": "session_lifecycle_decisions",
    "sql": "CREATE INDEX lifecycle_decision_latest ON session_lifecycle_decisions(session_key,created_at DESC,decision_id DESC)"
  },
  {
    "captured_rowid": 298,
    "name": "status_observation_latest",
    "type": "index",
    "table": "status_observations",
    "sql": "CREATE INDEX status_observation_latest ON status_observations(hub_session_id,kind,observed_at DESC,received_at DESC)"
  },
  {
    "captured_rowid": 299,
    "name": "fleet_launch_native",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_native ON fleet_launches(machine_id,native_id)"
  },
  {
    "captured_rowid": 300,
    "name": "session_control_created",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_created ON session_control_sessions(created_at DESC,id DESC)"
  },
  {
    "captured_rowid": 301,
    "name": "session_control_actor_created",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_actor_created ON session_control_sessions(actor,created_at DESC,id DESC)"
  },
  {
    "captured_rowid": 302,
    "name": "routing_scan_cursor",
    "type": "table",
    "table": "routing_scan_cursor",
    "sql": "CREATE TABLE routing_scan_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_rowid INTEGER NOT NULL, high_water INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 303,
    "name": "routing_pending",
    "type": "index",
    "table": "routing_jobs",
    "sql": "CREATE INDEX routing_pending ON routing_jobs(state, launch_id)"
  },
  {
    "captured_rowid": 304,
    "name": "fleet_queue_cursor",
    "type": "table",
    "table": "fleet_queue_cursor",
    "sql": "CREATE TABLE fleet_queue_cursor (singleton INTEGER PRIMARY KEY CHECK(singleton=1), after_rowid INTEGER NOT NULL, high_water INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 305,
    "name": "fleet_launch_state",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_state ON fleet_launches(state)"
  },
  {
    "captured_rowid": 306,
    "name": "fleet_launch_expiry",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_expiry ON fleet_launches(state,created_at)"
  },
  {
    "captured_rowid": 307,
    "name": "fleet_launch_account_state",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_account_state ON fleet_launches(account,state)"
  },
  {
    "captured_rowid": 308,
    "name": "fleet_session_account_phase",
    "type": "index",
    "table": "fleet_sessions",
    "sql": "CREATE INDEX fleet_session_account_phase ON fleet_sessions(account,phase)"
  },
  {
    "captured_rowid": 309,
    "name": "fleet_route_account",
    "type": "index",
    "table": "fleet_routes",
    "sql": "CREATE INDEX fleet_route_account ON fleet_routes(account)"
  },
  {
    "captured_rowid": 310,
    "name": "routing_launch",
    "type": "index",
    "table": "routing_jobs",
    "sql": "CREATE INDEX routing_launch ON routing_jobs(launch_id)"
  },
  {
    "captured_rowid": 311,
    "name": "session_control_fleet_lookup",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_fleet_lookup ON session_control_sessions(fleet_launch_id,machine_id)"
  },
  {
    "captured_rowid": 312,
    "name": "resource_memory_recent",
    "type": "index",
    "table": "session_resource_usage",
    "sql": "CREATE INDEX resource_memory_recent ON session_resource_usage(engine,observed_at DESC) WHERE finished_at IS NOT NULL AND peak_rss_mb > 0"
  },
  {
    "captured_rowid": 313,
    "name": "resource_cpu_recent",
    "type": "index",
    "table": "session_resource_usage",
    "sql": "CREATE INDEX resource_cpu_recent ON session_resource_usage(observed_at DESC) WHERE finished_at IS NOT NULL"
  },
  {
    "captured_rowid": 314,
    "name": "resource_history_revision",
    "type": "table",
    "table": "resource_history_revision",
    "sql": "CREATE TABLE resource_history_revision(id INTEGER PRIMARY KEY CHECK(id=1), revision TEXT NOT NULL)"
  },
  {
    "captured_rowid": 315,
    "name": "resource_history_session_resource_usage_insert",
    "type": "trigger",
    "table": "session_resource_usage",
    "sql": "CREATE TRIGGER resource_history_session_resource_usage_insert\n          AFTER INSERT ON session_resource_usage WHEN NEW.finished_at IS NOT NULL BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 316,
    "name": "resource_history_session_resource_usage_update",
    "type": "trigger",
    "table": "session_resource_usage",
    "sql": "CREATE TRIGGER resource_history_session_resource_usage_update\n          AFTER UPDATE ON session_resource_usage WHEN OLD.finished_at IS NOT NULL OR NEW.finished_at IS NOT NULL BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 317,
    "name": "resource_history_session_resource_usage_delete",
    "type": "trigger",
    "table": "session_resource_usage",
    "sql": "CREATE TRIGGER resource_history_session_resource_usage_delete\n          AFTER DELETE ON session_resource_usage WHEN OLD.finished_at IS NOT NULL BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 318,
    "name": "resource_history_session_cpu_usage_insert",
    "type": "trigger",
    "table": "session_cpu_usage",
    "sql": "CREATE TRIGGER resource_history_session_cpu_usage_insert\n          AFTER INSERT ON session_cpu_usage WHEN EXISTS(SELECT 1 FROM session_resource_usage WHERE session_id=NEW.session_id AND finished_at IS NOT NULL) BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 319,
    "name": "resource_history_session_cpu_usage_update",
    "type": "trigger",
    "table": "session_cpu_usage",
    "sql": "CREATE TRIGGER resource_history_session_cpu_usage_update\n          AFTER UPDATE ON session_cpu_usage WHEN EXISTS(SELECT 1 FROM session_resource_usage WHERE session_id=OLD.session_id AND finished_at IS NOT NULL) OR EXISTS(SELECT 1 FROM session_resource_usage WHERE session_id=NEW.session_id AND finished_at IS NOT NULL) BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 320,
    "name": "resource_history_session_cpu_usage_delete",
    "type": "trigger",
    "table": "session_cpu_usage",
    "sql": "CREATE TRIGGER resource_history_session_cpu_usage_delete\n          AFTER DELETE ON session_cpu_usage WHEN EXISTS(SELECT 1 FROM session_resource_usage WHERE session_id=OLD.session_id AND finished_at IS NOT NULL) BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 321,
    "name": "resource_history_resource_settings_insert",
    "type": "trigger",
    "table": "resource_settings",
    "sql": "CREATE TRIGGER resource_history_resource_settings_insert\n          AFTER INSERT ON resource_settings WHEN 1 BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 322,
    "name": "resource_history_resource_settings_update",
    "type": "trigger",
    "table": "resource_settings",
    "sql": "CREATE TRIGGER resource_history_resource_settings_update\n          AFTER UPDATE ON resource_settings WHEN 1 BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 323,
    "name": "resource_history_resource_settings_delete",
    "type": "trigger",
    "table": "resource_settings",
    "sql": "CREATE TRIGGER resource_history_resource_settings_delete\n          AFTER DELETE ON resource_settings WHEN 1 BEGIN UPDATE resource_history_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 324,
    "name": "fleet_launch_machine_state",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_machine_state ON fleet_launches(machine_id,state)"
  },
  {
    "captured_rowid": 325,
    "name": "disk_trend_samples",
    "type": "table",
    "table": "disk_trend_samples",
    "sql": "CREATE TABLE disk_trend_samples(\n      id TEXT NOT NULL,ordinal INTEGER NOT NULL,observation_rowid INTEGER NOT NULL,\n      machine_id TEXT NOT NULL,observed_at_ms INTEGER NOT NULL,mount_point TEXT NOT NULL,\n      used_bytes,total_bytes,percent,PRIMARY KEY(id,ordinal))"
  },
  {
    "captured_rowid": 327,
    "name": "disk_trend_machine_time",
    "type": "index",
    "table": "disk_trend_samples",
    "sql": "CREATE INDEX disk_trend_machine_time ON disk_trend_samples(machine_id,observed_at_ms,observation_rowid,ordinal)"
  },
  {
    "captured_rowid": 328,
    "name": "disk_trend_projection_meta",
    "type": "table",
    "table": "disk_trend_projection_meta",
    "sql": "CREATE TABLE disk_trend_projection_meta(id INTEGER PRIMARY KEY)"
  },
  {
    "captured_rowid": 329,
    "name": "disk_trend_insert",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_insert\n      AFTER INSERT ON disk_observations BEGIN \n      INSERT INTO disk_trend_samples SELECT NEW.id,e.key,NEW.rowid,NEW.machine_id,NEW.observed_at_ms,\n      json_extract(e.value,'$.mount_point'),\n      CASE WHEN json_type(e.value,'$.used_bytes') IN ('integer','real') THEN json_extract(e.value,'$.used_bytes') END,\n      CASE WHEN json_type(e.value,'$.total_bytes') IN ('integer','real') THEN json_extract(e.value,'$.total_bytes') END,\n      json_extract(e.value,'$.percent') FROM json_each(CASE WHEN json_valid(NEW.payload_json) THEN\n        CASE WHEN json_type(NEW.payload_json,'$.filesystems')='array' THEN json_extract(NEW.payload_json,'$.filesystems') ELSE '[]' END ELSE '[]' END) e\n      WHERE e.type='object' AND json_type(e.value,'$.mount_point')='text'; END"
  },
  {
    "captured_rowid": 330,
    "name": "disk_trend_update",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_update\n      AFTER UPDATE ON disk_observations BEGIN DELETE FROM disk_trend_samples WHERE id=OLD.id;\n      INSERT INTO disk_trend_samples SELECT NEW.id,e.key,NEW.rowid,NEW.machine_id,NEW.observed_at_ms,\n      json_extract(e.value,'$.mount_point'),\n      CASE WHEN json_type(e.value,'$.used_bytes') IN ('integer','real') THEN json_extract(e.value,'$.used_bytes') END,\n      CASE WHEN json_type(e.value,'$.total_bytes') IN ('integer','real') THEN json_extract(e.value,'$.total_bytes') END,\n      json_extract(e.value,'$.percent') FROM json_each(CASE WHEN json_valid(NEW.payload_json) THEN\n        CASE WHEN json_type(NEW.payload_json,'$.filesystems')='array' THEN json_extract(NEW.payload_json,'$.filesystems') ELSE '[]' END ELSE '[]' END) e\n      WHERE e.type='object' AND json_type(e.value,'$.mount_point')='text'; END"
  },
  {
    "captured_rowid": 331,
    "name": "disk_trend_delete",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_delete AFTER DELETE ON disk_observations BEGIN DELETE FROM disk_trend_samples WHERE id=OLD.id; END"
  },
  {
    "captured_rowid": 332,
    "name": "resource_reaps_native",
    "type": "index",
    "table": "resource_reaps",
    "sql": "CREATE INDEX resource_reaps_native ON resource_reaps(machine_id,native_id,session_id)"
  },
  {
    "captured_rowid": 333,
    "name": "disk_trend_revision",
    "type": "table",
    "table": "disk_trend_revision",
    "sql": "CREATE TABLE disk_trend_revision(machine_id TEXT PRIMARY KEY,revision TEXT NOT NULL)"
  },
  {
    "captured_rowid": 335,
    "name": "disk_trend_revision_insert",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_revision_insert AFTER INSERT ON disk_observations BEGIN\n        INSERT INTO disk_trend_revision(machine_id,revision) SELECT NEW.machine_id,lower(hex(randomblob(16)))\n          WHERE 1\n          ON CONFLICT(machine_id) DO UPDATE SET revision=excluded.revision; END"
  },
  {
    "captured_rowid": 336,
    "name": "disk_trend_revision_update",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_revision_update AFTER UPDATE ON disk_observations BEGIN\n        INSERT INTO disk_trend_revision(machine_id,revision) SELECT NEW.machine_id,lower(hex(randomblob(16)))\n          WHERE 1\n          ON CONFLICT(machine_id) DO UPDATE SET revision=excluded.revision;INSERT INTO disk_trend_revision(machine_id,revision) SELECT OLD.machine_id,lower(hex(randomblob(16)))\n          WHERE OLD.machine_id!=NEW.machine_id\n          ON CONFLICT(machine_id) DO UPDATE SET revision=excluded.revision; END"
  },
  {
    "captured_rowid": 337,
    "name": "disk_trend_revision_delete",
    "type": "trigger",
    "table": "disk_observations",
    "sql": "CREATE TRIGGER disk_trend_revision_delete AFTER DELETE ON disk_observations BEGIN\n        INSERT INTO disk_trend_revision(machine_id,revision) SELECT OLD.machine_id,lower(hex(randomblob(16)))\n          WHERE 1\n          ON CONFLICT(machine_id) DO UPDATE SET revision=excluded.revision; END"
  },
  {
    "captured_rowid": 338,
    "name": "status_lifecycle_revision",
    "type": "table",
    "table": "status_lifecycle_revision",
    "sql": "CREATE TABLE status_lifecycle_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision TEXT NOT NULL)"
  },
  {
    "captured_rowid": 339,
    "name": "status_lifecycle_session_control_sessions_insert",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_sessions_insert\n          AFTER INSERT ON session_control_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 340,
    "name": "status_lifecycle_session_control_sessions_update",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_sessions_update\n          AFTER UPDATE ON session_control_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 341,
    "name": "status_lifecycle_session_control_sessions_delete",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_sessions_delete\n          AFTER DELETE ON session_control_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 342,
    "name": "status_lifecycle_fleet_launches_insert",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_launches_insert\n          AFTER INSERT ON fleet_launches BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 343,
    "name": "status_lifecycle_fleet_launches_update",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_launches_update\n          AFTER UPDATE ON fleet_launches BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 344,
    "name": "status_lifecycle_fleet_launches_delete",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_launches_delete\n          AFTER DELETE ON fleet_launches BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 345,
    "name": "status_lifecycle_fleet_sessions_insert",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_sessions_insert\n          AFTER INSERT ON fleet_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 346,
    "name": "status_lifecycle_fleet_sessions_update",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_sessions_update\n          AFTER UPDATE ON fleet_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 347,
    "name": "status_lifecycle_fleet_sessions_delete",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER status_lifecycle_fleet_sessions_delete\n          AFTER DELETE ON fleet_sessions BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 348,
    "name": "status_lifecycle_session_control_protections_insert",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_protections_insert\n          AFTER INSERT ON session_control_protections BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 349,
    "name": "status_lifecycle_session_control_protections_update",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_protections_update\n          AFTER UPDATE ON session_control_protections BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 350,
    "name": "status_lifecycle_session_control_protections_delete",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_lifecycle_session_control_protections_delete\n          AFTER DELETE ON session_control_protections BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 351,
    "name": "status_lifecycle_session_lifecycle_observations_insert",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_lifecycle_session_lifecycle_observations_insert\n          AFTER INSERT ON session_lifecycle_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 352,
    "name": "status_lifecycle_session_lifecycle_observations_update",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_lifecycle_session_lifecycle_observations_update\n          AFTER UPDATE ON session_lifecycle_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 353,
    "name": "status_lifecycle_session_lifecycle_observations_delete",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_lifecycle_session_lifecycle_observations_delete\n          AFTER DELETE ON session_lifecycle_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 354,
    "name": "status_lifecycle_intake_task_projection_insert",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_lifecycle_intake_task_projection_insert\n          AFTER INSERT ON intake_task_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 355,
    "name": "status_lifecycle_intake_task_projection_update",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_lifecycle_intake_task_projection_update\n          AFTER UPDATE ON intake_task_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 356,
    "name": "status_lifecycle_intake_task_projection_delete",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_lifecycle_intake_task_projection_delete\n          AFTER DELETE ON intake_task_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 357,
    "name": "status_lifecycle_credentials_insert",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_lifecycle_credentials_insert\n          AFTER INSERT ON credentials BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 358,
    "name": "status_lifecycle_credentials_update",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_lifecycle_credentials_update\n          AFTER UPDATE OF id,kind,actor,subject,created_by_sub,family_id ON credentials BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 359,
    "name": "status_lifecycle_credentials_delete",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_lifecycle_credentials_delete\n          AFTER DELETE ON credentials BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 360,
    "name": "status_baselines_updated",
    "type": "index",
    "table": "status_baselines",
    "sql": "CREATE INDEX status_baselines_updated ON status_baselines(updated_at)"
  },
  {
    "captured_rowid": 361,
    "name": "status_baseline_payloads",
    "type": "table",
    "table": "status_baseline_payloads",
    "sql": "CREATE TABLE status_baseline_payloads(\n  caller_key TEXT PRIMARY KEY,\n  snapshot_json TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 363,
    "name": "status_baseline_payload_cleanup",
    "type": "trigger",
    "table": "status_baselines",
    "sql": "CREATE TRIGGER status_baseline_payload_cleanup\n      AFTER DELETE ON status_baselines BEGIN\n      DELETE FROM status_baseline_payloads WHERE caller_key=OLD.caller_key; END"
  },
  {
    "captured_rowid": 364,
    "name": "resource_request_projection",
    "type": "table",
    "table": "resource_request_projection",
    "sql": "CREATE TABLE resource_request_projection (\n    source TEXT NOT NULL,id TEXT NOT NULL,request_json TEXT NOT NULL,PRIMARY KEY(source,id))"
  },
  {
    "captured_rowid": 366,
    "name": "resource_request_fleet_launches_insert",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_request_fleet_launches_insert AFTER INSERT ON fleet_launches BEGIN INSERT OR REPLACE INTO resource_request_projection(source,id,request_json)\n    VALUES('fleet_launches',NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('project',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.project','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'task_kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.task_kind','null')),'kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.kind','null')),'tier',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.tier','null')),'intake_task_id',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.intake_task_id','null')),'keep',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.keep','null')),'provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END)); END"
  },
  {
    "captured_rowid": 367,
    "name": "resource_request_fleet_launches_update",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_request_fleet_launches_update AFTER UPDATE OF id,request_json ON fleet_launches BEGIN\n    DELETE FROM resource_request_projection WHERE source='fleet_launches' AND id=OLD.id; INSERT OR REPLACE INTO resource_request_projection(source,id,request_json)\n    VALUES('fleet_launches',NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('project',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.project','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'task_kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.task_kind','null')),'kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.kind','null')),'tier',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.tier','null')),'intake_task_id',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.intake_task_id','null')),'keep',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.keep','null')),'provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END)); END"
  },
  {
    "captured_rowid": 368,
    "name": "resource_request_fleet_launches_delete",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_request_fleet_launches_delete AFTER DELETE ON fleet_launches BEGIN\n    DELETE FROM resource_request_projection WHERE source='fleet_launches' AND id=OLD.id; END"
  },
  {
    "captured_rowid": 369,
    "name": "resource_request_session_control_sessions_insert",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_request_session_control_sessions_insert AFTER INSERT ON session_control_sessions BEGIN INSERT OR REPLACE INTO resource_request_projection(source,id,request_json)\n    VALUES('session_control_sessions',NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('project',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.project','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'task_kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.task_kind','null')),'kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.kind','null')),'tier',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.tier','null')),'intake_task_id',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.intake_task_id','null')),'keep',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.keep','null')),'provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END)); END"
  },
  {
    "captured_rowid": 370,
    "name": "resource_request_session_control_sessions_update",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_request_session_control_sessions_update AFTER UPDATE OF id,request_json ON session_control_sessions BEGIN\n    DELETE FROM resource_request_projection WHERE source='session_control_sessions' AND id=OLD.id; INSERT OR REPLACE INTO resource_request_projection(source,id,request_json)\n    VALUES('session_control_sessions',NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('project',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.project','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'task_kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.task_kind','null')),'kind',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.kind','null')),'tier',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.tier','null')),'intake_task_id',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.intake_task_id','null')),'keep',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.keep','null')),'provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END)); END"
  },
  {
    "captured_rowid": 371,
    "name": "resource_request_session_control_sessions_delete",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_request_session_control_sessions_delete AFTER DELETE ON session_control_sessions BEGIN\n    DELETE FROM resource_request_projection WHERE source='session_control_sessions' AND id=OLD.id; END"
  },
  {
    "captured_rowid": 372,
    "name": "mcp_tool_usage",
    "type": "table",
    "table": "mcp_tool_usage",
    "sql": "CREATE TABLE mcp_tool_usage (\n      tool_name TEXT NOT NULL, caller_kind TEXT NOT NULL,\n      calls INTEGER NOT NULL, last_called_at INTEGER NOT NULL,\n      PRIMARY KEY (tool_name, caller_kind)\n    )"
  },
  {
    "captured_rowid": 374,
    "name": "fleet_launch_native_state",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_native_state ON fleet_launches(machine_id,native_id,state)"
  },
  {
    "captured_rowid": 375,
    "name": "fleet_launch_inflight_identity",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_inflight_identity ON fleet_launches(machine_id,engine,account,profile) WHERE native_id IS NULL AND state IN ('starting','uncertain')"
  },
  {
    "captured_rowid": 376,
    "name": "fleet_session_incarnation",
    "type": "index",
    "table": "fleet_sessions",
    "sql": "CREATE INDEX fleet_session_incarnation ON fleet_sessions(machine_id,tag,engine,account,profile,workspace,phase)"
  },
  {
    "captured_rowid": 377,
    "name": "worker_access",
    "type": "table",
    "table": "worker_access",
    "sql": "CREATE TABLE worker_access (\n  family_id TEXT PRIMARY KEY, launch_id TEXT NOT NULL UNIQUE, session_id TEXT NOT NULL,\n  task_id TEXT NOT NULL, workspace TEXT NOT NULL, expires_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 380,
    "name": "resource_catalog_revision",
    "type": "table",
    "table": "resource_catalog_revision",
    "sql": "CREATE TABLE resource_catalog_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision TEXT NOT NULL,structure_revision TEXT NOT NULL)"
  },
  {
    "captured_rowid": 381,
    "name": "resource_catalog_session_control_sessions_insert",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_catalog_session_control_sessions_insert AFTER INSERT ON session_control_sessions\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 382,
    "name": "resource_catalog_session_control_sessions_update",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_catalog_session_control_sessions_update AFTER UPDATE ON session_control_sessions\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 383,
    "name": "resource_catalog_session_control_sessions_delete",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER resource_catalog_session_control_sessions_delete AFTER DELETE ON session_control_sessions\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 384,
    "name": "resource_catalog_fleet_launches_insert",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_catalog_fleet_launches_insert AFTER INSERT ON fleet_launches\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 385,
    "name": "resource_catalog_fleet_launches_update",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_catalog_fleet_launches_update AFTER UPDATE ON fleet_launches\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 386,
    "name": "resource_catalog_fleet_launches_delete",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER resource_catalog_fleet_launches_delete AFTER DELETE ON fleet_launches\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 387,
    "name": "resource_catalog_fleet_sessions_insert",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER resource_catalog_fleet_sessions_insert AFTER INSERT ON fleet_sessions\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 389,
    "name": "resource_catalog_fleet_sessions_delete",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER resource_catalog_fleet_sessions_delete AFTER DELETE ON fleet_sessions\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 390,
    "name": "resource_catalog_session_control_protections_insert",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER resource_catalog_session_control_protections_insert AFTER INSERT ON session_control_protections\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 391,
    "name": "resource_catalog_session_control_protections_update",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER resource_catalog_session_control_protections_update AFTER UPDATE ON session_control_protections\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 392,
    "name": "resource_catalog_session_control_protections_delete",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER resource_catalog_session_control_protections_delete AFTER DELETE ON session_control_protections\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 393,
    "name": "resource_catalog_resource_reaps_insert",
    "type": "trigger",
    "table": "resource_reaps",
    "sql": "CREATE TRIGGER resource_catalog_resource_reaps_insert AFTER INSERT ON resource_reaps\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 394,
    "name": "resource_catalog_resource_reaps_update",
    "type": "trigger",
    "table": "resource_reaps",
    "sql": "CREATE TRIGGER resource_catalog_resource_reaps_update AFTER UPDATE ON resource_reaps\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 395,
    "name": "resource_catalog_resource_reaps_delete",
    "type": "trigger",
    "table": "resource_reaps",
    "sql": "CREATE TRIGGER resource_catalog_resource_reaps_delete AFTER DELETE ON resource_reaps\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 396,
    "name": "resource_catalog_intake_tasks_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER resource_catalog_intake_tasks_insert AFTER INSERT ON intake_tasks\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 397,
    "name": "resource_catalog_intake_tasks_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER resource_catalog_intake_tasks_update AFTER UPDATE ON intake_tasks\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 398,
    "name": "resource_catalog_intake_tasks_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER resource_catalog_intake_tasks_delete AFTER DELETE ON intake_tasks\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 399,
    "name": "resource_catalog_intake_task_projection_insert",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER resource_catalog_intake_task_projection_insert AFTER INSERT ON intake_task_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 400,
    "name": "resource_catalog_intake_task_projection_update",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER resource_catalog_intake_task_projection_update AFTER UPDATE ON intake_task_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 401,
    "name": "resource_catalog_intake_task_projection_delete",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER resource_catalog_intake_task_projection_delete AFTER DELETE ON intake_task_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 402,
    "name": "resource_catalog_resource_request_projection_insert",
    "type": "trigger",
    "table": "resource_request_projection",
    "sql": "CREATE TRIGGER resource_catalog_resource_request_projection_insert AFTER INSERT ON resource_request_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 403,
    "name": "resource_catalog_resource_request_projection_update",
    "type": "trigger",
    "table": "resource_request_projection",
    "sql": "CREATE TRIGGER resource_catalog_resource_request_projection_update AFTER UPDATE ON resource_request_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 404,
    "name": "resource_catalog_resource_request_projection_delete",
    "type": "trigger",
    "table": "resource_request_projection",
    "sql": "CREATE TRIGGER resource_catalog_resource_request_projection_delete AFTER DELETE ON resource_request_projection\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 405,
    "name": "status_lifecycle_status_observations_insert",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_lifecycle_status_observations_insert\n          AFTER INSERT ON status_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 406,
    "name": "status_lifecycle_status_observations_update",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_lifecycle_status_observations_update\n          AFTER UPDATE ON status_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 407,
    "name": "status_lifecycle_status_observations_delete",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_lifecycle_status_observations_delete\n          AFTER DELETE ON status_observations BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 408,
    "name": "lifecycle_request_projection",
    "type": "table",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TABLE lifecycle_request_projection (\n    id TEXT PRIMARY KEY,request_json TEXT NOT NULL,has_brief INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 410,
    "name": "lifecycle_request_insert",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER lifecycle_request_insert AFTER INSERT ON session_control_sessions BEGIN INSERT OR REPLACE INTO lifecycle_request_projection VALUES\n    (NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'cwd',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.cwd','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END),COALESCE(json_type(CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END,'$.brief')='text',0)); END"
  },
  {
    "captured_rowid": 411,
    "name": "lifecycle_request_update",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER lifecycle_request_update AFTER UPDATE OF id,request_json ON session_control_sessions BEGIN\n    DELETE FROM lifecycle_request_projection WHERE id=OLD.id; INSERT OR REPLACE INTO lifecycle_request_projection VALUES\n    (NEW.id,(CASE WHEN json_type((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END))='object' AND NOT EXISTS(SELECT 1 FROM json_each((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END)) GROUP BY key HAVING count(*)>1) THEN json_object('provenance',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.provenance','null')),'workspace',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.workspace','null')),'cwd',json(COALESCE((CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) -> '$.cwd','null'))) ELSE (CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END) END),COALESCE(json_type(CASE WHEN json_valid(NEW.request_json) THEN NEW.request_json ELSE '{}' END,'$.brief')='text',0)); END"
  },
  {
    "captured_rowid": 412,
    "name": "lifecycle_request_delete",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER lifecycle_request_delete AFTER DELETE ON session_control_sessions BEGIN\n    DELETE FROM lifecycle_request_projection WHERE id=OLD.id; END"
  },
  {
    "captured_rowid": 413,
    "name": "status_source_revision",
    "type": "table",
    "table": "status_source_revision",
    "sql": "CREATE TABLE status_source_revision(id INTEGER PRIMARY KEY CHECK(id=1),revision TEXT NOT NULL)"
  },
  {
    "captured_rowid": 414,
    "name": "status_source_session_control_sessions_insert",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_source_session_control_sessions_insert\n        AFTER INSERT ON session_control_sessions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 415,
    "name": "status_source_session_control_sessions_update",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_source_session_control_sessions_update\n        AFTER UPDATE ON session_control_sessions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 416,
    "name": "status_source_session_control_sessions_delete",
    "type": "trigger",
    "table": "session_control_sessions",
    "sql": "CREATE TRIGGER status_source_session_control_sessions_delete\n        AFTER DELETE ON session_control_sessions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 417,
    "name": "status_source_status_observations_insert",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_source_status_observations_insert\n        AFTER INSERT ON status_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 418,
    "name": "status_source_status_observations_update",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_source_status_observations_update\n        AFTER UPDATE ON status_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 419,
    "name": "status_source_status_observations_delete",
    "type": "trigger",
    "table": "status_observations",
    "sql": "CREATE TRIGGER status_source_status_observations_delete\n        AFTER DELETE ON status_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 420,
    "name": "backup_rate",
    "type": "table",
    "table": "backup_rate",
    "sql": "CREATE TABLE backup_rate (\n  bucket TEXT PRIMARY KEY,\n  count INTEGER NOT NULL,\n  expires INTEGER NOT NULL\n)"
  },
  {
    "captured_rowid": 422,
    "name": "intake_worktrees",
    "type": "table",
    "table": "intake_worktrees",
    "sql": "CREATE TABLE intake_worktrees (\n      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, machine TEXT NOT NULL, path TEXT NOT NULL, branch TEXT,\n      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, session_ids TEXT NOT NULL DEFAULT '[]',\n      origin TEXT NOT NULL, state TEXT NOT NULL, status_json TEXT, status_at INTEGER, last_remove_json TEXT)"
  },
  {
    "captured_rowid": 424,
    "name": "intake_worktrees_task",
    "type": "index",
    "table": "intake_worktrees",
    "sql": "CREATE INDEX intake_worktrees_task ON intake_worktrees(task_id, created_at)"
  },
  {
    "captured_rowid": 425,
    "name": "intake_worktrees_path",
    "type": "index",
    "table": "intake_worktrees",
    "sql": "CREATE INDEX intake_worktrees_path ON intake_worktrees(machine, path)"
  },
  {
    "captured_rowid": 426,
    "name": "intake_worktrees_active_owner",
    "type": "index",
    "table": "intake_worktrees",
    "sql": "CREATE UNIQUE INDEX intake_worktrees_active_owner ON intake_worktrees(machine, path) WHERE state = 'active'"
  },
  {
    "captured_rowid": 427,
    "name": "intake_worktree_commands",
    "type": "table",
    "table": "intake_worktree_commands",
    "sql": "CREATE TABLE intake_worktree_commands (\n      id TEXT PRIMARY KEY, task_id TEXT NOT NULL, worktree_id TEXT NOT NULL, machine TEXT NOT NULL, kind TEXT NOT NULL,\n      state TEXT NOT NULL, actor TEXT NOT NULL, created_at INTEGER NOT NULL, claimed_at INTEGER, finished_at INTEGER, result_json TEXT)"
  },
  {
    "captured_rowid": 429,
    "name": "intake_worktree_commands_machine",
    "type": "index",
    "table": "intake_worktree_commands",
    "sql": "CREATE INDEX intake_worktree_commands_machine ON intake_worktree_commands(machine, state, created_at)"
  },
  {
    "captured_rowid": 430,
    "name": "intake_worktree_commands_worktree",
    "type": "index",
    "table": "intake_worktree_commands",
    "sql": "CREATE INDEX intake_worktree_commands_worktree ON intake_worktree_commands(worktree_id, created_at)"
  },
  {
    "captured_rowid": 431,
    "name": "resource_throttles",
    "type": "table",
    "table": "resource_throttles",
    "sql": "CREATE TABLE resource_throttles (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, machine_id TEXT NOT NULL, native_id TEXT NOT NULL, intake_id TEXT, action TEXT NOT NULL, cap_cores REAL, sustain_s REAL, cpu_max TEXT, outcome TEXT, at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 433,
    "name": "resource_throttles_native",
    "type": "index",
    "table": "resource_throttles",
    "sql": "CREATE INDEX resource_throttles_native ON resource_throttles(machine_id,native_id,session_id)"
  },
  {
    "captured_rowid": 434,
    "name": "webhook_routes",
    "type": "table",
    "table": "webhook_routes",
    "sql": "CREATE TABLE webhook_routes (\n  id TEXT PRIMARY KEY,\n  name TEXT UNIQUE NOT NULL,\n  target_label TEXT NOT NULL,\n  template TEXT,\n  auth_mode TEXT NOT NULL DEFAULT 'token',\n  secret_storage TEXT NOT NULL,\n  rate_limit_per_min INTEGER NOT NULL DEFAULT 30,\n  max_body_bytes INTEGER NOT NULL DEFAULT 65536,\n  enabled INTEGER NOT NULL DEFAULT 1,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL\n, prompt_id TEXT, prompt_vars TEXT)"
  },
  {
    "captured_rowid": 437,
    "name": "webhook_rate_buckets",
    "type": "table",
    "table": "webhook_rate_buckets",
    "sql": "CREATE TABLE webhook_rate_buckets (\n  webhook_id TEXT NOT NULL,\n  bucket INTEGER NOT NULL,\n  count INTEGER NOT NULL,\n  PRIMARY KEY (webhook_id, bucket)\n)"
  },
  {
    "captured_rowid": 439,
    "name": "session_deliveries",
    "type": "table",
    "table": "session_deliveries",
    "sql": "CREATE TABLE session_deliveries (\n  id TEXT PRIMARY KEY,\n  source TEXT NOT NULL,\n  source_id TEXT NOT NULL,\n  delivery_key TEXT NOT NULL,\n  target_label TEXT NOT NULL,\n  body TEXT NOT NULL,\n  status TEXT NOT NULL,\n  session_id TEXT,\n  command_id TEXT,\n  attempts INTEGER NOT NULL DEFAULT 0,\n  next_attempt_at TEXT,\n  last_error TEXT,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL,\n  delivered_at TEXT,\n  UNIQUE(source_id, delivery_key)\n)"
  },
  {
    "captured_rowid": 442,
    "name": "session_deliveries_due",
    "type": "index",
    "table": "session_deliveries",
    "sql": "CREATE INDEX session_deliveries_due ON session_deliveries(status, next_attempt_at)"
  },
  {
    "captured_rowid": 443,
    "name": "session_deliveries_recent",
    "type": "index",
    "table": "session_deliveries",
    "sql": "CREATE INDEX session_deliveries_recent ON session_deliveries(source_id, created_at)"
  },
  {
    "captured_rowid": 444,
    "name": "banked_reset_grants",
    "type": "table",
    "table": "banked_reset_grants",
    "sql": "CREATE TABLE banked_reset_grants (\n      id TEXT PRIMARY KEY, account TEXT NOT NULL, provider TEXT NOT NULL, label TEXT NOT NULL,\n      expires_at INTEGER NOT NULL, source TEXT NOT NULL, active INTEGER NOT NULL,\n      observed_at INTEGER NOT NULL, recorded_by TEXT)"
  },
  {
    "captured_rowid": 446,
    "name": "banked_reset_notices",
    "type": "table",
    "table": "banked_reset_notices",
    "sql": "CREATE TABLE banked_reset_notices (\n      grant_id TEXT NOT NULL, threshold TEXT NOT NULL, alert_subject TEXT NOT NULL,\n      created_at INTEGER NOT NULL, PRIMARY KEY(grant_id,threshold))"
  },
  {
    "captured_rowid": 448,
    "name": "banked_reset_accounts",
    "type": "table",
    "table": "banked_reset_accounts",
    "sql": "CREATE TABLE banked_reset_accounts (\n      account TEXT PRIMARY KEY, observed_at INTEGER NOT NULL, source TEXT NOT NULL)"
  },
  {
    "captured_rowid": 450,
    "name": "usage_quota_history",
    "type": "table",
    "table": "usage_quota_history",
    "sql": "CREATE TABLE usage_quota_history (\n    account TEXT NOT NULL, engine TEXT NOT NULL, provider TEXT NOT NULL,\n    observed_at_ms INTEGER NOT NULL, quota_json TEXT NOT NULL,\n    PRIMARY KEY(account,engine,provider,observed_at_ms))"
  },
  {
    "captured_rowid": 452,
    "name": "usage_quota_history_time",
    "type": "index",
    "table": "usage_quota_history",
    "sql": "CREATE INDEX usage_quota_history_time ON usage_quota_history(observed_at_ms)"
  },
  {
    "captured_rowid": 453,
    "name": "session_adoption_intents",
    "type": "table",
    "table": "session_adoption_intents",
    "sql": "CREATE TABLE session_adoption_intents (\n  id TEXT PRIMARY KEY, machine_id TEXT NOT NULL, native_id TEXT NOT NULL,\n  native_json TEXT NOT NULL, actor TEXT NOT NULL, owner_token_id TEXT,\n  intake_id TEXT, state TEXT NOT NULL, created_at TEXT NOT NULL,\n  UNIQUE(machine_id,native_id)\n)"
  },
  {
    "captured_rowid": 456,
    "name": "session_worktree_episode_notes",
    "type": "table",
    "table": "session_worktree_episode_notes",
    "sql": "CREATE TABLE session_worktree_episode_notes (subject TEXT NOT NULL, generation INTEGER NOT NULL, intake_id TEXT NOT NULL, PRIMARY KEY(subject, generation, intake_id))"
  },
  {
    "captured_rowid": 458,
    "name": "session_worktree_episodes",
    "type": "table",
    "table": "session_worktree_episodes",
    "sql": "CREATE TABLE session_worktree_episodes (subject TEXT PRIMARY KEY, active INTEGER NOT NULL, generation INTEGER NOT NULL, completed INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 460,
    "name": "resource_catalog_machines_insert",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER resource_catalog_machines_insert AFTER INSERT ON machines \n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 461,
    "name": "resource_catalog_machines_update",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER resource_catalog_machines_update AFTER UPDATE ON machines WHEN OLD.hostname IS NOT NEW.hostname OR OLD.id IS NOT NEW.id\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 462,
    "name": "resource_catalog_machines_delete",
    "type": "trigger",
    "table": "machines",
    "sql": "CREATE TRIGGER resource_catalog_machines_delete AFTER DELETE ON machines \n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))),structure_revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 463,
    "name": "recipient_cursors",
    "type": "table",
    "table": "recipient_cursors",
    "sql": "CREATE TABLE recipient_cursors (actor TEXT PRIMARY KEY, last_seq INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 465,
    "name": "message_events",
    "type": "table",
    "table": "message_events",
    "sql": "CREATE TABLE message_events (id INTEGER PRIMARY KEY AUTOINCREMENT, message_id TEXT NOT NULL,\n    event TEXT NOT NULL, actor TEXT, machine_id TEXT, at INTEGER NOT NULL, detail_json TEXT)"
  },
  {
    "captured_rowid": 466,
    "name": "message_events_message",
    "type": "index",
    "table": "message_events",
    "sql": "CREATE INDEX message_events_message ON message_events(message_id, id)"
  },
  {
    "captured_rowid": 467,
    "name": "message_events_at",
    "type": "index",
    "table": "message_events",
    "sql": "CREATE INDEX message_events_at ON message_events(at)"
  },
  {
    "captured_rowid": 468,
    "name": "host_release_targets",
    "type": "table",
    "table": "host_release_targets",
    "sql": "CREATE TABLE host_release_targets (machine_id TEXT PRIMARY KEY, sha TEXT, set_by TEXT, set_at TEXT, current_sha TEXT, state TEXT, error TEXT, reported_at TEXT, autostart_state TEXT)"
  },
  {
    "captured_rowid": 470,
    "name": "credentials_registry_actor",
    "type": "index",
    "table": "credentials",
    "sql": "CREATE INDEX credentials_registry_actor ON credentials(registry_actor)"
  },
  {
    "captured_rowid": 471,
    "name": "messaging_settings",
    "type": "table",
    "table": "messaging_settings",
    "sql": "CREATE TABLE messaging_settings (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 472,
    "name": "named_agent_delegates",
    "type": "table",
    "table": "named_agent_delegates",
    "sql": "CREATE TABLE named_agent_delegates (agent TEXT NOT NULL, client TEXT NOT NULL, created_by TEXT, created_at TEXT, PRIMARY KEY(agent, client))"
  },
  {
    "captured_rowid": 474,
    "name": "session_audit_meta",
    "type": "table",
    "table": "session_audit_meta",
    "sql": "CREATE TABLE session_audit_meta (id INTEGER PRIMARY KEY CHECK(id=1), last_tick INTEGER, last_source TEXT, last_result TEXT)"
  },
  {
    "captured_rowid": 475,
    "name": "session_audit_runs",
    "type": "table",
    "table": "session_audit_runs",
    "sql": "CREATE TABLE session_audit_runs (\n      id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, source TEXT NOT NULL,\n      scanned INTEGER NOT NULL, counts_json TEXT NOT NULL, alert TEXT)"
  },
  {
    "captured_rowid": 476,
    "name": "session_audit_decisions",
    "type": "table",
    "table": "session_audit_decisions",
    "sql": "CREATE TABLE session_audit_decisions (\n      id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER NOT NULL, at TEXT NOT NULL,\n      session_id TEXT, machine_id TEXT, native_id TEXT, intake TEXT, engine TEXT, mode TEXT,\n      idle_s INTEGER, rule TEXT NOT NULL, action TEXT NOT NULL, detail TEXT,\n      action_key TEXT, outcome TEXT, outcome_at TEXT,\n      features_json TEXT NOT NULL, screen_tail TEXT, dispatched INTEGER NOT NULL DEFAULT 1)"
  },
  {
    "captured_rowid": 477,
    "name": "session_audit_decisions_session",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_session ON session_audit_decisions(session_id, at)"
  },
  {
    "captured_rowid": 478,
    "name": "session_audit_decisions_run",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_run ON session_audit_decisions(run_id)"
  },
  {
    "captured_rowid": 479,
    "name": "named_agents",
    "type": "table",
    "table": "named_agents",
    "sql": "CREATE TABLE named_agents (name TEXT PRIMARY KEY, project TEXT, display TEXT NOT NULL,\n    credential_family TEXT, delegates_json TEXT NOT NULL DEFAULT '[]', forward_session_id TEXT,\n    created_by TEXT NOT NULL, created_at TEXT NOT NULL, disabled_at TEXT)"
  },
  {
    "captured_rowid": 481,
    "name": "named_agent_registry_insert",
    "type": "trigger",
    "table": "agents",
    "sql": "CREATE TRIGGER named_agent_registry_insert AFTER INSERT ON agents WHEN NEW.project = 'agent' BEGIN\n    INSERT OR IGNORE INTO named_agents (name, display, created_by, created_at) VALUES (NEW.tag, NEW.tag, 'hub:registry', COALESCE(NEW.registered_at, ''));\n    END"
  },
  {
    "captured_rowid": 482,
    "name": "messages_recipient_seq",
    "type": "index",
    "table": "messages",
    "sql": "CREATE INDEX messages_recipient_seq ON messages(to_actor, recipient_seq)"
  },
  {
    "captured_rowid": 483,
    "name": "messages_delivery_deadline",
    "type": "index",
    "table": "messages",
    "sql": "CREATE INDEX messages_delivery_deadline ON messages(state, deliver_by)"
  },
  {
    "captured_rowid": 484,
    "name": "message_thread_status",
    "type": "table",
    "table": "message_thread_status",
    "sql": "CREATE TABLE message_thread_status (thread_id TEXT NOT NULL, reason TEXT NOT NULL, message_id TEXT NOT NULL, PRIMARY KEY(thread_id, reason))"
  },
  {
    "captured_rowid": 486,
    "name": "session_message_order",
    "type": "index",
    "table": "session_control_commands",
    "sql": "CREATE INDEX session_message_order ON session_control_commands(session_id, kind, recipient_seq)"
  },
  {
    "captured_rowid": 487,
    "name": "status_source_fleet_launches_insert",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_source_fleet_launches_insert\n        AFTER INSERT ON fleet_launches BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 488,
    "name": "status_source_fleet_launches_update",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_source_fleet_launches_update\n        AFTER UPDATE ON fleet_launches BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 489,
    "name": "status_source_fleet_launches_delete",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER status_source_fleet_launches_delete\n        AFTER DELETE ON fleet_launches BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 490,
    "name": "status_source_session_control_protections_insert",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_source_session_control_protections_insert\n        AFTER INSERT ON session_control_protections BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 491,
    "name": "status_source_session_control_protections_update",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_source_session_control_protections_update\n        AFTER UPDATE ON session_control_protections BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 492,
    "name": "status_source_session_control_protections_delete",
    "type": "trigger",
    "table": "session_control_protections",
    "sql": "CREATE TRIGGER status_source_session_control_protections_delete\n        AFTER DELETE ON session_control_protections BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 493,
    "name": "status_source_session_lifecycle_observations_insert",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_observations_insert\n        AFTER INSERT ON session_lifecycle_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 494,
    "name": "status_source_session_lifecycle_observations_update",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_observations_update\n        AFTER UPDATE ON session_lifecycle_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 495,
    "name": "status_source_session_lifecycle_observations_delete",
    "type": "trigger",
    "table": "session_lifecycle_observations",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_observations_delete\n        AFTER DELETE ON session_lifecycle_observations BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 496,
    "name": "status_source_intake_task_projection_insert",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_source_intake_task_projection_insert\n        AFTER INSERT ON intake_task_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 497,
    "name": "status_source_intake_task_projection_update",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_source_intake_task_projection_update\n        AFTER UPDATE ON intake_task_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 498,
    "name": "status_source_intake_task_projection_delete",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER status_source_intake_task_projection_delete\n        AFTER DELETE ON intake_task_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 499,
    "name": "status_source_credentials_insert",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_source_credentials_insert\n        AFTER INSERT ON credentials BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 500,
    "name": "status_source_credentials_update",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_source_credentials_update\n        AFTER UPDATE OF id,kind,actor,subject,created_by_sub,family_id ON credentials BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 501,
    "name": "status_source_credentials_delete",
    "type": "trigger",
    "table": "credentials",
    "sql": "CREATE TRIGGER status_source_credentials_delete\n        AFTER DELETE ON credentials BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 502,
    "name": "status_source_lifecycle_request_projection_insert",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_source_lifecycle_request_projection_insert\n        AFTER INSERT ON lifecycle_request_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 503,
    "name": "status_source_lifecycle_request_projection_update",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_source_lifecycle_request_projection_update\n        AFTER UPDATE ON lifecycle_request_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 504,
    "name": "status_source_lifecycle_request_projection_delete",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_source_lifecycle_request_projection_delete\n        AFTER DELETE ON lifecycle_request_projection BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 505,
    "name": "status_source_session_lifecycle_decisions_insert",
    "type": "trigger",
    "table": "session_lifecycle_decisions",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_decisions_insert\n        AFTER INSERT ON session_lifecycle_decisions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 506,
    "name": "status_source_session_lifecycle_decisions_update",
    "type": "trigger",
    "table": "session_lifecycle_decisions",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_decisions_update\n        AFTER UPDATE ON session_lifecycle_decisions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 507,
    "name": "status_source_session_lifecycle_decisions_delete",
    "type": "trigger",
    "table": "session_lifecycle_decisions",
    "sql": "CREATE TRIGGER status_source_session_lifecycle_decisions_delete\n        AFTER DELETE ON session_lifecycle_decisions BEGIN UPDATE status_source_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 508,
    "name": "status_lifecycle_lifecycle_request_projection_insert",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_lifecycle_lifecycle_request_projection_insert\n          AFTER INSERT ON lifecycle_request_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 509,
    "name": "status_lifecycle_lifecycle_request_projection_update",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_lifecycle_lifecycle_request_projection_update\n          AFTER UPDATE ON lifecycle_request_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 510,
    "name": "status_lifecycle_lifecycle_request_projection_delete",
    "type": "trigger",
    "table": "lifecycle_request_projection",
    "sql": "CREATE TRIGGER status_lifecycle_lifecycle_request_projection_delete\n          AFTER DELETE ON lifecycle_request_projection BEGIN\n          UPDATE status_lifecycle_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  },
  {
    "captured_rowid": 511,
    "name": "intake_audit_status_v2",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_audit_status_v2 AFTER UPDATE ON intake_tasks\n        WHEN json_extract(OLD.data,'$.status') IS NOT json_extract(NEW.data,'$.status') AND (json_extract(NEW.data,'$.status') IN ('done','cancelled') OR json_extract(OLD.data,'$.status') IN ('done','cancelled')) BEGIN\n        INSERT INTO intake_audit(task_id,project,kind,actor,captured_at,from_status,to_status,reason)\n        VALUES(NEW.id,json_extract(NEW.data,'$.project'),CASE json_extract(NEW.data,'$.status') WHEN 'done' THEN CASE json_extract(NEW.data,'$.outcome') WHEN 'closed_unverified' THEN 'close_unverified' ELSE 'close' END WHEN 'cancelled' THEN 'cancel' ELSE 'reopen' END,\n          COALESCE(json_extract(NEW.data,'$.lifecycle_actor'),json_extract(NEW.data,'$.history[#-1].actor'),'hub:legacy'),COALESCE(json_extract(NEW.data,'$.updated_at'),CAST(strftime('%s','now') AS INTEGER)*1000),json_extract(OLD.data,'$.status'),json_extract(NEW.data,'$.status'),COALESCE(json_extract(NEW.data,'$.lifecycle_reason'),'legacy lifecycle transition')); END"
  },
  {
    "captured_rowid": 512,
    "name": "session_control_wakes",
    "type": "table",
    "table": "session_control_wakes",
    "sql": "CREATE TABLE session_control_wakes (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL, wake_at_ms INTEGER NOT NULL, wake_at TEXT NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, command_id TEXT, intake_task_id TEXT, owner_token_id TEXT, owner_family TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, delivered_at TEXT, acked_at TEXT, UNIQUE(session_id, actor, request_key))"
  },
  {
    "captured_rowid": 515,
    "name": "session_control_wakes_due",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX session_control_wakes_due ON session_control_wakes(state, wake_at_ms)"
  },
  {
    "captured_rowid": 516,
    "name": "session_control_wakes_session",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX session_control_wakes_session ON session_control_wakes(session_id, state)"
  },
  {
    "captured_rowid": 517,
    "name": "session_control_wake_alerts",
    "type": "table",
    "table": "session_control_wake_alerts",
    "sql": "CREATE TABLE session_control_wake_alerts (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, wake_id TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL, intake_task_id TEXT, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 519,
    "name": "session_control_wake_alerts_session",
    "type": "index",
    "table": "session_control_wake_alerts",
    "sql": "CREATE INDEX session_control_wake_alerts_session ON session_control_wake_alerts(session_id, created_at)"
  },
  {
    "captured_rowid": 520,
    "name": "session_idle_episodes",
    "type": "table",
    "table": "session_idle_episodes",
    "sql": "CREATE TABLE session_idle_episodes(session_id TEXT PRIMARY KEY,\n      episode TEXT NOT NULL, detected_at INTEGER NOT NULL, active INTEGER NOT NULL,\n      nudges_json TEXT NOT NULL, last_nudge_at INTEGER, replacement_key TEXT, replacement_id TEXT, binding_identity TEXT)"
  },
  {
    "captured_rowid": 522,
    "name": "session_control_state",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_state ON session_control_sessions(state)"
  },
  {
    "captured_rowid": 523,
    "name": "engine_credentials",
    "type": "table",
    "table": "engine_credentials",
    "sql": "CREATE TABLE engine_credentials (label TEXT PRIMARY KEY, provider TEXT NOT NULL, generation INTEGER NOT NULL, ciphertext TEXT NOT NULL, freshness_ms INTEGER, access_expires_ms INTEGER, status TEXT NOT NULL, status_reason TEXT, updated_by TEXT NOT NULL, updated_at INTEGER NOT NULL, refresh_attempt_at INTEGER, quota_attempt_at INTEGER)"
  },
  {
    "captured_rowid": 525,
    "name": "engine_credential_rejections",
    "type": "table",
    "table": "engine_credential_rejections",
    "sql": "CREATE TABLE engine_credential_rejections (machine_id TEXT NOT NULL, label TEXT NOT NULL, generation INTEGER NOT NULL, reason TEXT NOT NULL, observed_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))"
  },
  {
    "captured_rowid": 527,
    "name": "engine_credential_logins",
    "type": "table",
    "table": "engine_credential_logins",
    "sql": "CREATE TABLE engine_credential_logins (id TEXT PRIMARY KEY, label TEXT NOT NULL, provider TEXT NOT NULL, sealed TEXT NOT NULL, expires_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 529,
    "name": "intake_human_alerts",
    "type": "table",
    "table": "intake_human_alerts",
    "sql": "CREATE TABLE intake_human_alerts(task_id TEXT NOT NULL, reason TEXT NOT NULL, alerted_at INTEGER NOT NULL, PRIMARY KEY(task_id, reason))"
  },
  {
    "captured_rowid": 531,
    "name": "intake_triage_meta",
    "type": "table",
    "table": "intake_triage_meta",
    "sql": "CREATE TABLE intake_triage_meta(id INTEGER PRIMARY KEY CHECK(id=1),last_tick INTEGER,last_source TEXT,last_result TEXT, last_session TEXT, last_alert TEXT)"
  },
  {
    "captured_rowid": 532,
    "name": "intake_triage_requests",
    "type": "table",
    "table": "intake_triage_requests",
    "sql": "CREATE TABLE intake_triage_requests(task_id TEXT PRIMARY KEY, request_key TEXT NOT NULL,\n      session_id TEXT, state TEXT NOT NULL, requested_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 534,
    "name": "intake_verifier_meta",
    "type": "table",
    "table": "intake_verifier_meta",
    "sql": "CREATE TABLE intake_verifier_meta(id INTEGER PRIMARY KEY CHECK(id=1),enabled_at INTEGER NOT NULL,last_tick INTEGER,last_result TEXT)"
  },
  {
    "captured_rowid": 535,
    "name": "intake_verifications",
    "type": "table",
    "table": "intake_verifications",
    "sql": "CREATE TABLE intake_verifications(task_id TEXT NOT NULL, claim_id TEXT NOT NULL, mode TEXT NOT NULL,\n      reason TEXT NOT NULL, state TEXT NOT NULL, engine TEXT, session_id TEXT, request_key TEXT, attempts INTEGER NOT NULL DEFAULT 0,\n      detail TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, PRIMARY KEY(task_id, claim_id))"
  },
  {
    "captured_rowid": 537,
    "name": "engine_credential_leases",
    "type": "table",
    "table": "engine_credential_leases",
    "sql": "CREATE TABLE engine_credential_leases (label TEXT PRIMARY KEY, machine_id TEXT NOT NULL, generation INTEGER NOT NULL, expires_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 539,
    "name": "grok_policy",
    "type": "table",
    "table": "grok_policy",
    "sql": "CREATE TABLE grok_policy (singleton INTEGER PRIMARY KEY CHECK(singleton=1), mode TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, updated_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 540,
    "name": "grok_policy_audit",
    "type": "table",
    "table": "grok_policy_audit",
    "sql": "CREATE TABLE grok_policy_audit (id TEXT PRIMARY KEY, previous_mode TEXT NOT NULL, mode TEXT NOT NULL, reason TEXT NOT NULL, actor TEXT NOT NULL, created_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 542,
    "name": "engine_migrations",
    "type": "table",
    "table": "engine_migrations",
    "sql": "CREATE TABLE engine_migrations (name TEXT PRIMARY KEY, applied_at INTEGER NOT NULL, detail TEXT NOT NULL)"
  },
  {
    "captured_rowid": 544,
    "name": "session_observation_intake_meta",
    "type": "table",
    "table": "session_observation_intake_meta",
    "sql": "CREATE TABLE session_observation_intake_meta\n    (id INTEGER PRIMARY KEY CHECK(id=1), cursor TEXT NOT NULL, complete INTEGER NOT NULL, revision INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 545,
    "name": "session_observation_intakes",
    "type": "table",
    "table": "session_observation_intakes",
    "sql": "CREATE TABLE session_observation_intakes(task_id TEXT PRIMARY KEY, events TEXT NOT NULL)"
  },
  {
    "captured_rowid": 550,
    "name": "intake_watchdog_open",
    "type": "index",
    "table": "intake_task_projection",
    "sql": "CREATE INDEX intake_watchdog_open ON intake_task_projection(id) WHERE json_extract(data,'$.status') IN ('open','in_progress','blocked','pending_approval') AND json_extract(data,'$.archived_at') IS NULL"
  },
  {
    "captured_rowid": 551,
    "name": "intake_queue_movement",
    "type": "table",
    "table": "intake_queue_movement",
    "sql": "CREATE TABLE intake_queue_movement(seq INTEGER PRIMARY KEY AUTOINCREMENT,task_id TEXT NOT NULL,state TEXT NOT NULL,reason TEXT NOT NULL,at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 552,
    "name": "intake_queue_movement_task",
    "type": "index",
    "table": "intake_queue_movement",
    "sql": "CREATE INDEX intake_queue_movement_task ON intake_queue_movement(task_id,seq)"
  },
  {
    "captured_rowid": 553,
    "name": "intake_queue_cursor",
    "type": "table",
    "table": "intake_queue_cursor",
    "sql": "CREATE TABLE intake_queue_cursor(id INTEGER PRIMARY KEY CHECK(id=1),after_id TEXT NOT NULL,forgotten_seen INTEGER NOT NULL DEFAULT 0, sweep_after TEXT)"
  },
  {
    "captured_rowid": 554,
    "name": "intake_queue_digest",
    "type": "table",
    "table": "intake_queue_digest",
    "sql": "CREATE TABLE intake_queue_digest(task_id TEXT PRIMARY KEY,day TEXT NOT NULL)"
  },
  {
    "captured_rowid": 556,
    "name": "intake_queue_budget_meta",
    "type": "table",
    "table": "intake_queue_budget_meta",
    "sql": "CREATE TABLE intake_queue_budget_meta(id INTEGER PRIMARY KEY CHECK(id=1),overflow_ticks INTEGER NOT NULL DEFAULT 0,last_limited_at INTEGER)"
  },
  {
    "captured_rowid": 557,
    "name": "intake_queue_budget_alerts",
    "type": "table",
    "table": "intake_queue_budget_alerts",
    "sql": "CREATE TABLE intake_queue_budget_alerts(batch_key TEXT PRIMARY KEY,alerted_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 559,
    "name": "intake_queue_admissions",
    "type": "table",
    "table": "intake_queue_admissions",
    "sql": "CREATE TABLE intake_queue_admissions(task_id TEXT PRIMARY KEY,project TEXT NOT NULL,request_key TEXT NOT NULL,session_id TEXT)"
  },
  {
    "captured_rowid": 561,
    "name": "intake_queue_owned_active",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX intake_queue_owned_active ON session_control_sessions(state) WHERE actor='hub:service:intake-watchdog' AND json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.with_goal')=1"
  },
  {
    "captured_rowid": 562,
    "name": "intake_queue_session_task",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX intake_queue_session_task ON session_control_sessions(json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.intake_task_id'))"
  },
  {
    "captured_rowid": 563,
    "name": "intake_queue_session_native",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX intake_queue_session_native ON session_control_sessions(machine_id,json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_id'))"
  },
  {
    "captured_rowid": 564,
    "name": "intake_queue_native_id",
    "type": "index",
    "table": "fleet_sessions",
    "sql": "CREATE INDEX intake_queue_native_id ON fleet_sessions(native_id)"
  },
  {
    "captured_rowid": 565,
    "name": "intake_queue_native_workspace",
    "type": "index",
    "table": "fleet_sessions",
    "sql": "CREATE INDEX intake_queue_native_workspace ON fleet_sessions(workspace) WHERE phase NOT IN ('exited','stopped','failed')"
  },
  {
    "captured_rowid": 566,
    "name": "intake_queue_claim_task",
    "type": "index",
    "table": "claims",
    "sql": "CREATE INDEX intake_queue_claim_task ON claims(work_key,claim_source,released_at)"
  },
  {
    "captured_rowid": 567,
    "name": "intake_queue_escalations",
    "type": "table",
    "table": "intake_queue_escalations",
    "sql": "CREATE TABLE intake_queue_escalations(task_id TEXT NOT NULL,reason TEXT NOT NULL,alert_sent INTEGER NOT NULL DEFAULT 0,note_sent INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(task_id,reason))"
  },
  {
    "captured_rowid": 569,
    "name": "intake_queue_wakes_session",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX intake_queue_wakes_session ON session_control_wakes(session_id,state,wake_at_ms)"
  },
  {
    "captured_rowid": 570,
    "name": "launcher_release_targets",
    "type": "table",
    "table": "launcher_release_targets",
    "sql": "CREATE TABLE launcher_release_targets (machine_id TEXT PRIMARY KEY, sha TEXT, set_by TEXT, set_at TEXT, current_sha TEXT, state TEXT, error TEXT, reported_at TEXT, root TEXT)"
  },
  {
    "captured_rowid": 572,
    "name": "ops_liveness",
    "type": "table",
    "table": "ops_liveness",
    "sql": "CREATE TABLE ops_liveness(role TEXT PRIMARY KEY, tick_at INTEGER, tick_summary TEXT,\n      watchdog_at INTEGER, watchdog_state TEXT, recovery_failures INTEGER NOT NULL DEFAULT 0, detail TEXT, updated_at INTEGER NOT NULL, first_seen_at INTEGER)"
  },
  {
    "captured_rowid": 574,
    "name": "ops_backlog_idle",
    "type": "table",
    "table": "ops_backlog_idle",
    "sql": "CREATE TABLE ops_backlog_idle(id INTEGER PRIMARY KEY AUTOINCREMENT, started_at INTEGER NOT NULL,\n      resolved_at INTEGER, open_with_ac INTEGER NOT NULL, reason_json TEXT NOT NULL, kicks INTEGER NOT NULL DEFAULT 0,\n      failed_kicks INTEGER NOT NULL DEFAULT 0, last_kick_at INTEGER, last_kick_json TEXT)"
  },
  {
    "captured_rowid": 575,
    "name": "ops_backlog_idle_open",
    "type": "index",
    "table": "ops_backlog_idle",
    "sql": "CREATE INDEX ops_backlog_idle_open ON ops_backlog_idle(resolved_at, id)"
  },
  {
    "captured_rowid": 576,
    "name": "ops_liveness_meta",
    "type": "table",
    "table": "ops_liveness_meta",
    "sql": "CREATE TABLE ops_liveness_meta(id INTEGER PRIMARY KEY CHECK(id=1), last_eval INTEGER, expected_since INTEGER)"
  },
  {
    "captured_rowid": 577,
    "name": "intake_note_archive",
    "type": "table",
    "table": "intake_note_archive",
    "sql": "CREATE TABLE intake_note_archive(id TEXT PRIMARY KEY,task_id TEXT NOT NULL,seq INTEGER NOT NULL,data TEXT NOT NULL,UNIQUE(task_id,seq))"
  },
  {
    "captured_rowid": 580,
    "name": "intake_note_archive_no_update",
    "type": "trigger",
    "table": "intake_note_archive",
    "sql": "CREATE TRIGGER intake_note_archive_no_update BEFORE UPDATE ON intake_note_archive BEGIN SELECT RAISE(ABORT,'append_only_note_archive'); END"
  },
  {
    "captured_rowid": 581,
    "name": "intake_note_archive_no_delete",
    "type": "trigger",
    "table": "intake_note_archive",
    "sql": "CREATE TRIGGER intake_note_archive_no_delete BEFORE DELETE ON intake_note_archive BEGIN SELECT RAISE(ABORT,'append_only_note_archive'); END"
  },
  {
    "captured_rowid": 582,
    "name": "intake_storage_note_archive_insert",
    "type": "trigger",
    "table": "intake_note_archive",
    "sql": "CREATE TRIGGER intake_storage_note_archive_insert AFTER INSERT ON intake_note_archive BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB))),hot=hot+(0),events=events+(0),original_requests=original_requests+(0),count=count+(1) WHERE kind='note_archive'; END"
  },
  {
    "captured_rowid": 583,
    "name": "intake_storage_note_archive_update",
    "type": "trigger",
    "table": "intake_note_archive",
    "sql": "CREATE TRIGGER intake_storage_note_archive_update AFTER UPDATE ON intake_note_archive BEGIN UPDATE intake_storage_totals SET bytes=bytes+(length(CAST(NEW.data AS BLOB))+length(CAST(NEW.id AS BLOB)))-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot+(0)-(0),events=events+(0)-(0),original_requests=original_requests+(0)-(0),count=count+(1)-(1) WHERE kind='note_archive'; END"
  },
  {
    "captured_rowid": 584,
    "name": "intake_storage_note_archive_delete",
    "type": "trigger",
    "table": "intake_note_archive",
    "sql": "CREATE TRIGGER intake_storage_note_archive_delete AFTER DELETE ON intake_note_archive BEGIN UPDATE intake_storage_totals SET bytes=bytes-(length(CAST(OLD.data AS BLOB))+length(CAST(OLD.id AS BLOB))),hot=hot-(0),events=events-(0),original_requests=original_requests-(0),count=count-(1) WHERE kind='note_archive'; END"
  },
  {
    "captured_rowid": 585,
    "name": "host_failover",
    "type": "table",
    "table": "host_failover",
    "sql": "CREATE TABLE host_failover(machine_id TEXT PRIMARY KEY, state TEXT NOT NULL,\n      dead_since INTEGER NOT NULL, marked_at INTEGER NOT NULL, returned_at INTEGER)"
  },
  {
    "captured_rowid": 587,
    "name": "host_failover_sessions",
    "type": "table",
    "table": "host_failover_sessions",
    "sql": "CREATE TABLE host_failover_sessions(session_id TEXT PRIMARY KEY, machine_id TEXT NOT NULL,\n      intake_task_id TEXT NOT NULL, replacement_key TEXT NOT NULL, replacement_id TEXT, state TEXT NOT NULL,\n      fence_key TEXT, fence_command_id TEXT, created_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 589,
    "name": "session_control_held_cursor",
    "type": "table",
    "table": "session_control_held_cursor",
    "sql": "CREATE TABLE session_control_held_cursor (id INTEGER PRIMARY KEY CHECK(id=1), created_at TEXT NOT NULL, session_id TEXT NOT NULL)"
  },
  {
    "captured_rowid": 590,
    "name": "session_control_held_unplaced",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_held_unplaced ON session_control_sessions(created_at, id) WHERE state = 'held' AND fleet_launch_id IS NULL AND binding_json IS NULL"
  },
  {
    "captured_rowid": 591,
    "name": "session_control_unclaimed_stops",
    "type": "index",
    "table": "session_control_commands",
    "sql": "CREATE INDEX session_control_unclaimed_stops ON session_control_commands(created_at) WHERE kind = 'stop' AND state IN ('recorded','transported') AND claim_token IS NULL"
  },
  {
    "captured_rowid": 592,
    "name": "intake_session_links",
    "type": "table",
    "table": "intake_session_links",
    "sql": "CREATE TABLE intake_session_links (\n  session_key TEXT NOT NULL, task_id TEXT NOT NULL, authority TEXT NOT NULL, PRIMARY KEY(session_key, task_id))"
  },
  {
    "captured_rowid": 596,
    "name": "intake_session_links_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_session_links_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_session_links WHERE task_id=OLD.id; END"
  },
  {
    "captured_rowid": 597,
    "name": "worker_created_intakes",
    "type": "table",
    "table": "worker_created_intakes",
    "sql": "CREATE TABLE worker_created_intakes (\n  task_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, launch_id TEXT NOT NULL,\n  parent_task_id TEXT NOT NULL, created_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 599,
    "name": "worker_created_intakes_session",
    "type": "index",
    "table": "worker_created_intakes",
    "sql": "CREATE INDEX worker_created_intakes_session ON worker_created_intakes(session_id, task_id)"
  },
  {
    "captured_rowid": 600,
    "name": "intake_link_refs",
    "type": "table",
    "table": "intake_link_refs",
    "sql": "CREATE TABLE intake_link_refs (ref TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(ref, task_id)) WITHOUT ROWID"
  },
  {
    "captured_rowid": 601,
    "name": "intake_link_refs_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_link_refs_insert AFTER INSERT ON intake_tasks BEGIN\n    INSERT INTO intake_link_refs(ref,task_id) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.native_id')='text' ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 602,
    "name": "intake_link_refs_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_link_refs_update AFTER UPDATE ON intake_tasks BEGIN\n    INSERT INTO intake_link_refs(ref,task_id) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.kind')='link' AND json_type(e.value,'$.native_id')='text' ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 603,
    "name": "intake_link_refs_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_link_refs_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_link_refs WHERE task_id=OLD.id; END"
  },
  {
    "captured_rowid": 604,
    "name": "session_control_machine_created",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_machine_created ON session_control_sessions(machine_id, created_at, id)"
  },
  {
    "captured_rowid": 605,
    "name": "session_control_machine_created_state",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_machine_created_state ON session_control_sessions(machine_id, created_at, id, state)"
  },
  {
    "captured_rowid": 606,
    "name": "session_control_machine_state",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_machine_state ON session_control_sessions(machine_id, state, created_at)"
  },
  {
    "captured_rowid": 607,
    "name": "intake_session_links_projection_insert",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER intake_session_links_projection_insert AFTER INSERT ON intake_task_projection WHEN json_valid(NEW.links) BEGIN\n    INSERT INTO intake_session_links(session_key,task_id,authority) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id, json_extract(e.value,'$.authority') FROM json_each(NEW.links) e\n   WHERE 1 AND json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified') AND json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id, json_extract(e.value,'$.authority') FROM json_each(NEW.links) e\n   WHERE 1 AND json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified') AND json_type(e.value,'$.native_id')='text'\n    ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 608,
    "name": "intake_session_links_projection_update",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER intake_session_links_projection_update AFTER UPDATE OF links ON intake_task_projection WHEN NEW.links IS NOT OLD.links AND json_valid(NEW.links) BEGIN\n    INSERT INTO intake_session_links(session_key,task_id,authority) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id, json_extract(e.value,'$.authority') FROM json_each(NEW.links) e\n   WHERE 1 AND json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified') AND json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id, json_extract(e.value,'$.authority') FROM json_each(NEW.links) e\n   WHERE 1 AND json_extract(e.value,'$.kind')='link' AND json_extract(e.value,'$.authority') IN ('hub_launch_verified','host_census_verified') AND json_type(e.value,'$.native_id')='text'\n    ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 609,
    "name": "messages_expires_at",
    "type": "index",
    "table": "messages",
    "sql": "CREATE INDEX messages_expires_at ON messages(expires_at)"
  },
  {
    "captured_rowid": 610,
    "name": "claims_expires_at",
    "type": "index",
    "table": "claims",
    "sql": "CREATE INDEX claims_expires_at ON claims(expires_at)"
  },
  {
    "captured_rowid": 611,
    "name": "rows_read_stats",
    "type": "table",
    "table": "rows_read_stats",
    "sql": "CREATE TABLE rows_read_stats (\n  slot INTEGER NOT NULL,\n  scope TEXT NOT NULL,\n  hour INTEGER NOT NULL,\n  calls INTEGER NOT NULL DEFAULT 0,\n  rows_read INTEGER NOT NULL DEFAULT 0,\n  rows_written INTEGER NOT NULL DEFAULT 0,\n  max_read INTEGER NOT NULL DEFAULT 0,\n  PRIMARY KEY (slot, scope)\n)"
  },
  {
    "captured_rowid": 613,
    "name": "rows_read_stats_hour",
    "type": "index",
    "table": "rows_read_stats",
    "sql": "CREATE INDEX rows_read_stats_hour ON rows_read_stats(hour)"
  },
  {
    "captured_rowid": 614,
    "name": "intake_projection_link_refs",
    "type": "table",
    "table": "intake_projection_link_refs",
    "sql": "CREATE TABLE intake_projection_link_refs (ref TEXT NOT NULL, task_id TEXT NOT NULL, PRIMARY KEY(ref, task_id)) WITHOUT ROWID"
  },
  {
    "captured_rowid": 615,
    "name": "intake_projection_link_refs_insert",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER intake_projection_link_refs_insert AFTER INSERT ON intake_task_projection WHEN json_valid(NEW.links) BEGIN\n    INSERT INTO intake_projection_link_refs(ref,task_id) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id FROM json_each(NEW.links) e WHERE json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id FROM json_each(NEW.links) e WHERE json_type(e.value,'$.native_id')='text' ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 616,
    "name": "intake_projection_link_refs_update",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER intake_projection_link_refs_update AFTER UPDATE OF links ON intake_task_projection WHEN NEW.links IS NOT OLD.links AND json_valid(NEW.links) BEGIN\n    INSERT INTO intake_projection_link_refs(ref,task_id) SELECT json_extract(e.value,'$.hub_session_id'), NEW.id FROM json_each(NEW.links) e WHERE json_type(e.value,'$.hub_session_id')='text' UNION SELECT json_extract(e.value,'$.native_id'), NEW.id FROM json_each(NEW.links) e WHERE json_type(e.value,'$.native_id')='text' ON CONFLICT DO NOTHING; END"
  },
  {
    "captured_rowid": 617,
    "name": "intake_projection_link_refs_delete",
    "type": "trigger",
    "table": "intake_task_projection",
    "sql": "CREATE TRIGGER intake_projection_link_refs_delete AFTER DELETE ON intake_task_projection BEGIN DELETE FROM intake_projection_link_refs WHERE task_id=OLD.id; END"
  },
  {
    "captured_rowid": 618,
    "name": "hub_init_stamps",
    "type": "table",
    "table": "hub_init_stamps",
    "sql": "CREATE TABLE hub_init_stamps (key TEXT PRIMARY KEY, at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 620,
    "name": "messages_session_pending",
    "type": "index",
    "table": "messages",
    "sql": "CREATE INDEX messages_session_pending ON messages(deliver_by, id) WHERE to_session_id IS NOT NULL AND state IN ('accepted','dispatched','uncertain')"
  },
  {
    "captured_rowid": 621,
    "name": "host_goals",
    "type": "table",
    "table": "host_goals",
    "sql": "CREATE TABLE host_goals(machine_id TEXT PRIMARY KEY, generation TEXT NOT NULL, engines_json TEXT NOT NULL, workspace TEXT NOT NULL, sha TEXT NOT NULL, set_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 623,
    "name": "host_goal_proofs",
    "type": "table",
    "table": "host_goal_proofs",
    "sql": "CREATE TABLE host_goal_proofs(generation TEXT NOT NULL,label TEXT NOT NULL,revision TEXT NOT NULL,launch_id TEXT NOT NULL,verified_at TEXT,native_id TEXT,PRIMARY KEY(generation,label,revision))"
  },
  {
    "captured_rowid": 625,
    "name": "host_goal_holds",
    "type": "table",
    "table": "host_goal_holds",
    "sql": "CREATE TABLE host_goal_holds(generation TEXT NOT NULL,label TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(generation,label))"
  },
  {
    "captured_rowid": 627,
    "name": "host_goal_proof_launch",
    "type": "index",
    "table": "host_goal_proofs",
    "sql": "CREATE INDEX host_goal_proof_launch ON host_goal_proofs(launch_id)"
  },
  {
    "captured_rowid": 628,
    "name": "host_goal_running_insert",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER host_goal_running_insert\n        AFTER INSERT ON fleet_sessions WHEN NEW.phase='running' BEGIN \n        UPDATE host_goal_proofs SET verified_at=(SELECT observed_at FROM fleet_sessions WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id),native_id=NEW.native_id\n        WHERE verified_at IS NULL AND launch_id IN (\n          SELECT id FROM fleet_launches WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id AND state='running'\n        ) AND EXISTS (\n          SELECT 1 FROM fleet_launches l\n          JOIN fleet_sessions s ON s.machine_id=l.machine_id AND s.native_id=l.native_id\n          JOIN host_goals g ON g.generation=host_goal_proofs.generation AND g.machine_id=l.machine_id\n          JOIN host_release_targets r ON r.machine_id=g.machine_id\n          JOIN engine_installations i ON i.machine_id=g.machine_id AND i.label=host_goal_proofs.label AND i.revision=host_goal_proofs.revision\n          WHERE l.id=host_goal_proofs.launch_id AND l.state='running' AND s.phase='running'\n            AND l.account=s.account AND l.engine=s.engine AND l.profile IS s.profile AND l.workspace=s.workspace\n            AND l.created_at>=g.set_at AND s.observed_at>=l.created_at\n            AND julianday(s.observed_at) BETWEEN julianday('now','-180 seconds') AND julianday('now','+30 seconds')\n            AND r.sha=g.sha AND r.current_sha=g.sha AND r.state='current' AND r.reported_at>=g.set_at\n        ); END"
  },
  {
    "captured_rowid": 629,
    "name": "host_goal_running_update",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER host_goal_running_update\n        AFTER UPDATE ON fleet_sessions WHEN NEW.phase='running' BEGIN \n        UPDATE host_goal_proofs SET verified_at=(SELECT observed_at FROM fleet_sessions WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id),native_id=NEW.native_id\n        WHERE verified_at IS NULL AND launch_id IN (\n          SELECT id FROM fleet_launches WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id AND state='running'\n        ) AND EXISTS (\n          SELECT 1 FROM fleet_launches l\n          JOIN fleet_sessions s ON s.machine_id=l.machine_id AND s.native_id=l.native_id\n          JOIN host_goals g ON g.generation=host_goal_proofs.generation AND g.machine_id=l.machine_id\n          JOIN host_release_targets r ON r.machine_id=g.machine_id\n          JOIN engine_installations i ON i.machine_id=g.machine_id AND i.label=host_goal_proofs.label AND i.revision=host_goal_proofs.revision\n          WHERE l.id=host_goal_proofs.launch_id AND l.state='running' AND s.phase='running'\n            AND l.account=s.account AND l.engine=s.engine AND l.profile IS s.profile AND l.workspace=s.workspace\n            AND l.created_at>=g.set_at AND s.observed_at>=l.created_at\n            AND julianday(s.observed_at) BETWEEN julianday('now','-180 seconds') AND julianday('now','+30 seconds')\n            AND r.sha=g.sha AND r.current_sha=g.sha AND r.state='current' AND r.reported_at>=g.set_at\n        ); END"
  },
  {
    "captured_rowid": 630,
    "name": "host_goal_launch_running_v2",
    "type": "trigger",
    "table": "fleet_launches",
    "sql": "CREATE TRIGGER host_goal_launch_running_v2\n      AFTER UPDATE ON fleet_launches WHEN NEW.state='running' BEGIN \n        UPDATE host_goal_proofs SET verified_at=(SELECT observed_at FROM fleet_sessions WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id),native_id=NEW.native_id\n        WHERE verified_at IS NULL AND launch_id IN (\n          SELECT id FROM fleet_launches WHERE machine_id=NEW.machine_id AND native_id=NEW.native_id AND state='running'\n        ) AND EXISTS (\n          SELECT 1 FROM fleet_launches l\n          JOIN fleet_sessions s ON s.machine_id=l.machine_id AND s.native_id=l.native_id\n          JOIN host_goals g ON g.generation=host_goal_proofs.generation AND g.machine_id=l.machine_id\n          JOIN host_release_targets r ON r.machine_id=g.machine_id\n          JOIN engine_installations i ON i.machine_id=g.machine_id AND i.label=host_goal_proofs.label AND i.revision=host_goal_proofs.revision\n          WHERE l.id=host_goal_proofs.launch_id AND l.state='running' AND s.phase='running'\n            AND l.account=s.account AND l.engine=s.engine AND l.profile IS s.profile AND l.workspace=s.workspace\n            AND l.created_at>=g.set_at AND s.observed_at>=l.created_at\n            AND julianday(s.observed_at) BETWEEN julianday('now','-180 seconds') AND julianday('now','+30 seconds')\n            AND r.sha=g.sha AND r.current_sha=g.sha AND r.state='current' AND r.reported_at>=g.set_at\n        ); END"
  },
  {
    "captured_rowid": 632,
    "name": "liveness_alerts_open_kind",
    "type": "index",
    "table": "liveness_alerts",
    "sql": "CREATE INDEX liveness_alerts_open_kind ON liveness_alerts (subject_kind) WHERE resolved_at IS NULL"
  },
  {
    "captured_rowid": 633,
    "name": "liveness_alerts_resolved",
    "type": "index",
    "table": "liveness_alerts",
    "sql": "CREATE INDEX liveness_alerts_resolved ON liveness_alerts (resolved_at) WHERE resolved_at IS NOT NULL"
  },
  {
    "captured_rowid": 634,
    "name": "session_audit_decisions_pending",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_pending ON session_audit_decisions(id) WHERE outcome IS NULL AND action IN ('keystroke','nudge','stop','capture')"
  },
  {
    "captured_rowid": 635,
    "name": "session_audit_decisions_stop_timeout",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_stop_timeout ON session_audit_decisions(id) WHERE action = 'stop' AND outcome = 'stop_timeout'"
  },
  {
    "captured_rowid": 636,
    "name": "session_audit_decisions_enqueue_failed",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_enqueue_failed ON session_audit_decisions(id) WHERE action = 'nudge' AND dispatched = 0 AND outcome = 'enqueue_failed'"
  },
  {
    "captured_rowid": 637,
    "name": "session_audit_decisions_nudge_dispatched",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_nudge_dispatched ON session_audit_decisions(id) WHERE action = 'nudge' AND dispatched = 1"
  },
  {
    "captured_rowid": 638,
    "name": "session_audit_decisions_escalate_dispatched",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_escalate_dispatched ON session_audit_decisions(id) WHERE action = 'escalate' AND dispatched = 1"
  },
  {
    "captured_rowid": 639,
    "name": "session_audit_decisions_at",
    "type": "index",
    "table": "session_audit_decisions",
    "sql": "CREATE INDEX session_audit_decisions_at ON session_audit_decisions(at, id)"
  },
  {
    "captured_rowid": 641,
    "name": "session_stale_reconciliations",
    "type": "table",
    "table": "session_stale_reconciliations",
    "sql": "CREATE TABLE session_stale_reconciliations (session_id TEXT PRIMARY KEY, previous_state TEXT NOT NULL, reason TEXT NOT NULL, at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 644,
    "name": "fleet_session_machine_phase",
    "type": "index",
    "table": "fleet_sessions",
    "sql": "CREATE INDEX fleet_session_machine_phase ON fleet_sessions(machine_id,phase)"
  },
  {
    "captured_rowid": 645,
    "name": "fleet_launch_failed_recent",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_failed_recent ON fleet_launches(machine_id,finished_at) WHERE state = 'failed' AND mode = 'headless' AND verification_status = 'unverified'"
  },
  {
    "captured_rowid": 646,
    "name": "launch_schedules",
    "type": "table",
    "table": "launch_schedules",
    "sql": "CREATE TABLE launch_schedules (\n  id TEXT PRIMARY KEY,\n  name TEXT UNIQUE NOT NULL,\n  project TEXT NOT NULL,\n  cron TEXT NOT NULL,\n  tz TEXT NOT NULL,\n  prompt TEXT NOT NULL,\n  acceptance_criteria TEXT,\n  engine TEXT,\n  tier TEXT,\n  task_kind TEXT NOT NULL DEFAULT 'implement',\n  workspace TEXT,\n  overlap TEXT NOT NULL DEFAULT 'skip',\n  enabled INTEGER NOT NULL DEFAULT 1,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL,\n  last_tick_ms INTEGER,\n  next_due_ms INTEGER\n)"
  },
  {
    "captured_rowid": 649,
    "name": "launch_schedule_runs",
    "type": "table",
    "table": "launch_schedule_runs",
    "sql": "CREATE TABLE launch_schedule_runs (\n  id TEXT PRIMARY KEY,\n  schedule_id TEXT NOT NULL,\n  schedule_name TEXT NOT NULL,\n  tick_at TEXT NOT NULL,\n  fired_at TEXT NOT NULL,\n  intake_id TEXT,\n  session_id TEXT,\n  outcome TEXT NOT NULL,\n  reason TEXT,\n  created_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 651,
    "name": "launch_schedule_runs_recent",
    "type": "index",
    "table": "launch_schedule_runs",
    "sql": "CREATE INDEX launch_schedule_runs_recent ON launch_schedule_runs(schedule_id, created_at DESC)"
  },
  {
    "captured_rowid": 653,
    "name": "engine_sync_status",
    "type": "table",
    "table": "engine_sync_status",
    "sql": "CREATE TABLE engine_sync_status (machine_id TEXT NOT NULL, label TEXT NOT NULL, revision TEXT NOT NULL, state TEXT NOT NULL, error TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))"
  },
  {
    "captured_rowid": 655,
    "name": "engine_sync_hosts",
    "type": "table",
    "table": "engine_sync_hosts",
    "sql": "CREATE TABLE engine_sync_hosts (machine_id TEXT PRIMARY KEY, state TEXT NOT NULL, last_sync_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 657,
    "name": "jobs_offered",
    "type": "index",
    "table": "jobs",
    "sql": "CREATE INDEX jobs_offered ON jobs(offer_expires_at) WHERE status = 'offered'"
  },
  {
    "captured_rowid": 658,
    "name": "intake_merge_state",
    "type": "table",
    "table": "intake_merge_state",
    "sql": "CREATE TABLE intake_merge_state (\n      task_id TEXT PRIMARY KEY, project TEXT, state TEXT NOT NULL, machine TEXT, path TEXT, branch TEXT, head TEXT,\n      unmerged INTEGER, commits_json TEXT NOT NULL DEFAULT '[]', deploy_state TEXT, deploy_json TEXT, evidence_at INTEGER,\n      activity_at INTEGER NOT NULL, due_at INTEGER, probe_at INTEGER, flagged_at INTEGER, requeues INTEGER NOT NULL DEFAULT 0,\n      requeue_at INTEGER, finish_session_id TEXT, blocker_id TEXT, updated_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 660,
    "name": "intake_merge_state_due",
    "type": "index",
    "table": "intake_merge_state",
    "sql": "CREATE INDEX intake_merge_state_due ON intake_merge_state(state, due_at)"
  },
  {
    "captured_rowid": 661,
    "name": "fleet_login_holds",
    "type": "table",
    "table": "fleet_login_holds",
    "sql": "CREATE TABLE fleet_login_holds (machine_id TEXT NOT NULL, engine TEXT NOT NULL, account TEXT NOT NULL, profile TEXT NOT NULL DEFAULT '', held_at_ms INTEGER NOT NULL, PRIMARY KEY(machine_id, engine, account, profile))"
  },
  {
    "captured_rowid": 664,
    "name": "fleet_launch_model_unsupported",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_model_unsupported ON fleet_launches(machine_id,engine,account) WHERE exit_reason = 'model_unsupported'"
  },
  {
    "captured_rowid": 667,
    "name": "ask_messages",
    "type": "table",
    "table": "ask_messages",
    "sql": "CREATE TABLE ask_messages(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, agent_name TEXT, t_id TEXT NOT NULL,\n      seq INTEGER NOT NULL, ts INTEGER, text TEXT, text_hash TEXT NOT NULL, source TEXT NOT NULL, machine_id TEXT, actor TEXT,\n      intake_hint TEXT, received_at INTEGER NOT NULL, split_state TEXT NOT NULL DEFAULT 'pending', splitter TEXT,\n      conflict_count INTEGER NOT NULL DEFAULT 0, last_conflict_at INTEGER, role TEXT NOT NULL DEFAULT 'user', kind TEXT, reply_to TEXT,\n      attachments TEXT, batch_id TEXT, UNIQUE(agent_id, t_id))"
  },
  {
    "captured_rowid": 670,
    "name": "ask_messages_split",
    "type": "index",
    "table": "ask_messages",
    "sql": "CREATE INDEX ask_messages_split ON ask_messages(split_state, received_at)"
  },
  {
    "captured_rowid": 671,
    "name": "ask_messages_agent_seq",
    "type": "index",
    "table": "ask_messages",
    "sql": "CREATE INDEX ask_messages_agent_seq ON ask_messages(agent_id, seq)"
  },
  {
    "captured_rowid": 672,
    "name": "ask_messages_received",
    "type": "index",
    "table": "ask_messages",
    "sql": "CREATE INDEX ask_messages_received ON ask_messages(received_at)"
  },
  {
    "captured_rowid": 673,
    "name": "ask_messages_role",
    "type": "index",
    "table": "ask_messages",
    "sql": "CREATE INDEX ask_messages_role ON ask_messages(role)"
  },
  {
    "captured_rowid": 674,
    "name": "ask_messages_conflicts",
    "type": "index",
    "table": "ask_messages",
    "sql": "CREATE INDEX ask_messages_conflicts ON ask_messages(conflict_count) WHERE conflict_count>0"
  },
  {
    "captured_rowid": 675,
    "name": "asks",
    "type": "table",
    "table": "asks",
    "sql": "CREATE TABLE asks(id TEXT PRIMARY KEY, message_id TEXT NOT NULL, part INTEGER NOT NULL, text TEXT NOT NULL,\n      state TEXT NOT NULL, intake_id TEXT, ac_line INTEGER, ac_text TEXT, match TEXT, verified_by TEXT, evidence TEXT, actor TEXT,\n      created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, alert_subject TEXT, dropped_subject TEXT, UNIQUE(message_id, part))"
  },
  {
    "captured_rowid": 678,
    "name": "asks_state",
    "type": "index",
    "table": "asks",
    "sql": "CREATE INDEX asks_state ON asks(state, created_at)"
  },
  {
    "captured_rowid": 679,
    "name": "asks_intake",
    "type": "index",
    "table": "asks",
    "sql": "CREATE INDEX asks_intake ON asks(intake_id)"
  },
  {
    "captured_rowid": 680,
    "name": "asks_alert_subject",
    "type": "index",
    "table": "asks",
    "sql": "CREATE INDEX asks_alert_subject ON asks(alert_subject) WHERE alert_subject IS NOT NULL"
  },
  {
    "captured_rowid": 681,
    "name": "asks_dropped_subject",
    "type": "index",
    "table": "asks",
    "sql": "CREATE INDEX asks_dropped_subject ON asks(dropped_subject) WHERE dropped_subject IS NOT NULL"
  },
  {
    "captured_rowid": 682,
    "name": "ask_agents",
    "type": "table",
    "table": "ask_agents",
    "sql": "CREATE TABLE ask_agents(agent_id TEXT PRIMARY KEY, agent_name TEXT, covered_through INTEGER,\n      window_last_seq INTEGER, window_changed_at INTEGER, last_message_at INTEGER, silent_alert_day TEXT, window_diag TEXT,\n      msg_count INTEGER NOT NULL DEFAULT 0, last_any_at INTEGER)"
  },
  {
    "captured_rowid": 684,
    "name": "ask_gaps",
    "type": "table",
    "table": "ask_gaps",
    "sql": "CREATE TABLE ask_gaps(scope TEXT NOT NULL, from_seq INTEGER NOT NULL, to_seq INTEGER NOT NULL, kind TEXT NOT NULL,\n      detected_at INTEGER NOT NULL, resolved_at INTEGER, alert_subject TEXT, PRIMARY KEY(scope, from_seq, to_seq))"
  },
  {
    "captured_rowid": 686,
    "name": "ask_gaps_open",
    "type": "index",
    "table": "ask_gaps",
    "sql": "CREATE INDEX ask_gaps_open ON ask_gaps(detected_at) WHERE resolved_at IS NULL"
  },
  {
    "captured_rowid": 687,
    "name": "ask_gaps_subject",
    "type": "index",
    "table": "ask_gaps",
    "sql": "CREATE INDEX ask_gaps_subject ON ask_gaps(alert_subject) WHERE alert_subject IS NOT NULL"
  },
  {
    "captured_rowid": 688,
    "name": "ask_sources",
    "type": "table",
    "table": "ask_sources",
    "sql": "CREATE TABLE ask_sources(machine_id TEXT NOT NULL, source TEXT NOT NULL, last_ingest_at INTEGER NOT NULL,\n      alerted INTEGER NOT NULL DEFAULT 0, last_reason TEXT, PRIMARY KEY(machine_id, source))"
  },
  {
    "captured_rowid": 690,
    "name": "ask_meta",
    "type": "table",
    "table": "ask_meta",
    "sql": "CREATE TABLE ask_meta(id INTEGER PRIMARY KEY CHECK(id=1), gap_scope TEXT NOT NULL DEFAULT 'window',\n      last_tick INTEGER, last_result TEXT, daily_day TEXT, last_user_batch_at INTEGER, reconcile_intake TEXT, reconcile_session TEXT,\n      reconcile_started_at INTEGER, reconcile_batch_at INTEGER)"
  },
  {
    "captured_rowid": 691,
    "name": "ask_counts",
    "type": "table",
    "table": "ask_counts",
    "sql": "CREATE TABLE ask_counts(k TEXT PRIMARY KEY, n INTEGER NOT NULL DEFAULT 0)"
  },
  {
    "captured_rowid": 693,
    "name": "ask_counts_ins",
    "type": "trigger",
    "table": "asks",
    "sql": "CREATE TRIGGER ask_counts_ins AFTER INSERT ON asks BEGIN INSERT INTO ask_counts(k,n) VALUES('state:'||NEW.state,1) ON CONFLICT(k) DO UPDATE SET n=n+(1); END"
  },
  {
    "captured_rowid": 694,
    "name": "ask_counts_upd",
    "type": "trigger",
    "table": "asks",
    "sql": "CREATE TRIGGER ask_counts_upd AFTER UPDATE OF state ON asks WHEN OLD.state<>NEW.state BEGIN INSERT INTO ask_counts(k,n) VALUES('state:'||OLD.state,-1) ON CONFLICT(k) DO UPDATE SET n=n+(-1); INSERT INTO ask_counts(k,n) VALUES('state:'||NEW.state,1) ON CONFLICT(k) DO UPDATE SET n=n+(1); END"
  },
  {
    "captured_rowid": 695,
    "name": "ask_counts_del",
    "type": "trigger",
    "table": "asks",
    "sql": "CREATE TRIGGER ask_counts_del AFTER DELETE ON asks BEGIN INSERT INTO ask_counts(k,n) VALUES('state:'||OLD.state,-1) ON CONFLICT(k) DO UPDATE SET n=n+(-1); END"
  },
  {
    "captured_rowid": 696,
    "name": "ask_counts_msg_ins",
    "type": "trigger",
    "table": "ask_messages",
    "sql": "CREATE TRIGGER ask_counts_msg_ins AFTER INSERT ON ask_messages WHEN NEW.role='user' BEGIN INSERT INTO ask_counts(k,n) VALUES('user_messages',1) ON CONFLICT(k) DO UPDATE SET n=n+(1); END"
  },
  {
    "captured_rowid": 697,
    "name": "ask_counts_msg_del",
    "type": "trigger",
    "table": "ask_messages",
    "sql": "CREATE TRIGGER ask_counts_msg_del AFTER DELETE ON ask_messages WHEN OLD.role='user' BEGIN INSERT INTO ask_counts(k,n) VALUES('user_messages',-1) ON CONFLICT(k) DO UPDATE SET n=n+(-1); END"
  },
  {
    "captured_rowid": 698,
    "name": "session_control_commands_state",
    "type": "index",
    "table": "session_control_commands",
    "sql": "CREATE INDEX session_control_commands_state ON session_control_commands(state, session_id)"
  },
  {
    "captured_rowid": 700,
    "name": "engine_credential_acks",
    "type": "table",
    "table": "engine_credential_acks",
    "sql": "CREATE TABLE engine_credential_acks (machine_id TEXT NOT NULL, label TEXT NOT NULL, generation INTEGER NOT NULL, acked_at INTEGER NOT NULL, PRIMARY KEY(machine_id,label))"
  },
  {
    "captured_rowid": 702,
    "name": "engine_epoch",
    "type": "table",
    "table": "engine_epoch",
    "sql": "CREATE TABLE engine_epoch (k INTEGER PRIMARY KEY CHECK (k = 1), v TEXT NOT NULL)"
  },
  {
    "captured_rowid": 704,
    "name": "engine_rollout_desired",
    "type": "table",
    "table": "engine_rollout_desired",
    "sql": "CREATE TABLE engine_rollout_desired (engine TEXT PRIMARY KEY, version TEXT NOT NULL, latest TEXT, source TEXT NOT NULL, updated_at INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 706,
    "name": "engine_version_reports",
    "type": "table",
    "table": "engine_version_reports",
    "sql": "CREATE TABLE engine_version_reports (machine_id TEXT NOT NULL, engine TEXT NOT NULL, installed TEXT, latest TEXT, result TEXT, updated_at INTEGER NOT NULL, PRIMARY KEY(machine_id,engine))"
  },
  {
    "captured_rowid": 708,
    "name": "engine_rollouts",
    "type": "table",
    "table": "engine_rollouts",
    "sql": "CREATE TABLE engine_rollouts (id TEXT PRIMARY KEY, engine TEXT NOT NULL, target_version TEXT NOT NULL, previous_json TEXT NOT NULL, canary_machine TEXT NOT NULL, state TEXT NOT NULL, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, detail TEXT NOT NULL)"
  },
  {
    "captured_rowid": 711,
    "name": "session_control_pings",
    "type": "table",
    "table": "session_control_pings",
    "sql": "CREATE TABLE session_control_pings (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, actor TEXT NOT NULL, every_ms INTEGER NOT NULL, prompt TEXT NOT NULL, state TEXT NOT NULL, next_at_ms INTEGER NOT NULL, last_command_id TEXT, last_delivered_at TEXT, last_acked_at TEXT, delivered_count INTEGER NOT NULL DEFAULT 0, skipped_count INTEGER NOT NULL DEFAULT 0, failure_count INTEGER NOT NULL DEFAULT 0, last_error TEXT, intake_task_id TEXT, owner_token_id TEXT, owner_family TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, ended_at TEXT, ended_by TEXT, ended_reason TEXT)"
  },
  {
    "captured_rowid": 713,
    "name": "session_control_pings_active",
    "type": "index",
    "table": "session_control_pings",
    "sql": "CREATE UNIQUE INDEX session_control_pings_active ON session_control_pings(session_id) WHERE state = 'active'"
  },
  {
    "captured_rowid": 714,
    "name": "session_control_pings_due",
    "type": "index",
    "table": "session_control_pings",
    "sql": "CREATE INDEX session_control_pings_due ON session_control_pings(state, next_at_ms)\n"
  },
  {
    "captured_rowid": 716,
    "name": "intake_queue_due",
    "type": "index",
    "table": "intake_restart_queue",
    "sql": "CREATE INDEX intake_queue_due ON intake_restart_queue(next_at,task_id) WHERE state='queued'"
  },
  {
    "captured_rowid": 717,
    "name": "intake_queue_due_cursor",
    "type": "table",
    "table": "intake_queue_due_cursor",
    "sql": "CREATE TABLE intake_queue_due_cursor(id INTEGER PRIMARY KEY CHECK(id=1),next_at INTEGER NOT NULL,task_id TEXT NOT NULL)"
  },
  {
    "captured_rowid": 718,
    "name": "fleet_catalog_census",
    "type": "table",
    "table": "fleet_catalog_census",
    "sql": "CREATE TABLE fleet_catalog_census (machine_id TEXT PRIMARY KEY, observed_at TEXT NOT NULL, complete INTEGER NOT NULL CHECK (complete IN (0, 1)))"
  },
  {
    "captured_rowid": 721,
    "name": "intake_export",
    "type": "table",
    "table": "intake_export",
    "sql": "CREATE TABLE intake_export (\n    id TEXT PRIMARY KEY, project TEXT NOT NULL, status TEXT NOT NULL,\n    archived INTEGER NOT NULL, updated_at INTEGER NOT NULL, data TEXT NOT NULL)"
  },
  {
    "captured_rowid": 723,
    "name": "intake_export_recent",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_recent ON intake_export(updated_at DESC,id)"
  },
  {
    "captured_rowid": 724,
    "name": "intake_export_project_recent",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_project_recent ON intake_export(project,updated_at DESC,id)"
  },
  {
    "captured_rowid": 725,
    "name": "intake_export_status",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_status ON intake_export(status,archived,id)"
  },
  {
    "captured_rowid": 726,
    "name": "intake_export_project",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_project ON intake_export(project,id)"
  },
  {
    "captured_rowid": 727,
    "name": "intake_export_archive",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_archive ON intake_export(archived,id)"
  },
  {
    "captured_rowid": 728,
    "name": "intake_export_status_id",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_status_id ON intake_export(status,id)"
  },
  {
    "captured_rowid": 729,
    "name": "intake_export_project_archive",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_project_archive ON intake_export(project,archived,id)"
  },
  {
    "captured_rowid": 730,
    "name": "intake_export_project_status",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_project_status ON intake_export(project,status,id)"
  },
  {
    "captured_rowid": 731,
    "name": "intake_export_project_status_archive",
    "type": "index",
    "table": "intake_export",
    "sql": "CREATE INDEX intake_export_project_status_archive ON intake_export(project,status,archived,id)"
  },
  {
    "captured_rowid": 732,
    "name": "intake_export_migration",
    "type": "table",
    "table": "intake_export_migration",
    "sql": "CREATE TABLE intake_export_migration(id INTEGER PRIMARY KEY CHECK(id=1), after_id TEXT, done INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 733,
    "name": "intake_export_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_export_insert AFTER INSERT ON intake_tasks BEGIN\n    INSERT INTO intake_export SELECT NEW.id,COALESCE(json_extract(NEW.data,'$.project'),''),COALESCE(json_extract(NEW.data,'$.status'),'open'),CASE WHEN json_extract(NEW.data,'$.archived_at') IS NULL THEN 0 ELSE 1 END,COALESCE(json_extract(NEW.data,'$.updated_at'),0),\n      json_object('id',NEW.id,'project',json_extract(NEW.data,'$.project'),'status',json_extract(NEW.data,'$.status'),'version',json_extract(NEW.data,'$.version'),\n        'title',json_extract(NEW.data,'$.title'),'owner',json_extract(NEW.data,'$.owner'),'current_state',json_extract(NEW.data,'$.current_state'),'next_step',json_extract(NEW.data,'$.next_step'),\n        'created_at',json_extract(NEW.data,'$.created_at'),'updated_at',json_extract(NEW.data,'$.updated_at'),'archived_at',json_extract(NEW.data,'$.archived_at'),\n        'last_event_kind',json_extract(NEW.data,'$.history[#-1].kind'),'last_event_at',json_extract(NEW.data,'$.history[#-1].captured_at'),\n        'last_session_id',(SELECT json_extract(value,'$.hub_session_id') FROM json_each(NEW.data,'$.history')\n          WHERE json_extract(value,'$.kind')='link' AND json_extract(value,'$.hub_session_id') IS NOT NULL ORDER BY CAST(key AS INTEGER) DESC LIMIT 1)) ON CONFLICT(id) DO UPDATE SET\n      project=excluded.project,status=excluded.status,archived=excluded.archived,updated_at=excluded.updated_at,data=excluded.data; END"
  },
  {
    "captured_rowid": 734,
    "name": "intake_export_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_export_update AFTER UPDATE ON intake_tasks BEGIN\n    INSERT INTO intake_export SELECT NEW.id,COALESCE(json_extract(NEW.data,'$.project'),''),COALESCE(json_extract(NEW.data,'$.status'),'open'),CASE WHEN json_extract(NEW.data,'$.archived_at') IS NULL THEN 0 ELSE 1 END,COALESCE(json_extract(NEW.data,'$.updated_at'),0),\n      json_object('id',NEW.id,'project',json_extract(NEW.data,'$.project'),'status',json_extract(NEW.data,'$.status'),'version',json_extract(NEW.data,'$.version'),\n        'title',json_extract(NEW.data,'$.title'),'owner',json_extract(NEW.data,'$.owner'),'current_state',json_extract(NEW.data,'$.current_state'),'next_step',json_extract(NEW.data,'$.next_step'),\n        'created_at',json_extract(NEW.data,'$.created_at'),'updated_at',json_extract(NEW.data,'$.updated_at'),'archived_at',json_extract(NEW.data,'$.archived_at'),\n        'last_event_kind',json_extract(NEW.data,'$.history[#-1].kind'),'last_event_at',json_extract(NEW.data,'$.history[#-1].captured_at'),\n        'last_session_id',(SELECT json_extract(value,'$.hub_session_id') FROM json_each(NEW.data,'$.history')\n          WHERE json_extract(value,'$.kind')='link' AND json_extract(value,'$.hub_session_id') IS NOT NULL ORDER BY CAST(key AS INTEGER) DESC LIMIT 1)) ON CONFLICT(id) DO UPDATE SET\n      project=excluded.project,status=excluded.status,archived=excluded.archived,updated_at=excluded.updated_at,data=excluded.data; END"
  },
  {
    "captured_rowid": 735,
    "name": "intake_export_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER intake_export_delete AFTER DELETE ON intake_tasks BEGIN DELETE FROM intake_export WHERE id=OLD.id; END"
  },
  {
    "captured_rowid": 736,
    "name": "intake_export_counts",
    "type": "table",
    "table": "intake_export_counts",
    "sql": "CREATE TABLE intake_export_counts(project TEXT NOT NULL,status TEXT NOT NULL,archived INTEGER NOT NULL,n INTEGER NOT NULL,\n    PRIMARY KEY(project,status,archived))"
  },
  {
    "captured_rowid": 738,
    "name": "intake_export_count_insert",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_count_insert AFTER INSERT ON intake_export BEGIN\n    \n    INSERT INTO intake_export_counts VALUES(NEW.project,NEW.status,NEW.archived,1) ON CONFLICT(project,status,archived) DO UPDATE SET n=n+1;\n    END"
  },
  {
    "captured_rowid": 739,
    "name": "intake_export_count_update",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_count_update AFTER UPDATE ON intake_export BEGIN\n    UPDATE intake_export_counts SET n=n-1 WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_counts WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived AND n=0;\n    INSERT INTO intake_export_counts VALUES(NEW.project,NEW.status,NEW.archived,1) ON CONFLICT(project,status,archived) DO UPDATE SET n=n+1;\n    END"
  },
  {
    "captured_rowid": 740,
    "name": "intake_export_count_delete",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_count_delete AFTER DELETE ON intake_export BEGIN\n    UPDATE intake_export_counts SET n=n-1 WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_counts WHERE project=OLD.project AND status=OLD.status AND archived=OLD.archived AND n=0;\n    \n    END"
  },
  {
    "captured_rowid": 741,
    "name": "intake_export_totals",
    "type": "table",
    "table": "intake_export_totals",
    "sql": "CREATE TABLE intake_export_totals(status TEXT NOT NULL,archived INTEGER NOT NULL,n INTEGER NOT NULL,\n    PRIMARY KEY(status,archived))"
  },
  {
    "captured_rowid": 743,
    "name": "intake_export_total_insert",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_total_insert AFTER INSERT ON intake_export BEGIN\n    \n    INSERT INTO intake_export_totals VALUES(NEW.status,NEW.archived,1) ON CONFLICT(status,archived) DO UPDATE SET n=n+1;\n    END"
  },
  {
    "captured_rowid": 744,
    "name": "intake_export_total_update",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_total_update AFTER UPDATE ON intake_export BEGIN\n    UPDATE intake_export_totals SET n=n-1 WHERE status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_totals WHERE status=OLD.status AND archived=OLD.archived AND n=0;\n    INSERT INTO intake_export_totals VALUES(NEW.status,NEW.archived,1) ON CONFLICT(status,archived) DO UPDATE SET n=n+1;\n    END"
  },
  {
    "captured_rowid": 745,
    "name": "intake_export_total_delete",
    "type": "trigger",
    "table": "intake_export",
    "sql": "CREATE TRIGGER intake_export_total_delete AFTER DELETE ON intake_export BEGIN\n    UPDATE intake_export_totals SET n=n-1 WHERE status=OLD.status AND archived=OLD.archived; DELETE FROM intake_export_totals WHERE status=OLD.status AND archived=OLD.archived AND n=0;\n    \n    END"
  },
  {
    "captured_rowid": 747,
    "name": "resource_queue_choking_recent",
    "type": "index",
    "table": "resource_choking_events",
    "sql": "CREATE INDEX resource_queue_choking_recent ON resource_choking_events(machine_id,observed_at DESC) WHERE reason!='host_runtime_choking'"
  },
  {
    "captured_rowid": 748,
    "name": "fleet_launch_runtime_exit",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_runtime_exit ON fleet_launches(machine_id,updated_at DESC) WHERE state IN ('failed','exited') AND exit_reason IN ('engine_runtime_timeout','engine_signal_exit')"
  },
  {
    "captured_rowid": 749,
    "name": "fleet_queue_live_machines",
    "type": "index",
    "table": "machines",
    "sql": "CREATE INDEX fleet_queue_live_machines ON machines(last_checkin_at,id) WHERE drained=0 AND revoked_at IS NULL"
  },
  {
    "captured_rowid": 751,
    "name": "session_control_capacity_binding",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_capacity_binding\n      ON session_control_sessions(fleet_launch_id,machine_id,engine)\n      WHERE state IN ('running','uncertain') AND binding_json IS NOT NULL"
  },
  {
    "captured_rowid": 752,
    "name": "ops_intake_created",
    "type": "index",
    "table": "intake_task_projection",
    "sql": "CREATE INDEX ops_intake_created ON intake_task_projection(json_extract(data,'$.created_at'))"
  },
  {
    "captured_rowid": 753,
    "name": "ops_intake_closed_time",
    "type": "index",
    "table": "intake_audit",
    "sql": "CREATE INDEX ops_intake_closed_time ON intake_audit(captured_at,task_id) WHERE kind IN ('close','cancel')"
  },
  {
    "captured_rowid": 754,
    "name": "ops_intake_native_started",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX ops_intake_native_started ON session_control_sessions(\n      json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_created_at_ms'))"
  },
  {
    "captured_rowid": 756,
    "name": "session_rss_median",
    "type": "table",
    "table": "session_rss_median",
    "sql": "CREATE TABLE session_rss_median (session_id TEXT PRIMARY KEY, p50_rss_mb REAL, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 759,
    "name": "session_oom_kills",
    "type": "table",
    "table": "session_oom_kills",
    "sql": "CREATE TABLE session_oom_kills (session_id TEXT PRIMARY KEY, oom_kills INTEGER NOT NULL, observed_at TEXT NOT NULL)"
  },
  {
    "captured_rowid": 762,
    "name": "hub_prompts",
    "type": "table",
    "table": "hub_prompts",
    "sql": "CREATE TABLE hub_prompts (\n  id TEXT PRIMARY KEY,\n  slug TEXT UNIQUE NOT NULL,\n  title TEXT,\n  current_version INTEGER NOT NULL,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL\n)"
  },
  {
    "captured_rowid": 765,
    "name": "hub_prompt_versions",
    "type": "table",
    "table": "hub_prompt_versions",
    "sql": "CREATE TABLE hub_prompt_versions (\n  prompt_id TEXT NOT NULL,\n  version INTEGER NOT NULL,\n  body TEXT NOT NULL,\n  note TEXT,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  PRIMARY KEY (prompt_id, version)\n)"
  },
  {
    "captured_rowid": 767,
    "name": "session_schedules",
    "type": "table",
    "table": "session_schedules",
    "sql": "CREATE TABLE session_schedules (\n  id TEXT PRIMARY KEY,\n  name TEXT UNIQUE NOT NULL,\n  target_label TEXT NOT NULL,\n  cron TEXT NOT NULL,\n  tz TEXT NOT NULL DEFAULT 'UTC',\n  prompt_text TEXT,\n  prompt_id TEXT,\n  prompt_vars TEXT,\n  enabled INTEGER NOT NULL DEFAULT 1,\n  created_by TEXT NOT NULL,\n  created_at TEXT NOT NULL,\n  updated_at TEXT NOT NULL,\n  next_due_ms INTEGER,\n  last_fire_ms INTEGER,\n  last_fire_at TEXT\n)"
  },
  {
    "captured_rowid": 771,
    "name": "session_control_wakes_due_order",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX session_control_wakes_due_order ON session_control_wakes(state, wake_at_ms, created_at, id)"
  },
  {
    "captured_rowid": 772,
    "name": "session_control_wakes_delivery_order",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX session_control_wakes_delivery_order ON session_control_wakes(state, delivered_at, id)"
  },
  {
    "captured_rowid": 773,
    "name": "session_control_wakes_resolution_order",
    "type": "index",
    "table": "session_control_wakes",
    "sql": "CREATE INDEX session_control_wakes_resolution_order ON session_control_wakes(state, updated_at)"
  },
  {
    "captured_rowid": 775,
    "name": "session_control_live_created",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_live_created ON session_control_sessions(created_at DESC, id DESC) WHERE state IN ('queued','held','offered','starting','running','uncertain')"
  },
  {
    "captured_rowid": 776,
    "name": "session_control_live_actor_created",
    "type": "index",
    "table": "session_control_sessions",
    "sql": "CREATE INDEX session_control_live_actor_created ON session_control_sessions(actor, created_at DESC, id DESC) WHERE state IN ('queued','held','offered','starting','running','uncertain')"
  },
  {
    "captured_rowid": 778,
    "name": "fleet_launch_account_failed_recent",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_account_failed_recent ON fleet_launches(account,updated_at DESC) WHERE state = 'failed'"
  },
  {
    "captured_rowid": 779,
    "name": "fleet_launch_updated",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX fleet_launch_updated ON fleet_launches(updated_at)"
  },
  {
    "captured_rowid": 780,
    "name": "fleet_memory_holds_retry",
    "type": "index",
    "table": "fleet_memory_holds",
    "sql": "CREATE INDEX fleet_memory_holds_retry ON fleet_memory_holds(retry_id)"
  },
  {
    "captured_rowid": 782,
    "name": "intake_queue_launch_updated",
    "type": "index",
    "table": "fleet_launches",
    "sql": "CREATE INDEX intake_queue_launch_updated ON fleet_launches(updated_at)"
  },
  {
    "captured_rowid": 783,
    "name": "session_observation_intake_schema",
    "type": "table",
    "table": "session_observation_intake_schema",
    "sql": "CREATE TABLE session_observation_intake_schema\n    (id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL)"
  },
  {
    "captured_rowid": 784,
    "name": "session_observation_intake_insert",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER session_observation_intake_insert\n    AFTER INSERT ON intake_tasks BEGIN\n      INSERT INTO session_observation_intakes(task_id,events) VALUES(NEW.id,(SELECT json_group_array(json_object(\n    'hub_session_id',json_extract(e.value,'$.hub_session_id'),\n    'native_id',json_extract(e.value,'$.native_id'),'host',json_extract(e.value,'$.host'),\n    'kind',json_extract(e.value,'$.kind'),'intake_key',json_extract(e.value,'$.intake_key'),\n    'text',CASE WHEN json_extract(e.value,'$.kind')='note'\n      THEN json_extract(e.value,'$.text') ELSE NULL END))\n    FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.hub_session_id') IS NOT NULL\n      OR json_extract(e.value,'$.native_id') IS NOT NULL\n      OR (json_extract(e.value,'$.kind')='note' AND (\n        upper(ltrim(json_extract(e.value,'$.text'))) LIKE 'START%'\n        OR json_extract(e.value,'$.intake_key') GLOB 'note-aplexer-*'))))\n        ON CONFLICT(task_id) DO UPDATE SET events=excluded.events;\n      UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1;\n    END"
  },
  {
    "captured_rowid": 785,
    "name": "session_observation_intake_update",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER session_observation_intake_update\n    AFTER UPDATE ON intake_tasks BEGIN\n      INSERT INTO session_observation_intakes(task_id,events) VALUES(NEW.id,(SELECT json_group_array(json_object(\n    'hub_session_id',json_extract(e.value,'$.hub_session_id'),\n    'native_id',json_extract(e.value,'$.native_id'),'host',json_extract(e.value,'$.host'),\n    'kind',json_extract(e.value,'$.kind'),'intake_key',json_extract(e.value,'$.intake_key'),\n    'text',CASE WHEN json_extract(e.value,'$.kind')='note'\n      THEN json_extract(e.value,'$.text') ELSE NULL END))\n    FROM json_each(NEW.data,'$.history') e WHERE json_extract(e.value,'$.hub_session_id') IS NOT NULL\n      OR json_extract(e.value,'$.native_id') IS NOT NULL\n      OR (json_extract(e.value,'$.kind')='note' AND (\n        upper(ltrim(json_extract(e.value,'$.text'))) LIKE 'START%'\n        OR json_extract(e.value,'$.intake_key') GLOB 'note-aplexer-*'))))\n        ON CONFLICT(task_id) DO UPDATE SET events=excluded.events;\n      UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1;\n    END"
  },
  {
    "captured_rowid": 786,
    "name": "session_observation_intake_delete",
    "type": "trigger",
    "table": "intake_tasks",
    "sql": "CREATE TRIGGER session_observation_intake_delete AFTER DELETE ON intake_tasks BEGIN\n    DELETE FROM session_observation_intakes WHERE task_id=OLD.id;\n    UPDATE session_observation_intake_meta SET revision=revision+1 WHERE id=1; END"
  },
  {
    "captured_rowid": 787,
    "name": "resource_catalog_fleet_sessions_update",
    "type": "trigger",
    "table": "fleet_sessions",
    "sql": "CREATE TRIGGER resource_catalog_fleet_sessions_update AFTER UPDATE ON fleet_sessions WHEN OLD.\"machine_id\" IS NOT NEW.\"machine_id\" OR OLD.\"native_id\" IS NOT NEW.\"native_id\" OR OLD.\"tag\" IS NOT NEW.\"tag\" OR OLD.\"engine\" IS NOT NEW.\"engine\" OR OLD.\"phase\" IS NOT NEW.\"phase\" OR OLD.\"agent_state\" IS NOT NEW.\"agent_state\" OR OLD.\"workspace\" IS NOT NEW.\"workspace\" OR OLD.\"profile\" IS NOT NEW.\"profile\" OR OLD.\"account\" IS NOT NEW.\"account\" OR OLD.\"cwd\" IS NOT NEW.\"cwd\" OR OLD.\"created_at_ms\" IS NOT NEW.\"created_at_ms\" OR OLD.\"progress_json\" IS NOT NEW.\"progress_json\"\n        BEGIN UPDATE resource_catalog_revision SET revision=lower(hex(randomblob(16))) WHERE id=1; END"
  }
];

export const SCHEMA_SQL = `
CREATE TABLE IF NOT EXISTS users (
  sub TEXT PRIMARY KEY,
  email TEXT NOT NULL,
  is_admin BOOLEAN NOT NULL,
  first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL, -- host|agent|job|human
  token_hash TEXT UNIQUE NOT NULL,
  refresh_hash TEXT UNIQUE,
  family_id TEXT NOT NULL,
  subject TEXT NOT NULL,
  machine_id TEXT,
  actor TEXT,
  scopes JSON NOT NULL,
  created_by_sub TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  revoked_at TEXT,
  last_used_at TEXT,
  approval_seq INTEGER -- R5 R2 enrollment authority inherited at issuance and preserved across refresh, NULL is pre-migration legacy
);

CREATE TABLE IF NOT EXISTS device_pairings (
  device_code_hash TEXT PRIMARY KEY,
  user_code TEXT UNIQUE NOT NULL,
  client_kind TEXT NOT NULL,
  name TEXT NOT NULL,
  hostname TEXT NOT NULL,
  ip_country TEXT,
  actor TEXT,
  requested_scopes JSON NOT NULL,
  status TEXT NOT NULL, -- pending|approved|denied|consumed|expired
  approved_by_sub TEXT,
  granted_scopes JSON,
  machine_id TEXT,
  interval_s INTEGER NOT NULL,
  last_poll_at TEXT NOT NULL,
  created_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  approved_seq INTEGER -- R5 R2 approval authority assigned at operator approval, NULL is pending or pre-migration
);

CREATE TABLE IF NOT EXISTS machines (
  id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  hostname TEXT NOT NULL,
  network TEXT NOT NULL DEFAULT 'home',
  os TEXT NOT NULL DEFAULT '',
  roles JSON NOT NULL DEFAULT '[]',
  capabilities JSON NOT NULL DEFAULT '{}',
  credential_id TEXT,
  registered_at TEXT NOT NULL,
  last_checkin_at TEXT,
  last_heartbeat JSON,
  drained INTEGER NOT NULL DEFAULT 0,
  notes TEXT,
  approval_seq INTEGER -- R5 R2 current enrollment authority for this machine, NULL is pre-migration legacy
);

CREATE TABLE IF NOT EXISTS login_states (
  state_hash TEXT PRIMARY KEY,
  expires_at INTEGER NOT NULL,
  consumed INTEGER NOT NULL DEFAULT 0
);

-- Existing from M0
CREATE TABLE IF NOT EXISTS health_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  subject_kind TEXT,
  subject TEXT,
  state TEXT,
  detail TEXT,
  at TEXT
);

CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,
  idempotency_key TEXT UNIQUE NOT NULL,
  payload JSON NOT NULL,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  deadline_at TEXT NOT NULL,
  actor TEXT NOT NULL,
  offer_host_id TEXT,
  offer_machine_id TEXT,
  offer_expires_at TEXT,
  offer_attempts JSON NOT NULL DEFAULT '[]',
  claim_token_hash TEXT,
  holder_host_id TEXT,
  holder_machine_id TEXT,
  started_at TEXT,
  first_action_at TEXT,
  evidence_digest TEXT,
  native_session TEXT,
  launcher_task_id TEXT,
  lease_epoch INTEGER,
  finished_at TEXT,
  outcome_summary TEXT,
  outcome_evidence JSON
);

CREATE TABLE IF NOT EXISTS leases (
  name TEXT PRIMARY KEY,
  holder_job_id TEXT,
  holder_machine_id TEXT,
  holder_host_id TEXT,
  holder_actor TEXT,
  epoch INTEGER NOT NULL DEFAULT 0,
  acquired_at TEXT,
  renewed_at TEXT,
  expires_at TEXT,
  released_at TEXT,
  release_reason TEXT
);

CREATE TABLE IF NOT EXISTS offers (
  job_id TEXT,
  machine_id TEXT,
  offered_at TEXT,
  response TEXT,
  reason TEXT
);

CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  resource TEXT,
  target TEXT,
  detail TEXT,
  at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS handoff_notes (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lease_name TEXT NOT NULL,
  job_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  session_id TEXT,
  epoch INTEGER NOT NULL,
  summary TEXT NOT NULL,
  evidence_refs JSON,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS root_actions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT NOT NULL,
  host_id TEXT NOT NULL,
  session_id TEXT,
  epoch INTEGER NOT NULL,
  action_name TEXT NOT NULL,
  action_type TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  payload_digest TEXT NOT NULL,
  result JSON NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  tag TEXT NOT NULL,
  project TEXT,
  role TEXT,
  machine_id TEXT,
  registered_at TEXT,
  engine TEXT,
  session_id TEXT,
  workspace TEXT,
  team_id TEXT,
  imported_from TEXT,
  state TEXT,
  health_state TEXT,
  health_detail JSON,
  detail TEXT,
  lasthealth TEXT,
  health_at TEXT,
  last_seen_at TEXT,
  last_seen TEXT,
  updated_at TEXT,
  pid INTEGER,
  started_at TEXT
);

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  from_actor TEXT NOT NULL,
  to_actor TEXT NOT NULL,
  kind TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  reply_to TEXT,
  body TEXT NOT NULL,
  idempotency_key TEXT NOT NULL,
  lease_epoch INTEGER,
  created_at INTEGER NOT NULL,
  delivered_at INTEGER,
  read_at INTEGER,
  acked_at INTEGER,
  accepted_at INTEGER,
  completed_at INTEGER,
  expires_at INTEGER NOT NULL,
  UNIQUE(from_actor, idempotency_key)
);

CREATE TABLE IF NOT EXISTS claims (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  scope TEXT NOT NULL,
  holder TEXT NOT NULL,
  created_at INTEGER NOT NULL,
  expires_at INTEGER NOT NULL,
  released_at INTEGER,
  work_key TEXT,
  claim_mode TEXT,
  claim_source TEXT,
  worktree TEXT,
  session_state TEXT,
  native_claim_id TEXT
);
`;


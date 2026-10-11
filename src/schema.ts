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
  last_used_at TEXT
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
  expires_at TEXT NOT NULL
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
  notes TEXT
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
`;

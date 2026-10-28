import type { SqlStorage } from '@cloudflare/workers-types';

export const AGENT_NAME = /^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/;
export const RESERVED_REGISTRY_NAME = /^(human|role|machine|routing|routing-client|job|schedule|admin|root|principal|system|hub)$/i;
export function validRegistryName(value: unknown): value is string {
  return typeof value === 'string' && AGENT_NAME.test(value) && !RESERVED_REGISTRY_NAME.test(value) && !/^root/i.test(value);
}
/** SQL equivalent of validRegistryName, shared by triggers and registry consumers. */
export function registryNameSql(column: string): string {
  return `(length(${column}) BETWEEN 1 AND 64 AND ${column} NOT GLOB '*[^A-Za-z0-9._-]*'
    AND substr(${column},1,1) GLOB '[A-Za-z0-9]' AND lower(${column}) NOT IN
    ('human','role','machine','routing','routing-client','job','schedule','admin','root','principal','system','hub')
    AND lower(${column}) NOT LIKE 'root%')`;
}
export function registryRowSql(prefix = ''): string {
  return `(length(CASE WHEN COALESCE(${prefix}project,'') = '' THEN ${prefix}tag ELSE ${prefix}project || ':' || ${prefix}tag END) <= 128 AND ${registryNameSql(prefix + 'tag')} AND (COALESCE(${prefix}project,'') = '' OR ${registryNameSql(prefix + 'project')}))`;
}
export function registryAddress(raw: string): { tag: string; project: string | null } | null {
  const parts = raw.split(':');
  if (raw.length > 128 || !parts.every(validRegistryName)) return null;
  if (parts.length === 1) return { tag: parts[0], project: null };
  if (parts.length !== 2) return null;
  return { project: parts[0], tag: parts[1] };
}

/** Ensure the current agents layout: project-scoped identity indexes plus the
 * projectless-collision guard. The M3/M4 global-tag-PK rebuild was retired
 * (intake 19848714): the live DO schema is current and no pre-migration rows
 * remain, so a legacy DDL here now fails closed at the index/check below
 * instead of silently rebuilding.
 * The caller must supply the real storage transactionSync; any failure rolls back all DDL. */
export function migrateAgentRegistry(sql: SqlStorage, transaction: <T>(fn: () => T) => T): void {
  transaction(() => {
    if (!(sql.exec('PRAGMA table_info(agents)').toArray() as any[]).some(c => c.name === 'project')) {
      throw new Error('unsupported_agents_schema');
    }
    if (sql.exec("SELECT 1 FROM agents a WHERE COALESCE(a.project,'') = '' AND (SELECT COUNT(*) FROM agents b WHERE b.tag = a.tag) > 1 LIMIT 1").toArray().length) {
      throw new Error('ambiguous_legacy_registry');
    }
    sql.exec("CREATE UNIQUE INDEX IF NOT EXISTS idx_agents_project_tag_unique ON agents(COALESCE(project, ''), tag)");
    sql.exec('CREATE INDEX IF NOT EXISTS idx_agents_tag_lookup ON agents(tag)');
  });
}

/** Resolve once at issuance/migration. Empty binding is an immutable denied identity.
 * A pre-migration bare credential binds only if dated no earlier than the registration.
 * Legacy bare message ownership cannot be proven from present registry metadata. */
export function bindAgentCredentials(sql: SqlStorage, transaction: <T>(fn: () => T) => T): void {
  transaction(() => {
    const valid = registryRowSql('a.');
    const binding = (raw: string, created?: string) => `COALESCE((SELECT CASE WHEN COALESCE(a.project,'') = '' THEN a.tag ELSE a.project || ':' || a.tag END
      FROM agents a WHERE ${valid} AND
      (CASE WHEN instr(${raw},':') = 0 THEN a.tag ELSE a.project || ':' || a.tag END) = ${raw}
      AND (instr(${raw},':') > 0 OR (SELECT COUNT(*) FROM agents WHERE tag = ${raw}) = 1)
      ${created ? `AND julianday(a.registered_at) <= julianday(${created})` : ''}
      LIMIT 1), '')`;
    const cols = sql.exec('PRAGMA table_info(credentials)').toArray() as any[];
    if (!cols.some(c => c.name === 'registry_actor')) {
      sql.exec('ALTER TABLE credentials ADD COLUMN registry_actor TEXT');
      sql.exec(`UPDATE credentials SET registry_actor = ${binding("COALESCE(NULLIF(credentials.actor,''), credentials.subject)", 'credentials.created_at')} WHERE kind = 'agent'`);
    }
    const messageCols = sql.exec('PRAGMA table_info(messages)').toArray() as any[];
    for (const side of ['from','to']) {
      const flag = `registry_unbound_${side}`;
      if (!messageCols.some(c => c.name === flag)) {
        sql.exec(`ALTER TABLE messages ADD COLUMN ${flag} INTEGER NOT NULL DEFAULT 0`);
        // Reserved traffic belongs to existing human/system/root/role authority, never agents.
        sql.exec(`UPDATE messages SET ${flag} = 1 WHERE instr(${side}_actor,':') = 0 AND ${registryNameSql(side + '_actor')}`);
      }
    }
    const canonical = (raw: string) => `COALESCE(NULLIF(${binding(raw)},''), ${raw})`;
    sql.exec(`CREATE TRIGGER IF NOT EXISTS messages_bind_agents AFTER INSERT ON messages BEGIN
      UPDATE messages SET from_actor = ${canonical('NEW.from_actor')}, to_actor = ${canonical('NEW.to_actor')},
        registry_unbound_from = CASE WHEN instr(NEW.from_actor,':') = 0 AND ${registryNameSql('NEW.from_actor')} AND ${binding('NEW.from_actor')} = '' THEN 1 ELSE NEW.registry_unbound_from END,
        registry_unbound_to = CASE WHEN instr(NEW.to_actor,':') = 0 AND ${registryNameSql('NEW.to_actor')} AND ${binding('NEW.to_actor')} = '' THEN 1 ELSE NEW.registry_unbound_to END
        WHERE id = NEW.id;
      END`);
    sql.exec(`CREATE TRIGGER IF NOT EXISTS credentials_bind_agent AFTER INSERT ON credentials
      WHEN NEW.kind = 'agent' AND NEW.registry_actor IS NULL BEGIN
      UPDATE credentials SET registry_actor = ${binding("COALESCE(NULLIF(NEW.actor,''), NEW.subject)")} WHERE id = NEW.id;
      END`);
    sql.exec(`CREATE TRIGGER IF NOT EXISTS credentials_keep_binding BEFORE UPDATE OF registry_actor ON credentials
      WHEN OLD.registry_actor IS NOT NULL AND NEW.registry_actor IS NOT OLD.registry_actor
      BEGIN SELECT RAISE(ABORT, 'immutable_agent_binding'); END`);
    // Deletion ends exactly this registration's credential lifetime, including refresh tokens.
    sql.exec(`CREATE TRIGGER IF NOT EXISTS agents_revoke_credentials AFTER DELETE ON agents BEGIN
      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
      WHERE kind = 'agent' AND registry_actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;
      END`);
    // A deleted machine must not become usable again by reusing its ID.
    sql.exec(`CREATE TRIGGER IF NOT EXISTS machines_revoke_agent_credentials AFTER DELETE ON machines BEGIN
      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
      WHERE kind = 'agent' AND machine_id = OLD.id;
      END`);
    sql.exec(`CREATE TRIGGER IF NOT EXISTS agents_revoke_machine_binding AFTER UPDATE OF machine_id ON agents
      WHEN OLD.machine_id IS NOT NEW.machine_id BEGIN
      UPDATE credentials SET revoked_at = COALESCE(revoked_at, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
      WHERE kind = 'agent' AND registry_actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;
      END`);
    // Approval belongs to the current registration; deletion/reassignment cannot revive an old code.
    sql.exec(`CREATE TRIGGER IF NOT EXISTS agents_end_pairing_on_delete AFTER DELETE ON agents BEGIN
      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent'
      AND actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;
      END`);
    sql.exec(`CREATE TRIGGER IF NOT EXISTS agents_end_pairing_on_move AFTER UPDATE OF tag, project, machine_id ON agents
      WHEN OLD.tag IS NOT NEW.tag OR COALESCE(OLD.project,'') != COALESCE(NEW.project,'') OR OLD.machine_id IS NOT NEW.machine_id BEGIN
      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent'
      AND actor = CASE WHEN COALESCE(OLD.project,'') = '' THEN OLD.tag ELSE OLD.project || ':' || OLD.tag END;
      END`);
    sql.exec(`CREATE TRIGGER IF NOT EXISTS machines_end_agent_pairing AFTER DELETE ON machines BEGIN
      UPDATE device_pairings SET status = 'denied' WHERE status = 'approved' AND client_kind = 'agent' AND machine_id = OLD.id;
      END`);
    // Imported legacy rows are retained. Future writes cannot introduce projectless collisions.
    for (const event of ['INSERT','UPDATE OF tag, project']) {
      const name = event.startsWith('UPDATE') ? 'agents_no_legacy_collision_update' : 'agents_no_legacy_collision_insert';
      sql.exec(`CREATE TRIGGER IF NOT EXISTS ${name} BEFORE ${event} ON agents
        WHEN EXISTS (SELECT 1 FROM agents WHERE tag = NEW.tag AND rowid != NEW.rowid AND COALESCE(project,'') != COALESCE(NEW.project,'')
          AND (COALESCE(project,'') = '' OR COALESCE(NEW.project,'') = ''))
        BEGIN SELECT RAISE(ABORT, 'agent_exists'); END`);
    }
  });
}

export function credentialActor(sql: SqlStorage, cred: any): string | null {
  if (cred.kind !== 'agent') return cred.actor || cred.subject || null;
  // No mutable actor/subject fallback, including denied/unknown credentials.
  const actor = cred.registry_actor;
  const address = typeof actor === 'string' ? registryAddress(actor) : null;
  if (!address) return null;
  const rows = sql.exec("SELECT project, tag, machine_id FROM agents WHERE tag = ?", address.tag).toArray() as any[];
  const row = rows.find(r => (r.project || null) === address.project);
  if (!row || (row.machine_id ?? null) !== (cred.machine_id ?? null)) return null;
  if (cred.machine_id != null && !sql.exec('SELECT id FROM machines WHERE id = ? AND registered_at IS NOT NULL', cred.machine_id).toArray().length) return null;
  if (!address.project) return rows.length === 1 && !rows[0].project ? actor : null;
  return rows.some(r => r.project === address.project) ? actor : null;
}

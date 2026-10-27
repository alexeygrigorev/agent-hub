import { adoptObservedNativeSessions } from '../src/session_control';

/** Seed sessions adopted by an older release. Production census is no longer
 * authority to create new bindings; these fixtures exercise legacy control,
 * identity refresh, retirement and projection without bypassing that gate. */
export function seedLegacyObservedSessions(sql: any, machine: string, sessions: any[], now: number, family?: string | null) {
  let adopted = 0;
  for (const native of sessions) {
    if (!native || !/^[a-f0-9-]{36}$/.test(native.native_id) || !native.tag || !native.workspace) continue;
    if (sql.exec('SELECT id FROM session_control_sessions WHERE id=?', native.native_id).toArray().length) continue;
    if (sql.exec('SELECT id FROM fleet_launches WHERE machine_id=? AND native_id=?', machine, native.native_id).toArray().length) continue;
    const managed = sql.exec('SELECT binding_json FROM session_control_sessions WHERE fleet_launch_id IS NOT NULL AND machine_id=?', machine).toArray();
    if (managed.some((row: any) => JSON.parse(row.binding_json || '{}').native_id === native.native_id)) continue;
    const terminal = ['exited','dead','pruned','forgotten'].includes(native.phase) && native.confirmed_exit === true;
    const state = terminal ? 'stopped' : ['running','starting'].includes(native.phase) ? native.phase : 'uncertain';
    const at = new Date(now).toISOString();
    const created = typeof native.created_at_ms === 'number' && native.created_at_ms > 0 ? native.created_at_ms : null;
    const request = { provenance: 'observed_native', origin: 'aplexer', workspace: native.workspace,
      cwd: native.cwd ?? null, engine: native.engine ?? null, tag: native.tag,
      name: native.name || null, owner: native.owner || null, created_at_ms: created };
    const binding = { native_id: native.native_id, tag: native.tag, workspace: native.workspace,
      generation: 1, native_created_at_ms: created, provenance: 'observed_native', adopted_at: at };
    let hostFamily = family;
    if (!hostFamily) hostFamily = sql.exec('SELECT c.family_id FROM machines m LEFT JOIN credentials c ON c.id=m.credential_id WHERE m.id=?', machine).toArray()[0]?.family_id ?? null;
    const type = native.session_type === 'headless' || native.mode === 'headless' ? 'headless' : 'aplexer';
    sql.exec(`INSERT INTO session_control_sessions
      (id,actor,request_key,request_json,fleet_launch_id,machine_id,engine,mode,state,binding_json,
       parent_id,parent_source,session_type,owner_token_id,owner_family,host_family,created_at,updated_at)
      VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?, ?,NULL,NULL,?,?,?)`,
      native.native_id, native.owner || 'unknown', 'native-' + native.native_id, JSON.stringify(request), machine,
      native.engine ?? null, type === 'headless' ? 'headless' : 'interactive', state, JSON.stringify(binding),
      native.parent_id || null, native.parent_id ? 'inferred' : 'unknown_external', type, hostFamily,
      created ? new Date(created).toISOString() : at, at);
    adopted++;
  }
  const result = adoptObservedNativeSessions(sql, machine, sessions, now, family);
  return { ...result, adopted };
}

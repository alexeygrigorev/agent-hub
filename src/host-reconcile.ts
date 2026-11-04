import type { SqlStorage } from '@cloudflare/workers-types';
import { classifyKey } from './session_lifecycle_registry';

const terminal = new Set(['stopped','cancelled','failed','finished','exited','refused','dead']);
const parse = (value: any): any => { try { return JSON.parse(value || '{}'); } catch { return {}; } };
const fields = ['native_id','tag','workspace','native_created_at_ms','engine'];
const humanActor = (actor:unknown):boolean => typeof actor==='string' && (actor==='admin'||actor.startsWith('human:'));

export function validReconcileNative(value: any): boolean {
  return value && typeof value === 'object' && !Array.isArray(value) &&
    Object.keys(value).length === fields.length && fields.every(k => Object.hasOwn(value,k)) &&
    /^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(value.native_id) &&
    ['tag','workspace','engine'].every(k => typeof value[k] === 'string' && value[k].length > 0 && value[k].length <= 4096 && !/[\x00-\x1f\x7f]/.test(value[k])) &&
    Number.isSafeInteger(value.native_created_at_ms) && value.native_created_at_ms > 0;
}

/** Read-only exact-incarnation authority. No filesystem/worktree cleanup is authorized. */
export function hostReconcileFact(sql: SqlStorage, machine: string, native: any, now = Date.now()): any | null {
  if (!validReconcileNative(native)) return null;
  const observed: any = sql.exec('SELECT * FROM fleet_sessions WHERE machine_id=? AND native_id=?',machine,native.native_id).toArray()[0];
  if (!observed || observed.created_at_ms !== native.native_created_at_ms || observed.tag !== native.tag ||
      observed.workspace !== native.workspace || observed.engine !== native.engine) return null;
  const age = now - Date.parse(observed.observed_at);
  if (!Number.isFinite(age) || age < -30000 || age > 180000) return null;
  const creators = new Map<string,any>();
  const humanCustody = (actor:unknown,request:any,ownerToken?:unknown):boolean => {
    if (humanActor(actor)) return true;
    const tokens:unknown[] = ownerToken!=null ? [ownerToken] : [];
    // Provenance is never sufficient to GRANT authority. An unknown or human
    // creator hint can only protect, including when owner_token_id was lost.
    if (request?.provenance && typeof request.provenance==='object' &&
        Object.hasOwn(request.provenance,'creator_token_id')) tokens.push(request.provenance.creator_token_id);
    for (const token of new Set(tokens)) {
      if (typeof token!=='string' || !token || token.length>512) return true;
      if (!creators.has(token)) creators.set(token,
        sql.exec('SELECT kind,actor FROM credentials WHERE id=?',token).toArray()[0]);
      const creator=creators.get(token);
      if (!creator || !['host','agent','job'].includes(creator.kind) || humanActor(creator.actor)) return true;
    }
    return false;
  };
  // Bound retained identity candidates before reading request JSON or making
  // negative authority decisions. Duplicate aliases are deliberately not
  // filtered by state: any live/protected alias must continue to block.
  const launchKeys: any[] = sql.exec(`SELECT rowid AS source_rowid,id FROM fleet_launches
    INDEXED BY fleet_launch_native_state WHERE machine_id=? AND native_id=? LIMIT 129`,machine,native.native_id).toArray();
  if (launchKeys.length > 128) return null;
  const launches: any[] = launchKeys.length ? sql.exec(`SELECT l.* FROM json_each(?) ids
    CROSS JOIN fleet_launches l ON l.rowid=ids.value`,JSON.stringify(launchKeys.map(row=>row.source_rowid))).toArray() : [];
  if (!launches.length || launches.some(l => l.mode !== 'headless' || l.engine !== native.engine || l.requester_kind==='human' || humanCustody(l.actor,parse(l.request_json)) || parse(l.request_json).keep === true)) return null;
  const aliasKeys: any[] = sql.exec(`SELECT rowid AS source_rowid FROM session_control_sessions
    INDEXED BY intake_queue_session_native WHERE machine_id=?
    AND json_extract(CASE WHEN json_valid(binding_json) THEN binding_json ELSE '{}' END,'$.native_id')=? LIMIT 129`,
    machine,native.native_id).toArray();
  if (aliasKeys.length > 128) return null;
  const aliasIds = new Set(aliasKeys.map(row=>row.source_rowid));
  // Each launch's alias range is sought by the existing binding index. The
  // one GLOBAL raw sentinel caps the union before any binding/request parse.
  const launchAliases: any[] = sql.exec(`SELECT s.rowid AS source_rowid FROM json_each(?) ids
    CROSS JOIN session_control_sessions s INDEXED BY session_control_fleet_lookup
    ON s.fleet_launch_id=ids.value AND s.machine_id=? LIMIT 129`,
    JSON.stringify(launchKeys.map(row=>row.id)),machine).toArray();
  if (launchAliases.length > 128) return null;
  for (const row of launchAliases) aliasIds.add(row.source_rowid);
  if (aliasIds.size > 128) return null;
  const aliases: any[] = aliasIds.size ? sql.exec(`SELECT s.* FROM json_each(?) ids
    CROSS JOIN session_control_sessions s ON s.rowid=ids.value`,JSON.stringify([...aliasIds].sort((a,b)=>a-b))).toArray() : [];
  const governing: any[] = [];
  for (const alias of aliases) {
    const binding = parse(alias.binding_json), request = parse(alias.request_json);
    // Authority is an intersection of ALL canonical custody rows, not just
    // the oldest alias selected by the lifecycle display. A newer human
    // alias/credential must not disappear behind an older agent-owned row.
    if (humanCustody(alias.actor,request,alias.owner_token_id)) return null;
    if (parse(alias.request_json).keep === true || sql.exec('SELECT session_id FROM session_control_protections WHERE session_id=?',alias.id).toArray().length) return null;
    if (binding.native_id !== native.native_id || binding.native_created_at_ms !== native.native_created_at_ms ||
        binding.tag !== native.tag || binding.workspace !== native.workspace || alias.engine !== native.engine) return null;
    // An observed-native shadow is a catalog projection, not live work
    // custody. It cannot hide that the original managed Hub row vanished.
    if (alias.id === native.native_id && alias.request_key === 'native-' + native.native_id &&
        request.provenance === 'observed_native' && binding.provenance === 'observed_native') continue;
    if (alias.mode !== 'headless' || !alias.fleet_launch_id || !terminal.has(alias.state)) return null;
    governing.push(alias);
  }
  if (!governing.length) {
    // Routing jobs legitimately execute directly through Fleet without a
    // SessionControl row. They are not missing-session orphans.
    for (const launch of launches) if (sql.exec('SELECT id FROM routing_jobs INDEXED BY routing_launch WHERE launch_id=? LIMIT 1',launch.id).toArray().length && !terminal.has(launch.state)) return null;
  }
  const classified = classifyKey(sql, machine + '/' + native.native_id, now, {machine_id:machine,native_id:native.native_id});
  if (classified?.refs?.protected_session !== false) return null;
  return {...native,mode:'headless',managed:true,protected:false,
    state:governing.length ? 'finished' : 'missing'};
}

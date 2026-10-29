import type { SqlStorage } from '@cloudflare/workers-types';
import { classifyKey } from './session_lifecycle_registry';

const terminal = new Set(['stopped','cancelled','failed','finished','exited','refused','dead']);
const parse = (value: any): any => { try { return JSON.parse(value || '{}'); } catch { return {}; } };
const fields = ['native_id','tag','workspace','native_created_at_ms','engine'];

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
  const launches: any[] = sql.exec('SELECT * FROM fleet_launches WHERE machine_id=? AND native_id=?',machine,native.native_id).toArray();
  if (!launches.length || launches.some(l => l.mode !== 'headless' || l.engine !== native.engine || parse(l.request_json).keep === true)) return null;
  const aliases: any[] = sql.exec(`SELECT s.* FROM session_control_sessions s
    WHERE s.machine_id=? AND (json_extract(CASE WHEN json_valid(s.binding_json) THEN s.binding_json ELSE '{}' END,'$.native_id')=?
    OR s.fleet_launch_id IN (SELECT id FROM fleet_launches WHERE machine_id=? AND native_id=?))`,
    machine,native.native_id,machine,native.native_id).toArray();
  const governing: any[] = [];
  for (const alias of aliases) {
    const binding = parse(alias.binding_json), request = parse(alias.request_json);
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
    for (const launch of launches) if (sql.exec('SELECT id FROM routing_jobs WHERE launch_id=?',launch.id).toArray().length && !terminal.has(launch.state)) return null;
  }
  const classified = classifyKey(sql, machine + '/' + native.native_id, now);
  if (classified?.refs?.protected_session !== false) return null;
  return {...native,mode:'headless',managed:true,protected:false,
    state:governing.length ? 'finished' : 'missing'};
}

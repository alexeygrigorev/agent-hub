import type { SqlStorage } from '@cloudflare/workers-types';
import type { FleetService } from './fleet';
import { canonicalQuotaProvider, dedupeAccountsByCanonical } from './quota';
import { queuePolicyFrom } from './intake-watchdog';
import routingPolicy from '../config/model-routing.json';

const OWNED_ACTIVE = `actor='hub:service:intake-watchdog'
  AND json_extract(CASE WHEN json_valid(request_json) THEN request_json ELSE '{}' END,'$.with_goal')=1
  AND state IN ('queued','held','offered','starting','running','uncertain')`;
const parse = (s: any) => { try { return JSON.parse(s || '{}'); } catch { return {}; } };
const finite = (n: any): n is number => typeof n === 'number' && Number.isFinite(n);

/** Queue budget only: canonical Fleet placement and host admission remain authoritative.
 * Count each account once across hosts, each host once across accounts. Taking their
 * minimum is an upper bound; incompatible routes/workspaces can still hold in Fleet.
 * No new slots are inferred from stale/offline hosts or unknown quota/resource data.
 */
export function liveQueuePolicy(env: Record<string, unknown>, sql: SqlStorage,
  fleet: Pick<FleetService, 'accountStatus' | 'resources' | 'withMaintenance'>, now = Date.now()) {
  if (env.HUB_INTAKE_QUEUE_MAX_INFLIGHT !== 'auto') return queuePolicyFrom(env);
  let active = 0, machineSlots = 0, accountSlots = 0;
  try {
    active = Number(sql.exec(`SELECT COUNT(*) AS n FROM session_control_sessions WHERE ${OWNED_ACTIVE}`).one().n);
    const definitions = sql.exec('SELECT * FROM fleet_accounts LIMIT 129').toArray() as any[];
    if (definitions.length > 128) throw new Error('capacity_observation_limited');
    const accounts = fleet.withMaintenance(() => dedupeAccountsByCanonical(definitions)
      .filter(a => a.enabled && !routingPolicy.rules.disabled_providers.includes(canonicalQuotaProvider(a.provider)) &&
        !routingPolicy.rules.explicit_only_providers.includes(canonicalQuotaProvider(a.provider)))
      .map(a => fleet.accountStatus(a))).filter(a => a.enabled && a.supports_goal && a.status === 'ok' &&
        !routingPolicy.rules.disabled_providers.includes(a.provider) &&
        !routingPolicy.rules.explicit_only_providers.includes(a.provider));
    const reachable = new Set(accounts.flatMap(a => a.placeable_machines));
    const machines = sql.exec(`SELECT m.id,m.last_checkin_at,m.last_heartbeat,r.max_agents FROM machines m
      LEFT JOIN routing_machines r ON r.machine_id=m.id
      WHERE m.drained=0 AND m.revoked_at IS NULL LIMIT 65`).toArray() as any[];
    if (machines.length > 64) throw new Error('capacity_observation_limited');
    const usable = new Set<string>();
    for (const m of machines) {
      const at = Date.parse(m.last_checkin_at), hb = parse(m.last_heartbeat);
      if (!reachable.has(m.id) || !Number.isFinite(at) || at > now + 60000 || now - at > 180000) continue;
      const memory = fleet.resources.headroom(m.id), config = fleet.resources.config();
      if (!finite(hb.disk_free_gb) || hb.disk_free_gb < config.disk_floor_gb ||
        !fleet.resources.cpuHeadroom(m.id).admitted || !finite(memory.mem_available_mb) ||
        !finite(memory.requested_mb) || memory.requested_mb <= 0) continue;
      let slots = Math.max(0, Math.floor((memory.mem_available_mb - memory.reserved_mb - memory.floor_mb) / memory.requested_mb));
      // Honor an advertised machine ceiling without changing it. Native residents
      // and unbound launch reservations occupy distinct slots.
      if (Number.isInteger(m.max_agents) && m.max_agents > 0) {
        const occupied = Number(sql.exec(`SELECT
          (SELECT COUNT(*) FROM fleet_sessions WHERE machine_id=? AND phase='running') +
          (SELECT COUNT(*) FROM fleet_launches WHERE machine_id=? AND native_id IS NULL
            AND state IN ('queued','offered','starting','running','uncertain')) AS n`, m.id, m.id).one().n);
        slots = Math.min(slots, Math.max(0, m.max_agents - occupied));
      }
      if (!slots) continue;
      machineSlots += slots;
      usable.add(m.id);
    }
    for (const a of accounts) {
      if (a.placeable_machines.some(id => usable.has(id)) && finite(a.placeable_slots)) accountSlots += Math.max(0, a.placeable_slots);
    }
  } catch { machineSlots = 0; accountSlots = 0; }
  const available = Math.min(machineSlots, accountSlots);
  const policy = queuePolicyFrom(env, active + available);
  return { ...policy, capacity: { source: 'live_host_account', active, machine_slots: machineSlots,
    account_slots: accountSlots, available_slots: available, observation_limit: 32 } };
}

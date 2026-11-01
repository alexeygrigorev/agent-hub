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
  fleet: Pick<FleetService, 'accountStatus' | 'resources' | 'queueCapacitySnapshot'>, now = Date.now()) {
  if (env.HUB_INTAKE_QUEUE_MAX_INFLIGHT !== 'auto') return queuePolicyFrom(env);
  let active = 0, machineSlots = 0, accountSlots = 0, observationLimited = false;
  try {
    active = sql.exec(`SELECT id FROM session_control_sessions WHERE ${OWNED_ACTIVE} LIMIT 33`).toArray().length;
    if (active > 32) throw new Error('capacity_observation_limited');
    const snapshot = fleet.queueCapacitySnapshot(now);
    const accounts = dedupeAccountsByCanonical(snapshot.definitions as any[])
      .filter(a => a.enabled && !routingPolicy.rules.disabled_providers.includes(canonicalQuotaProvider(a.provider)) &&
        !routingPolicy.rules.explicit_only_providers.includes(canonicalQuotaProvider(a.provider)))
      .map(a => fleet.accountStatus(a, snapshot)).filter(a => a.enabled && a.supports_goal && a.status === 'ok' &&
        !routingPolicy.rules.disabled_providers.includes(a.provider) &&
        !routingPolicy.rules.explicit_only_providers.includes(a.provider));
    const reachable = new Set(accounts.flatMap(a => a.placeable_machines));
    const machines = snapshot.machines;
    const usable = new Set<string>();
    fleet.resources.withReadSnapshot(() => {
      for (const m of machines) {
        const at = Date.parse(m.last_checkin_at), hb = parse(m.last_heartbeat);
        if (!reachable.has(m.id) || !Number.isFinite(at) || at > now + 60000 || now - at > 180000) continue;
        const memory = fleet.resources.headroom(m.id), config = fleet.resources.config();
        if (!finite(hb.disk_free_gb) || hb.disk_free_gb < config.disk_floor_gb ||
          !fleet.resources.capacityCpuAdmitted(m.id) || !finite(memory.mem_available_mb) ||
          !finite(memory.requested_mb) || memory.requested_mb <= 0) continue;
        let slots = Math.max(0, Math.floor((memory.mem_available_mb - memory.reserved_mb - memory.floor_mb) / memory.requested_mb));
        // Honor an advertised machine ceiling without changing it. Native residents
        // and unbound launch reservations occupy distinct slots.
        if (Number.isInteger(m.max_agents) && m.max_agents > 0) {
          const occupied = snapshot.natives.filter(n => n.machine_id === m.id && n.phase === 'running').length +
            snapshot.launches.filter(l => l.machine_id === m.id && l.native_id === null).length;
          slots = Math.min(slots, Math.max(0, m.max_agents - occupied));
        }
        if (!slots) continue;
        machineSlots += slots;
        usable.add(m.id);
      }
    });
    for (const a of accounts) {
      if (a.placeable_machines.some(id => usable.has(id)) && finite(a.placeable_slots)) accountSlots += Math.max(0, a.placeable_slots);
    }
  } catch (error) {
    observationLimited = error instanceof Error && error.message === 'capacity_observation_limited';
    machineSlots = 0; accountSlots = 0;
  }
  const available = Math.min(machineSlots, accountSlots);
  const policy = queuePolicyFrom(env, active + available);
  return { ...policy, capacity: { source: 'live_host_account', active, machine_slots: machineSlots,
    account_slots: accountSlots, available_slots: available, observation_limit: 32, observation_limited: observationLimited } };
}

/**
 * Machine aliasing: two machine records for one physical host.
 *
 * The stale record `hetzner` (offline ~220h) and the live host
 * `urgent-hetzner` report the same physical host (RMTHZ). Records are
 * never deleted without operator approval — the stale id is marked as an
 * alias of the live (canonical) record instead.
 *
 * Grouping key is the normalized hostname (case-insensitive). Records with
 * an empty hostname never alias. Within a group the canonical record is the
 * non-drained member with the freshest check-in; ties break by id. Every
 * other member aliases to it.
 */

export type AliasRow = {
  id: string;
  hostname: unknown;
  drained: unknown;
  /** Effective check-in epoch ms, or null when never seen. */
  last_seen_ms: number | null;
};

export function normalizeHostname(hostname: unknown): string | null {
  if (typeof hostname !== 'string') return null;
  const trimmed = hostname.trim();
  if (!trimmed) return null;
  return trimmed.toUpperCase();
}

export type AliasResolution = {
  /** alias machine id -> canonical machine id (only non-canonical members). */
  aliasOf: Map<string, string>;
  /** canonical machine id -> all member ids (including itself), sorted. */
  membersOf: Map<string, string[]>;
};

export function resolveMachineAliases(rows: AliasRow[]): AliasResolution {
  const aliasOf = new Map<string, string>();
  const membersOf = new Map<string, string[]>();
  const groups = new Map<string, AliasRow[]>();
  const solo: AliasRow[] = [];
  for (const row of rows) {
    const key = normalizeHostname(row.hostname);
    if (!row.id || key === null) {
      if (row.id) solo.push(row);
      continue;
    }
    const list = groups.get(key);
    if (list) list.push(row);
    else groups.set(key, [row]);
  }
  const rank = (row: AliasRow): [number, number, string] => {
    const live = row.drained ? 0 : 1;
    const seen = typeof row.last_seen_ms === 'number' && Number.isFinite(row.last_seen_ms)
      ? row.last_seen_ms
      : Number.NEGATIVE_INFINITY;
    return [live, seen, row.id];
  };
  const pickCanonical = (members: AliasRow[]): AliasRow => {
    let best = members[0];
    let bestRank = rank(best);
    for (const candidate of members.slice(1)) {
      const candidateRank = rank(candidate);
      if (
        candidateRank[0] > bestRank[0] ||
        (candidateRank[0] === bestRank[0] && candidateRank[1] > bestRank[1]) ||
        (candidateRank[0] === bestRank[0] && candidateRank[1] === bestRank[1] && candidateRank[2] < bestRank[2])
      ) {
        best = candidate;
        bestRank = candidateRank;
      }
    }
    return best;
  };
  for (const members of groups.values()) {
    if (members.length < 2) {
      membersOf.set(members[0].id, [members[0].id]);
      continue;
    }
    const canonical = pickCanonical(members);
    const ids = members.map((m) => m.id).sort();
    membersOf.set(canonical.id, ids);
    for (const member of members) {
      if (member.id !== canonical.id) aliasOf.set(member.id, canonical.id);
    }
  }
  for (const row of solo) membersOf.set(row.id, [row.id]);
  return { aliasOf, membersOf };
}

/** Canonical id for a machine, falling back to the id itself when unaliased. */
export function canonicalMachineId(id: string, resolution: AliasResolution): string {
  return resolution.aliasOf.get(id) ?? id;
}

export function aliasDetail(aliasId: string, canonicalId: string, hostname: unknown): string {
  const host = typeof hostname === 'string' && hostname.trim() ? hostname.trim() : 'unknown host';
  return `alias of ${canonicalId} (same physical host ${host}; reporting via ${canonicalId})`;
}

/** Alias membership proves the host identity, never a session's liveness.
 * Copy a fresher matching native observation; otherwise stale evidence stays
 * unknown even while the canonical host is online. Source rows are untouched.
 */
export function projectAliasSessions(rows: any[], aliasOf: Map<string, string>, now: number, catalogSeen = new Map<string, number>()): any[] {
  const byNative = new Map(rows.map(s => [`${s.machine_id}/${s.native_id}`, s]));
  return rows.map(original => {
    const canonical = aliasOf.get(String(original.machine_id));
    if (!canonical) return { ...original, resolved_machine_id: original.machine_id };
    const current = byNative.get(`${canonical}/${original.native_id}`);
    const censusAt = catalogSeen.get(canonical);
    const freshCensus = typeof censusAt === 'number' && censusAt <= now + 60000 && now - censusAt <= 180000;
    const source = current && (freshCensus || Date.parse(current.observed_at) > Date.parse(original.observed_at)) ? current : original;
    const at = current && source === current && freshCensus ? censusAt! : Date.parse(source.observed_at);
    const fresh = Number.isFinite(at) && at <= now + 60000 && now - at <= 180000;
    return { ...source, machine_id: original.machine_id, resolved_machine_id: canonical,
      phase: fresh ? source.phase : 'unknown', agent_state: fresh ? source.agent_state : null,
      alias_observation: fresh ? 'observed' : 'no_current_native_observation' };
  });
}

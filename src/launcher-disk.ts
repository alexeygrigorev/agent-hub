/** Read-only projection of the maintained headless launcher's disk gate. */
export function launcherDiskHeadroom(hb: any, pendingIds: string[], configuredFloorGb: number, workspace?: string, cwd?: string) {
  const r = hb?.launcher_resources;
  if (!r || !['active_disk_mb', 'disk_floor_mb', 'next_disk_mb'].every(k =>
      typeof r[k] === 'number' && Number.isFinite(r[k]) && r[k] >= 0 && r[k] <= 1e9)) return null;
  const aliases = r.disk_aliases && typeof r.disk_aliases === 'object' ? r.disk_aliases : {};
  const base = typeof workspace === 'string' && Object.hasOwn(aliases, workspace) ? aliases[workspace] : workspace;
  const target = typeof cwd === 'string' && cwd !== '.' ? (cwd.startsWith('/') ? (Object.hasOwn(aliases, cwd) ? aliases[cwd] : cwd) :
    typeof base === 'string' && base.startsWith('/') ? base.replace(/\/$/, '') + '/' + cwd : null) : base;
  const reading = (path: unknown) => Array.isArray(r.disk_paths) && typeof path === 'string' ? r.disk_paths
    .filter((p: any) => p && typeof p.path === 'string' && (path === p.path || path.startsWith(p.path.replace(/\/$/, '') + '/')))
    .sort((a: any, b: any) => b.path.length - a.path.length)[0]?.free_gb : null;
  // The native backend uses workspace/.local/tmp and the selected execution
  // directory. Both filesystems must fit. Unknown alias mapping is advisory.
  const workspaceFree = reading(base), tempFree = reading(typeof base === 'string' ? base.replace(/\/$/, '') + '/.local/tmp' : null), cwdFree = reading(target);
  if (typeof base !== 'string' || !base.startsWith('/')) return null;
  const free = Math.min(tempFree ?? workspaceFree ?? hb.disk_free_gb, cwdFree ?? workspaceFree ?? hb.disk_free_gb);
  if (!Number.isFinite(free)) return null;
  const covered = new Set(Array.isArray(r.disk_task_ids) ? r.disk_task_ids : []);
  const pending = pendingIds.filter(id => !covered.has('hub-' + id)).length;
  const floor = Math.max(configuredFloorGb * 1024, r.disk_floor_mb);
  const required = floor + r.active_disk_mb + (pending + 1) * r.next_disk_mb;
  return { disk_free_gb: free, disk_floor_gb: floor / 1024,
    reserved_disk_mb: r.active_disk_mb, pending_disk_mb: pending * r.next_disk_mb,
    requested_disk_mb: r.next_disk_mb, required_disk_gb: required / 1024,
    admitted: Number.isFinite(free) && free * 1024 >= required };
}

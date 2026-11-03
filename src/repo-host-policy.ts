/** Host policy and path comparisons for repository checkout evidence. */
export function allowedMachinesValid(value: unknown): boolean {
  return value === null || (Array.isArray(value) && value.length <= 256 &&
    value.every(id => typeof id === 'string' && /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(id)) &&
    new Set(value).size === value.length);
}

/** null/absent means unrestricted; an explicit empty set admits no hosts. */
export function repoAllowsMachine(allowed: unknown, machine: string): boolean {
  return allowed === undefined || allowed === null ||
    (allowedMachinesValid(allowed) && (allowed as string[]).includes(machine));
}

export function repoPathKey(value: unknown): string | null {
  if (typeof value !== 'string' || value.length > 512 || /[\x00-\x1f\x7f]/.test(value)) return null;
  const windows = /^[A-Za-z]:[\\/]/.test(value);
  if (!windows && !value.startsWith('/')) return null;
  const normalized = windows ? value.replace(/\\/g, '/').toLowerCase() : value;
  if (normalized.split('/').some(part => part === '.' || part === '..')) return null;
  return normalized.replace(/\/+$/, '') || '/';
}

/** A declared repo path counts; a generic root never invents a child checkout. */
export function advertisedRepoPaths(capabilities: any, name: string): string[] {
  const workspaces = capabilities?.workspaces;
  const paths: [string | null, unknown][] = Array.isArray(workspaces) ? workspaces.map(path => [null, path]) :
    workspaces && typeof workspaces === 'object' ? Object.entries(workspaces) : [];
  if (Array.isArray(capabilities?.workspace_roots)) {
    paths.push(...capabilities.workspace_roots.map((path: unknown): [null, unknown] => [null, path]));
  }
  const found = new Map<string, string>();
  for (const [label, path] of paths) {
    const key = repoPathKey(path);
    if (!key) continue;
    const leaf = key.slice(key.lastIndexOf('/') + 1);
    const mapped = label?.toLowerCase() === name.toLowerCase();
    if (!mapped && leaf !== (/^[A-Za-z]:/.test(String(path)) ? name.toLowerCase() : name)) continue;
    found.set(key, String(path));
  }
  return [...found.values()];
}

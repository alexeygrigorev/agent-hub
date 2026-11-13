/** Canonical repository-origin workspace identities shared with the host. */
export const QUALIFIED_REPOSITORY_PREFIX = 'repo-';
export const QUALIFIED_REPOSITORY_WORKSPACE = /^repo-[0-9a-f]{59}$/;
export const REPOSITORY_IDENTITY_CAPABILITY = 'repo-identity:v1';

/**
 * Normalize a registry URL or local Git remote URL to a credential-free key.
 * HTTPS/SSH and scp-style SSH spellings share a key; GitHub owner/repo casing
 * and the optional `.git` suffix do not affect identity.
 */
export function normalizeRepositoryOrigin(value: unknown): string | undefined {
  if (typeof value !== 'string' || !value || new TextEncoder().encode(value).byteLength > 2048)
    return undefined;
  const raw = value.trim();
  if (!raw || /[\x00-\x1f\x7f]/.test(raw)) return undefined;

  let host: string | undefined;
  let path: string | undefined;
  let port: string | undefined;
  let scheme = 'ssh';
  if (!raw.includes('://')) {
    const scp = /^(?:[^@/:\s]+@)?([^/:?#\s]+):([^?#]+)$/.exec(raw);
    if (!scp) return undefined;
    host = scp[1];
    path = scp[2];
  } else {
    try {
      const url = new URL(raw);
      if (!['http:', 'https:', 'ssh:', 'git+ssh:'].includes(url.protocol)
          || url.search || url.hash || !url.hostname) return undefined;
      scheme = url.protocol === 'git+ssh:' ? 'ssh' : url.protocol.slice(0, -1);
      host = url.hostname;
      path = url.pathname;
      port = url.port || undefined;
    } catch {
      return undefined;
    }
  }

  host = host?.toLowerCase().replace(/\.$/, '');
  if (!host || /[\s\\/?#@]/.test(host)) return undefined;
  if (port) {
    const portNumber = Number(port);
    if (!Number.isInteger(portNumber) || portNumber < 1 || portNumber > 65535) return undefined;
    const defaultPort = scheme === 'http' ? '80' : scheme === 'https' ? '443' : '22';
    if (port === defaultPort) port = undefined;
  }

  const rawParts = path?.replace(/^\/+|\/+$/g, '').split('/');
  if (!rawParts || rawParts.length < 2 || rawParts.some(part => !part)) return undefined;
  const parts: string[] = [];
  for (const rawPart of rawParts) {
    if (/%(?:2f|5c)/i.test(rawPart)) return undefined;
    let part: string;
    try { part = decodeURIComponent(rawPart); } catch { return undefined; }
    if (!part || part === '.' || part === '..' || /[\x00-\x20\x7f/\\?#]/.test(part)) return undefined;
    parts.push(part);
  }
  if (parts.at(-1)!.toLowerCase().endsWith('.git')) parts[parts.length - 1] = parts.at(-1)!.slice(0, -4);
  if (!parts.at(-1)) return undefined;
  if (host === 'github.com') {
    for (let index = 0; index < parts.length; index++) parts[index] = parts[index].toLowerCase();
  }

  const authority = host.includes(':') && !host.startsWith('[') ? `[${host}]` : host;
  return `${authority}${port ? `:${port}` : ''}/${parts.join('/')}`;
}

/** Produce a <=64-character identity key from a canonical registry URL.
 * The 236-bit token is an identity binding, not an authorization grant. Hosts
 * still require a unique full-origin match and reread that origin at launch.
 */
export async function qualifiedRepositoryWorkspace(value: unknown): Promise<string | undefined> {
  const origin = normalizeRepositoryOrigin(value);
  if (!origin) return undefined;
  try {
    const bytes = new TextEncoder().encode(origin);
    const digest = new Uint8Array(await crypto.subtle.digest('SHA-256', bytes));
    const hex = [...digest].map(byte => byte.toString(16).padStart(2, '0')).join('');
    return QUALIFIED_REPOSITORY_PREFIX + hex.slice(0, 59);
  } catch {
    return undefined;
  }
}

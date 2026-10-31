/**
 * Canonical machine identifier boundary validation.
 *
 * Machine IDs are issued by the Hub (e.g. via 16-byte base64url randomString(16))
 * or registered during host enrollment. Genuine base64url characters include
 * [A-Za-z0-9_-], and valid machine IDs may start with '-' or '_' as well as
 * alphanumeric characters.
 *
 * Length is bounded between 1 and 64 characters.
 */
export const MACHINE_ID_REGEX = /^[A-Za-z0-9_-]{1,64}$/;

export function isValidMachineId(value: unknown): value is string {
  return typeof value === 'string' && MACHINE_ID_REGEX.test(value);
}

/**
 * Resolve an operator machine reference to the Hub machine id. An exact id
 * wins; otherwise a unique case-insensitive enrolled name or hostname match
 * (e.g. `DESKTOP-FP4OP26`) resolves. Unknown or ambiguous references are
 * returned unchanged so placement holds with a visible rejected[] reason.
 */
export function resolveMachineRef(sql: { exec: (query: string, ...args: unknown[]) => { toArray(): unknown[] } }, ref: string): string {
  try {
    if (sql.exec('SELECT id FROM machines WHERE id = ?', ref).toArray().length) return ref;
    const rows = sql.exec('SELECT id FROM machines WHERE lower(name) = lower(?) OR lower(hostname) = lower(?)', ref, ref).toArray() as { id: unknown }[];
    return rows.length === 1 ? String(rows[0].id) : ref;
  } catch {
    return ref;
  }
}

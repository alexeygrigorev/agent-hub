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

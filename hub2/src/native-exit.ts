/** Returncode convention: preserve raw codes, encode Unix signal termination as -signal. */
export function nativeReturncode(value: unknown): number | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const { code, signal } = value as { code?: unknown; signal?: unknown };
  if (typeof code === 'number' && Number.isInteger(code) && code >= -2147483648 && code <= 2147483647 && signal == null) return code;
  if (code === null && typeof signal === 'number' && Number.isInteger(signal) && signal >= 1 && signal <= 127) return -signal;
  return undefined;
}

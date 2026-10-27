import { redactExitText } from './exit-evidence';

function redactCommandText(text: string): string {
  return redactExitText(text.replace(/-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)/g, '[redacted private key]'))
    .replace(/sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{8,}/g, '[redacted]');
}

/** Validate and re-redact host command evidence before storage or replay. */
export function sanitizeCommandFields(value: any): boolean {
  const keys = ['command', 'stdout_tail', 'stderr_tail'];
  if (value.exit_code !== undefined && !Number.isSafeInteger(value.exit_code)) return false;
  if (keys.every(key => value[key] === undefined)) return true; // Legacy diagnostics.
  if (typeof value.command !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}$/.test(value.command)) return false;
  for (const key of ['stdout_tail', 'stderr_tail']) {
    if (typeof value[key] !== 'string' || value[key].length > 512 || /[\x00-\x1f\x7f]/.test(value[key])) return false;
  }
  const command = redactCommandText(value.command);
  value.command = command === value.command ? command : 'redacted';
  for (const key of ['stdout_tail', 'stderr_tail']) value[key] = redactCommandText(value[key]).slice(-512);
  return true;
}

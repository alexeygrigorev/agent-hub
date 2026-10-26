import { redactForBus } from './work_claim';

/** Redact the full bounded window before truncating any published field. */
export function redactExitText(text: string): string {
  if (text.length > 65536) return '';
  text = text.replace(/\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))/g, '')
    .replace(/\bBearer\s+[^\s"',;]+/gi, 'Bearer [redacted]')
    .replace(/(["']?\b(?:password|passwd|secret|token|access_token|refresh_token|api[_-]?key|authorization|cookie|set-cookie|aws_secret_access_key)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;]+)/gi, '$1[redacted]')
    .replace(/((?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*/gim, '$1[redacted]');
  return String(redactForBus(text)).replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
}

/** Host evidence is informational; it never establishes native retirement. */
export function sanitizeExitEvidence(raw: any): any | null {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw) ||
      Object.keys(raw).some(k => !['exit_code', 'exit_signal', 'wall_time_ms', 'log_tail', 'engine_outcome', 'observed_commits', 'dirty_paths', 'outcome'].includes(k))) return null;
  for (const k of ['exit_code', 'exit_signal', 'wall_time_ms']) {
    if (raw[k] !== null && (!Number.isSafeInteger(raw[k]) || (k !== 'exit_code' && raw[k] < 0))) return null;
  }
  if (typeof raw.log_tail !== 'string' || raw.log_tail.length > 2048 ||
      ![null, 'succeeded', 'failed', 'cancelled'].includes(raw.engine_outcome) ||
      !['reported', 'failed-with-partial-work', 'failed-without-report'].includes(raw.outcome) ||
      !Array.isArray(raw.observed_commits) || raw.observed_commits.length > 8 || raw.observed_commits.some((s: any) => typeof s !== 'string' || !/^[a-f0-9]{40}$/.test(s)) ||
      !Array.isArray(raw.dirty_paths) || raw.dirty_paths.length > 16 || raw.dirty_paths.some((s: any) => typeof s !== 'string' || s.length > 256)) return null;
  const clean = redactExitText;
  return { ...raw, log_tail: clean(raw.log_tail).slice(0, 2048).replace(/[\uD800-\uDBFF]$/, ''), dirty_paths: raw.dirty_paths.map((s: string) => clean(s).slice(0, 256).replace(/[\uD800-\uDBFF]$/, '')) };
}

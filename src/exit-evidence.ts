import { redactForBus } from './work_claim';

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
  const clean = (s: string) => String(redactForBus(s))
    .replace(/\bBearer\s+[^\s"',;]+/gi, 'Bearer [REDACTED]')
    .replace(/[\x00-\x08\x0b-\x1f\x7f]/g, '');
  return { ...raw, log_tail: clean(raw.log_tail), dirty_paths: raw.dirty_paths.map(clean) };
}

import { describe, it, expect } from 'vitest';
import { redactExitText, sanitizeExitEvidence } from '../src/exit-evidence';

const evidence = () => ({ exit_code: 0, exit_signal: null, wall_time_ms: 2100,
  log_tail: 'worked then exited', engine_outcome: 'succeeded', observed_commits: ['a'.repeat(40)],
  dirty_paths: [' M host/example.py'], outcome: 'failed-with-partial-work' });

describe('exit evidence boundary', () => {
  it('removes complete multiline quoted headers before line filtering', () => {
    for (const key of ['authorization', 'Cookie', 'Set-Cookie']) {
      for (const quote of ['"', "'"]) {
        const text = `${key}: ${quote}prefix\npanic: short-test-value\nend${quote}`;
        expect(redactExitText(text)).not.toContain('short-test-value');
        expect(sanitizeExitEvidence({ ...evidence(), log_tail: text }).log_tail).not.toContain('short-test-value');
      }
    }
  });
  it('preserves measured exit data and partial work, sanitizes before storage', () => {
    const raw = evidence();
    raw.log_tail = 'Bearer sensitive-value\nhubt_fake-private-value\x00';
    const result = sanitizeExitEvidence(raw);
    expect(result.exit_code).toBe(0);
    expect(result.wall_time_ms).toBe(2100);
    expect(result.observed_commits).toEqual(['a'.repeat(40)]);
    expect(result.log_tail).not.toContain('sensitive-value');
    expect(result.log_tail).not.toContain('fake-private-value');
    expect(result.log_tail).not.toContain('\x00');
  });
  it('rejects unbounded or invented measurements and unknown fields', () => {
    for (const changes of [{ wall_time_ms: -1 }, { exit_signal: Infinity }, { log_tail: 'x'.repeat(2049) },
      { observed_commits: ['not-sha'] }, { dirty_paths: ['x'.repeat(257)] }, { secret: 'value' }]) {
      expect(sanitizeExitEvidence({ ...evidence(), ...changes })).toBeNull();
    }
    expect(sanitizeExitEvidence({ ...evidence(), exit_code: null, wall_time_ms: null })).not.toBeNull();
  });
});

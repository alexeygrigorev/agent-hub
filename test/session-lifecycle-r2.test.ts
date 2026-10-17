import { describe, it, expect } from 'vitest';
import {
  classifySession,
  planCleanup,
  validateCleanupTarget,
  validateOwnershipTransfer,
  sanitizeObservationText,
  toHandoffDto,
} from '../src/session_lifecycle';

const NOW = 1792238400000; // 2026-10-17T12:00:00Z
const base = (overrides: Record<string, unknown> = {}) => ({
  machine_id: 'synth-host',
  native_id: '11111111-1111-4111-8111-111111111111',
  tag: 'synth:alpha',
  engine: 'synth-engine',
  workspace: '/synth/ws',
  starter: 'synth-starter',
  owner: 'synth-starter',
  owner_provenance: 'starter' as const,
  human_owned: false,
  task_refs: ['synth-task'],
  last_activity_at: '2026-10-17T10:00:00Z',
  observed_at: '2026-10-17T10:05:00Z',
  phase: 'running',
  ...overrides,
});

describe('m15 r2 C1 superseded needs proof and fresh evidence', () => {
  it('unproven newer-activity superseded never finishes', () => {
    const r = classifySession(
      base({ superseded_by: 'synth-next', report_written: true, screen_done: true, report_at: '2026-10-17T09:00:00Z', last_activity_at: '2026-10-17T11:30:00Z' }),
      { now_ms: NOW },
    );
    expect(r.group).not.toBe('finished_stale');
    // Migrated Python author negative: stale banner with proof still blocked by newer activity.
    const staleBanner = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T09:00:00Z', last_activity_at: '2026-10-17T11:30:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(staleBanner.group).not.toBe('finished_stale');
    const provenFresh = classifySession(
      base({ superseded_by: 'synth-next', report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(provenFresh.state).toBe('superseded');
  });
});

describe('m15 r2 C2 terminal and report freshness fail closed', () => {
  it('terminal older than activity, corrupt/future values never finish', () => {
    const resumed = classifySession(
      base({ terminal_evidence: 'explicit_completion', terminal_evidence_at: '2026-10-17T09:00:00Z', last_activity_at: '2026-10-17T11:30:00Z', authoritative_no_pending_proof: true, report_written: true, screen_done: true, report_at: '2026-10-17T09:00:00Z' }),
      { now_ms: NOW },
    );
    expect(resumed.group).not.toBe('finished_stale');
    const corrupt = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: 'corrupt', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(corrupt.group).not.toBe('finished_stale');
    const futureReport = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T15:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(futureReport.group).not.toBe('finished_stale');
    const futureActivity = classifySession(
      base({ terminal_evidence: 'explicit_completion', terminal_evidence_at: '2026-10-17T11:30:00Z', last_activity_at: '2026-10-17T15:00:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(futureActivity.group).not.toBe('finished_stale');
  });
});

describe('m15 r2 C3 ownership provenance binding epochs', () => {
  it('blank unknown legacy owner caller provenance never cleanup', () => {
    const finished = { ...classifySession(base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }), { now_ms: NOW }), owner: 'synth-starter', human_owned: false, pending_obligations: false };
    expect(planCleanup([finished], 'synth-starter').eligible).toHaveLength(1);
    expect(planCleanup([{ ...finished, owner: '' }], '').eligible).toHaveLength(0);
    expect(planCleanup([{ ...finished, owner: '   ' }], '   ').eligible).toHaveLength(0);
    expect(planCleanup([{ ...finished, owner: 'unknown' }], 'unknown').eligible).toHaveLength(0);
    expect(planCleanup([{ ...finished, owner: 'legacy', owner_provenance: 'unknown' as const }], 'legacy').eligible).toHaveLength(0);
    // Immutable starter/human protection retained in projection.
    expect(finished.owner).toBe('synth-starter');
    expect(finished.starter).toBe('synth-starter');
  });
  it('target binding and epochs strict', () => {
    const good = { session_key: 'h/n', expected_machine_id: 'h', expected_native_id: 'n', expected_owner: 'o', observed_machine_id: 'h', observed_native_id: 'n', observed_owner: 'o', busy_or_draft_or_pending: false, contained: true };
    expect(validateCleanupTarget(good)).toEqual({ ok: true });
    expect(validateCleanupTarget({ session_key: 'h/n', expected_machine_id: '', expected_native_id: '', expected_owner: '', observed_machine_id: '', observed_native_id: '', observed_owner: '', busy_or_draft_or_pending: false, contained: true }).ok).toBe(false);
    expect(validateCleanupTarget({ ...good, expected_owner: 'unknown', observed_owner: 'unknown' }).ok).toBe(false);
    const t = { session_key: 'h/n', current_owner: 'a', current_epoch: 3, expected_owner: 'a', expected_epoch: 3, new_owner: 'b', actor: 'a', human_owned: false };
    expect(validateOwnershipTransfer(t)).toEqual({ ok: true });
    expect(validateOwnershipTransfer({ ...t, current_epoch: -1, expected_epoch: -1 }).ok).toBe(false);
    expect(validateOwnershipTransfer({ ...t, current_epoch: 1.5, expected_epoch: 1.5 }).ok).toBe(false);
    expect(validateOwnershipTransfer({ ...t, current_epoch: Number.MAX_SAFE_INTEGER + 1, expected_epoch: Number.MAX_SAFE_INTEGER + 1 }).ok).toBe(false);
  });
});

describe('m15 r2 C4 naive reset unknown two TZs', () => {
  it('naive and expired reset stay unknown; offset qualifies', () => {
    expect(classifySession(base({ limit_hit: true, limit_window: '5h', limit_reset_at: '2026-10-21 13:16', limit_zone: null }), { now_ms: NOW }).state).toBe('limit_blocked_unknown_reset');
    expect(classifySession(base({ limit_hit: true, limit_reset_at: '2026-10-17T10:00:00Z' }), { now_ms: NOW }).state).toBe('limit_blocked_unknown_reset');
    expect(classifySession(base({ limit_hit: true, limit_reset_at: '2026-10-17T14:00:00Z' }), { now_ms: NOW }).state).toBe('limit_blocked');
    expect(classifySession(base({ limit_hit: true, limit_reset_at: '2026-10-17T16:00:00+02:00' }), { now_ms: NOW }).state).toBe('limit_blocked');
  });
});

describe('m15 r2 C5 unknown reachable future provenance', () => {
  it('empty stale future nonrunning without fresh activity is unknown', () => {
    expect(classifySession(base({ phase: null, last_activity_at: null, observed_at: null, owner_provenance: 'unknown' }), { now_ms: NOW }).group).toBe('unknown');
    expect(classifySession(base({ last_activity_at: '2026-10-17T15:00:00Z', phase: 'running' }), { now_ms: NOW }).group).toBe('unknown');
    expect(classifySession(base({ phase: 'exited', last_activity_at: null, observed_at: null }), { now_ms: NOW }).group).toBe('unknown');
    const both = classifySession(base({ phase: null, last_activity_at: null, observed_at: null, awaiting_alexey: true, has_unsubmitted_draft: true }), { now_ms: NOW });
    expect(both.group).toBe('unknown');
    expect(both.obligations).toContain('unsubmitted_draft');
  });
});

describe('m15 r2 C6 privacy cutoff reason codes default', () => {
  it('secret prefix at 500 redacted, oversize dropped, text-as-data', () => {
    const raw = 'note '.repeat(100) + 'KeyValue9876543210ABCDEFGH';
    const out = sanitizeObservationText(raw, 512);
    expect(out === null || !out.includes('KeyValue9876')).toBe(true);
    expect(sanitizeObservationText('x'.repeat(641), 512)).toBeNull();
    expect(classifySession(base({ observation_note: 'token=ABC' }), { now_ms: NOW }).sanitized_note).toBeNull();
    expect(toHandoffDto(classifySession(base(), { now_ms: NOW })).text_as_data).toBe(true);
  });
});

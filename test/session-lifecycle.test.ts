import { describe, it, expect } from 'vitest';
import {
  classifySession,
  projectSessions,
  planCleanup,
  validateCleanupTarget,
  validateOwnershipTransfer,
  sanitizeObservationText,
  toStatusDto,
  toHandoffDto,
} from '../src/session_lifecycle';

const NOW = 1792238400000;
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

describe('m15 synthetic lifecycle classification', () => {
  it('finished requires authoritative proof, terminal evidence may qualify, banners alone never do', () => {
    const done = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(done.group).toBe('finished_stale');
    expect(done.state).toBe('finished');
    const terminalOnly = classifySession(
      base({ terminal_evidence: 'explicit_completion', terminal_evidence_at: '2026-10-17T11:30:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }),
      { now_ms: NOW },
    );
    expect(terminalOnly.group).toBe('finished_stale');
    const unproven = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z' }),
      { now_ms: NOW },
    );
    expect(unproven.group).not.toBe('finished_stale');
    const blocked = classifySession(
      base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true, pending_obligations: true }),
      { now_ms: NOW },
    );
    expect(blocked.group).not.toBe('finished_stale');
  });

  it('limit, waiting-Alexey, idle, flags preserved, text opt-in only, deterministic projection', () => {
    expect(classifySession(base({ limit_hit: true, limit_window: '5h', limit_reset_at: '2026-10-17T14:00:00Z' }), { now_ms: NOW }).group).toBe('limit_blocked');
    expect(classifySession(base({ awaiting_alexey: true, awaiting_alexey_at: '2026-10-17T11:30:00Z' }), { now_ms: NOW }).group).toBe('waiting_alexey');
    expect(classifySession(base({ worktree_missing: true }), { now_ms: NOW }).state).toBe('missing_worktree_unknown');
    const both = classifySession(base({ worktree_missing: true, awaiting_alexey: true, awaiting_alexey_at: '2026-10-17T11:30:00Z', has_unsubmitted_draft: true }), { now_ms: NOW });
    expect(both.flags).toContain('missing_worktree');
    expect(both.obligations).toContain('unsubmitted_draft');
    expect(classifySession(base({ observation_note: 'token=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234' }), { now_ms: NOW }).sanitized_note).toBeNull();
    expect(sanitizeObservationText('done token=ABCDEFGHIJKLMNOPQRSTUVWXYZ1234 ok')).not.toContain('ABCDEFGHIJKLMNOPQRSTUVWXYZ1234');
    const idle = classifySession(base({ last_activity_at: '2026-10-15T10:00:00Z' }), { now_ms: NOW });
    expect(idle.state).toBe('idle');
    expect(idle.group).toBe('active');
    const inputs = [0, 1, 2, 3].map((i) =>
      base({ native_id: `00000000-0000-4000-8000-${String(i).padStart(12, '0')}`, limit_hit: i === 0, limit_window: i === 0 ? '5h' : undefined, awaiting_alexey: i === 1, awaiting_alexey_at: i === 1 ? '2026-10-17T12:00:00Z' : undefined }),
    );
    const first = projectSessions(inputs, {}, { now_ms: NOW, limit: 2 });
    const second = projectSessions(inputs, {}, { now_ms: NOW, limit: 2 });
    expect(first).toEqual(second);
    expect(first.total).toBe(4);
    expect(first.next_cursor).not.toBeNull();
    const dto = toStatusDto(classifySession(base(), { now_ms: NOW }));
    expect(dto.session_key).toBe('synth-host/11111111-1111-4111-8111-111111111111');
    expect(dto.flags).toBeDefined();
    expect(toHandoffDto(classifySession(base(), { now_ms: NOW })).text_as_data).toBe(true);
  });
});

describe('m15 synthetic cleanup plan and executor seam', () => {
  it('only finished owned sessions without obligations are eligible; seams revalidate binding and ownership', () => {
    const finished = { ...classifySession(base({ report_written: true, screen_done: true, report_at: '2026-10-17T11:00:00Z', last_activity_at: '2026-10-17T10:00:00Z', authoritative_no_pending_proof: true }), { now_ms: NOW }), owner: 'synth-starter', human_owned: false, pending_obligations: false };
    expect(planCleanup([finished], 'synth-starter').eligible).toHaveLength(1);
    const idle = { ...classifySession(base({ last_activity_at: '2026-10-15T10:00:00Z' }), { now_ms: NOW }), owner: 'synth-starter', human_owned: false, pending_obligations: false };
    const foreign = { ...finished, owner: 'other-coord' };
    const human = { ...finished, owner: 'synth-starter', human_owned: true };
    const plan = planCleanup([idle, foreign, human], 'synth-starter');
    expect(plan.eligible).toHaveLength(0);
    expect(plan.ineligible).toHaveLength(3);
    const good = { session_key: 'h/n', expected_machine_id: 'h', expected_native_id: 'n', expected_owner: 'o', observed_machine_id: 'h', observed_native_id: 'n', observed_owner: 'o', busy_or_draft_or_pending: false, contained: true };
    expect(validateCleanupTarget(good)).toEqual({ ok: true });
    expect(validateCleanupTarget({ ...good, observed_native_id: 'other' }).ok).toBe(false);
    expect(validateCleanupTarget({ ...good, contained: false }).ok).toBe(false);
    const transfer = { session_key: 'h/n', current_owner: 'synth-starter', current_epoch: 3, expected_owner: 'synth-starter', expected_epoch: 3, new_owner: 'synth-next', actor: 'synth-starter', human_owned: false };
    expect(validateOwnershipTransfer(transfer)).toEqual({ ok: true });
    expect(validateOwnershipTransfer({ ...transfer, actor: 'admin' }).ok).toBe(false);
    expect(validateOwnershipTransfer({ ...transfer, new_owner: 'unknown' }).ok).toBe(false);
  });
});

import { describe, expect, it } from 'vitest';
import { launchFailureStreak, recoveryAction, recoveryEngineOrder, type LaunchOutcome } from '../src/intake-recovery';

const now = Date.parse('2026-10-29T05:00:00Z');
const failed = (id: string, extra: Partial<LaunchOutcome> = {}): LaunchOutcome => ({
  id, machine_id: 'host-a', engine: 'antigravity', state: 'failed',
  updated_at: new Date(now + Number(id) * 1000).toISOString(),
  verification_status: 'not_started', exit_reason: 'launcher_command_failed', ...extra,
});

describe('intake recovery decisions', () => {
  it.each(['no_native_progress', 'agent_exited_children_running'])('stalled but alive %s requires stop before restart', stall_reason => {
    expect(recoveryAction({ id: 'old', state: 'stalled', lifecycle_state: 'running', stall_reason }, now)).toBe('stop');
    expect(recoveryAction({ id: 'old', state: 'stopped' }, now)).toBe('restart');
  });
  it('does not interrupt scheduled waiting or treat uncertainty as exit proof', () => {
    expect(recoveryAction({ id: 'waiting', state: 'stalled', lifecycle_state: 'running', stall_reason: 'no_native_progress',
      waiting_until: new Date(now + 60000).toISOString() }, now)).toBe('wait');
    expect(recoveryAction({ id: 'unknown', state: 'uncertain' }, now)).toBe('wait');
  });
  it.each(['failed', 'refused', 'cancelled', 'engine_failed', 'engine_oom_killed'])('restarts terminal %s', state => {
    expect(recoveryAction({ id: 'old', state }, now)).toBe('restart');
  });
  it('orders only eligible engines, moving the failed engine behind alternatives', () => {
    expect(recoveryEngineOrder(['antigravity', 'codex', 'claude', 'claude'], 'codex')).toEqual(['claude', 'antigravity', 'codex']);
    expect(recoveryEngineOrder(['opencode', 'zcodex'])).toEqual(['opencode', 'zcodex']);
  });
  it('three proven antigravity launch failures form a host-local streak', () => {
    const outcomes = [failed('1'), failed('2'), failed('3')];
    expect(launchFailureStreak(outcomes, 'host-a', 'antigravity')).toBe(3);
    expect(launchFailureStreak(outcomes, 'host-b', 'antigravity')).toBe(0);
    expect(launchFailureStreak(outcomes, 'host-a', 'codex')).toBe(0);
  });
  it('counts a failed launcher that spawned a native process and then confirmed its exit', () => {
    const outcomes=[failed('1'),failed('2'),failed('3')].map(o=>({...o,native_id:'native-'+o.id,verification_status:'verified'}));
    expect(launchFailureStreak(outcomes,'host-a','antigravity')).toBe(3);
  });
  it('does not let a late exit of an older successful launch erase newer failures', () => {
    const outcomes: LaunchOutcome[]=[failed('1'),failed('2'),failed('3')].map(o=>({...o,created_at:o.updated_at}));
    outcomes.push(failed('4',{created_at:new Date(now).toISOString(),state:'exited',native_id:'older-healthy-native'}));
    expect(launchFailureStreak(outcomes,'host-a','antigravity')).toBe(3);
  });
  it('a successful probe resets old failures; failed probes cannot supply a reset timestamp', () => {
    expect(launchFailureStreak([failed('1'), failed('2'), failed('3')], 'host-a', 'antigravity', failed('2').updated_at)).toBe(1);
  });
  it('successful native start breaks the streak; quota exits and uncertain failures do not count', () => {
    expect(launchFailureStreak([failed('1'), failed('2', { state: 'running', native_id: 'native' }), failed('3')], 'host-a', 'antigravity')).toBe(1);
    expect(launchFailureStreak([failed('1', { verification_status: 'unknown' }), failed('2', { exit_reason: 'quota_exhausted' })], 'host-a', 'antigravity')).toBe(0);
  });
});

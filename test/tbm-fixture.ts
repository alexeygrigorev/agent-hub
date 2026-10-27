/**
 * TBM Phase 2 test support: headless starts must carry an open intake task.
 * Legacy session fixtures that predate the gate route their start payloads
 * through withFixtureIntake() and seed the matching task once per service.
 */
export const FIXTURE_INTAKE_ID = 'fixture-intake';

export function seedFixtureIntake(sql: any): void {
  sql.exec('INSERT OR REPLACE INTO intake_tasks (id, data) VALUES (?, ?)', FIXTURE_INTAKE_ID,
    JSON.stringify({ id: FIXTURE_INTAKE_ID, project: 'demo', status: 'open', acceptance_criteria: 'Synthetic fixture criteria.', history: [] }));
}

/** Worktree-guard (intake a0f01448): close the fixture intake so cleanup and
 * watchdog paths that require a closed intake stay eligible in legacy tests.
 * Tests for the open-intake refusal leave the fixture open. */
export function closeFixtureIntake(sql: any, id: string = FIXTURE_INTAKE_ID): void {
  sql.exec("UPDATE intake_tasks SET data=json_set(data,'$.status','done','$.history',json(?)) WHERE id=?",
    JSON.stringify([{ kind: 'note', text: 'DONE abc1234\nlive check: synthetic cleanup fixture\nresult: all synthetic acceptance criteria verified' }]), id);
}

/** Adds the fixture intake id to non-interactive session starts that do not set one. */
export function withFixtureIntake(path: string, data: any, method = 'POST'): any {
  if (method !== 'POST' || path !== '/api/sessions' || !data || typeof data !== 'object' || Array.isArray(data)) return data;
  if (data.mode === 'interactive' || 'intake_task_id' in data) return data;
  return { ...data, intake_task_id: FIXTURE_INTAKE_ID };
}

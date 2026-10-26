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

/** Adds the fixture intake id to non-interactive session starts that do not set one. */
export function withFixtureIntake(path: string, data: any, method = 'POST'): any {
  if (method !== 'POST' || path !== '/api/sessions' || !data || typeof data !== 'object' || Array.isArray(data)) return data;
  if (data.mode === 'interactive' || 'intake_task_id' in data) return data;
  return { ...data, intake_task_id: FIXTURE_INTAKE_ID };
}

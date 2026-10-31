/** Worker outcome surface for cleanly finished headless launches (intake 17e774f8).
 *
 * The /result endpoint bounds runtime evidence but does not enforce that an
 * exited receipt's runtime is internally consistent. A reported worker
 * outcome is only shown when the launch's current runtime proves a clean
 * native completion: integer exit 0, no signal, no failed/cancelled engine
 * outcome, and a forwarded worker report. Applied at ingestion and again
 * at every view, so a later contradictory replay suppresses it. */
export const WORKER_STATUSES = ['done', 'failed', 'waiting-on-human', 'stuck'];

export function cleanReportedExit(runtime: any): boolean {
  return !!runtime && typeof runtime === 'object' && !Array.isArray(runtime) &&
    runtime.outcome === 'reported' &&
    Number.isInteger(runtime.exit_code) && runtime.exit_code === 0 &&
    (runtime.exit_signal === null || runtime.exit_signal === undefined || runtime.exit_signal === 0) &&
    !['failed', 'cancelled'].includes(runtime.engine_outcome);
}

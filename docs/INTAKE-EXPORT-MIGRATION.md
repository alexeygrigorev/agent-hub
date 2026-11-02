# Intake export migration safety (ef429827)

Production never deployed the reverted series, so the first installation creates
empty export tables, counters, write triggers and a durable migration cursor in
the existing intake initialization transaction. No original intake row is changed.
Missing/null project uses an empty index bucket (no project grant matches it);
missing/null status uses the open index bucket. Compact JSON retains original
nulls, and full export hydration returns the original ledger.

Initialization copies at most 25 primary-key-ordered retained rows. Each subsequent
authenticated summary/export request advances at most another 25 rows in a separate
transaction. Until complete it returns 503 intake_export_migrating with retry_after
1, never incomplete totals/pages. The batch transaction commits before that response;
it does not roll back with an error. There is no unbounded backfill on a request.
Each row is bounded by the existing intake record size policy.

Write triggers are installed before copying: concurrent inserts/updates maintain
current metadata and counters, and deletes remove it. INSERT OR IGNORE prevents
double counting rows already maintained by triggers. Cursor and rows commit together;
restarts resume safely, retries are idempotent, and deletes behind/ahead of the
cursor cannot resurrect a ledger. Counter tables start empty and are maintained by
the same triggers during copying; no aggregate scan of retained ledgers is needed.
After completion only the tiny cursor row is checked, and full history is hydrated
only for the requested export page. Existing production intake writes remain
available during migration. Previously installed partial export schemas are outside
this rollout path (the failed release never deployed).

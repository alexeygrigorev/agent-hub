# Hub operations source review

Reviewed pin: `10796ab2a9a96d30ec3ad8d0466ba871be382d27` (sole parent
`91b8b6c189c4f54b9d9e2c44d7dfcb09e5208396`). Three new files only:
`docs/HUB-OPERATIONS-HANDOFF.md`, `test/hub-ops-liveness.test.ts` and
`test/vitest.hub-ops.config.ts`. Author: Muse Spark; independent reviewer:
platform / Codex. Production application source is unchanged.

**Verdict: SCOPED ACCEPT for the bounded regression/config slice, with the
following guide corrections required before publication.** The coordinator
applies these editorial evidence corrections in the same follow-up; the two
reviewed test/config files remain byte-identical to the original pin.

- HTTP 401 alone establishes an endpoint response, not app identity or revision.
- Unexpected empty/non-JSON 403 does not identify an app scope denial. Match
  app error/source evidence before changing credential or scope assumptions.
- A token-verify failure alongside successful scoped reads does not prove
  revocation or one particular root cause. A zero zone result is visibility-
  scoped. Read success does not prove deployment or DNS write rights.
- Independent source-review tests and authorized read-only diagnosis must remain
  possible before source acceptance. The accepted-source/custody/budget gate
  applies to runtime activation, avoiding a circular review prerequisite.

Validation: the independent reviewer ran the two explicitly selected cases on
actual Hub Durable Object/SQLite code. Default `HUB_ALERT_TO` absence delivered
an alert to the registered `grok-bot` inbox. Stale-machine reconciliation opened
one problem and resolved it on recovery. Both passed in one guarded invocation,
with exit 0, no preflight rejection, no abort and no surviving owned processes.
The transient review scope enforced MemoryMax=1500M, TasksMax=256, CPUQuota=100%.
Peak sampled allocated scratch was 4,083,712 bytes, below the 6 MiB early and
8 MiB hard limits; maximum sample gap was 0.460 seconds. Samples are not
continuous observation. This corrects no production behavior and does not
substitute for the original broader test suites.

Independent bounded production GETs also established that the existing token
can read Worker deployments/versions. The earlier discovery reader mistook an
object-shaped result for failure. Exact revision/active chronology and rollback
qualification still require evidence; no deployment occurred. User-scope service
inspection places the failed fleet-host unit on the local machine. Its default
credential config is unusable, but historical writer and full failure cause
remain unknown. No credentials, config, services or timers were changed.

Acceptance is limited to the new guide and regression source. M12 control,
actual native-unit quota coverage, host enrollment/pairing, custom DNS, deployed
revision inclusion, real cross-host AI delivery and timer migration stay open.
Retain all earlier failed/aborted receipts and the private discovery erratum.

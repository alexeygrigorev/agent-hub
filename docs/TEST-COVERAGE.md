# Standalone regression coverage

Intake `da079ce2-128f-4924-a7d7-f9d08b6ee43e` restores the tests identified by audit `e2501fde` to routine verification.

`scripts/test-full-local.sh` and the CI test job both invoke
`node scripts/test-browser-local.mjs`. The dispatcher discovers every
`test/*.browser.mjs` and `test/*.checks.mjs`, plus the HTTP/CLI integration
scripts `machine-pairing.mjs` and `conversation-platform.mjs`. It runs them
sequentially, reports each failure, continues to the remaining suites, and
fails the run if any suite fails. Each process group has a three-minute
deadline and descendant cleanup. Native-session modes remain explicit manual
checks; automated runs use local fixture identities and loopback Workers.

Browser dependencies come from the local `playwright` package or an existing
installation selected with `PLAYWRIGHT_PACKAGE=/path/to/package.json`.
Chromium uses Playwright's installed browser by default; `CHROMIUM_PATH` and
`PLAYWRIGHT_BROWSERS_PATH` support an existing shared browser installation.
The harness contains no developer-specific dependency or browser paths.
CI installs Playwright and Chromium before running the same dispatcher.

| Audit item | Maintained verification |
| --- | --- |
| conversations UI and checks | Discovered browser suite and static checks, including contract, account isolation, races and vendored kit hashes |
| existing UI and checks | Discovered real Worker/browser suite and official kit lock, color, border and scale checks |
| metrics charts and R6 | Both browser suites, including pagination and stale-response races |
| provisioning and quota M13 | Both real Worker/browser suites |
| machine pairing | Real browser approval and Python CLI login/refresh against a local Worker |
| conversation platform | Real Worker, SQLite, browser and Python CLI integration |
| registry F7 | Renamed `registry-f7.local.ts` to `registry-f7.test.ts`, discovered by the default Vitest config locally and in CI |

The redundant F7 config/entrypoint are removed: they re-exported the same
production Worker and `Hub` as the default config. The migration test checks
preservation of every old message field while permitting columns added by
later migrations. Its credential/message-binding upgrade fixture retains the
current project-scoped agents schema: the global-tag primary-key rebuild was
retired by `b9d3b22c` / intake `19848714`. Binding-upgrade rollback still checks
that the current agent keys and every old credential/message field survive.

`conversation-bridge-gap.mjs` and its Worker/config are retired. They rebuilt
the historical commit `d9b4cde` from Git rather than the current source, pinned
native mode to a specific retired session, and tested native recovery only
under that mode. Current-source replacements are:

- `host/tests/test_conversation_bridge_gap.py`: prefix/interior gaps, HOLD,
  explicit missing-range acknowledgment, retained/future-range refusal,
  changed/revoked authority, immutable audit, restart reconciliation and
  once-only retained delivery.
- `host/tests/test_conversation_bridge.py`: noncontiguous or wrong-conversation
  history never advances read state.
- `test/conversation-platform.mjs`: real HTTP/CLI history after SQLite retention,
  browser reconnect, idempotency and membership removal.

Python discovery already runs the maintained bridge tests. The current
conversation integration now runs in the shared standalone dispatcher.

## Routing policy consumers

`src/pacing.ts` reads `preferred_providers`, `provider_preference`,
`reset_horizon_hours`, `reset_urgency_factor` and `headroom_floor` directly from
`config/model-routing.json`. `test/pacing.test.ts` changes those settings and
checks actual candidate factors and weights, restoring the policy afterward.

`allow_paid` and `cost_mode` are enforced as no-paid invariants: candidate
selection refuses any policy other than `false` / `existing-subscription-only`.
Paid routing is unsupported. `allow_cross_tier_failover` is removed because
candidate selection always requires membership in the requested tier; the
behavior is tested directly. There is no configurable cross-tier fallback.

The full runner preserves isolated Python HOME and credential selectors and
uses a short Python TMPDIR, then an absolute browser TMPDIR. These safeguards
and real config-write/socket regressions incorporate the runner work from
intake `885239f1` (candidate `e0a709c1`), with its browser invocation assertion
updated for the shared dispatcher.

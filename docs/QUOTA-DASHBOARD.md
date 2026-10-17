# Quota Dashboard (M13)

Collect per-engine usage on each enrolled host, serve sanitized fleet-wide and
per-host quota over authenticated API + `hub quota` CLI + dakit dashboard, and
gate existing routing dispatch and launch claim fail-closed on
exhausted/stale/unknown quota. No second quota store: single
`host_quota_snapshots` row per host plus existing `fleet_accounts`,
`fleet_routes`, `fleet_subscriptions`.

## Schema versions

- `1.0`: exact legacy contract (`percent_remaining`, `reset_at`, `present`,
  `rolling`, `error`, `limit_reached`, `stale`, `has_grok_code_access` on grok).
  Payloads containing any newer key are rejected (`400`) — explicit version
  negotiation, so old readers/writers keep working byte-for-byte.
- `1.1` (additive): optional envelope `collector_version` (bounded ASCII label,
  e.g. `quota_snapshot/1.1`) and optional per-window / per-details native
  fields: `credits_remaining` (finite `0..1e12`), `spend_used`/`spend_limit`
  (finite `0..1e15`), `unit` (`percent|credits|usd_cents|requests|tokens`),
  `profile` (bounded label, e.g. a Muse Spark model id). Absent natives stay
  `null`/`unknown`; percentages are never converted to dollars or tokens.
  Unknown or secret keys (tokens, cookies, emails, raw output, private config)
  are rejected (`400`) in both versions.
- An absent window (`present: false`) carries `null` measurements and no error.
  A window with `error: "unavailable"` fails its route closed even when sibling
  windows look healthy.

## API

- `POST /api/quota`: host bearer only (machine-bound, registered, unrevoked,
  unexpired). Replay/out-of-order `captured_at` → `409`.
- `GET /api/quota`: own-host read (host bearer) or admin session with explicit
  `?host_id=` (Origin/CSRF integrity kept). Unchanged from the M3 contract.
- `GET /api/quota?view=fleet[&host_id=&provider=&engine=&account=]`: fleet-wide
  projection. Grants reuse the canonical Hub authority
  (`authenticateBusRequest`): human admins (session or admin bearer),
  `agent` bearers with `fleet:launch` scope, `routing-client` bearers with
  `routing:submit`. Host bearers are denied (`403`); revoked/expired callers get
  `401`/`403` from canonical auth. Admin sessions without `?host_id=` and
  without `view=fleet` still get `400` (legacy contract preserved).
- The fleet body (`schema_version "1.1"`, `Cache-Control: private, no-store`)
  carries `pools[]` (one row per canonical verified
  `(provider, account_label)`, never summed: `remaining_percent` is the minimum
  across fresh observations, `observed_by_hosts` retains provenance,
  conflicting/stale observations resolve to `stale`/`unknown`), `hosts[]`
  (per-host snapshot + subscriptions), and `coverage` (fresh/stale/missing).
- Pool states: `fresh`, `stale`, `exhausted` (`limit_reached`), `unavailable`
  (route/window error), `unknown` (no fresh evidence). A passed `reset_at`
  alone never marks capacity recovered.

## Admission (`src/quota_admission.ts`)

`evaluateQuotaAdmission({provider, quota, subscriptionObservedAt, nowMs,
reserves})` is the single gate used by `accountStatus`, `plan`, `dispatch`
(model shortlist filter) and `claim` (re-checked atomically before effect).
Reserves come from `config/model-routing.json rules.reserve_percent`
(Codex 15, Grok 5) applied to **every** available applicable window.
Fail-closed on: stale/future/malformed observation times, `status !== "ok"`,
`limit_reached !== false`, missing Grok code entitlement, any
`error: "unavailable"` window, malformed numbers/dates, reset passed without a
post-reset observation, and zero available windows. Benign absence of an
optional unsupported window (`present: false`, nulls, no error) is ignored.
Model preference (Muse Spark 1.3, Z.ai `glm-5.3-flash`) applies only among
eligible routes; exhaustion gates are never overridden by any manual flag.

## CLI and UI

- `hub quota [--host ID] [--provider P] [--engine E] [--account L] [--json]`
  (`host/agent_hub_host/quota.py`): one authenticated `GET`, text table by
  default, `--json` for machines. Extends the existing parser; status, intake,
  handoff and session commands are untouched.
- Dashboard `public/fleet.html` quota card + `public/fleet.js renderQuota()`:
  existing dakit classes/components only, `textContent`/DOM rendering,
  pool states, native units, resets, account/profile/host attribution and
  honest coverage (`Coverage: N fresh, N stale, N missing`).

## Verification

Collector/CLI Python suites plus `test/quota-m13.test.ts` (HTTP/SQLite
integration: native roundtrip, window error vs benign absence, shared-pool
dedupe, revocation, privacy, reset/malformed negatives, dispatch → exhausted →
claim refusal) and `test/quota-m13.browser.mjs` (same UI/API contract,
390/1440 light/dark). Live `quse` today reports percent-only windows, so live
native fields stay `null`; non-null roundtrip is proven by schema-valid
fixtures, never synthetic live zeros.

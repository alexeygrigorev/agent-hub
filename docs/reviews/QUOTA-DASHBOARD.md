# M13 Independent Source & Launch-Policy Review (`docs/reviews/QUOTA-DASHBOARD.md`)

## 1. Review Metadata & Source Proof

| Field | Value |
| --- | --- |
| **Review Pin (`HEAD`)** | `2041df59c7f9cf42344c26cefb3b6d1f5700bab6` |
| **Merge Parents** | Parent 1 (`main`): `77ab932c421c39397b20636a77fb3d3d72c280e8`<br>Parent 2 (Author M13): `811c086db193a1ac6e47422829edfec27f5d137b` |
| **Author Worker** | `platform-quota` (`62c912d6-7f75-47cc-bd1c-a40a9942c66d`), engine `opencode/opencode-go/muse-spark-1.3-contributor`, task `actual0439` / `platform-quota-0439` |
| **Reviewer Worker** | `platform-quota-review` (`5db97f80-9a53-4051-8a50-109d60f964e2`), engine `antigravity/gemini-4-argon-high`, parent `e3f61b6c-9fda-4bcc-bf3d-cf66cb7ef363` (`platform`), task `actual0442` / `platform-quota-rev-0442` |
| **Review Worktree** | `/home/alexey/git/agent-hub-wt-platform-quota-review` |
| **Source 238 Manifest Digest** | `0818b95460d0bb1dca08182efb0f014aa0e5e43bb9d4c229e33b2acd08bf97ab` |
| **Pre-Review 238 Proof** | Verified (`238/238` files match `/home/alexey/git/agent-coordination/.local/platform/quota-review-source-manifest.json`; `219` unchanged from `77ab932c421c39397b20636a77fb3d3d72c280e8`, `12` modified + `7` added = `19` author files byte-identical to `811c086db193a1ac6e47422829edfec27f5d137b`; saved in `.local/review/source-pre-verify.json`) |
| **Post-Review 238 Proof** | Verified (`238/238` files byte-identical to pre-review manifest, digest `0818b95460d0bb1dca08182efb0f014aa0e5e43bb9d4c229e33b2acd08bf97ab`, zero tracked source mutations; saved in `.local/review/source-post-verify.json`) |
| **Verdict** | **APPROVED FOR SOURCE SLICE INTEGRATION (ZERO SHIPPING BLOCKERS; EXPLICIT ADVISORY FINDINGS & LIVE NATIVE/ALL-MACHINE COVERAGE GAPS REMAIN OPEN)** |

---

## 2. Executive Summary & Verdict Rationale

1. **Source Slice Quality & Integration Readiness**:
   - The 19-file M13 source candidate (`811c086db193a1ac6e47422829edfec27f5d137b` merged onto `77ab932c421c39397b20636a77fb3d3d72c280e8` at `2041df59c7f9cf42344c26cefb3b6d1f5700bab6`) cleanly implements additive schema `1.1` (`credits_remaining`, `spend_used`, `spend_limit`, `unit`, `profile`, `cooldown_until`, `collector_version`, window `error: "unavailable"`) while keeping schema `1.0` backward-compatible, wires a single canonical quota admission evaluator (`src/quota_admission.ts`) across `FleetService.accountStatus`, `FleetService.plan`, `FleetService.reserve`, `RoutingService.modelCandidates`, `RoutingService.dispatch`, and atomic host `claim`, adds the fleet quota projection (`GET /api/quota?view=fleet`), `hub quota` CLI, and `dakit` UI section (`public/fleet.html`, `public/fleet.js`), and enforces the 2026-10-17 no-new-Grok launch policy (`config/model-routing.json`, `src/fleet.ts`).
   - All **122 Python unit tests** across all 6 suites (`test_quota_snapshot.py` 39, `test_fleet.py` 18, `test_cli.py` 9, `test_http_cli.py` 30, `test_quota_m13.py` 11, `test_intake.py` 15), `tsc --noEmit`, all **8 new M13 Vitest integration tests** (`test/quota-m13.test.ts`), all **10 existing QuotaStore Vitest tests** (`test/quota.test.ts`), all **7 Fleet Assets Vitest tests** (`test/fleet-assets.test.ts`), representative `test/fleet.test.ts` pairs, **4 independent novel Python/HTTP/CLI probes**, and **all 4 real headless Chromium 390/1440 light/dark browser checks** (`test/quota-m13.browser.mjs`) pass on `2041df59c7f9cf42344c26cefb3b6d1f5700bab6` with zero guard aborts.
2. **Why M13 End-State Remains OPEN Despite Source Slice Approval**:
   - Per `docs/reviews/QUOTA-STATUS-PLAN.md` (`M13-IG1`..`M13-IG3`), shipping the source slice does **not** close M13 end-state: live `quse --json` on `hetzner` today emits **percent-only** windows (`any_native_unit_fields_present: false` in `.local/review/live-quse-coverage.json`), so native `credits_remaining`, `spend_used`, `spend_limit`, `unit`, `profile`, and `cooldown_until` are exercised only via round-trip fixtures until upstream `quse` / provider collectors emit them live across all registered machines (`hetzner`, `workstation`, `macbook`, `win35`).

---

## 3. Technical Findings (0 Blockers, 5 Advisory / Follow-Up Findings, 1 Cross-Milestone Note)

### Cross-Milestone Integration Note (M12 + M13 Composition)
- **M12 Session Control (`POST /api/sessions`) & No-New-Grok Policy**:
  - In M13 (`config/model-routing.json`, `src/model-routing.ts`, `src/fleet.ts:22-26, 185, 234, 399`), `disabled_providers: ["claude", "grok"]` and `disabledEngines()` block new Grok model selection, `FleetService.plan`, `FleetService.reserve`, and pre-hold `POST /api/fleet/launches/:id/claim`, while preserving running Grok job result reporting, replay, and Bot quota telemetry.
  - Because M12 session control (`src/sessions.ts`) is not in `2041df59c7f9cf42344c26cefb3b6d1f5700bab6`, when M12 and M13 are composed on `main`, any direct session-start path in M12 that does not pass through `FleetService.reserve` or `RoutingService.modelCandidates` must also check `disabled_providers` / `disabledEngines` so no new `grok`-engine worker can be started via session control.

### Advisory Finding 1 — `evaluateQuotaAdmission` Early Return on `reserve` Masks Sibling Window Errors & Lower Percentages; Loose ISO Parsing (`src/quota_admission.ts:29-33, 70-73, 82-106`, `src/fleet.ts:150-152`)
- **Severity**: Low / Advisory (admission still fails closed with `ok: false`).
- **Evidence**:
  - In `src/quota_admission.ts:82-106`, the loop over `Object.values(windows)` updates `rawMin` and immediately executes `if (pct <= reserve) return { ok: false, reason: "reserve", remaining: rawMin };` (`L99`) on the first window at or below `reserve`, before inspecting the remaining windows in `quota.windows`.
  - If an earlier window is at/below reserve (e.g., `5h` at `15%` against Codex `15%` reserve) while a sibling window has `error: "unavailable"`, `reset_passed_since_observation`, `malformed` fields, or a lower percentage (e.g., `7d` at `2%` or `0%`), `evaluateQuotaAdmission` returns `reason: "reserve", remaining: 15` instead of `reason: "window_unavailable"` / `"malformed"` or `remaining: 2`. In `FleetService.accountStatus()` (`src/fleet.ts:150-152`), this classifies the route as `status: "blocked", remaining_percent: 15` rather than `status: "unknown", remaining_percent: null` (or `remaining_percent: 2`).
  - Additionally, `validObservedAt` (`L29-33`), `cooldownUntil` (`L70-73`), and `w.reset_at` (`L94-96`) in `src/quota_admission.ts` use `Date.parse(...)` without enforcing a UTC timezone suffix (`Z` or `+00:00`), unlike `parseUtcIso` in `src/quota.ts:270` and `fresh()` in `src/fleet.ts:78`. And for `w.present === false` (`L87`), `evaluateQuotaAdmission` checks `if (w.error === "unavailable")` rather than rejecting any non-null `w.error`.
- **Follow-up**: Validate all windows in `quota.windows` before checking `rawMin <= reserve`, and enforce `UTC_ISO` on timestamps in `src/quota_admission.ts`.

### Advisory Finding 2 — Host Collector Passes Through `spend_used > spend_limit` While Hub Rejects It (`host/agent_hub_host/quota_snapshot.py:132-151`, `host/agent_hub_host/fleet.py:67-84` vs `src/quota.ts:288-308`)
- **Severity**: Low / Advisory (verified by novel probe `test_03` in `.local/review/novel_probes.py`).
- **Evidence**:
  - `src/quota.ts:299` (`validateNativeQuantities`) enforces `if (spendUsed !== null && spendLimit !== null && spendUsed > spendLimit) return { ok: false, reason: "spend_used_exceeds_spend_limit" };`, returning `400 Bad Request` for any snapshot where `spend_used > spend_limit`.
  - However, `host/agent_hub_host/quota_snapshot.py:132-151` (`_sanitize_native_fields`) and `host/agent_hub_host/fleet.py:67-84` (`_copy_natives`) sanitize `spend_used` and `spend_limit` independently with `sanitize_non_negative_number` without checking `spend_used <= spend_limit` (and `sanitizedQuota` in `src/fleet.ts:51-68` also omits the `spend_used <= spend_limit` check).
- **Follow-up**: Drop incoherent `(spend_used, spend_limit)` pairs when `spend_used > spend_limit` in `_sanitize_native_fields` / `_copy_natives` and reject them in `src/fleet.ts:sanitizedQuota`.

### Advisory Finding 3 — Fleet View & UI Pool State Display Diverges from Canonical Admission on Passed `reset_at`, `cooldown_until`, Reserve, and Absent Windows (`src/quota.ts:240-257, 1223-1241`, `public/fleet.js:58-65`, `host/agent_hub_host/quota.py:42-60`)
- **Severity**: Low / Advisory (display-only; placement/dispatch/claim admission uses `evaluateQuotaAdmission` and fails closed).
- **Evidence**:
  - `QuotaStore.getFleetView` (`src/quota.ts:1229-1241`) derives each shared pool's `state` (`"fresh" | "stale" | "exhausted" | "unknown"`) from `freshObs.length`, `limit_reached`, and `observationUnavailable(o.quota)` without calling `evaluateQuotaAdmission`. As a result, `getFleetView` reports `state: "fresh"` when:
    1. A present window's `reset_at` has already passed without a post-reset observation (`reset_passed_since_observation`).
    2. `details.cooldown_until > nowMs` is active (`cooldown_active`).
    3. `remaining_percent <= reserve` (or `percent_remaining === 0` with `limit_reached: false`).
    4. `provider === "grok"` has `has_grok_code_access === false`.
    5. All windows have `present: false` (`remaining_percent: null`).
  - Similarly, `quotaWindowState(snap, w)` in `public/fleet.js:58-65` returns `'fresh'` for any `w.present === true` window even when `w.percent_remaining === 0` or `Date.parse(w.reset_at) <= Date.now()`.
  - In `host/agent_hub_host/quota.py:42-60`, `render_text` prints pool summary lines and coverage without rendering per-window native quantities (`credits_remaining`, `spend_used`, `profile`), and `host/agent_hub_host/cli.py:678-680` prints a single generic message (`"Hub quota operation failed; check authentication, filters and quota grants."`) for both `401` and `403`.
- **Follow-up**: Derive pool/window display state in `getFleetView` and `public/fleet.js` from `evaluateQuotaAdmission` so passed `reset_at`, active cooldowns, and exhausted/all-absent windows never display as `"fresh"`.

### Advisory Finding 4 — Fleet Quota Query Filter Strictness, Pagination, and Broad Read Grant (`src/quota.ts:178-185, 215-232, 1149-1292, 1365-1378`, `src/hub.ts:1257-1338`)
- **Severity**: Low / Advisory.
- **Evidence**:
  - `parseFleetQuotaFilters` (`src/quota.ts:215-232`) inspects only `host_id`, `provider`, `engine`, and `account` and silently ignores unknown query keys (e.g., `?view=fleet&host=hetzner` or `?view=fleet&unknown=1` returns the unfiltered fleet view with `200 OK`) and coerces empty-string filter values (`?view=fleet&host_id=`) to `null`. `getFleetView` has no pagination parameter, though it is bounded in practice by the fleet registry caps (`<= 128` accounts, `<= 512` routes).
  - `fleetReadGrant` (`src/quota.ts:178-185`) grants full multi-host `GET /api/quota?view=fleet` reads to any authenticated `agent` with `fleet:launch` or `routing-client` with `routing:submit` without scoping the returned `machines`/`hosts`/`pools` to the caller's project or re-checking active agent registration in `agents`.
- **Follow-up**: Reject unknown or empty query parameters in `parseFleetQuotaFilters` with `400 invalid_query_filter` and tighten non-admin fleet view scoping if multi-tenant `routing-client` tokens are introduced.

### Advisory Finding 5 — Provider Masquerade Defense-in-Depth & `claimRoute` Engine Key (`src/fleet.ts:22-26, 185, 234, 399, 406`)
- **Severity**: Low / Advisory.
- **Evidence**:
  - `FleetService.plan` (`L185`), `reserve` (`L234`), and `claim` (`L399`) check `disabledEngines().has(route.engine)` (which maps `disabled_providers: ["claude", "grok"]` through `policy.models` to `Set(["claude", "grok"])`), but do not directly check whether `account.provider` in `fleet_accounts` is in `policy.rules.disabled_providers`. In the canonical registry (`scripts/import_fleet_registry.py`), `grok` accounts are only routed to `engine: "grok"`, so no live route can masquerade, but checking `disabledProviders.has(account.provider)` directly adds defense-in-depth.
  - At `src/fleet.ts:406`, `claimRoute` queries `SELECT machine_id, profile FROM fleet_routes WHERE machine_id = ? AND account = ?` without `AND engine = ?` (`row.engine`), whereas `fleet_routes` has primary key `(machine_id, engine, account)`.
- **Follow-up**: Add `AND engine = ?` (`row.engine`) to the `claimRoute` query at `src/fleet.ts:406` and check `disabledProviders.has(account.provider)` alongside `disabledEngines().has(route.engine)`.

---

## 4. Gate Coverage (`M13-IG1`..`M13-IG3` & Review Focus 1–4)

| Gate / Focus | Status | Evidence & Notes |
| --- | --- | --- |
| **M13-IG1 / Focus 1**: Collector -> Hub Schema `1.1`, Native Units, Attribution & Honest Absence | **Source Slice Met / Live Native Coverage OPEN** | `quota_snapshot.py`, `fleet.py`, `src/quota.ts`, and `src/fleet.ts` negotiate `schema_version: "1.1"` additively while preserving `1.0`. Native fields (`credits_remaining`, `spend_used`, `spend_limit`, `unit`, `profile`, `cooldown_until`, `collector_version`, window `error: "unavailable"`) round-trip and strip secrets/raw errors. Live `quse` probe (`.local/review/live-quse-coverage.json`) confirms Gemini `monthly: null/null` stays `present: false, error: null` (never `0%` or `unavailable`), and confirms live `quse` today is percent-only (`any_native_unit_fields_present: false`). |
| **M13-IG2 / Focus 2**: Authenticated Fleet Quota API, `hub quota` CLI, and `dakit` UI | **Source Slice Met / Multi-Machine Live Runtime OPEN** | `GET /api/quota?view=fleet` deduplicates shared `(provider, account)` pools across hosts using minimum `percent_remaining` across fresh present windows and retains host provenance and `coverage` (`fresh`/`stale`/`missing`). `hub quota` CLI supports `--host`, `--provider`, `--engine`, `--account`, and `--json` without zero-filling unknown capacity. `public/fleet.html` + `public/fleet.js` use `dakit` and `textContent` exclusively (no `innerHTML`). Verified with 4 headless Chromium renders (`390x900` & `1440x900`, light & dark) with zero script errors. |
| **M13-IG3 / Focus 3**: Single Canonical Quota Admission Evaluator Across Placement, Routing & Atomic Claim | **Source Slice Met** | `evaluateQuotaAdmission` (`src/quota_admission.ts`) is the single evaluator used by `FleetService.accountStatus`, `FleetService.plan`, `FleetService.reserve`, `RoutingService.modelCandidates`, `RoutingService.dispatch`, and atomic `POST /api/fleet/launches/:id/claim`. Enforces Codex `15%` and Grok `5%` reserves, fails closed on stale/missing/errored/exhausted/passed-`reset_at`/cooldown observations, and preserves unexpired `cooldown_until` in `fleet_subscriptions` and `host_quota_snapshots` when omitted by a newer report. |
| **Focus 4**: 2026-10-17 No-New-Grok Launch Policy | **Met in M13 Scope** | `config/model-routing.json` sets `"disabled_providers": ["claude", "grok"]`. `RoutingService.modelCandidates`, `FleetService.plan`, `FleetService.reserve`, and pre-hold `POST /api/fleet/launches/:id/claim` reject `grok` (`409 provider_disabled`), while idempotent launch replay, running Grok job `result` / `report` completion, and Grok Bot quota telemetry (`7d` window) remain intact. No Grok worker or model probe was executed. |

---

## 5. Test Execution & Guarded Evidence Audit

### 5.1 Audit of Author's Raw Guarded Receipts (`../agent-hub-wt-platform-quota/.local/quota/guarded`)
- **Total guarded receipts**: `68` (audited in `.local/review/author-evidence-audit.json`).
  - **Clean passes (`exit == 0` and `abort_reason == null`)**: `52` runs (`0` runs had `exit == 0` with `abort_reason != null`).
  - **Guard-aborted runs (`abort_reason != null`, never counted as green)**: `6` runs:
    1. `quota-pytest-existing` (`exit: -9`, `abort_reason: "early_scratch_limit"`, peak allocated `12,599,296` B > `12,582,912` B, max sample gap `1.163s`).
    2. `quota-pytest-snap` (`exit: null`, `preflight_rejected: true`, `abort_reason: "preflight_early_limit"`).
    3. `quota-pytest-snap2` (`exit: null`, `preflight_rejected: true`, `abort_reason: "preflight_early_limit"`).
    4. `quota-m13-pair12` (`exit: 1`, `abort_reason: "early_scratch_limit"`, peak allocated `13,561,856` B > `12,582,912` B).
    5. `quota-hhttp` (`exit: -9`, `abort_reason: "early_scratch_limit"`, peak allocated `21,102,592` B > `20,971,520` B).
    6. `quota-hhttp2` (`exit: null`, `preflight_rejected: true`, `abort_reason: "preflight_early_limit"`).
  - **Non-zero exits without guard abort (`exit != 0`, `abort_reason == null`)**: `10` runs (`quota-malformed`, `quota-pytest-m13`, `quota-reg-c`, `quota-hsnap`, `quota-hsnap2`, `quota-m13-grok`, `quota-m13-grok2`, `quota-m13-grok3`, `quota-m13-grok4`, `quota-m13-grok5`).
  - **Author Guard Telemetry Peaks**: max allocated `23,040,000` B (`21.97 MiB`), max apparent `20,301,112` B (`19.36 MiB`), max sample gap `1.163s` (`0.906s` on `quota-browser`).
  - **Author Documentation Discrepancies Noted**: Author `PROGRESS.md` (17,313 B) retained stale heading `New test/quota-m13.test.ts: 7/7 green (4 runs)` at line 21 after commit `811c086` added the 8th test (`quota-m13-grok6`), and neither `tsc --noEmit` nor the claimed `6361 bytes` live `quse` sample had a saved receipt in `.local/quota/guarded/`. Both were independently executed and verified below under `run-guarded.py`.

### 5.2 Reviewer's Independent Guarded Runs (`.local/review/guarded`, `--early-mib 14 --hard-mib 18 --retire-child-tmp`)
- **Total reviewer guarded runs**: `30` (`25` clean `exit: 0` with `abort_reason: null`, `5` failed probe/ordering runs with `abort_reason: null`, `0` guard aborts; full summary in `.local/review/reviewer-runs-summary.json`):
  1. **TypeScript Typecheck (`1` run, `0` errors)**:
     - `rev-tsc` (`./node_modules/.bin/tsc --noEmit`): `exit: 0`, `abort_reason: null`, peak allocated `589,824` B, max sample gap `0.376s`.
  2. **Python Unittest Matrix (`6` runs, `122/122` unique tests green, `0` failures, `0` aborts)**:
     - `rev-py-quota-snap` (`host/tests/test_quota_snapshot.py`, `39` tests): `exit: 0`, `abort_reason: null`, peak allocated `622,592` B, max gap `0.253s`.
     - `rev-py-fleet` (`host/tests/test_fleet.py`, `18` tests): `exit: 0`, `abort_reason: null`, peak allocated `626,688` B, max gap `0.368s`.
     - `rev-py-cli` (`host/tests/test_cli.py`, `9` tests): `exit: 0`, `abort_reason: null`, peak allocated `651,264` B, max gap `0.407s`.
     - `rev-py-http-cli` (`host/tests/test_http_cli.py`, `30` tests): `exit: 0`, `abort_reason: null`, peak allocated `5,107,712` B, max gap `0.799s`.
     - `rev-py-quota-m13` (`host/tests/test_quota_m13.py`, `11` tests): `exit: 0`, `abort_reason: null`, peak allocated `798,720` B, max gap `0.195s`.
     - `rev-py-intake` (`host/tests/test_intake.py`, `15` tests): `exit: 0`, `abort_reason: null`, peak allocated `815,104` B, max gap `0.349s`.
  3. **Vitest Worker/DO Suites (`17` runs, all `<= 2` selected cases per `-t`, `29` unique cases green, `1` repeat case, `1` ordering failure preserved)**:
     - `test/quota-m13.test.ts` (`8/8` unique cases green across `rev-m13-12`, `rev-m13-34`, `rev-m13-56`, `rev-m13-78`).
     - `test/quota.test.ts` (`10/10` unique cases green across `rev-quota-12`, `rev-quota-34`, `rev-quota-5`, `rev-quota-46`, `rev-quota-78`, `rev-quota-910`; note `rev-quota-56` exited `1` because case 6 in `test/quota.test.ts:873` reads `hostAlpha` state written by case 4 in the same DO instance, so running cases 4 and 6 together in `rev-quota-46` passed `2/2`).
     - `test/fleet-assets.test.ts` (`7/7` unique cases green across `rev-assets-12`, `rev-assets-34`, `rev-assets-56`, `rev-assets-7`).
     - `test/fleet.test.ts` (`4` cases verified green across `rev-fleet-12`, `rev-fleet-34`; remaining `fleet.test.ts` and `routing.test.ts` pairs audited from raw author receipts `quota-fleet-56`..`quota-route-25` after `15:54` Berlin urgent ship override).
  4. **Headless Browser & Novel Adversarial Probes (`6` runs)**:
     - `rev-browser` (`node test/quota-m13.browser.mjs`): `exit: 0`, `abort_reason: null`, peak allocated `2,490,368` B, max gap `0.537s`, `surviving_owned_pids: []`. Verified 4 distinct PNG renders in `.local/review/browser/`:
       - `quota-1440-dark.png` (`1440x900`, `72,005` B, SHA-256 `40f0bd74239b61eb65b2fb3b23de45e1fd06f80436b78f84cb734cf3ef720898`)
       - `quota-1440-light.png` (`1440x900`, `73,174` B, SHA-256 `dd4715149c1a1588d823b8fa4c9c3305975df1abbc9c597325150fa0d8a556f4`)
       - `quota-390-dark.png` (`390x900`, `50,829` B, SHA-256 `614669395d3deecfa57ba6f8bb84a20622d44e119e964b0497c1ed78ca919b1e`)
       - `quota-390-light.png` (`390x900`, `50,803` B, SHA-256 `cc62e4bf00be2caaa8ce9013ec6df511284e0703a3c749ef0ba01d28e79ba13f`)
     - `rev-novel-py`..`rev-novel-py4` (iterative probe refinement runs, `exit: 1`, `abort_reason: null`, preserved in `.local/review/guarded/`) and `rev-novel-py5` (`.local/review/novel_probes.py`, `4/4` novel checks green, `exit: 0`, `abort_reason: null`, peak allocated `1,720,320` B, max gap `0.171s`).
- **Reviewer Guard Telemetry Peaks**:
  - Max peak allocated across all 30 reviewer runs: `5,107,712` B (`4.87 MiB`, well below `14 MiB` early / `18 MiB` hard limits; final `.local` disk `1,724,416` B = `1.64 MiB`).
  - Max sample gap across all 30 reviewer runs: `0.991s` (`rev-m13-56`).
  - Surviving child PIDs across all 30 reviewer runs: `0`.

---

## 6. Native Collector Coverage & Runtime OPEN Ledger

Per `docs/reviews/QUOTA-STATUS-PLAN.md` (`M13-IG1`..`M13-IG3`), the following items remain **OPEN** beyond this source slice:
1. **Live Native Credits / Spend / Cooldown Collector Coverage (`quse` Follow-Up)**:
   - Our bounded read-only sanitized `quse` capture on `hetzner` (`.local/review/live-quse-coverage.json`, `3,416` sanitized bytes, `schema_version: "1.1"`, `collector_version: "quota_snapshot/1.1"`) confirms that all 7 allowlisted providers (`claude`, `codex`, `copilot`, `gemini`, `go`, `grok`, `zai`) currently emit **percent-only** windows (`any_native_unit_fields_present: false`). Upstream `quse` / provider collectors do not yet emit live `credits_remaining`, `spend_used`, `spend_limit`, `unit`, `profile`, or `cooldown_until`.
2. **All-Machine Live Quota Reporting (`hetzner`, `workstation`, `macbook`, `win35`)**:
   - End-to-end live `POST /api/quota` and `PUT /api/fleet/subscriptions` reporting across all registered hosts and live operator verification of `/fleet` (`#quota-section`) and `hub quota` against production data remain post-deploy runtime gates owned by `platform`.

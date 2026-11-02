# TBM 12-metric catalog audit and API contract

Audit of the source tree at `origin/main` (`942631ea`), before catalog changes.

| Metric | Computed or stored today | REST today | CLI today | TBM dashboard today |
|---|---|---|---|---|
| `burn_rate_vs_pacing_target` | `src/tbm-controller.ts` writes `avg_burn_rate_vs_target` from the current pool `pacing_ratio`; live state is also on `tbm_quota_pools`. | `/api/quota/pacing` exposes current pool ratio; `/api/quota/rewards` exposes stored rollups. | `hub quota pacing` renders the current ratio. | Pacing card shows current ratio and target/current rates; no historical 24h/7d metric. |
| `leftover_quota_at_planned_reset` | `tbm_pool_resets` captures reset snapshots; controller rollup sums them into `tbm_metrics_rollup.leftover_quota_at_reset`. The rollup currently turns absent snapshots into zero. | Only indirectly in `/api/quota/rewards`; no metric response. | Not exposed. | Not exposed. |
| `reserve_floor_breaches` | Controller rollup compares the current pool against soft/hard floors and stores counts in the rollup. This is a current-state boolean, not historical breach event/duration evidence. | Only indirectly in `/api/quota/rewards`. | Not exposed. | Not exposed. |
| `useful_work_per_tokens` (`eta`) | `tbm_reward_events.token_efficiency_eta` is calculated from verified work and known native session tokens; rollups store ETA and known flags. | `/api/quota/rewards` exposes events and rollups. | Not exposed. | Not exposed. |
| `idle_wasted_quota_over_time` | PID controller state `tbm_controller_state.deficit_tokens` and rollup `idle_wasted_quota`; percent-only pools are unavailable. | Only indirectly in `/api/quota/rewards`. | Not exposed. | Not exposed. |
| `engine_mix` | Controller rollup estimates free/Grok shares from session counts, forces paid share to zero, and is not token-weighted evidence. | Only indirectly in `/api/quota/rewards`. | Not exposed. | Not exposed. |
| `admission_rejects_rate` / `no_eligible_route_rate` | Rollup estimates these from `session_control_sessions` held/refused states; it does not read authoritative dispatch submission/rejection counters. | Only indirectly in `/api/quota/rewards`. | Not exposed. | Not exposed. |
| `time_to_useful_outcome` | Rollup averages duration of stopped/failed sessions from session timestamps, not verified outcome lifecycle timestamps. | Only indirectly in `/api/quota/rewards`. | Not exposed. | Not exposed. |
| `banked_remaining` | `tbm_banked_tranches.remaining_tokens` with `quantity_known`; unknown quantities must remain unknown. | `/api/quota/banked` exposes tranche rows. | `hub quota banked` prints remaining quantity when known. | Banked table displays tranche remaining/allocated quantity. |
| `time_to_expiry` | Derived from `tbm_banked_tranches.expires_at_ms` and current time. | `/api/quota/banked` exposes expiry timestamp. | `hub quota banked` prints expiry timestamp, not a computed duration. | Banked table computes and displays a countdown. |
| `expired_unused` | `tbm_banked_tranches.expired_unused_tokens` and controller rollup `banked_tokens_expired_unused`; unknown quantity provenance is not carried through the rollup. | `/api/quota/banked` exposes the raw tranche field; `/api/quota/rewards` exposes its aggregate. | Not rendered. | Not rendered. |
| `banked_burn_rate` | `tbm_banked_tranches.banked_burn_rate_tpm` has a schema default of zero, but the controller only updates the target rate; it does not record observed tranche consumption rate. | `/api/quota/banked` exposes the raw field (including an unproven default). | CLI omits both rates. | Dashboard omits both rates. |

## Catalog response

`GET /api/quota/metrics` returns the pool inventory under `windows.24h.pools`
and `windows.7d.pools`. Each pool carries every catalog field as `{value, unit}`; a
missing or partial source uses `value: null` with a reason. The CLI command is
`hub quota metrics [--json]`; the TBM dashboard renders both windows per pool.

The endpoint currently computes useful-work-per-token from verified outcome
events plus measured native tokens; engine mix from token totals joined to
recorded engines (and labels its observed coverage); reset leftover from actual
reset snapshots; useful-outcome latency from session creation and reward
timestamps; and banked inventory, expiry, and expiration loss from the tranche
ledger. It exposes a positive recorded banked burn rate and leaves it unknown
when only the schema default is present. It leaves historical pacing ratios, floor-breach events,
idle deficit history, and admission/no-route rates unknown because current
storage does not retain timestamped measurements for those windows. A missing
tranche inventory is unknown; known empty inventory is zero only after tranche
records establish it.

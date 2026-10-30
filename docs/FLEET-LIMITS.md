# Fleet concurrency limits (`hub limits`, intake cbcadaa1)

Operators read and change per-account concurrency caps from the CLI without a
redeploy. Caps live in the Hub `fleet_accounts` table (Durable Object SQLite),
which every launch admission reads, so a change applies to the next placement
and persists across Hub restarts and deploys. Both commands are admin-only
(an admin `hub auth login`); host, agent and job credentials get `403`.

```
hub limits show [--json]
hub limits set <engine|account> <n> [--allow-lower] [--json]
```

## show

`GET /api/fleet/limits` (MCP tool `limits`). Two tables:

- Engines: one row per canonical provider, with the sum of its account caps,
  the sum of active slots, and the accounts behind it.
- Accounts: `CAP` = `max_concurrency`; `ACTIVE` = counted slots (`RESERVED`
  launch reservations + `OBSERVED` orphan native sessions); `LIVE` = host
  catalog verified live processes; `FREE` = `available_slots`; `STATUS` = quota
  health (`ok`, `unknown`, `blocked`, or `disabled`). Same counting as
  `hub fleet` / `accountStatus`.

## set

`PATCH /api/fleet/limits` with `{"target": "...", "max_concurrency": n,
"allow_lower": false}` (MCP tool `limits_set`). `n` is an integer 1..100.

Target resolution:

- An account label (`hub-claude`, or a canonical label that addresses a legacy
  alias row such as `shared-go` -> `urgent-muse`) sets that one account.
- Otherwise the target is an engine: every account whose canonical provider is
  that engine, or that serves it through a registry route. `claude` resolves to
  `hub-claude` and `shared-claude`; `opencode` resolves through its route.
- No match returns `404 unknown_limit_target`.

Lowering:

- Default is raise-only, matching `PATCH /api/fleet/accounts`. An engine set
  raises accounts below `n` and keeps higher caps (`kept_higher`), so
  `hub limits set claude 13` raises `hub-claude` to 13 and leaves
  `shared-claude` at 100.
- An account set below its current cap is refused with
  `409 account_lowering_requires_allow_lower` and changes nothing.
- `--allow-lower` sets every resolved account to exactly `n`. Lowering never
  stops running sessions; admission simply refuses new launches on that
  account until active work drops below the new cap. Use an account target to
  lower one account.

Each account in the response reports `from`, `to` and `result`
(`raised`, `unchanged`, `lowered`, `kept_higher`). Every effective change is
recorded in the `audit` table as `fleet_limits_set`. The whole set is one
transaction.

## Related limits

- Hub-central account default on create: `PROVIDER_DEFAULT_MAX_CONCURRENCY`
  in `src/provisioning.ts` (8); see
  [ENGINE-PROVISIONING.md](ENGINE-PROVISIONING.md). It applies only when an
  account is created; `hub limits set` changes existing accounts.
- Per-host dispatch concurrency (`dispatch_concurrency` in host settings,
  1..13, default 13) bounds how many offers one host runs at once. It is a
  host-local setting, not managed by `hub limits`.
- `PUT /api/fleet/accounts` replaces the whole registry and remains the path
  for adding or removing accounts and routes.

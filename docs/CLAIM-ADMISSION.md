# Ownership-aware launch admission

Editable fleet and routing launches consult the existing `/api/bus/claims` table inside the same Durable Object SQL transaction that reserves account and machine capacity. Claims remain the sole lock. Launch rows keep the bound claim identity in existing `request_json`; there is no parallel lock registry.

## Trust boundary

Admission declarations are a control-plane gate on enqueue, offer/reservation, and native `offered → starting`. They are not an OS sandbox, seccomp profile, or filesystem jail. A process that starts after a successful edit admission can still write wherever the host already allows. A `read` declaration proves only that the caller asked for read access, not that later tool use is confined.

## Access declaration

Fleet `POST /api/fleet/launches` and routing `POST /api/tasks` accept an optional typed `access` object. Free-text prompts, summaries, and `approved_action` strings are never interpreted as authority.

| `access.mode` | Meaning | Claims |
|---|---|---|
| `read` | Explicit proven read-only *declaration* | Not required |
| `edit` | Explicit editable intent | Required: live claim id + scopes the claim covers |
| omitted or `legacy_unspecified` | Compatible legacy / unspecified | Not proven read-only |

Edit body:

```json
{
  "access": {
    "mode": "edit",
    "claim_id": "<ulid from POST /api/bus/claims>",
    "project": "projA",
    "scopes": ["src/fleet.ts"]
  }
}
```

`claim_id` is required for edit. `scopes` are path prefixes using the same normalization as `/api/bus/claims` (`..` rejected; `.` and empty segments dropped). The live claim's `scope` must cover every requested path (equal or ancestor). `project`, when sent, must match the claim's project. Holder identity is taken from the claims row and the authenticated caller; client-supplied holder, actor, or secret fields are rejected as unknown keys.

Read body: `{ "access": { "mode": "read" } }`. Claim fields are not allowed on read.

Responses and job views label `access` as `read`, `edit`, or `legacy_unspecified`. Unspecified work is never relabeled `read`.

## Who may use a claim

Admission copies the caller and sets `isAdmin: false` before consulting bus identity. An admin session cannot spend another agent's claim. A `routing:submit`-only token cannot spend a claim unless that token's actor *is* the registered claim holder. Unknown, malformed, root-role, or ambiguous callers fail closed (`claim_actor_ambiguous`, `claim_holder_mismatch`, `claim_authority_unsupported`).

Creating and releasing claims is unchanged: `POST /api/bus/claims` and `POST /api/bus/claims/:id/release` with `bus:send`. Launch admission only reads current rows.

## Policy for configured workspaces

Admins can require claims for named workspaces without pretending unspecified launches are read-only:

```
GET  /api/fleet/claim-policy
PUT  /api/fleet/claim-policy
{ "workspaces": [{ "workspace": "demo", "project": "projA", "require_claim": true }] }
```

`project` associates that workspace with a claims project. When `require_claim` is true, `legacy_unspecified` requests fail with `claim_required` and create no reservation. Explicit `read` remains allowed. Explicit `edit` still needs a live covering claim in that project.

## When the check runs

1. **Enqueue** (routing `POST /api/tasks`): invalid edit authority is refused before a job row exists. Queuing may still precede placement.
2. **Reserve / offer** (`POST /api/fleet/launches`, including routing dispatch): the live claim, holder, scope, project, and "no other active launch bound to this claim" check run inside the existing `transactionSync` that inserts `fleet_launches`. A failed check returns before `INSERT`, so account/machine capacity does not change.
3. **Native start** (`POST /api/fleet/launches/:id/claim`): bound edit launches revalidate the same claim before `offered → starting`. The host does not have to be the claim holder; it may not change the bound identity.

A preflight `/api/fleet/plan` check is extra and is not the reservation.

Claim release, expiry, or reassignment while a job is queued prevents the later offer. Routing dispatch re-reads claims before calling launches; the launch transaction is authoritative.

## Concurrency, retries, running expiry

- One live claim authorizes at most one active launch (`offered` / `starting` / `running` / `uncertain`). Same-holder retries with a new idempotency key conflict at reserve; an identical retry of the same key and bound action replays without a new reservation.
- Changing `access`, scopes, approved action, or other canonical fields under the same key is `idempotency_conflict` (409).
- If a claim expires or is released after a launch is already starting/running/uncertain, capacity stays reserved until a confirmed terminal result. The hub does not invent a process kill or replay uncertain effects. Operators reconcile using claims + launch metadata.
- Failed SQL in that same transaction rolls back; there is no partial reservation.

## Atomicity

Fleet, routing, and claims already share the Hub Durable Object SQL. Admission instantiates `BusService` on that same store and runs inside the fleet `transactionSync` callback. There is no `await` inside that callback.

Limits:

- `BusService.claim` / `listClaims` still use `Date.now()` for their own writes. Admission expiry at reserve/start uses the fleet clock against the claims row.
- Routing `dispatch` still `await`s `fleet.handle` to parse a JSON body. Claim + `INSERT` happen after that parse, in one transaction.
- This worker cannot change `src/hub.ts`; it does not inject the Hub's `busService` instance. Both objects speak the same tables.

## Errors

| error | typical status | meaning |
|---|---|---|
| `invalid_access` | 400 | malformed `access` object |
| `claim_required` | 403 | workspace policy requires a claim; request is unspecified |
| `claim_holder_mismatch` | 403 | authenticated caller is not the claim holder |
| `claim_actor_ambiguous` | 403 | caller identity missing or unusable |
| `claim_scope_mismatch` | 403 | requested paths are not covered |
| `claim_project_mismatch` | 403 | workspace/project policy does not match the claim |
| `claim_authority_unsupported` | 403 | unknown/root/unusable required identity |
| `claim_inactive` | 409 | missing, released, or expired claim |
| `claim_conflict` | 409 | another active launch already bound to this claim |
| `idempotency_conflict` | 409 | same key, different bound action/access |

## Adoption limits

- Existing callers that omit `access` keep current placement behavior and are labeled `legacy_unspecified`.
- Routing-only credentials do not mint claims. Editable routed work requires the submitting actor to be the registered holder of a live claim.
- Host execution is not sandboxed by this gate.
- Model tier policy, quota unknown/reserve gates, fair queue scanning, and uncertain-run retention are unchanged.

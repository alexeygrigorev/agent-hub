# Ownership-aware launch admission

Fleet and routing use the current `/api/bus/claims` rows in the Hub Durable Object SQL database. Final claim admission, account/machine reservation, and routed job linkage run in the same synchronous SQL transaction. There is no asynchronous gap between authorization and reservation, and no second lock table. Existing launch lifecycle facts retain references to claims and affected scopes.

## Trust boundary and adoption

This is a control-plane admission gate, not an OS sandbox or a filesystem jail. The host's existing process permissions determine what a launched process can actually write. An explicit `read` declaration records caller intent; it does not prove or enforce read-only tool use. Free text never grants claims authority.

| Declaration | Meaning | Admission |
|---|---|---|
| `access.mode: "read"` | Caller explicitly declares read-only intent | Existing placement gates; no claim required |
| `access.mode: "edit"` | Caller explicitly requests editable work | Current workspace mapping, credential, covering claim, and lifecycle conflict checks required |
| Omitted, `null`, or `legacy_unspecified` | Compatible legacy/unspecified intent | Existing placement gates; refused where workspace policy requires claims |

Unspecified launches are never labeled read-only. Configuring claim requirements closes the unspecified entry path for that workspace; it cannot stop a dishonest caller from declaring `read` and later writing. Host sandboxing or a trusted read-only executor is a separate adoption requirement if that stronger guarantee is needed. Legacy launches and externally observed sessions have no provable edit scope, and this feature does not retrofit claims onto them.

## Supported HTTP contract

Canonical fleet endpoints are under `/api/fleet/`. Routing submission uses **`POST /api/fleet/tasks`**, with get/list/result under the same prefix. `/api/tasks` is the routing service's internal path; the Hub does not expose that bare prefix. No Hub route changes are needed.

Both `POST /api/fleet/launches` and `POST /api/fleet/tasks` accept:

```json
{
  "access": {
    "mode": "edit",
    "claim_id": "<claim ULID>",
    "project": "demo-project",
    "scopes": ["src/example.ts"]
  }
}
```

`claim_id` and one to 32 relative path prefixes are required for edit. `project` is optional, but must match when supplied. Every scope must equal or descend from the authoritative claim's scope. Paths use `/`; `.` and empty interior segments normalize away. Absolute paths, `..`, backslashes, colons, and control characters are refused. These are logical project-relative prefixes, not filesystem resolution or symlink checks.

Fleet edit requests also require a nonempty `approved_action` string (at most 12,000 characters), in addition to the normal launch fields and `idempotency_key`. Routing retains its existing required `approved_action` or batch of `{prompt, approved_action}` tasks. It binds that exact task array into the resulting private fleet request. These action strings are retained execution instructions, never authorization credentials.

For read use `{"access":{"mode":"read"}}`. Read and unspecified declarations cannot carry claim/project/scope authority. Unknown fields such as `holder`, `caller`, `token`, or `edit_authority` are refused. Caller identity comes exclusively from Hub authentication.

Creation/release of claims remains unchanged: `/api/bus/claims` and `/api/bus/claims/:id/release` with `bus:send`. Admission reads those authoritative rows and never acquires or renews a lease itself.

## Credential and resource authority

Editable requests require an authenticated **agent credential** with:

- `bus:send`, plus `fleet:launch` for fleet submission or `routing:submit` for routing submission;
- a current, unexpired, unrevoked stored credential matching the authenticated actor;
- a uniquely registered claims holder in the same project, authorized by the bus's existing identity checks;
- an admin-configured workspace-to-project association matching the current claim.

An admin session cannot impersonate a holder. Host, job, root-role, unknown, and unsupported required identities fail closed for edit submission. A routing-only token, including one issued by `/api/fleet/routing/clients/:name`, has no claims authority even if an actor string happens to match a holder. An appropriate existing agent credential must be provisioned through the supported administration path; this feature does not upgrade credentials.

The server retains the actor, nonsecret credential ID, required submit permission, and immutable claim identity (`id`, `project`, `scope`, `holder`, `created_at`) in existing private request JSON. Tokens and token hashes are never copied into launch/job metadata. Neither the host nor request JSON can substitute this binding. Revalidation resolves the original credential ID; it never falls back to another credential for the actor. Token refresh that replaces/revokes that ID therefore holds pending work until it is reconciled.

## Workspace policy

Admins may read/replace policy through:

```text
GET /api/fleet/claim-policy
PUT /api/fleet/claim-policy
```

Example PUT body:

```json
{
  "workspaces": [
    {"workspace":"demo", "project":"demo-project", "require_claim":true}
  ]
}
```

The PUT replaces the complete policy transactionally. Policy identifies workspace labels; administrators must map aliases consistently because the hub does not attest host filesystem paths. `project` may be `null`; any edit request without a non-null mapping fails with `claim_workspace_unconfigured`. A non-null project associates that named host workspace with the claims namespace. `require_claim: true` refuses unspecified submissions and queued placement. Explicit read declarations remain compatible. Mapping changes are checked at actual placement and native start, including for already queued edit requests. Removing a mapping also prevents edit placement/start.

## Transactions and retries

1. Routing enqueue validates current authority before inserting a job. It may queue work while no eligible machine is available. Fleet plan is a preview, never a reservation.
2. Actual placement selects the existing model/account/machine candidate and re-reads policy, credential, holder, and claim state in the reservation transaction. Routing commits its selection and job-to-launch link in that same transaction. SQL failure rolls back all reservation/link effects. It does not discard an earlier valid queued job.
3. Native `POST /api/fleet/launches/:id/claim` revalidates the bound edit authority inside the transaction that changes `offered` to `starting`. The authenticated destination host claims execution; it does not become the claims holder.

A claim released, expired, or reassigned while queued prevents any later offer. A revoked/expired credential or changed project mapping also prevents placement/start. Same-key retries with the identical retained request return the existing job/launch without acquiring effects, including when its authority has since expired. Changing action, access, scope, claim ID, or other canonical intent under the same key returns `idempotency_conflict`. A new key is a distinct request and still faces active-scope exclusion.

Dispatch preserves the existing fair queue cursor, tier policy, candidate ranking, explicit account pinning, quota reserves, unknown gates, and machine/account capacity checks. Interrupted-link recovery relinks only an existing launch whose retained action, access, and authority match the queued job; a request-key collision alone is insufficient.

## Active effects and reconciliation

One claim authorizes at most one active launch. Different same-holder claim IDs do not bypass overlapping scope exclusion. Any offered/starting/running/uncertain launch with an edit binding blocks a replacement claim in the same project whose claim scope overlaps the retained scope. This remains true after release, expiry, claim retention deletion, or a different holder acquiring a replacement lease. The full original claim scope is conservatively protected until the existing launch lifecycle confirms termination.

After a running claim or credential expires, the process lifecycle and capacity remain reserved. Admin fleet views and routed task views expose an `admission_hold` reason. Held offers are withheld from host polling and native start is refused. Offer scanning continues past held rows so they do not hide eligible work in other available slots. Running receipts and confirmed terminal reports continue to use the existing lifecycle protocol. The hub never invents a process kill, automatically replays an uncertain side effect, or frees capacity because a lease expired. A host-confirmed exit/failure (or routing terminal report with `confirmed_exit`) ends the lifecycle reservation. Operators must investigate outstanding effects through the existing launcher/native receipt and lifecycle protocol; there is no new force-release API. An offered launch with invalid authority remains reserved for operator reconciliation because the existing protocol has no offered-cancellation receipt.

Older edit launch rows without the new server binding are held and conservatively block new edits until reconciled. Unknown/corrupt retained authority fails closed. Read/legacy work remains explicitly outside claims scope protection.

## Errors

| Error | Status | Meaning |
|---|---|---|
| `invalid_access` | 400 | Malformed declaration or unsupported path |
| `approved_action_required` | 400 | Fleet edit omitted explicit action |
| `claim_required` | 403 | Workspace policy refuses unspecified work |
| `claim_workspace_unconfigured` | 403 | Workspace has no project association |
| `claim_project_mismatch` | 403 | Claim mismatches workspace/declared project |
| `claim_scope_mismatch` | 403 | Claim does not cover every requested prefix |
| `claim_holder_mismatch` | 403 | Current registered identity cannot use this holder |
| `claim_actor_ambiguous` | 403 | Actor is missing |
| `claim_authority_unsupported` | 403 | Unsupported identity, invalid/revoked/expired credential, or missing permission |
| `claim_authority_changed` | 409 | Retained claim identity changed |
| `claim_inactive` | 409 | Claim missing, released, or expired |
| `claim_conflict` | 409 | Another active launch retains overlapping authority |
| `idempotency_conflict` | 409 | Same key with changed immutable request |

There are no new resources, deployments, provider credentials, process controls, or model policy changes in this feature.

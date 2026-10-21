# Work activity: seeing who is already on a task

Read-only visibility slice. It shows what already exists — active Hub work
declarations (`claims` table) and recent Hub bus traffic (`messages` table) —
so an operator can see that someone is already working on a task or workspace
before starting overlapping work (for example, a second CI fix on the same
task). It creates no store, writes nothing, and defines no agent MUST rules:
the full claim/messaging protocol (leases, TTL policy, where agents must
write) is owned by a separate design document when it lands.

## Operator recipe

```bash
hub activity --workspace /home/alexey/git/agent-hub
hub activity --project agent-hub
hub activity --holder agent-hub:worker-1
hub activity --actor agent-hub:worker-1 --limit 10
hub activity --project agent-hub --all   # include released/expired claims
```

UI: each session detail view (`Sessions & Lineage`) has a "Work activity"
panel scoped to that session's workspace, and Fleet has a "Work activity"
section with hub-wide active claims and recent bus messages. MCP: the
`bus_activity` tool maps to `GET /api/bus/activity` with query
`workspace`, `project`, `holder`, `actor`, `limit` (1–100, default 20),
`all`.

## Semantics (existing primitives only)

- Claims come from `BusService.listClaims` with the same auth scoping as
  `claims_list`; recent messages come from `BusService.getBusMessages` with
  the same scoping as the bus message APIs. Auth is `bus:read` or admin.
- A `workspace` is resolved to a claim project through the existing
  `fleet_claim_policy` table (workspace → project). An explicit `?project=`
  wins. A workspace with no policy row yields an empty claim set with an
  explanatory notice — never caller-global or hub-global rows under a
  workspace heading. Configure the mapping with the existing
  `fleet_claim_policy_write` tool / `PUT /api/fleet/claim-policy`.
- The `notice` field is a plain-English summary ("N active work
  declarations, M recent bus messages in scope — someone may already be on
  this"). Read the rows before starting overlapping work; an empty scope is
  reported honestly, not as zero evidence.

## Non-goals

Lease/TTL policy, cross-host bridging, and any "agents MUST write here"
rules are out of scope for this slice. Aplexer-native state (`a message
log`, `a work join`, `a context`) is deliberately not duplicated here: the
Hub already bridges hub messages to native aplexer mailboxes for registered
tags (SPEC §7.5), and this slice only surfaces Hub-side declarations and
traffic that already exist.

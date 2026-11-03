# Fleet launch left offered without a binding (intake 718b1c18)

## Incident evidence

Hub session `f521a611-7103-40f2-9c7a-14b7ab21642a` was created at
`2026-10-29T14:14Z` as a Codex interactive replacement for intake
`ea6a811c`. Its history records repeated `offered` state with no binding,
although that intake's notes record work during the same period. The native
session that performed the work was `ad6e0910-b59a-4a12-86dd-5e446347c6b2`,
launched under Fleet launch `a8ccb7c5-a1c9-4724-b8d6-c6638c20d65a` with tag
`hub-a8ccb7c5-a1c9-4724-b8d6-c6638c20d65a`. The native appeared at
`2026-10-29T14:21:59Z`; the recorded work transcript runs from about 14:22Z to
14:36Z, with later acknowledgements through 15:11Z. Hub later bound the row to
that native; the current session record is stopped. This later binding does
not change the earlier period when the work was attributed to an unbound offer.
The live `hub sessions show f521a611-7103-40f2-9c7a-14b7ab21642a` read on
2026-11-03 confirms `state=stopped`, binding native
`ad6e0910-b59a-4a12-86dd-5e446347c6b2`, and workspace
`/home/alexey/git/agent-hub-wt-oneinstall`; the binding was reported at
`2026-10-30T20:40:12Z`.

Wake `58d30f22` became due at about 14:46Z and later missed with
`could not queue delivery within 10 min of due time`. A pending wake could
only enqueue a send for a `running` session, so the still-offered row waited
until the deadline even though its native was alive.

The task records 24 sessions on intake `ea6a811c` at report time. Its current
history has 29 session-link records, including `f521a611`; these are historical
links and do not imply that all 29 sessions are live.

## Cause

Dispatch allowed reuse of the intake worktree lock after fetching fresh Hub
proof that its previous owner was terminal. The later host binding path called
`protect_adoption` without the same terminal-owner proof. It then refused the
held lock because the predecessor's native was not terminal. That refusal was
silently skipped: the launch stayed `starting` with `native_id` null, the
offer remained eligible for re-offer, and the running native never advanced
the Hub session. The wake path had no fast failure reason for an unbound
session whose launch had no live native.

## Ownership and implementation boundary

- **Intake 718b1c18 owns first binding of a Fleet-launched session.** Dispatch
  and host binding must use the same fresh same-intake terminal-owner proof.
  A complete catalog reconcile settles a claimed start that has no live
  native, and a verified binding records the native on the launch. Wakes fail
  once with an explicit reason when complete current evidence shows no live
  native, and idle recovery is enqueued once. Idle replacement is held while
  the intake already has a live replacement candidate.
- **Intake 750b36f3 owns adoption/reconciliation of aplexer-launched sessions
  that lack a Fleet launch row.** This fix reuses the existing native census
  and host-binding/reconciliation mechanisms; it does not add a second
  observer or adoption loop.
- **Intake 5f38de5e owns repairing an existing binding after its worktree
  moves.** This fix binds a newly launched Fleet native using the path the
  host actually reported, and does not rewrite an existing binding's
  workspace or generation.

The ownership split keeps Fleet launch settlement, external native adoption,
and workspace-move repair as separate lifecycle operations.

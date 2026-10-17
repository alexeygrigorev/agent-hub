# Host-enrollment authority (M12e prerequisite for future M9 backport)

Standalone, no metrics/UI dependency: per-machine approval witness
(`approval_seq`, integer assigned server-side at APPROVAL in one atomic
transaction) plus synchronous live-credential checks inside the SAME SQL
transaction as every mutation. Crypto token-hash prep stays async BEFORE
the txn; kind/scope/expiry/revocation/family/machine checks are sync
INSIDE it — no awaited final-verifier gap.

## Contract matrix (all enforced, all tested)

- Pending C (approved, not yet exchanged) leaves the CURRENT bound family B
  fully operational on register/refresh/session-offer paths.
- After C exchanges, the old B credential is denied and C owns the machine
  (predecessor-CAS rotation; delayed/stale exchanges cannot retake).
- Legacy both-NULL (unbound machine, no witness anywhere) grants ONLY for a
  genuinely unbound machine and never overwrites a bound legacy owner or a
  newer witness (unbound old-seq / legacy-hole closure).
- Replay/rollback: approval replay mints no new authority; issuance retry
  stays idempotent; refresh inherits the witness and rotates the machine
  binding with predecessor CAS.
- Register re-reads the live credential by id synchronously inside the
  mutation txn (replacing the awaited re-verifier): denial mutates nothing
  (no row, no machine credential overwrite, no NULL stamp).
- Compatible with all other auth flows (human/device CSRF preserved);
  legitimate humans and active credentials are retained.

## Files

- NEW `src/host_enrollment.ts`: per-machine approval-seq helper.
- `src/schema.ts`: `approval_seq` on machines/credentials/device_pairings.
- `src/registry.ts`: CAS + authority match on register.
- `src/hub.ts`: narrow approve (mints witness in-txn) / issuance (enforces
  `pairing.approved_seq == machine.approval_seq` matrix, no consume on deny)
  / refresh (inherits witness) hunks. No `metrics.ts`, routes, or UI.
- `test/host-authority-r6.test.ts` (11 actual-route cases H1..H11) +
  `test/vitest.host-authority-r6.config.ts`.

## Provenance

Isolated exact-hunk port of the frozen `f20b35a6` hostauth core (read-only
source `agent-hub-wt-platform-metrics-r5`), adapted to session/registry
surfaces only. New atomicity gap (awaited `verifyBearerScope` outside the
txn, demonstrated by real registerHTTP with a queued microtask) is closed
by the sync-inside-txn rule above. Independent different-provider review
required before main.

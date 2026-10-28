# Autonomous intake queue

Hub's existing durable alarm runs the intake watchdog once per minute. New
open or in-progress intakes with acceptance criteria enter the existing canonical session
admission path, without an engine override. Model policy, quota, capacity,
resource and workspace admission checks remain authoritative. Scope flags are informational under scope policy v2. Blocked and pending-approval intakes remain visible without automatic admission. A watchdog-owned held intent is visible and rechecked through canonical retry rather than duplicated. User-owned held sessions are observed without automatic retry or stop.

Rollout defaults to at most two watchdog-owned in-flight intents globally and one per project. `HUB_INTAKE_QUEUE_MAX_INFLIGHT` (1–32) and `HUB_INTAKE_QUEUE_MAX_PER_PROJECT` (1 through the global limit) configure these limits. `HUB_INTAKE_QUEUE_ENABLED=false` disables automatic starts, retries and stops while keeping queue visibility and alerts. Invalid policy values disable automatic mutations. These are operator Worker bindings, subject to the normal production approval gate; no settings are changed by this feature. Watchdog status exposes the effective policy. Held canonical sessions count toward capacity; a durable reservation also occupies capacity while admission awaits or its reply is unknown. A canonical receipt releases the reservation. Admission checks also release reservations for missing, blocked, archived or completed intakes; actual active canonical sessions still count independently. Receipt lookups and capacity reads are indexed and bounded. Capacity holds consume no retry attempt and are checked again after one minute.

The queue preserves session identities and idempotency keys across lost
responses. A native worker with stale or missing observation is unknown,
not dead. A failed canonical row does not authorize a replacement beside a
live native worker. Only a watchdog-owned goal session may receive an automatic stop. Pending wakes, operator-owned interactive sessions and working sessions are protected. An idle stop requires sampled progress plus the canonical host-observed stall classification; elapsed age alone is insufficient. Terminal faults retain their failure diagnostic separately from the native lifecycle. Only confirmed exit permits a replacement.
Each tick admits at most eight intakes and performs at most eight recovery or
escalation actions, rotating fairly through due work. Recovery is limited to three attempts per intake, with exponential delays of 15, 30 and 60 minutes. Exhaustion creates one durable needs_attention alert and note per intake/reason, preserving the open intake. Failed delivery is retried with the same note key. A fresh, healthy watchdog-owned canonical worker clears escalation and resets the episode retry count. Operational failures do not
become NEEDS_ALEXEY. That designation is reserved for production approvals,
secrets, deletions and 2FA; agents never approve production gates.

`GET /api/intake/queue` is an authorized, paginated view of visible open intakes. A page returns at most 64 task records, with one additional record read to detect a next page; ordinal cursors reveal no unauthorized task IDs. Queue positions are relative to the authorized page, which the dashboard labels explicitly. The
main dashboard shows queue positions, running work, recovery/escalation,
last movement time and the most recent 32 movements per intake. Queue positions
follow due-time eligibility within the displayed page. Dispatch uses a durable cursor across 64-intake batches so continuously arriving work cannot keep the same first IDs at the front. Offered and starting work stays queued until the replacement reaches canonical running state. Unproven launch or native receipts receive bounded read-only checks with backoff, then escalation; their age never authorizes a stop or replacement. Repeated
polls do not refresh movement time. Items unchanged for 30 minutes are
highlighted. The existing forgotten-intake view remains available.

The retry count measures the current unproven or failed episode. A freshly matched, healthy running watchdog-owned worker resets it even before escalation, so several productive sessions followed by confirmed exits do not exhaust a lifetime launch budget.

The minute tick shares one bounded observation snapshot for its batch. Retained session, claim, native and wake lookups use indexes and result limits; observation overflow is unknown and cannot authorize a launch. Watchdog status exposes the overflow reason and cumulative count; one durable operator alert identifies each affected batch so this limit cannot silently strand work. Alert markers are lifetime markers for the same sorted set of intake IDs; a later recurrence does not send another alert, while the cumulative counter and last-limit timestamp continue to advance. Admission rechecks only its specific intake after awaited work. Daily digest delivery advances through the same batches with per-intake daily markers, preserving complete coverage without repeated whole-ledger reads. The budget regression seeds 764 intakes, 5,000 retained sessions, 2,000 natives and 3,000 claims and checks cursor coverage and tick time inside the test session's CPU/memory limits.

Stop retries use one canonical command key per session until the native exit receipt arrives. A replacement has a new canonical session ID and a new stop key; an acknowledgement is never a completed restart.

A session exiting successfully is not completion evidence. The queue never
closes or deletes intakes or writes a DONE note. A displayed done state
means the current intake has a DONE note containing a source/deployment
reference and live check/result. The display explicitly identifies that as
reported evidence, not independent verification of every acceptance criterion.
The implementing agent must verify each criterion before appending DONE.

The schedules intake owns scheduled prompts; the driver intake owns the
singleton driver and its command surface. They use this queue and canonical
session APIs rather than introducing a second dispatch loop.

Keep orchestrator-main as the interim watcher until deployed acceptance
proves both unattended pickup of a fresh intake and recovery of a deliberately
failed session owned by the proof run. After both proofs are recorded on the
parent intake, the watcher becomes optional oversight. This code does not
stop or replace the watcher, approve a production gate, or change its schedule.

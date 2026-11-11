# Picker diagnostic occurrence authority

This source adds two optional Node authorities: `picker_excluded` and
`picker_quota_stale`. It does not activate them, stage an enrollment, send mail,
change routing or establish all-seven-producer Phase7 coverage. Root and the
sole deployment owner must independently review the installed release and
activation boundary. Other missing producer authorities remain UNKNOWN.

## What counts

Each invocation of `pickModelRouteDetailed` begins observation before its first
await, including calls from Control's serialized actual draws and dry-run picker
requests. It enqueues the exact existing console decisions before emitting them:

- `picker_excluded`: `diagnostics.excluded.length > 0`.
- `picker_quota_stale`: `diagnostics.warnings.length > 0`.

One nonempty decision increments that producer once, irrespective of candidate
count. Healthy completion advances invocation counters without an occurrence.
The credential monitor's public response also projects credential advisories
into `.excluded`; that separate predicate remains unchanged. Advisory-only
fallback therefore need not increment the console-exclusion counter.

Proof never performs a second draw, refresh, provider probe, mail send or RPC.
The picker does not await proof IO. Missing, broken, exhausted or slow observation
cannot refuse/delay its business response on durability. The existing counters
for three actual degraded picks and their Control coverage epoch are unchanged.
These diagnostic counters are not delivery ACKs or SMTP-success counters.

## Explicit enrollment

The credential default retains `HUB2_ALERT_EVIDENCE_DIR`, its existing
`hub_credential_monitor_enrollment_v1` / `hub_credential_monitor_journal_v1`
formats and the existing three-artifact staging contract. No installer/stager
change is part of this slice.

Each diagnostic needs a separate NEW private directory configured explicitly by
`HUB2_PICKER_EXCLUDED_EVIDENCE_DIR` or
`HUB2_PICKER_QUOTA_STALE_EVIDENCE_DIR`. Directories must not be shared with each
other or the credential enrollment. Nothing creates them automatically. The
existing credential stager cannot stage a diagnostic enrollment.

The canonical `enrollment.json` uses exactly:

```json
{"kind":"hub_picker_diagnostic_enrollment_v1","producer":"picker_excluded","recipient":"dev@dtcdev.click","enrollment_id":"<32 lowercase hex>","source_id":"<64 lowercase hex>","release_sha":"<full reviewed actual release SHA>","artifacts":[{"path":"<absolute installed artifact>","sha256":"<actual SHA256>"}]}
```

`artifacts` must contain exactly the following five unique absolute paths to the
actual installed files, all backed by both load-time and current hashes:

1. `hub2/node/alert-evidence.ts`
2. `hub2/node/main.ts`
3. `hub2/src/worker.ts`
4. `hub2/src/model-picker.ts`
5. `hub2/src/routing-pool.ts`

`source_id` is SHA256 of canonical UTF-8 JSON of the ordered artifacts array
(sorted object keys, compact separators). The new `journal.json` uses exactly:

```json
{"kind":"hub_picker_diagnostic_journal_v1","producer":"picker_excluded","source_id":"<same source ID>","enrollment_id":"<same enrollment ID>","occurrence_count":0,"started_total":0,"finished_total":0}
```

For the other directory, both producer fields are `picker_quota_stale`. Files
must be canonical, owner-only regular single-link files; directory components
cannot be symlinks. Use the existing authority's owner/mode/durability checks.
Zero is a separately verified NEW activation boundary, never historical proof.
The release label is operator-verified provenance, not cryptographic attestation
of a Git commit or all runtime dependencies.

Adding source changes the credential authority's existing three artifact hashes.
Its wire/file contract stays compatible, but owner00 must re-pin/restage metadata
at a reviewed release boundary. Never substitute new hashes into an active
acceptance interval or claim continuity across a source/controller change.

## Completion and failures

Begin and completion update memory synchronously and enqueue bounded immutable
snapshots using the existing core. Durability is established only by a clean
fresh snapshot barrier after writes finish. Starts never wait for it. A decision
already emitted remains counted if route selection subsequently fails. Failure
before a known decision invalidates that producer's generation instead of
recording an invented healthy observation.

Pending or inflight work is UNKNOWN. Observer errors, corruption, timeout,
identity mismatch or capacity exhaustion irreversibly invalidate that producer
for the incarnation. A later successful write cannot heal it. Every initialized
sentinel and committed current-incarnation record remains required; no overwrite,
mutable current pointer or receipt eviction is introduced. Each producer retains
the existing128-invocation /256-snapshot horizon. Capacity is not a start gate.

A restart changes controller/epoch even with unchanged private files. Persisted
records are audit evidence, not permission to resume a clean historical interval.
Counts are monotonic within a coverage epoch. A baseline and final sample must
have identical controller, epoch, enrollment and source identity; otherwise the
interval is UNKNOWN. There is no automatic rebaseline, old-record cleanup or
claim of cross-restart global continuity.

## Authenticated remote barrier

The existing credential endpoint stays unchanged. The new admin-only GET is:

```text
/api/model-picker/diagnostic-evidence?producer=picker_excluded&nonce=<fresh32hex>
/api/model-picker/diagnostic-evidence?producer=picker_quota_stale&nonce=<fresh32hex>
```

Only those two producer names are valid. The response is `no-store` and uses the
existing `hub_controller_coverage_v1` identity/counter/nonce fields, or explicit
`coverage_unknown`. Machine credentials and policy-only deployment credentials
are not authorized. No new credential issuance or elevated grant is provided.

The existing `hub_alert_evidence.py` reader accepts external remote entries with
`kind: picker_diagnostic_v1`, HTTPS origin, private origin-bound operator
credential path, exact remote source ID and release SHA. It constructs the fixed
endpoint itself; no arbitrary remote path is accepted. Existing bounds, nonce
verification, redirect refusal and isolated fetch remain. It does not use cached
zeros or advance observation timestamps. Its existing local fetcher artifact
pins and the remote VM artifact pins remain distinct.

Owner-reviewed activation and installed-source/caller proof are separate from
source tests. Canary, quota and backup invocation witnesses remain outside this
slice; never fabricate their enrollments to obtain an aggregate zero.

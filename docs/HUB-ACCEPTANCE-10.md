# Phase 7 acceptance evidence

`scripts/hub-acceptance-10.sh` is a live acceptance driver, not a unit test or an
installer. Root owns the actual run and intake attachment. Do not run it until
Root has verified readiness, the exact deployment/canary/CI evidence, and the real
seven-producer alert sampler. Source validation alone proves none of those live
conditions. This change does not enroll an alert producer or supply a zero count.

## Root-validated evidence bundle

Before live acceptance, Root independently checks the actual deployment receipt,
the live canary verdict and the required CI e2e result on the same full commit.
Root writes a private JSON file with this shape:

```json
{
  "kind": "phase7_operator_validated_evidence_v1",
  "canary": "success",
  "canary_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "ci_e2e": "success",
  "ci_e2e_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operator_validation": {
    "actor": "root",
    "target_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "capture_transports": {
      "codex": {
        "engine": "codex",
        "machine_id": "hetzner-aplexer",
        "format": "codex_jsonl_v1",
        "host_release_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "provider_version": "Root-verified actual provider version",
        "profile_set_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "reference": "Root-verified effective launch and raw stream receipt",
        "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "path": "codex-capture-transport-receipt.json"
      }
    },
    "receipts": {
      "deployment": {
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "reference": "deployment receipt identifier",
        "sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        "path": "deployment-receipt.json"
      },
      "canary": {
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "reference": "live canary receipt identifier",
        "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
        "path": "canary-receipt.json"
      },
      "ci_e2e": {
        "commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "reference": "required CI e2e run URL",
        "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
        "path": "ci-e2e-receipt.json"
      }
    }
  }
}
```

The hashes and commit above are placeholders. Root supplies actual values and
nonsecret receipt bytes. Every reference, commit and SHA256 is required; `path`
is optional. When supplied, a path is absolute or relative to the evidence file,
and its bytes must match the SHA256. Files must be regular, single-link and not
symlinks. Evidence is bounded to128000 bytes and each local receipt to2MiB.
Each pre/post check validates and hashes the same bounded byte buffer, without
reopening the file for its digest. The digest must remain unchanged and is
included in the report.

These fields explicitly record Root's validation. An actor string or hash does
not authenticate a remote system, establish deployment provenance or verify a CI
result. The script does not fetch GitHub, inspect services or grant credentials.
Four unreferenced green strings are insufficient. Root must also establish that
the installed CLI and target deployment are the reviewed release; a public asset
manifest alone is not evidence of the running server.

## Session and output checks

The driver requires ten distinct Hub session identities and ten distinct canonical
lowercase native UUIDs. Every show response must name the exact requested session.
A mismatch ends the run; cleanup uses only the ID returned by this run's keyed
start, never a foreign ID from a mismatching show response.

The probe asks for a separate bounded 60-second background hold first, followed
by a separate foreground marker print tool call and an immediate provider-native
blocking monitor, `TaskOutput`, or status wait on the background hold task before
any final prose or completion bookkeeping.
JSON shape alone cannot prove a tool ran: a plaintext assistant can print a fake
JSON event. Root must attest the actual effective launch transport before the
run, using `operator_validation.capture_transports`. The map contains at most
five records, keyed only by `codex`, `zai`, `go`, `claude`, or `antigravity`. Every drawn engine needs its
own record. Codex and ZAI require `codex_jsonl_v1`; Go requires
`opencode_json_v1`. Claude requires `claude_stream_json_v1`, and Antigravity
requires `argon_stream_json_v1`. Parsers accept only the corresponding engine's completed
tool-output event. A Codex event cannot prove Go output, or the reverse.

Each record requires the exact engine, `hetzner-aplexer` machine, known format,
full host release SHA equal to `operator_validation.target_commit`, actual
provider version, `profile_set_sha256`, and receipt reference plus SHA256.
`path` is optional and, when supplied, uses the same bounded local-receipt hash
checks. The example includes Codex only; Root must supply separately verified
records for every other engine that can be selected. A missing record ends proof and
cleans up the owned session; it does not change production model routing.

Root computes `profile_set_sha256` over canonical JSON (UTF-8, sorted keys,
compact separators) describing the **nonsecret effective launch descriptor**:
engine, headless mode, stream protocol, provider identity/version, profile
identity and effective argv template with prompt placeholders. Never hash or
copy credential files, tokens, secret environment values or prompt contents.
The receipt must identify that descriptor and Root's verification that the raw
capture is the provider's structured stream rather than assistant-authored text.
Before writing this bundle, Root independently verifies actual current host
version, effective profile/argv, provider version and raw stream framing. The
script cannot measure these facts from a session show response; the records are
explicit trusted operator attestations, not remote authentication. Both the
transport map and evidence digest come from the same validated byte buffer.

Claude and Argon support is based on independently reviewed genuine, qualified
same-release Hub captures. Raw private captures are not committed. Offline
fixtures use synthetic identities, commands and paths. Provider-shaped JSON in
plain text remains insufficient without the verified effective stream transport.

For Claude, one capture must contain a complete assistant Bash `tool_use` and a
later user `tool_result` with the same tool ID and provider session. The result
must explicitly report `is_error:false` and `interrupted:false`, empty stderr,
and exactly equal structured stdout/content containing the standalone nonce.
A validated `run_in_background:true` Bash `tool_use` paired with its
`backgroundTaskId` control `tool_result` (`stdout:""` and matching `Command running in background with ID: <id>.` prefix) never proves marker output, but does not invalidate a distinct completed foreground Bash nonce pair in the same capture.
Orphan results, mismatched/reused IDs, errors, interruptions, contradictory background/foreground results, and text/input echoes
cannot prove output.

For Argon, one capture must contain complete `step_update` tool/run_command
ACTIVE and DONE frames with the same conversation, step and exact parameters.
DONE alone is insufficient. Its output must have the native wrapper's anchored
Created At / Completed At timestamps, `Command exited with code 0.` status, and
`Command output:` block containing the standalone nonce. A fake zero status
inside the output cannot replace a missing/nonzero native header. Error states,
changed parameters and reused identities fail proof.

A leading clipped line is discarded without repair; complete paired frames
must remain in the same capture. Later malformed or partial JSON fails proof.
No state is carried between capture requests to reconstruct a missing pair.
Existing Codex/ZAI JSONL and Go parsing stay unchanged. A custom ZAI bridge's new
format remains unsupported until a genuine trace and separate source review;
an evidence-file override cannot enable an unknown format.

The hold request uses a separate bounded provider-native background tool and
internal monitoring. It does not chain printing with foreground sleeps or
request an external waiter without a Hub wake. Capture/ACK limits remain
unchanged: `--max-bytes` does not make this change deliver a larger stream. A
clipped capture lacking a complete pair fails proof even if the full private
native log contains it. Do not force a model or weaken checks to make acceptance
pass. Production start/stop behavior and the150s/90s/60s limits are unchanged.

Picker evidence remains tied to each actual current draw, including its bounded
fallback history and normalized pool shares. No independent picker samples prove
a committed session. Timing limits, authenticated waited CLI stops, read-only
native absence checks and keyed lost-response cleanup remain in force.

The final alert samples must satisfy the existing durable cumulative watermark
contract, including complete resolved/deduplicated/failed-send occurrences and
the post-run cutoff. A missing or unknown authority fails acceptance. Root keeps
the final JSON/stdout plus the evidence bundle and attaches the accepted output
to the intake; the driver does not post or claim acceptance automatically.

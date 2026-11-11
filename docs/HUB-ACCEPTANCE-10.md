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
The bundle is checked before and after the run; its digest must remain unchanged
and is included in the report.

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

The probe asks for a completed print tool call followed by a separate hold call.
Only completed tool-output fields containing the decoded nonce prove output:
Codex/ZAI command-execution events, Go/OpenCode completed tool events, and Claude
structured tool-result events when available. Assistant messages, final result
prose, prompt echoes, reasoning, tool inputs, unfinished events and plain text do
not count. Unknown capture formats fail closed. Current maintained headless Claude/Antigravity command builders do not request
structured tool streams. Their tool-output transport needs separate owner
verification or correction. Do not weaken this check or force a model to make a
live acceptance attempt pass.

Picker evidence remains tied to each actual current draw, including its bounded
fallback history and normalized pool shares. No independent picker samples prove
a committed session. Timing limits, authenticated waited CLI stops, read-only
native absence checks and keyed lost-response cleanup remain in force.

The final alert samples must satisfy the existing durable cumulative watermark
contract, including complete resolved/deduplicated/failed-send occurrences and
the post-run cutoff. A missing or unknown authority fails acceptance. Root keeps
the final JSON/stdout plus the evidence bundle and attaches the accepted output
to the intake; the driver does not post or claim acceptance automatically.

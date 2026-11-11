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

The probe asks for a completed print tool call followed by a separate hold call.
JSON shape alone cannot prove a tool ran: a plaintext assistant can print a fake
JSON event. Root must attest the actual effective launch transport before the
run, using `operator_validation.capture_transports`. The map contains at most
three records, keyed only by `codex`, `zai`, or `go`. Every drawn engine needs its
own record. Codex and ZAI require `codex_jsonl_v1`; Go requires
`opencode_json_v1`. Parsers accept only the corresponding engine's completed
tool-output event. A Codex event cannot prove Go output, or the reverse.

Each record requires the exact engine, `hetzner-aplexer` machine, known format,
full host release SHA equal to `operator_validation.target_commit`, actual
provider version, `profile_set_sha256`, and receipt reference plus SHA256.
`path` is optional and, when supplied, uses the same bounded local-receipt hash
checks. The example includes Codex only; Root must supply separately verified
ZAI/Go records if those engines can be selected. A missing record ends proof and
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

Current plaintext Claude and Antigravity captures are **always refused**, even
if they contain synthetic `tool_use`, `tool_result` or command-execution JSON.
Neither engine can be added through an evidence-file override. A separately
reviewed native format-support change and explicit parser support are required.
Assistant messages, final result prose, prompt echoes, reasoning, tool inputs,
unfinished events and plain text never count. Do not force a model or weaken
this check to make live acceptance pass.

Picker evidence remains tied to each actual current draw, including its bounded
fallback history and normalized pool shares. No independent picker samples prove
a committed session. Timing limits, authenticated waited CLI stops, read-only
native absence checks and keyed lost-response cleanup remain in force.

The final alert samples must satisfy the existing durable cumulative watermark
contract, including complete resolved/deduplicated/failed-send occurrences and
the post-run cutoff. A missing or unknown authority fails acceptance. Root keeps
the final JSON/stdout plus the evidence bundle and attaches the accepted output
to the intake; the driver does not post or claim acceptance automatically.

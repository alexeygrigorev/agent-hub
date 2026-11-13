# Phase 7 acceptance evidence (V2 interactive native-pane)

`scripts/hub_acceptance_10.py` (invoked via `scripts/hub-acceptance-10.sh`) is a
live acceptance driver, not a unit test or an installer. Root owns the actual
run and intake attachment. Do not run it until Root has verified readiness, the
exact deployment/canary/CI evidence, unassisted interactive transport
attestations for every eligible engine, and the real seven-producer alert
sampler. Source validation alone proves none of those live conditions. This
change does not enroll an alert producer, supply a zero count, or grant
runtime GO.

## Root-validated interactive evidence bundle

Before live acceptance, Root independently checks the actual deployment receipt,
the live canary verdict, and the required CI e2e result on the same full commit.
Root writes a private JSON file with `kind: "phase7_native_operator_validated_evidence_v1"`:

```json
{
  "kind": "phase7_native_operator_validated_evidence_v1",
  "canary": "success",
  "canary_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "ci_e2e": "success",
  "ci_e2e_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "operator_validation": {
    "actor": "root",
    "target_commit": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "capture_transports": {
      "claude": {
        "engine": "claude",
        "machine_id": "hetzner-aplexer",
        "mode": "interactive",
        "format": "claude_native_pane_v1",
        "proof_class": "native_bash_output_plus_artifact_v2",
        "capture_command": "a capture <native_id> --screen --plain",
        "artifact_schema": "phase7_native_work_v2",
        "host_release_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "provider_version": "Root-verified actual provider version",
        "profile_set_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "reference": "Root-verified unassisted interactive capture receipt",
        "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "path": "claude-native-transport-receipt.json"
      },
      "antigravity": {
        "engine": "antigravity",
        "machine_id": "hetzner-aplexer",
        "mode": "interactive",
        "format": "hydrogen_native_pane_v1",
        "proof_class": "native_action_plus_artifact_v2",
        "capture_command": "a capture <native_id> --screen --plain",
        "artifact_schema": "phase7_native_work_v2",
        "host_release_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "provider_version": "Root-verified actual provider version",
        "profile_set_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        "reference": "Root-verified unassisted interactive capture receipt",
        "sha256": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
        "path": "hydrogen-native-transport-receipt.json"
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

Legacy `phase7_operator_validated_evidence_v1` headless stream bundles are
refused by the formal driver. Every file is bounded (`128000` bytes for the
bundle, `2 MiB` for local receipts), regular, single-link, and non-symlink;
each pre/post check validates and hashes the same bounded byte buffer.

## Keyed child intakes and interactive starts

For each of the 10 consecutive samples:

1. **Private run manifest**: persisted exclusively in `--output-dir` before any
   child intake creation (`manifest-<run_id>.json`).
2. **Unique keyed child intake**: created via `hub intake create` with
   `--text-file`, `--acceptance-criteria-file`, `--source-type forwarded`,
   `--source-reference parent:<parent>;run:<run_id>;sample:<n>`, and a stable
   `--key`. Lost responses replay the exact same key/body. Parent/child
   ownership is recorded via a keyed `hub intake append` note on the parent
   intake (never an invented `--parent` flag).
3. **Pre-start 32-byte challenge**: created exclusively in `--output-dir` after
   freezing the immutable brief/AC bytes and before `hub sessions start`. Only
   the challenge path is included in the brief, never its bytes or expected
   digest.
4. **Model-less interactive start**: invoked via
   `hub sessions start --repo alexeygrigorev/agent-hub --workspace <base> --machine hetzner-aplexer --interactive --task-kind ask --intake-task-id <child_id> --key <start_key> --timeout 150`.
   No `--prompt`, `--cwd`, `--headless`, `--with-goal`, `--engine`, `--model`,
   `--effort`, or `--purpose` override is passed.
5. **Resolved child workspace**: `requested_workspace` is bound to `<base>`,
   while `workspace` is dynamically resolved from `show`/`list`/`a list` as the
   actual child workspace.

## Native-pane proof classes and V2 work artifact

Within the 90-second capture window, the driver requires BOTH:

1. **Independent V2 work artifact** at
   `<actual_workspace>/.local/acceptance-native/<run_id>/<sample>/result.json`:
   regular, single-link, no-symlink-ancestor file `<= 8192` bytes matching
   exact canonical UTF-8 JSON bytes (`C(result_object) + b'\n'`) with no
   duplicate keys, independently verified against `git -C <actual_workspace> rev-parse --verify HEAD`,
   `git -C <actual_workspace> status --porcelain=v1 -z --untracked-files=no`,
   `calculation == 5050`, and
   `proof_digest = sha256(b"HUB_PHASE7_NATIVE_WORK_V2\x00" + challenge32 + b"\x00" + C(work_object))`.
2. **Direct native-pane capture** via `a capture <native_id> --screen --plain`:
   - **Claude (`claude_native_pane_v1`)**: completed `● Bash(...)` heading and
     output-tree block carrying `AC10_WORK_V2 <run_id> <sample> <proof_digest> <head> 5050`
     (`Waiting…`, hooks-only, and assistant prose are nonproof).
   - **Hydrogen (`hydrogen_native_pane_v1`)**: completed `● Ran (...)` action
     row plus post-action `AC10_WORK_V2 ...` output (`native_action_plus_artifact_v2`,
     explicitly distinct from typed tool stdout; `Command exited with code 0.`
     in prose is nonproof).
   - Trust menus (`Do you trust the contents of this project?`), raw headless
     JSON stream frames, prompt echoes, and unknown engines fail closed.

## Pre-STOP barrier, V2 owned-lineage cleanup, and CAS archival

Immediately before live STOP, the driver verifies a fresh running barrier on
both `a list --json --running` and `hub sessions show <sid> --json`. A natural
exit after proof but before live STOP fails the sample even if cleanup
succeeds.

Cleanup runs under a single 60-second budget:
- Issues standard `hub sessions stop <sid> --key <stop_key> --wait` even when
  `native_id` is `null`.
- Requires `state == "stopped"`, `stop_confirmed == true`, and native absence
  from `a list --json --running`.
- Scans complete paged `hub intake history <child_id>` bracketed by fresh
  `hub intake show <child_id>` snapshots. Only source-proven
  `task_continuation_started` and `task_verification_started` events prove
  automatic descendants (capped at 8 descendants within the same 60s budget).
  Descendants are recorded in `cleanup_lineage` only and never counted as
  sample draws or model diversity.
- Incomplete archives, sequence gaps, snapshot changes, pending completion
  actions, foreign session assignments, or exhausted bounds mark cleanup
  `UNKNOWN` and stop the run.
- Only after `cleanup_confirmed`, the child intake is archived with an exact
  CAS `--version <updated_at>` (never forced, never blindly retried).

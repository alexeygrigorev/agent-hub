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

1. **Private run manifest**: `--output-dir` must be a NEW directory (created
   0700 with `mkdir`, never an existing path or symlink, no `..`, canonical
   parent); the manifest is persisted exclusively there before any child intake
   creation (`manifest-<run_id>.json`). Every create/replace fsyncs the file AND
   its containing directory and re-reads the exact private bytes (custody check).
2. **Unique keyed child intake**: created via `hub intake create` with
   `--title`, `--text-file`, `--acceptance-criteria-file`, and a stable `--key`
   (lost responses replay the exact same key/body). The actual hub2 Project
   create (`hub2/src/project-do.ts` create + `viewIntake`) ignores any request
   `source` and persists `provenance == {"type":"user","code_task":true}`,
   `original_ask == {title,text}` and `parent_intake_id == null`; the driver
   sends no `--source-*` flags and requires exactly that view plus the exact
   project, title, verbatim text, normalized acceptance criteria, `open` status
   and no session. No source-reference persistence or structural parent link is
   claimed. Ownership is an explicit keyed maintained `hub intake append` note
   on the parent (`acc10-own-<run>-<n>`) binding child id, run, sample, project,
   create/start keys and the text/AC sha256 values, also kept in the private
   `create_receipt` (never an invented `--parent` flag).
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
   - **Hydrogen (`hydrogen_native_pane_v2`, proof class
     `hydrogen_expanded_native_bash_plus_artifact_v1`), model
     `gemini-4-hydrogen-medium` only.** Proof comes from ONE live styled-screen
     SVG buffer captured with `a capture <native_id> --screen --svg`, never from
     the plain viewport, a collapsed `Ran` row, or the artifact alone:
     - `scripts/hydrogen_frame.py` (reviewed recognizer V2) accepts exactly one
       styled SVG with the exact native Bash header, the per-sample command
       `python3 .local/h7_<12hex>_<NN>.py` (`12hex = sha256(C([run_id, sample]))[:12]`;
       the brief is frozen before the child id exists, so the child id is bound
       through the artifact instead), completed timestamps (strict offset and
       round trip), exit code 0 and the canonical challenge stdout. It refuses
       collapsed, plain, ANSI, footer, quoted, running, error, duplicate,
       truncated and hidden/nested SVG frames, and contradictory same-command
       or context rows.
     - `scripts/hydrogen_acquire.py` (display acquirer V2) runs inside the
       single 90-second proof window. An already expanded frame needs zero
       keys. Otherwise only the observed idle collapsed/aggregate completion
       view (real `ctrl+o` hint, empty idle composer, aggregate count 2..9)
       under the fresh owned Hub/native identity, provider and version allows
       exactly one `0x0f`. No text, Enter, startup, trust or permission assist
       is ever sent. After the key there are at most two post-key captures
       within a 10-second absolute budget (actual remaining time per call, no
       floor), and a lost send ACK is never replayed. Any other view, identity
       drift or ordinary race yields UNKNOWN with no retry.
     - `scripts/hydrogen_native.py` (native adapter, review N1 closed) uses the
       existing `a capture <uuid> --screen --svg` and `a send <uuid> --hex 0f
       --json`. Before the send it writes a durable Root-owned `0600` `O_EXCL`
       attempt fence under `<output-dir>/hydrogen-fence` (file and parent
       fsync) that binds lineage, native birth, session, epoch, attempt,
       operation, digest and artifact. There is no fallback, and a malformed
       or equal intent never mints a second fence or key.
     - `Runner.run_bounded` is used only by the Hydrogen adapter. It is a
       Popen/selector collector with a cap per stream, the remaining deadline
       and terminate/kill/reap of only its own capture child, so output is
       bounded while it is collected, not checked after return. All other
       `Runner` callers are unchanged.
     - `scripts/hydrogen_helper.py` renders the Root-pinned canonical helper
       per sample before dispatch. The artifact must match those exact pinned
       probe bytes.
     - The transport record (`HYDROGEN_TRANSPORT_KEYS`) must carry the
       Root-attested client and provider executable paths/SHA-256 values, the
       provider version and the same-commit host release SHA. A missing or
       mismatched record fails the sample before any capture or key.
     - Every OTHER antigravity model keeps the honest UNAVAILABLE gate
       (`UNKNOWN_NATIVE_FRAMING`, `NEEDS_ROOT_RENDERER_ATTESTATION`), and a
       Hydrogen record never extends to them. Draws are never resampled and no
       model is excluded from the picker.
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
- **Endpoint-specific operation identity.** Each endpoint uses its own shape
  (see `hub2/src/machine-do.ts` sessionView and claim mapping, and
  `hub2/src/project-do.ts` sagaView and the previous-attempt relation
  `${operation_id}:${attempt}`):
  - Machine `sessions show` and `stop_wait`: `operation_id` is the ATTEMPT id
    `<base-uuid>:<attempt>`. The view has no `attempt_operation_id` and no
    `attempt`.
  - Project intake `dispatch`, `final_evidence`, the nested
    `final_evidence.finish_after_stop`, and the stopped Machine view's nested
    exit `evidence`: `operation_id` is the BASE UUID and
    `attempt_operation_id` is `<base>:<attempt>`. `dispatch` also has an
    integer `attempt`.
  - The start response top level is the Project saga view. `binding_wait` is
    Machine-shaped.

  Every view is normalized into one `(session_id, base, attempt_operation_id,
  attempt, epoch, machine_id, native_id)` tuple, and raw `operation_id`
  labels are never compared across endpoints. Normalization accepts only the
  strict `<lowercase-uuid>:<attempt>` scheme. Opaque ids are refused, never
  pattern-stripped. An optional redundant Machine alias
  (`attempt_operation_id`, `attempt`, `dispatch_id`, `host`/`machine`) is not
  required, but if present it must agree; a contradiction fails. The only
  start-response difference allowed is the source-backed previous-attempt
  relation (same base and SID, attempt n-1) when a waited start falls back.
- Current custody is mandatory (missing/NULL is UNKNOWN, never equality):
  the current `session_id` must be an owned SID, `assigned_machine_id` must be
  the host, and the current `dispatch` must exist, name the current owned SID,
  be terminal, and normalize to the immutable original tuple (from the start
  show) or the proven-descendant tuple (from its pre-STOP show). The pre-STOP
  barrier and stop views may only confirm that tuple and never overwrite it.
  A NULL original native stays UNKNOWN: a late native id is checked for
  absence but never adopted.
- A normal operator STOP leaves the intake `failed` with
  `completion.reason == session_stop_confirmed` and terminal dispatch state
  `failed`. Before archiving, `final_evidence` must bind the exact owned
  base/attempt tuple, show `stopped: true` and `reason: stop_confirmed`, and
  carry a `finish_after_stop` that repeats the same tuple and intake with
  `native_exit_confirmed: true` and `evidence.containment_empty: true`. This
  is in addition to fresh native absence. There is no force or AC accept.
- Archive with exact CAS `--version <updated_at>` only after that proof
  (never forced, never active, never blindly retried). `cleanup_status` is
  `CONFIRMED` only after the archive response or a final `intake show`
  recheck proves `archived` with the same terminal owned dispatch; otherwise it
  stays `ARCHIVE_PENDING` (explicit, not CONFIRMED) or `UNKNOWN`.
- The 90-second proof window is a single deadline. Before EACH Git call
  (`rev-parse`, then `status`) the driver computes the remaining budget fresh,
  capped at 30s, and it rechecks the deadline after each call returns. The
  second call never reuses the first call's timeout, and no timeout is
  widened.

## Hydrogen: source prep vs remaining runtime and full-10 work (open)

This source adds offline-tested Hydrogen native-proof support (see
`host/tests/test_cli_hydrogen_acceptance.py`: recognizer positives and
negatives on sanitized synthetic SVG fixtures, idle-aggregate eligibility,
N1 regression, bounded-collector caps/timeout/inherited pipe, real helper
execution, and process-fake end-to-end zero-key, one-key, lost-ACK no-replay,
post-key failure, unsupported view/identity, attestation mismatch and
natural-exit-after-proof-before-live-STOP negatives). It does **not** complete
the formal ten-sample goal. These items remain open:

1. Root review of this exact source, exact full CI and head integration.
2. Root attestation of the Hydrogen transport record (client/provider
   executables, provider version) at the same commit.
3. The separate trust-menu fix: the earlier live Hydrogen preflight was
   assisted, so it earns no unassisted formal credit.
4. A Root-run formal ten-sample live qualification. Formal Hydrogen support
   and the full ten-sample acceptance criteria stay OPEN until all of these
   are done.

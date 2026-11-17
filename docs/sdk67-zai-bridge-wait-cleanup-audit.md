# Z.ai SDK bridge — post-input wait/error/cleanup boundary audit (read-only)

Read-only repo: `/home/alexey/storagebox/workspaces/fable-zai-phase-diagnostics-20261117`.
Original `2fed7c37f813736c4144ec74009f0b5cbd5df178`; diagnostic merge `58d2212af8e9171887389729f46caa2cf549420e`
(= `6a59aee5750102eedb5bf77dcea4f53645897dd9` "Add opt-in bounded ZCode bridge phase diagnostics" merged into
`2fed7c3`), reviewed tree `d7c04bdc05b1bc1e8ca024758a9c31c45596fe18`. All paths/lines below are `codex-rs/core/src/`
at the reviewed tree unless marked "original".

## Trace: bare child waits (pre-existing, unchanged by the diagnostic commit)

- `client.rs:3722-3735` (original `client.rs:3617`): startup race, stdout closed/read error before the first line →
  bare `child.wait().await`, unbounded, no signal, no timeout. Comment marks it S2: "preserve the existing unbounded
  EOF wait". The child was spawned in its own process group (`client.rs:3651`).
- `client.rs:4250-4266` (original `client.rs:4077`): stream ended on its own (EOF) → bare `child.wait().await`, even
  when a `session.error`/`turn.failed` frame already set `failed`. S1 comment (`client.rs:4257`): "`failed.is_some()`
  is deliberately NOT a teardown condition". This is the post-input wait/error/cleanup gap: a terminal error frame is
  followed by a wait that has no bound and no kill.
- Contrast, stall/consumer-abort path (`client.rs:4234-4248`, stall arm at `client.rs:3753-3770`): bounded teardown
  via `zcode_process.rs:200 dispose_and_wait_once` (TERM the process group → grace timeout → force-kill tree).
  The bare-wait paths never invoke it after a terminal frame.
- `zcode_process.rs:172 next_stream_line`: `Eof` conflates clean stdout close and read error (`Ok(None) | Err(_)`,
  and with a timeout `Ok(_)`), so "why the stream ended" is already collapsed before any diagnostic sees it.

## Trace: warm-versus-spawn selection (`client.rs:3050-3086`)

Warm runs iff `warm_bridge_enabled()` && not goal bookkeeping && `zcode_warm_spawn_failures <
zcode_warm::ZCODE_WARM_MAX_CONSECUTIVE_FAILURES` (`client.rs:3052-3058`). On warm error → `WarmFallback`
(`client.rs:3069`) → spawn-per-turn `stream_zcode_with_diagnostics` (`client.rs:3438`). Warm emits `BranchSelected`,
`ChildReused` (`client.rs:3160`) or `PermitWait`/`PermitAcquired`/`ChildSpawned` (`client.rs:3227/3239/3285`),
`WarmSessionReturned`/`WarmStreamCreated` (`client.rs:3304/3338`); spawn emits its own `BranchSelected` (3447),
permit pair (3564/3572), `ChildSpawned` (3656). Diagnostic instances are per `ModelClientSession` (one turn, shared
across warm→spawn fallback), initialized from `ZCODE_BRIDGE_DIAGNOSTICS=1` (`zcode_phase_diagnostics.rs:39-43`),
capped at 16 records/turn (`:39`, budget marker `limit_reached` `:77-82`) and 384 bytes/line (`:40`), written
best-effort to stderr with output errors ignored. Records carry only closed metadata: `source_release: "unknown"`
(`:116`), `scope: "direct_child_only"` (`:118`).

## What the new closed phase records can distinguish

- Which branch served the turn (warm / spawn / warm-then-spawn fallback) and whether the warm child was reused or
  newly spawned; whether permit acquisition happened; whether warm session/stream setup succeeded.
- That stdout closed or errored before the first frame vs after the first inner frame (`first_inner_frame`, 3865);
  which terminal frame kind arrived (`terminal_event` with frame ∈ session.error/turn.failed/result, 4144/4158).
- That a child wait was entered (`child_wait_pending`) and, if it returned Ok, that it settled (`child_wait_settled`)
  — with `seq` ordering and `elapsed_ms` per record, bounded per turn.

## What remains unobserved

- Why stdout closed (clean EOF vs read error) — conflated upstream in `next_stream_line`; no errno captured.
- The child's exit status/code/signal: records only gate `ChildWaitSettled` on `is_ok()`, never the status payload.
- Whether a bare wait ever returns: a persistent `child_wait_pending` with no `child_wait_settled` is
  indistinguishable (while the process runs) from a hang; there is no timeout or hung-wait record by design (S1/S2).
- Descendant custody: `scope: "direct_child_only"` — a successful wait reaps only the direct child; the process
  group from `client.rs:3651` can outlive it, and the bare paths contain no tree teardown, so complete child closure
  is not observable from these records.
- Child identity and release: no PID/argv/config/env; `source_release` is always `"unknown"`; stderr tail is
  captured elsewhere (`ZCODE_STDERR_TAIL_CAP`) but not correlated into records. Records are best-effort, unflushed,
  droppable (16-record budget), and exist only when `ZCODE_BRIDGE_DIAGNOSTICS=1`.
- Warm-branch failure localization: before stream creation only `warm_fallback` is emitted; which warm step failed
  is not recorded.

## Hypotheses vs historical evidence

- S1 and S2 are source hypotheses (comments at `client.rs:4257` / `:3729`) about unbounded waits, not observed facts.
- Historical canary evidence was startup-only (first line vs early exit); it cannot attribute any post-input
  wait/error/cleanup outcome and must not be cited as a historical cause. The new records extend observation into
  the stream/terminal/wait phases but still prove neither exit cause nor tree closure.

## Test run (named existing pure selector unit, synthetic TOML only)

```
cd /data/home/agent-hub-fable-root/.local/agent-branches/2026-11-17-sdk67-useful-audit
TMPDIR="$PWD/.local/tmp/sdk67-audit-b1c1925f" \
PYTHONPATH="$PWD/hub2/host:$PWD/hub2/host/tests" \
scripts/capped-test-run.sh -- python3 -m unittest \
  test_zai_diagnostic_selector.SameProfileSelectorTests.test_existing_executable_preserves_all_values_and_text_outside_literal -v
```

Result: admitted (heavy-test admission, normal ticket 8817, queue_wait 0.0s, timeout 1800s);
`Ran 1 test in 0.001s` — **OK**. Package roots used explicitly (`hub2/host` for `agent_hub_host`,
`hub2/host/tests` for the module), matching CI's `python-tests.yml` pattern; real parent HOME preserved; no Cargo,
compilation, SDK binary, ldd, network/provider calls, or personal config/profile reads.

## Concrete next engineering step

Only if an enabled real run's records show the S1 shape (`terminal_event` → `child_wait_pending` with no
`child_wait_settled` and no return): bound the S1 stream-end wait with a grace timeout that escalates to the
existing `dispose_and_wait_once` process-tree teardown (`zcode_process.rs:200`), preserving behavior in all other
branches — not a naive `failed`-flag → immediate-kill change, and gated on the diagnostic evidence first.

## Limits

Read-only audit of two immutable commits; no build, no runtime observation of the SDK, no provider calls, no
historical-cause claims, no completion/closure proof from diagnostic records. Phase records are closed-metadata and
default-off; anything above rests on source reading at the exact reviewed tree.

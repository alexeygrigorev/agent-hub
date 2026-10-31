"""Trusted Hub launch rules, separate from the caller's unchanged brief."""

AWS_MAIN_TEMP_RULE = (
    "Never use the main-temp AWS profile or static key unless this brief cites "
    "Alexey's explicit approval for this exact job; if you need AWS access that "
    "is not otherwise available, stop and report a blocker."
)
CPU_LOAD_RULE = (
    "Never generate artificial host-wide CPU or memory load on shared hosts "
    "(busy loops per nproc, stress, stress-ng, yes>/dev/null, or similar). "
    "Load-dependent flake checks must run inside this session's own cgroup "
    "with a CPU cap (for example systemd-run --user --scope -p CPUQuota=200%) "
    "and a bounded duration, and every spawned load process must be cleaned up."
)
SESSION_PREAMBLE = "Hub session rules:\n" + AWS_MAIN_TEMP_RULE + "\n" + CPU_LOAD_RULE

CAPACITY_RETRY_RULE = (
    "On a host capacity-gate refusal for a heavy step (capped-test-run exit 3 with "
    "its refusal marker, 'heavy-test slot not acquired', 'host_overload', or "
    "'no heavy-test slot free'), wait and retry with backoff instead of exiting: "
    "scripts/capped-test-retry.sh -- <command> (default 5 attempts, 30s base delay "
    "doubling to 600s max); for the full gate run "
    "scripts/capped-test-retry.sh -- scripts/test-full-local.sh --priority final. "
    "A refusal is a HOLD, not a code failure; a command's own exit 3 (e.g. pytest "
    "INTERNALERROR) is a FAIL and is never retried. Never abandon the session on a "
    "refusal; Hub re-places held offers on the next cycle once load clears."
)
CLEAN_EXIT_RULE = (
    "Never exit with an uncommitted or unrecorded fix without a Hub note and an "
    "automatically queued follow-up: run scripts/require-clean-exit.sh --intake <id>; "
    "if it holds, commit on your agent branch, record the sha as a `report` event "
    "(reference = full sha), append a note with the current state and test output via "
    "`hub intake append`, then append a `followup` event (workers: `blocker`) so Hub "
    "re-places the work, and keep the worktree while the intake is open. A clean result does NOT authorize pushing to main; push to "
    "main only with the orchestrator-main release slot."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + CAPACITY_RETRY_RULE + "\n" + CLEAN_EXIT_RULE

# Incident 504d662b (2026-10-31): a reviewer session ran
#   TMPDIR=$(mktemp -d) && HOME=$(mktemp -d) ... python3 -m pytest ...; rm -rf "$TMPDIR" "$HOME"
# `HOME=... cmd` sets HOME only for that one command, so the shell's $HOME was
# still /home/alexey and `rm -rf "$HOME"` deleted ~/.local/bin and part of
# ~/.local/state before the session died, killing 45 agent sessions.
TEMP_HOME_RULE = (
    "Disposable HOME (incident 504d662b): `HOME=$(mktemp -d) cmd` sets HOME for that "
    "one command only; your shell's $HOME is still the real user home, so a later "
    "rm -rf of $HOME deletes the whole home (it wiped ~/.local/bin and killed every "
    "agent session). NEVER rm -rf $HOME, ~, /home/*, ~/.local, ~/.local/bin, ~/.local/lib, "
    "~/prod, ~/git or a main checkout, and never rm -rf a variable you did not assign "
    "yourself in the same shell. For a disposable HOME/TMPDIR run "
    "scripts/with-temp-home.sh -- <command>; it creates, uses and removes only its own "
    "temporary directory."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + TEMP_HOME_RULE

# Worktree-guard footer (intake a0f01448, RCA 95c1862c): the 95c1862c session
# removed its own git worktree while its intake was still open, and the stale
# " (deleted)" label then hid the recreated tree. Hub locks every session
# worktree on start/adopt, so NEVER remove your worktree yourself — not with
# `git worktree remove`, `git worktree prune`, `rm -rf`, or branch deletes.
# Remove the worktree only after the linked intake has a DONE note with live
# proof of every AC; while blocked or waiting, keep it.
WORKTREE_CLEANUP_FOOTER = (
    "Worktree cleanup (incident 95c1862c): NEVER remove your git worktree "
    "yourself (`git worktree remove`/`prune`, `rm -rf`, branch deletes) — Hub "
    "locks it on start/adopt and the lock refuses plain removal while the "
    "session is live or its intake is open. Remove the worktree only after "
    "the linked intake has a DONE note with live proof of every AC; while "
    "blocked or waiting, keep it."
)


def compose_prompt(brief: str) -> str:
    """Prefix trusted rules without interpreting or rewriting the brief."""
    return SESSION_PREAMBLE + "\n\n" + brief + "\n\n" + WORKTREE_CLEANUP_FOOTER

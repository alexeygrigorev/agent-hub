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

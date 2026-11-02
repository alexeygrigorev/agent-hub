"""Trusted Hub launch rules, separate from the caller's unchanged brief."""

import hashlib
import re
from pathlib import Path

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
    "doubling to 600s max). "
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
CI_ONLY_TESTS_RULE = (
    "Tests run in CI (Alexey t2558u, intake 178ec5bb): locally run ONLY targeted unit "
    "tests for the files you changed. Never run a full suite locally (test-full-local.sh, "
    "make test/test-all, manage.py test without labels, pytest playwright_tests). "
    "Flow: worktree from origin/main, push your branch, open a PR, CI runs the full "
    "suite, then gh pr merge --auto --rebase. This supersedes any push-to-main or "
    "full-local-suite instruction below."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + CAPACITY_RETRY_RULE + "\n" + CLEAN_EXIT_RULE + "\n" + CI_ONLY_TESTS_RULE

PREPUSH_TEST_RULE = (
    "Hub worker releases: after obtaining the orchestrator-main release slot, "
    "use `scripts/push-main.sh` from the Agent Hub checkout to push to main. "
    "The helper installs the same pre-push hook that Hub installs before worker "
    "launch/adoption; plain git pushes to main also run the gate. "
    "It runs affected tests against the remote's advertised main SHA (full tests for "
    "BROAD changes), refuses failures before push, and pushes only the tested "
    "commit from a clean tree. If main advances, integrate and rerun the helper. "
    "Never disable the hook or use --no-verify; post-push CI is not pre-push evidence."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + PREPUSH_TEST_RULE

SCOPE_TRAILER_RULE = (
    "Scope boundary: every commit that changes session control, registry, auth, or public files "
    "must end with `Scope: in` or `Scope: gray <intake-UUID>`. Hub installs a commit-msg hook "
    "in Agent Hub session worktrees and the primary checkout; it adds `Scope: in` when absent, "
    "preserves a valid gray intake trailer, and also runs for merge commits. Never use `--no-verify`. "
    "If an Agent Hub aplexer workspace was started outside Hub host bootstrap, install it before "
    "committing with the host's Python, for example `python3 host/agent_hub_host/prepush.py install-scope` "
    "on Linux or `python host/agent_hub_host/prepush.py install-scope` on Windows."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + SCOPE_TRAILER_RULE

HEAVY_TEST_ADMISSION_UPGRADE_RULE = (
    "Before a heavy-test gate, check whether the worktree contains the fixed "
    "admission revision with `git merge-base --is-ancestor "
    "d8740aaca3944a295680177a40148bb00accea78 HEAD`. If the check fails, "
    "honor the HOLD from `scripts/test-affected-local.sh`; do not bypass it by "
    "using an older wrapper or nesting another admission. Any fleet-wide "
    "worktree rollout is opt-in and dry-run first, and skips dirty, unpushed, "
    "locked, or live worktrees. For your own older worktree, preserve your "
    "changes and ask the Hub head to coordinate a normal, non-forced rebase "
    "after a clean checkpoint; never reset, force-rebase, or edit another "
    "session's worktree. Recheck the revision before gating, or report a HOLD "
    "and continue with non-heavy work."
)
SESSION_PREAMBLE = SESSION_PREAMBLE + "\n" + HEAVY_TEST_ADMISSION_UPGRADE_RULE

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


# Claude Code rejects a /goal condition longer than 4000 characters. Apply
# this known cap to every goal-capable route as a conservative shared bound.
GOAL_CHAR_LIMIT = 4000
CONTAINER_HYGIENE_RULE = (
    "Container hygiene: use docker run --rm for throwaway/test containers; label every "
    "test container agent.session=<your Hub session id, or native session id registered "
    "in Hub>. Install trap/finally cleanup before creation and clean up all resources you "
    "create before session exit, including failed starts and docker create/compose tests. "
    "Preserve other sessions and Coolify containers."
)
# Short goal sent instead of an over-limit brief. The full text is in the brief file.
GOAL_BRIEF_POINTER = (
    "Hub session rules and the full brief are in {path}. Read that file completely before "
    "any other work and follow it exactly: it holds the session rules, the acceptance "
    "criteria and the worktree cleanup rules."
)
# Engine rejection text, anchored to a complete line and optional known CLI
# error markers so ordinary discussion of the limit cannot mark a session failed.
GOAL_REJECTED_RE = re.compile(
    r'(?m)^[ \t]*(?:(?:Error:|[✕✗✘×❌])[ \t]*)?'
    r'Goal condition is limited to \d+ characters(?: \(got \d+\))?[ \t]*$'
)


def goal_brief_dir():
    return Path.home() / ".local" / "state" / "agent-hub" / "goal-briefs"


def _goal_char_count(text: str) -> int:
    """Count UTF-16 code units, which is at least as strict as Python chars."""
    return len(text.encode("utf-16-le")) // 2


def launch_goal(wt_note: str, brief: str, offer_id: str, *, brief_dir=None) -> str:
    """Return the `/goal` argument for a start. The full brief goes inline when it fits
    the engine limit; otherwise it is written to a private file and the goal points at it.
    Never truncates the brief."""
    from . import private_fs
    full = compose_prompt(brief) + "\n" + CONTAINER_HYGIENE_RULE
    goal = "/goal " + wt_note + full
    if _goal_char_count(goal) <= GOAL_CHAR_LIMIT:
        return goal
    name = hashlib.sha256(str(offer_id).encode()).hexdigest()[:32] + ".md"
    path = Path(brief_dir or goal_brief_dir()) / name
    private_fs.atomic_write(path, full.encode(), 512 * 1024)
    goal = "/goal " + wt_note + GOAL_BRIEF_POINTER.format(path=path)
    if _goal_char_count(goal) > GOAL_CHAR_LIMIT:
        raise ValueError("goal_too_long: goal exceeds the engine limit even as a pointer")
    return goal

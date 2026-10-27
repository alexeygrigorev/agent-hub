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


def compose_prompt(brief: str) -> str:
    """Prefix trusted rules without interpreting or rewriting the brief."""
    return SESSION_PREAMBLE + "\n\n" + brief

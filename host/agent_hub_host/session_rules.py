"""Trusted Hub launch rules, separate from the caller's unchanged brief."""

AWS_MAIN_TEMP_RULE = (
    "Never use the main-temp AWS profile or static key unless this brief cites "
    "Alexey's explicit approval for this exact job; if you need AWS access that "
    "is not otherwise available, stop and report a blocker."
)
SESSION_PREAMBLE = "Hub session rules:\n" + AWS_MAIN_TEMP_RULE


def compose_prompt(brief: str) -> str:
    """Prefix trusted rules without interpreting or rewriting the brief."""
    return SESSION_PREAMBLE + "\n\n" + brief

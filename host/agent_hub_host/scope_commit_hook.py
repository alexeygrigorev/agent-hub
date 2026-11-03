"""Commit-message trailer helper installed for Hub sessions on all host OSes."""
from pathlib import Path
import re
import sys

UUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
SCOPE = re.compile(r"Scope: (?:in|gray " + UUID + r")")


def add_scope_trailer(message_path):
    """Ensure the final commit-message paragraph contains one valid Scope."""
    path = Path(message_path)
    raw = path.read_bytes()
    message = raw.decode("utf-8", "surrogateescape")
    if not message.strip():
        raise ValueError("commit message is empty")

    scopes = set()
    for line in message.splitlines():
        candidate = line.strip()
        if candidate.startswith("Scope:"):
            if not SCOPE.fullmatch(candidate):
                raise ValueError("Scope trailer must be 'Scope: in' or 'Scope: gray <intake-UUID>'")
            scopes.add(candidate)
    if len(scopes) > 1:
        raise ValueError("commit message contains conflicting Scope trailers")
    scope = next(iter(scopes), "Scope: in")

    normalized = message.replace("\r\n", "\n").rstrip("\n")
    footer = normalized.split("\n\n")[-1]
    if any(line.strip() == scope for line in footer.splitlines()):
        return False

    newline = "\r\n" if "\r\n" in message else "\n"
    body = message.rstrip("\r\n")
    amended = body + (newline * 2 if body else "") + scope + newline
    path.write_bytes(amended.encode("utf-8", "surrogateescape"))
    return True


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("scope hook: expected commit message file", file=sys.stderr)
        return 2
    try:
        add_scope_trailer(argv[0])
    except (OSError, ValueError):
        print("scope hook: refused malformed or empty commit message", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

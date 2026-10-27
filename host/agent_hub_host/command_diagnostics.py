"""Bounded command identity and output; never publish argv or raw exceptions."""
import re

TAIL_LIMIT = 512
WINDOW_LIMIT = 8 * 1024 * 1024  # Matches run_json's largest bounded capture.


def output_tail(value):
    if isinstance(value, bytes):
        value = value.decode('utf-8', 'replace')
    if not isinstance(value, str):
        return ''
    # Withhold oversized windows rather than cutting into a multiline secret
    # before redaction. Process capture has its own independent hard bounds.
    if len(value) > WINDOW_LIMIT:
        return '[output exceeds diagnostic window]'
    # A traceback can echo arbitrary source, locals and private filesystem paths.
    # Token redaction cannot make those safe; the structured error classification
    # still retains the recognized exception type and safe failure summary.
    if 'Traceback (most recent call last):' in value:
        return '[traceback output withheld]'
    try:
        from .log_redaction import redact_log
        value = re.sub(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)',
                       '[redacted private key]', value, flags=re.S)
        value = redact_log(value, max_chars=WINDOW_LIMIT)
        value = re.sub(r'sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9_]{16,}|github_pat_[A-Za-z0-9_]{16,}|xox[baprs]-[A-Za-z0-9-]{8,}',
                       '[redacted]', value)
    except Exception:
        return '[output unavailable]'
    value = ' | '.join(value.splitlines())
    value = ''.join(c if 32 <= ord(c) < 127 else ' ' for c in value)
    return value[-TAIL_LIMIT:]


def command_details(command, stdout='', stderr=''):
    # Only the executable basename crosses this boundary, never flags, prompt,
    # working directory, payload or account configuration.
    name = str(command or 'unknown').replace('\\', '/').rsplit('/', 1)[-1]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.+-]{0,63}', name) or output_tail(name) != name:
        name = 'redacted'
    return {'command': name, 'stdout_tail': output_tail(stdout),
            'stderr_tail': output_tail(stderr)}

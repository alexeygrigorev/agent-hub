"""Bounded command identity and output; never publish argv or raw exceptions."""
import re

TAIL_LIMIT = 512
WINDOW_LIMIT = 8 * 1024 * 1024  # Matches run_json's largest bounded capture.


_FINAL_EXCEPTION = re.compile(
    r'[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception|Exit|Timeout|Interrupt|Locked): [^\r\n]{1,200}')
_PATH = re.compile(r'(?:[A-Za-z]:)?[\\/][^\s\'"]+')


def _traceback_summary(value):
    """Withhold the traceback body but keep its final `ExcType: message` line.

    The body can echo source, locals and private paths. The last line names the
    failure (for example `OperationalError: database is locked`), so it is
    redacted, stripped of filesystem paths and bounded."""
    marker = '[traceback output withheld]'
    lines = [ln.strip() for ln in value.splitlines() if ln.strip()]
    last = lines[-1] if lines else ''
    if not _FINAL_EXCEPTION.fullmatch(last):
        return marker
    try:
        from .log_redaction import redact_log
        last = redact_log(last, max_chars=400)
    except Exception:
        return marker
    last = _PATH.sub('<path>', last)
    last = ''.join(c if 32 <= ord(c) < 127 else ' ' for c in last)
    return marker + ' ' + last[:240]


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
        return _traceback_summary(value)
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

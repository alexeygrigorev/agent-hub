"""Bounded launch diagnostics: never persist arbitrary process output or argv."""
from __future__ import annotations
import re

# Match complete known launcher refusal lines, including traceback final lines.
# Messages are constants: suffixes, payloads, paths and credentials never survive.
REFUSALS = {
    'host MemAvailable < 10GiB': ('launcher_memory_reserve', 'Host memory headroom after launcher reservations is below 10 GiB.'),
    'requested worker memory > 1500MiB': ('launcher_worker_memory', 'Requested worker memory exceeds 1500 MiB.'),
    'No valid routes available': ('launcher_admission_refused', 'Fresh launcher admission found no valid provider route.'),
    'Configured Codex launch wrapper unavailable': ('launcher_codex_wrapper_unavailable', 'The configured protected Codex wrapper is unavailable.'),
    'Maintained launcher plan refused': ('launcher_plan_refused', 'The maintained launcher plan refused the selected route.'),
    'Native Codex receipt mismatch': ('launcher_receipt_mismatch', 'The native Codex receipt did not match the launch.'),
    'Native lifecycle receipt is incomplete': ('launcher_receipt_incomplete', 'The native lifecycle receipt is incomplete.'),
}


def diagnostic(raw='', *, error_type='RuntimeError', exit_code=None, stage='launcher_run'):
    reason, message = 'launcher_command_failed', 'The launcher command failed; unrecognized output was withheld.'
    # Only complete known messages may be copied across the privacy boundary.
    for line in str(raw)[-16384:].splitlines():
        typed = re.match(r'^(ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', line.strip())
        if typed:
            error_type = typed.group(1)
        candidate = re.sub(r'^(?:ValueError|RuntimeError|PermissionError|OSError|FileNotFoundError|TimeoutError|JSONDecodeError): ', '', line.strip())
        if candidate in REFUSALS:
            reason, message = REFUSALS[candidate]
        elif candidate == 'Unsupported provider: codex (genuine protected wrapper scripts/launch-codex.sh not configured; raw routing denied)':
            reason, message = 'launcher_codex_wrapper_unavailable', 'The protected Codex wrapper is not configured in the launcher environment.'
    if error_type not in {'ValueError', 'RuntimeError', 'OSError', 'PermissionError', 'FileNotFoundError', 'TimeoutError', 'JSONDecodeError'}:
        error_type = 'RuntimeError'
    value = {'reason': reason, 'message': message, 'error_type': error_type,
             'stage': stage if stage in {'launcher_run', 'dispatch_headless', 'dispatch_interactive', 'dispatch_route'} else 'launcher_run'}
    if type(exit_code) is int:
        value['exit_code'] = exit_code
    return value


class LocalCommandError(RuntimeError):
    def __init__(self, raw='', *, exit_code=None):
        self.diagnostic = diagnostic(raw, exit_code=exit_code)
        super().__init__(self.diagnostic['message'])


def exception_diagnostic(exc, stage):
    existing = getattr(exc, 'diagnostic', None)
    if isinstance(existing, dict):
        return {**existing, 'stage': stage}
    return diagnostic(str(exc), error_type=type(exc).__name__, stage=stage)

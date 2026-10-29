"""Render aplexer hooks for the shells used by installed agent engines."""
import base64
import shlex
import sys


def hook_command(executable, *arguments, quiet=False):
    """Invoke exactly one executable and return success even if its hook fails.

    Windows engines use cmd, Git Bash or PowerShell. An encoded PowerShell
    command avoids passing a user profile path through those incompatible
    quoting rules. PowerShell is already part of supported Windows hosts.
    """
    words = [str(executable), *(str(value) for value in arguments)]
    if any('\0' in word or '\n' in word or '\r' in word for word in words):
        raise ValueError('Invalid hook argument')
    if sys.platform != 'win32':
        return shlex.join(words) + (' 2>/dev/null' if quiet else '') + ' || true'

    quote = lambda value: "'" + value.replace("'", "''") + "'"
    invoke = '& ' + ' '.join(quote(word) for word in words)
    if quiet:
        invoke += ' 2>$null'
    script = "$ErrorActionPreference='Stop'; try { " + invoke + ' } catch {}; exit 0'
    encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
    return 'powershell.exe -NoProfile -NonInteractive -EncodedCommand ' + encoded

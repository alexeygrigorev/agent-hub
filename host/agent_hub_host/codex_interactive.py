"""Keep managed interactive Codex execution inside its own workload scope."""


def isolated_argv(provider, argv):
    """Global flags precede the prompt; exec and diagnostic invocations stay intact.

    A shared daemon inherits the first caller's cgroup. Killing that workload
    otherwise kills the app-server used by unrelated interactive sessions.
    """
    args = list(argv)
    if provider != 'codex' or any(x in args for x in ('exec', 'review', 'app-server',
                                                      '--help', '-h', '--version', '-V')):
        return args
    if '--no-daemon' not in args:
        args.insert(0, '--no-daemon')
    return args


def reconnect_failure(screen):
    """Require disconnected TUI chrome, not error strings in a report/draft."""
    import re
    if not isinstance(screen, str):
        return False
    screen = screen.replace('\r', '')
    footer = re.search(r'(?m)^[ \t]*› Ask Codex to do anything[ \t]*\n(?:[ \t]*\n)*'
                       r'[ \t]*(?:GPT-|gpt-)[^\n]*\n[ \t]*\^c quit[ \t]*\s*\Z', screen)
    if not footer:
        return False
    status = screen[:footer.start()]
    return bool(re.search(r'(?m)^[ \t]*■[ \t]+Automatic\s+reconnect\s+could\s+not\s+restore\s+this\s+session\.', status)
                and re.search(r'(?m)^[ \t]*■[ \t]+Server\s+connection\s+could\s+not\s+be\s+restored', status)
                and re.search(r'(?m)^[ \t]*•[ \t]+Reconnect\s+failed\s+[—-]\s+check\s+the\s+endpoint,\s+then\s+relaunch[^\n]*\n'
                              r'(?:[ \t]*(?:└[^\n]*)?\n)*[ \t]*\Z', status))


def current_screen(executable, row):
    """Read one UUID-addressed screen, bounded in time and retained size."""
    import re
    import subprocess
    sid = row.get('id')
    if not isinstance(sid, str) or not re.fullmatch(r'[0-9a-fA-F-]{36}', sid):
        return None
    try:
        result = subprocess.run([executable, 'capture', sid, '--screen', '--plain'],
                                capture_output=True, text=True, timeout=.5)
        if result.returncode == 0 and len(result.stdout.encode('utf-8')) <= 32768:
            return result.stdout
    except (OSError, subprocess.SubprocessError, UnicodeError):
        pass
    return None

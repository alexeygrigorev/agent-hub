#!/usr/bin/env python3
"""Bounded retry for wrangler commands around transient Cloudflare API failures.

The CI/CD Deploy workflow's wrangler deploy steps used to fail the whole run on
a single transient Cloudflare API blip (error 10013 on 2026-10-27, run
40494634371 attempt #3; attempt #4 deployed the same SHA unchanged). This
wrapper runs the wrapped command with live streaming output and retries at most
MAX_ATTEMPTS times, but only when the captured output matches a known-transient
pattern. Authentication, configuration and build failures do not match and fail
immediately on the first attempt, as does any unrecognized output.

Usage: python3 scripts/ci/wrangler_retry.py <command> [args...]
"""

import re
import subprocess
import sys
import time

MAX_ATTEMPTS = 3
# Sleep between attempt 1->2 and 2->3; bounded worst case is 30s of backoff.
RETRY_SLEEP_SECONDS = (10, 20)

# Checked before transient patterns: a fatal marker ends the run immediately
# even when a transient pattern also matches somewhere in the output.
FATAL_PATTERNS = (
    ('authentication', r'\[code: 10000\]|authentication error|invalid api token|not authorized|unauthorized logins? are not allowed|you must be logged in'),
    ('config', r'could not (?:read|find|parse) config|invalid config|config validation failed|unrecognized setting'),
    ('build', r'build failed|no entry-?point (?:found|specified)|module not found|\[ERROR\] Could not resolve'),
)

TRANSIENT_PATTERNS = (
    ('code 10013', r'\[code: 10013\]'),
    ('unknown api error', r'an unknown error has occurred'),
    ('api 5xx', r'\b(?:status(?:\s+code)?|http)\s*[:=]?\s*5\d\d\b|internal server error|bad gateway|service unavailable|gateway time-?out'),
    ('rate limited', r'\[code: 429\]|too many requests|rate limit(?:ed)? \('),
    ('network', r'\b(?:econnreset|etimedout|epipe|econnaborted|econnrefused|socket hang up|network connection lost|other side closed|fetch failed|tunnel connection failed)\b|connection (?:reset|terminated|refused|timed out)'),
)


def classify_failure(text):
    """Classify captured command output as ('fatal'|'transient', reason) or (None, None)."""
    for kind, patterns in (('fatal', FATAL_PATTERNS), ('transient', TRANSIENT_PATTERNS)):
        for reason, pattern in patterns:
            if re.search(pattern, text, re.IGNORECASE):
                return kind, reason
    return None, None


def error_code(text):
    """Return the first '[code: NNNN]' marker in the output, or ''."""
    match = re.search(r'\[code:\s*(\d+)\]', text)
    return match.group(1) if match else ''


def run_once(argv):
    """Run argv, stream its combined output live, return (returncode, output)."""
    process = subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, errors='replace', bufsize=1,
    )
    chunks = []
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        chunks.append(line)
    process.stdout.close()
    return process.wait(), ''.join(chunks)


def run_with_retries(argv, max_attempts=MAX_ATTEMPTS, sleeps=RETRY_SLEEP_SECONDS,
                     runner=run_once, clock=time.sleep, log=sys.stderr):
    """Run argv with bounded retries; return (exit_code, attempts_used, kind)."""
    for attempt in range(1, max_attempts + 1):
        code, output = runner(argv)
        if code == 0:
            return 0, attempt, None
        kind, reason = classify_failure(output)
        if kind != 'transient':
            if kind == 'fatal':
                print(f'wrangler_retry: non-transient failure ({reason}) on attempt '
                      f'{attempt}/{max_attempts}; failing immediately', file=log)
            return code, attempt, kind
        if attempt == max_attempts:
            print(f'wrangler_retry: transient failure ({reason}) persisted through '
                  f'{attempt}/{max_attempts} attempts; giving up', file=log)
            return code, attempt, kind
        delay = sleeps[attempt - 1] if attempt - 1 < len(sleeps) else sleeps[-1]
        marker = error_code(output)
        detail = f', code={marker}' if marker and marker not in reason else ''
        print(f'wrangler_retry: attempt {attempt}/{max_attempts} hit transient '
              f'error ({reason}{detail}); retrying in {delay}s', file=log)
        clock(delay)
    raise AssertionError('unreachable')


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print('usage: python3 scripts/ci/wrangler_retry.py <command> [args...]', file=sys.stderr)
        return 2
    code, attempts, kind = run_with_retries(argv)
    if code != 0 and kind != 'transient':
        print(f'wrangler_retry: command failed on attempt {attempts} '
              f'(no bounded retry: {kind or "unrecognized"} failure)', file=sys.stderr)
    return code


if __name__ == '__main__':
    sys.exit(main())

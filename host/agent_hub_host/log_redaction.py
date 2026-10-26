"""Full-window privacy filtering shared by terminal and diagnostic reports."""
import re

from .workclaim import redact_for_bus

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def redact_log(text):
    if not isinstance(text, str) or len(text) > 65536:
        return ''
    # JSON permits lone surrogates, while the private journal uses UTF-8.
    text = ''.join('\ufffd' if 0xd800 <= ord(c) <= 0xdfff else c for c in text)
    text = ANSI.sub('', text).replace('\x00', '')
    text = re.sub(r'(?i)\bbearer\s+[^\s\"\x27,;]+', 'Bearer [redacted]', text)
    text = re.sub(r'(?im)((?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*', r'\1[redacted]', text)
    text = re.sub(
        r'''(?ix)(["']?\b(?:password|passwd|secret|token|access_token|refresh_token|api[_-]?key|authorization|cookie|set-cookie|aws_secret_access_key)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\s,;]+)''',
        r'\1[redacted]', text)
    return redact_for_bus(text)

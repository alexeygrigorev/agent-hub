"""Full-window privacy filtering shared by terminal and diagnostic reports."""
import re

from .workclaim import redact_for_bus

ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))')


def redact_log(text, *, max_chars=65536):
    if (not isinstance(text, str) or type(max_chars) is not int
            or not 1 <= max_chars <= 8 * 1024 * 1024 or len(text) > max_chars):
        return ''
    # JSON permits lone surrogates, while the private journal uses UTF-8.
    text = ''.join('\ufffd' if 0xd800 <= ord(c) <= 0xdfff else c for c in text)
    text = ANSI.sub('', text).replace('\x00', '')
    # Codex and other providers use JWTs in diagnostics. They can also be
    # truncated before a field name is visible, so remove recognizable JWT
    # headers/prefixes before the general high-entropy pass.
    text = re.sub(r'\beyJ[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*){0,2}', '[redacted]', text)
    text = re.sub(r'(?i)\bbearer\s+[^\s\"\x27,;]+', 'Bearer [redacted]', text)
    text = re.sub(
        r'''(?ix)(["']?\b(?:password|passwd|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|auth[_-]?token|oauth[_-]?(?:access|refresh)?[_-]?token|client[_-]?secret|api[_-]?key|authorization|cookie|set-cookie|aws_secret_access_key)["']?\s*[:=]\s*)(?:"(?:\\.|[^"\\])*(?:"|$)|'(?:\\.|[^'\\])*(?:'|$)|[^\s,;]+)''',
        r'\1[redacted]', text)
    # Preserve quoted multiline context until its complete value is removed.
    text = re.sub(r'(?im)((?:authorization|cookie|set-cookie)\s*:\s*)[^\r\n]*', r'\1[redacted]', text)
    return redact_for_bus(text)

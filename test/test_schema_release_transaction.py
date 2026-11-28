"""Compatibility entry for the retained deployment transaction fixture body.

Execute in this module's globals to preserve existing imported patch points and
unittest module identity. The body owns the sole unittest.main invocation.
"""
from pathlib import Path as _Path
_body_path = _Path(__file__).resolve().parents[1] / 'scripts/tests/test_schema_release_transaction.py'
_facade_file = __file__
try:
    __file__ = str(_body_path)
    exec(compile(_body_path.read_bytes(), __file__, 'exec'), globals())
finally:
    __file__ = _facade_file

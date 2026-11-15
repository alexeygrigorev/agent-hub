"""Compatibility import/direct entry for the retained rollback-preflight fixture.

Shared globals retain original patch points. Original direct execution defines
fixtures only: no unittest.main is added or invoked by this facade.
"""
from pathlib import Path as _Path
_body_path = _Path(__file__).resolve().parents[1] / 'scripts/tests/test_schema_rollback_preflight.py'
_facade_file = __file__
try:
    __file__ = str(_body_path)
    exec(compile(_body_path.read_bytes(), __file__, 'exec'), globals())
finally:
    __file__ = _facade_file

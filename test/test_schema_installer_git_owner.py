"""Compatibility entry for the maintained schema fixture body.

Execute in this module's globals, preserving original imported patch points,
file identity and the body's sole original unittest.main call.
"""
from pathlib import Path as _Path
_body_path = _Path(__file__).resolve().parents[1] / 'scripts/tests/test_schema_installer_git_owner.py'
_facade_file = __file__
try:
    __file__ = str(_body_path)
    exec(compile(_body_path.read_bytes(), __file__, 'exec'), globals())
finally:
    __file__ = _facade_file

#!/usr/bin/env python3
"""Discover actual argparse CLI leaves without performing commands or auth."""
import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'host'))

def commands():
    from agent_hub_host import cli
    class Captured(Exception):
        pass
    captured = []
    def capture(parser, *args, **kwargs):
        captured.append(parser)
        raise Captured()
    with patch.object(argparse.ArgumentParser, 'parse_args', capture), contextlib.redirect_stdout(io.StringIO()):
        try:
            cli.main()
        except Captured:
            pass
    def walk(parser, prefix=''):
        subs = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
        if not subs:
            return [prefix]
        result = [prefix] if prefix and any(not s.required for s in subs) else []
        for sub in subs:
            for name, child in sub.choices.items():
                result.extend(walk(child, (prefix + ' ' + name).strip()))
        return result
    return sorted(walk(captured[0]))

if __name__ == '__main__':
    print(json.dumps(commands(), indent=2))

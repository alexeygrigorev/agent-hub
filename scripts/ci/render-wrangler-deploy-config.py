#!/usr/bin/env python3
"""Render a temporary production config with one explicitly selected Hub mode."""

import argparse
from pathlib import Path
import re
import sys


def render(config: Path, output: Path, mode: str) -> None:
    if mode not in {"bootstrap", "active"}:
        raise ValueError("release mode must be bootstrap or active")
    source = config.read_text(encoding="utf-8")
    pattern = re.compile(r'(?m)^(\s*"HUB2_RELEASE_MODE"\s*:\s*)"[^"]*"')
    if len(pattern.findall(source)) != 1:
        raise ValueError("expected exactly one HUB2_RELEASE_MODE in production config")
    rendered = pattern.sub(lambda match: match.group(1) + f'"{mode}"', source, count=1)
    with output.open("x", encoding="utf-8") as stream:
        stream.write(rendered)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("wrangler.jsonc"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--mode", choices=("bootstrap", "active"), required=True)
    args = parser.parse_args()
    try:
        render(args.config, args.output, args.mode)
    except (OSError, ValueError) as exc:
        print(f"render-wrangler-deploy-config: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

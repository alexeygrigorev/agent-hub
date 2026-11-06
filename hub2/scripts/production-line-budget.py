#!/usr/bin/env python3
"""Report Hub 2 product SLOC, physical lines, tests, and verification tooling."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HUB2 = ROOT / "hub2"
REQUIRED_HOST_FILES = (
    HUB2 / "host" / "agent_hub_host" / "agent.py",
    HUB2 / "host" / "agent_hub_host" / "transport.py",
    HUB2 / "host" / "bootstrap.py",
    HUB2 / "host" / "pyproject.toml",
)
TEXT_SUFFIXES = {
    ".cjs", ".css", ".html", ".js", ".json", ".jsonc", ".mjs", ".py",
    ".sh", ".sql", ".svg", ".toml", ".ts", ".tsx", ".yaml", ".yml",
}
GENERATED_PARTS = {"__pycache__", "build", "dist", "node_modules"}


def is_generated(path: Path) -> bool:
    parts = path.relative_to(ROOT).parts
    return any(part in GENERATED_PARTS or part.endswith(".egg-info") for part in parts)


def text_files(path: Path) -> set[Path]:
    if not path.exists():
        return set()
    return {
        file for file in path.rglob("*")
        if file.is_file() and file.suffix.lower() in TEXT_SUFFIXES
        and not is_generated(file)
        and not any(part.startswith(".") for part in file.relative_to(ROOT).parts)
    }


def production_files() -> set[Path]:
    files = text_files(HUB2 / "src") | text_files(HUB2 / "host" / "agent_hub_host") | text_files(HUB2 / "public")
    # Production Wrangler entry keeps first activation in maintenance mode.
    production_worker = HUB2 / "production-worker.ts"
    if production_worker.is_file():
        files.add(production_worker)
    package = HUB2 / "package.json"
    if package.is_file():
        files.add(package)
    package_lock = HUB2 / "package-lock.json"
    if package_lock.is_file():
        files.add(package_lock)
    package = HUB2 / "host" / "pyproject.toml"
    if package.is_file():
        files.add(package)
    bootstrap = HUB2 / "host" / "bootstrap.py"
    if bootstrap.is_file():
        files.add(bootstrap)
    # These scripts/workflows create or reconcile real production billing
    # policies, so classify them as operational product code, not test tooling.
    for path in (
        HUB2 / "scripts" / "cloudflare-billing-policy.py",
        ROOT / ".github" / "workflows" / "hub2-billing-audit.yml",
    ):
        if path.is_file():
            files.add(path)
    for path in (HUB2 / "wrangler.jsonc", HUB2 / "wrangler.production.jsonc"):
        if path.is_file():
            files.add(path)
    files.update(path for path in (HUB2 / "scripts").glob("deploy*") if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES)
    files.update(path for path in (HUB2 / "scripts").glob("registry-import*") if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES)
    return files


def verification_files() -> set[Path]:
    files = text_files(HUB2 / "scripts")
    files.difference_update(
        HUB2 / "scripts" / name
        for name in ("cloudflare-billing-policy.py",)
    )
    files.update(
        file for file in (
            HUB2 / "wrangler.dev.jsonc",
            ROOT / ".github" / "workflows" / "hub2.yml",
        ) if file.is_file()
    )
    return files


def test_files() -> set[Path]:
    return text_files(HUB2 / "tests") | text_files(HUB2 / "host" / "tests")


def source_loc(path: Path) -> tuple[int, int]:
    """Return (SLOC, physical lines); do not rewrite or merge source lines."""
    suffix = path.suffix.lower()
    lines = path.read_text(encoding="utf-8").splitlines()
    count = 0
    in_block = False
    block_open, block_close = ("<!--", "-->") if suffix in {".html", ".svg"} else ("/*", "*/")
    hash_comments = suffix in {".py", ".sh", ".toml", ".yaml", ".yml"}
    slash_comments = suffix in {".cjs", ".css", ".js", ".jsonc", ".mjs", ".ts", ".tsx"}

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        if in_block:
            if block_close not in line:
                continue
            _, line = line.split(block_close, 1)
            line = line.strip()
            in_block = False
            if not line:
                continue
        if hash_comments and line.startswith("#"):
            continue
        if slash_comments and line.startswith("//"):
            continue
        if line.startswith(block_open):
            remainder = line[len(block_open):]
            if block_close not in remainder:
                in_block = True
                continue
            _, line = remainder.split(block_close, 1)
            line = line.strip()
            if not line:
                continue
        count += 1
    return count, len(lines)


def describe(files: set[Path]) -> dict[str, object]:
    entries = {}
    sloc = physical = 0
    for path in sorted(files):
        source, total = source_loc(path)
        name = str(path.relative_to(ROOT))
        entries[name] = {"sloc": source, "physical_lines": total}
        sloc += source
        physical += total
    return {"sloc": sloc, "physical_lines": physical, "file_count": len(files), "files": entries}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=6000, help="product SLOC target")
    parser.add_argument(
        "--accepted-baseline", type=int, default=8962,
        help="Alexey-approved maximum product SLOC for the current release; growth above it fails",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args()
    if args.limit < 1 or args.accepted_baseline < args.limit:
        parser.error("--limit must be positive and --accepted-baseline must be at least --limit")

    product = production_files()
    verification = verification_files()
    tests = test_files()
    missing_host_files = [str(path.relative_to(ROOT)) for path in REQUIRED_HOST_FILES if not path.is_file()]
    host_runtime_complete = not missing_host_files
    all_scoped = product | verification | tests
    unknown = text_files(HUB2) - all_scoped
    if unknown:
        names = ", ".join(str(path.relative_to(ROOT)) for path in sorted(unknown))
        print(f"Unclassified Hub 2 source/config files: {names}", file=sys.stderr)
        return 2

    product = describe(product)
    within_target = product["sloc"] <= args.limit
    accepted_maximum = args.accepted_baseline
    report = {
        "limit": args.limit,
        "accepted_release_baseline": args.accepted_baseline,
        "accepted_over_target": args.accepted_baseline - args.limit,
        "accepted_maximum": accepted_maximum,
        "product": product,
        "within_target": within_target,
        "verification_tooling": describe(verification),
        "tests_and_harnesses": describe(tests),
        "all_scoped_code": describe(all_scoped),
        "within_product_limit": product["sloc"] <= accepted_maximum and host_runtime_complete,
        "host_runtime_complete": host_runtime_complete,
        "missing_host_files": missing_host_files,
        "policy": "6000 product SLOC remains the architecture target. Alexey approved the exact 8962 product-SLOC tree, including Resource Manager, as the maximum for this release on 2026-11-06; this supersedes the prior 7853 release maximum and the obsolete 6362 maximum. Any growth above 8962 fails this release gate and needs a new decision. SLOC is nonblank/noncomment physical source lines; physical totals are reported separately. No source is minified. Test-only harnesses and CI verification are separate categories, never silently omitted from this report.",
        "excluded": ["documentation", "hidden local files", "generated caches/build/dist/node_modules", "Python *.egg-info packaging metadata", "binary assets"],
    }
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for name in ("product", "verification_tooling", "tests_and_harnesses", "all_scoped_code"):
            group = report[name]
            print(f"{name}: {group['sloc']} SLOC; {group['physical_lines']} physical lines; {group['file_count']} files")
        print(f"product target: {report['product']['sloc']}/{args.limit} SLOC")
        if not within_target and report["product"]["sloc"] <= accepted_maximum:
            print(f"Alexey-approved release overage: {report['product']['sloc'] - args.limit}/{report['accepted_over_target']} SLOC")
        print(f"Alexey-approved current-release maximum: {accepted_maximum} SLOC")
        if missing_host_files:
            print(f"complete product gate: FAIL (host runtime missing: {', '.join(missing_host_files)})")
        print("policy: SLOC counts nonblank/noncomment physical lines; no minification")
        for path, counts in report["product"]["files"].items():
            print(f"  {counts['sloc']:5d} SLOC  {counts['physical_lines']:5d} physical  {path}")
    return 0 if report["within_product_limit"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

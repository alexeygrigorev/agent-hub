#!/usr/bin/env python3
"""Reuse matching installed dependencies without installing or replacing files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


class DependencyError(ValueError):
    pass


def read_json(path):
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise DependencyError(f"Cannot read dependency metadata {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise DependencyError(f"Invalid dependency metadata: {path}")
    return value


def validate_install(repo, modules):
    """Check npm's installed lock and package versions against this checkout."""
    expected = read_json(repo / "package-lock.json").get("packages")
    installed = read_json(modules / ".package-lock.json").get("packages")
    if not isinstance(expected, dict) or not isinstance(installed, dict) or not installed:
        raise DependencyError("Dependency locks must contain installed package entries")
    for name, entry in installed.items():
        relative = Path(name)
        if not name.startswith("node_modules/") or ".." in relative.parts:
            raise DependencyError(f"Invalid installed package path: {name}")
        wanted = expected.get(name)
        if not isinstance(entry, dict) or not isinstance(wanted, dict):
            raise DependencyError(f"Installed package absent from root lock: {name}")
        for field in ("version", "integrity"):
            if entry.get(field) != wanted.get(field):
                raise DependencyError(f"Installed {field} differs from root lock: {name}")
        package = modules / Path(*relative.parts[1:]) / "package.json"
        if read_json(package).get("version") != wanted.get("version"):
            raise DependencyError(f"Installed package version differs from lock: {name}")
    for name, entry in expected.items():
        if not isinstance(entry, dict):
            raise DependencyError(f"Invalid root lock package entry: {name}")
        if name and not entry.get("optional") and name not in installed:
            raise DependencyError(f"Required package missing from installed lock: {name}")
    for executable in ("tsc", "vitest"):
        path = modules / ".bin" / executable
        if not path.is_file() or not os.access(path, os.X_OK):
            raise DependencyError(f"Required dependency executable missing: {path}")
    if not (modules / "wrangler" / "package.json").is_file():
        raise DependencyError("Required wrangler package missing")


def compare_manifests(repo, source):
    for name in ("package.json", "package-lock.json"):
        try:
            matches = (repo / name).read_bytes() == (source / name).read_bytes()
        except (OSError, RuntimeError) as exc:
            raise DependencyError(f"Cannot compare {name}: {exc}") from exc
        if not matches:
            raise DependencyError(f"Dependency donor {name} differs: {source}")


def canonical_checkout(repo):
    result = subprocess.run(
        ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
        cwd=repo, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise DependencyError("Cannot find canonical checkout; supply --source")
    common = Path(result.stdout.strip())
    if common.name != ".git":
        raise DependencyError("Canonical checkout unavailable; supply --source")
    return common.parent


def prepare(repo, source=None):
    repo = Path(repo).resolve()
    target = repo / "node_modules"
    if target.is_symlink():
        try:
            modules = target.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise DependencyError(f"Existing node_modules symlink is broken: {target}") from exc
        if not modules.is_dir():
            raise DependencyError("Existing node_modules symlink is not a directory")
        compare_manifests(repo, modules.parent)
        validate_install(repo, modules)
        return "Validated existing dependency symlink"
    if target.exists():
        if not target.is_dir():
            raise DependencyError("Existing node_modules is not a directory")
        validate_install(repo, target)
        return "Validated existing local dependencies"
    source = Path(source).resolve() if source else canonical_checkout(repo)
    compare_manifests(repo, source)
    modules = source / "node_modules"
    if modules.is_symlink() or not modules.is_dir():
        raise DependencyError(f"Donor node_modules must be a real directory: {modules}")
    validate_install(repo, modules)
    try:
        # symlink(2) publishes the complete link atomically and refuses EEXIST.
        target.symlink_to(modules, target_is_directory=True)
    except OSError as exc:
        raise DependencyError(f"Cannot create dependency link without replacing files: {exc}") from exc
    return f"Reusing dependencies from {modules}"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source", type=Path, help="Checkout with identical manifests and installed dependencies")
    args = parser.parse_args()
    try:
        print(prepare(args.repo, args.source))
    except (DependencyError, OSError) as exc:
        print(f"local-dependencies: {exc}; no install or replacement performed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Reuse matching installed dependencies without installing or replacing files."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

try:
    import pwd
except ImportError:  # pragma: no cover - Windows fallback
    pwd = None


class DependencyError(ValueError):
    pass


PLAYWRIGHT_VERSION = "1.64.1"
CHROMIUM_REVISION = "1248"
OWNED_PLAYWRIGHT_REL = Path(".local/share/agent-hub/playwright")
OWNED_BROWSERS_REL = Path(".cache/ms-playwright")
CHROMIUM_BIN_CANDIDATES = ("chrome-linux64/chrome", "chrome-linux/chrome")
HEADLESS_SHELL_BIN_CANDIDATES = (
    "chrome-headless-shell-linux64/chrome-headless-shell",
    "chrome-linux/headless_shell",
)


def current_user_home():
    """Resolve the account home from the system database, not managed HOME."""
    if pwd is not None:
        try:
            home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError) as exc:
            raise DependencyError(f"Cannot resolve the current user's home directory: {exc}") from exc
    else:  # pragma: no cover - test runner and production full-suite host are POSIX
        home = Path.home()
    if not home.is_absolute():
        raise DependencyError(f"Current user's home directory is not absolute: {home}")
    return home


def playwright_setup_hint(home=None):
    home = Path(home) if home is not None else current_user_home()
    donor = home / OWNED_PLAYWRIGHT_REL
    browsers = home / OWNED_BROWSERS_REL
    return (
        "Prepare the pinned, user-owned Playwright installation (no global install):\n"
        f"  npm --prefix {donor} install --save-exact playwright@{PLAYWRIGHT_VERSION}\n"
        f"  PLAYWRIGHT_BROWSERS_PATH={browsers} {donor / 'node_modules/.bin/playwright'} install chromium"
    )


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


def resolve_playwright_package(repo, user_home=None):
    """Return the repo-local package or the stable user-owned donor package."""
    repo = Path(repo).resolve()
    local = repo / "node_modules" / "playwright" / "package.json"
    if local.is_file():
        return local, False
    home = Path(user_home) if user_home is not None else current_user_home()
    donor = home / OWNED_PLAYWRIGHT_REL / "node_modules" / "playwright" / "package.json"
    return donor, True


def _check_browser_dir(browsers_dir, folder_name, candidates, label, hint):
    target_dir = browsers_dir / folder_name
    marker = target_dir / "INSTALLATION_COMPLETE"
    if not marker.is_file():
        raise DependencyError(f"{label} is unavailable (missing {marker}).\n{hint}")
    for relative in candidates:
        binary = target_dir / relative
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
    raise DependencyError(
        f"{label} executable is missing or not executable under {target_dir}.\n{hint}"
    )


def validate_playwright(repo, user_home=None):
    """Fail early unless the pinned Playwright and its matching Chromium exist."""
    repo = Path(repo).resolve()
    home = Path(user_home) if user_home is not None else current_user_home()
    package_file, owned = resolve_playwright_package(repo, home)
    hint = playwright_setup_hint(home)
    if not package_file.is_file():
        raise DependencyError(f"Pinned Playwright is unavailable (missing {package_file}).\n{hint}")

    try:
        package = read_json(package_file)
    except DependencyError as exc:
        raise DependencyError(f"Cannot validate Playwright package {package_file}: {exc}\n{hint}") from exc
    version = package.get("version")
    if version != PLAYWRIGHT_VERSION:
        raise DependencyError(
            f"Playwright version {version!r} in {package_file} does not match pinned {PLAYWRIGHT_VERSION}.\n{hint}"
        )

    modules = package_file.parent.parent
    core_file = modules / "playwright-core" / "package.json"
    browsers_file = modules / "playwright-core" / "browsers.json"
    if not core_file.is_file() or not browsers_file.is_file():
        missing = core_file if not core_file.is_file() else browsers_file
        raise DependencyError(f"Playwright installation is incomplete (missing {missing}).\n{hint}")
    try:
        core_version = read_json(core_file).get("version")
        browsers = read_json(browsers_file).get("browsers")
    except DependencyError as exc:
        raise DependencyError(f"Cannot validate Playwright installation: {exc}\n{hint}") from exc
    if core_version != PLAYWRIGHT_VERSION or core_version != version:
        raise DependencyError(
            f"Playwright/playwright-core version mismatch ({version!r}/{core_version!r}); "
            f"expected {PLAYWRIGHT_VERSION}.\n{hint}"
        )

    if owned:
        donor_manifest = package_file.parents[2] / "package.json"
        if not donor_manifest.is_file():
            raise DependencyError(f"Owned Playwright donor manifest is missing: {donor_manifest}.\n{hint}")
        try:
            declared = read_json(donor_manifest).get("dependencies", {}).get("playwright")
        except DependencyError as exc:
            raise DependencyError(f"Cannot validate owned Playwright donor {donor_manifest}: {exc}\n{hint}") from exc
        if declared != PLAYWRIGHT_VERSION:
            raise DependencyError(
                f"Owned Playwright donor must declare playwright@{PLAYWRIGHT_VERSION}; "
                f"{donor_manifest} declares {declared!r}.\n{hint}"
            )

    if not isinstance(browsers, list):
        raise DependencyError(f"Invalid Playwright browser metadata: {browsers_file}.\n{hint}")
    by_name = {
        entry.get("name"): entry
        for entry in browsers
        if isinstance(entry, dict) and isinstance(entry.get("name"), str)
    }
    chromium = by_name.get("chromium")
    headless = by_name.get("chromium-headless-shell")
    if not isinstance(chromium, dict) or not isinstance(headless, dict):
        raise DependencyError(f"Playwright metadata lacks Chromium browser revisions: {browsers_file}.\n{hint}")
    chromium_revision = chromium.get("revision")
    headless_revision = headless.get("revision")
    if chromium_revision != CHROMIUM_REVISION or headless_revision != CHROMIUM_REVISION:
        raise DependencyError(
            f"Playwright Chromium revisions {chromium_revision!r}/{headless_revision!r} "
            f"do not match pinned {CHROMIUM_REVISION}.\n{hint}"
        )

    browsers_dir = home / OWNED_BROWSERS_REL
    chromium_binary = _check_browser_dir(
        browsers_dir, f"chromium-{chromium_revision}", CHROMIUM_BIN_CANDIDATES,
        f"Chromium chromium-{chromium_revision}", hint,
    )
    headless_binary = _check_browser_dir(
        browsers_dir, f"chromium_headless_shell-{headless_revision}", HEADLESS_SHELL_BIN_CANDIDATES,
        f"Chromium headless shell chromium_headless_shell-{headless_revision}", hint,
    )
    source = "repo node_modules" if not owned else "owned user donor"
    return (
        f"Validated Playwright {version} from {source} ({package_file}); "
        f"Chromium {chromium_revision} ({chromium_binary}) and headless shell ({headless_binary})"
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--source", type=Path, help="Checkout with identical manifests and installed dependencies")
    parser.add_argument(
        "--check-playwright",
        action="store_true",
        help="Preflight the pinned Playwright package and matching Chromium without installing anything",
    )
    args = parser.parse_args()
    try:
        if args.check_playwright:
            print(validate_playwright(args.repo))
        else:
            print(prepare(args.repo, args.source))
    except (DependencyError, OSError) as exc:
        print(f"local-dependencies: {exc}; no install or replacement performed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

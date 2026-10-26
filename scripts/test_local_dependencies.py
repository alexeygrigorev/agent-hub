"""Small fixtures use the runner's temporary retention directory when provided."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).with_name("local-dependencies.py")
SPEC = importlib.util.spec_from_file_location("local_dependencies", SCRIPT)
DEPS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DEPS)


class LocalDependenciesTests(unittest.TestCase):
    def setUp(self):
        base = Path(os.environ.get("TMPDIR", SCRIPT.parent.parent / ".local" / "tmp")).resolve()
        base.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix="dependency-test-", dir=base))
        self.repo = self.root / "worktree"
        self.source = self.root / "main"
        packages = {f"node_modules/{name}": {"version": "1.0.0", "integrity": f"sha512-{name}"}
                    for name in ("typescript", "vitest", "wrangler")}
        self.lock = {"lockfileVersion": 3, "packages": packages}
        for repo in (self.repo, self.source):
            repo.mkdir()
            (repo / "package.json").write_text('{"name":"fixture"}')
            (repo / "package-lock.json").write_text(json.dumps(self.lock))
        self.modules = self.source / "node_modules"
        self.modules.mkdir()
        (self.modules / ".package-lock.json").write_text(json.dumps(self.lock))
        for name in ("typescript", "vitest", "wrangler"):
            package = self.modules / name
            package.mkdir()
            (package / "package.json").write_text('{"version":"1.0.0"}')
        (self.modules / ".bin").mkdir()
        for name in ("tsc", "vitest"):
            binary = self.modules / ".bin" / name
            binary.write_text("#!/bin/sh\nexit 0\n")
            binary.chmod(0o755)

    def test_reuses_matching_donor_and_validates_existing_link(self):
        DEPS.prepare(self.repo, self.source)
        target = self.repo / "node_modules"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), self.modules)
        DEPS.prepare(self.repo)
        (self.source / "package.json").write_text('{"name":"changed"}')
        with self.assertRaisesRegex(DEPS.DependencyError, "package.json differs"):
            DEPS.prepare(self.repo)

    def test_manifest_mismatch_refuses_to_create_link(self):
        (self.repo / "package-lock.json").write_text('{}')
        with self.assertRaisesRegex(DEPS.DependencyError, "package-lock.json differs"):
            DEPS.prepare(self.repo, self.source)
        self.assertFalse((self.repo / "node_modules").exists())

    def test_invalid_installed_integrity_is_refused(self):
        self.lock["packages"]["node_modules/vitest"]["integrity"] = "sha512-wrong"
        (self.modules / ".package-lock.json").write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(DEPS.DependencyError, "integrity differs"):
            DEPS.prepare(self.repo, self.source)

    def test_actual_installed_version_is_checked(self):
        (self.modules / "vitest" / "package.json").write_text('{"version":"2.0.0"}')
        with self.assertRaisesRegex(DEPS.DependencyError, "package version differs"):
            DEPS.prepare(self.repo, self.source)

    def test_existing_real_directory_is_preserved(self):
        target = self.repo / "node_modules"
        self.modules.rename(target)
        marker = target / "owned-file"
        marker.write_text("preserve")
        inode = target.stat().st_ino
        DEPS.prepare(self.repo, self.root / "nonexistent-donor")
        self.assertFalse(target.is_symlink())
        self.assertEqual(target.stat().st_ino, inode)
        self.assertEqual(marker.read_text(), "preserve")

    def test_existing_broken_link_is_refused_and_preserved(self):
        target = self.repo / "node_modules"
        target.symlink_to(self.root / "missing")
        with self.assertRaisesRegex(DEPS.DependencyError, "broken"):
            DEPS.prepare(self.repo, self.source)
        self.assertTrue(target.is_symlink())

    def test_missing_required_package_is_refused(self):
        del self.lock["packages"]["node_modules/wrangler"]
        (self.modules / ".package-lock.json").write_text(json.dumps(self.lock))
        with self.assertRaisesRegex(DEPS.DependencyError, "Required package missing"):
            DEPS.prepare(self.repo, self.source)

    def test_existing_symlink_cycle_is_refused_and_preserved(self):
        target = self.repo / "node_modules"
        target.symlink_to(target)
        with self.assertRaisesRegex(DEPS.DependencyError, "broken"):
            DEPS.prepare(self.repo, self.source)
        self.assertTrue(target.is_symlink())

    def test_canonical_checkout_is_found_from_worktree_common_dir(self):
        subprocess.run(["git", "init", "-q", str(self.source)], check=True)
        subprocess.run(["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                        "commit", "--allow-empty", "-qm", "fixture"], cwd=self.source, check=True)
        linked = self.root / "linked-worktree"
        subprocess.run(["git", "worktree", "add", "--detach", "-q", str(linked)],
                       cwd=self.source, check=True)
        self.assertEqual(DEPS.canonical_checkout(linked), self.source)


if __name__ == "__main__":
    unittest.main()

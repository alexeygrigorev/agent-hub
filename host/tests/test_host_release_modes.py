import os
import json
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
sys.path.insert(0, str(SCRIPTS))
import host_release_modes as modes


class HostReleaseModesTest(unittest.TestCase):
    def test_harden_tree_clears_write_bits_and_does_not_follow_symlinks(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            root.mkdir(mode=0o775)
            root.chmod(0o775)
            directory = root / "nested"
            directory.mkdir(mode=0o775)
            directory.chmod(0o775)
            file = directory / "entry"
            file.write_text("release data")
            file.chmod(0o664)
            outside = Path(temp) / "outside"
            outside.write_text("outside data")
            outside.chmod(0o666)
            link = root / "link"
            link.symlink_to(outside)

            changed = modes.harden_tree(root)

            self.assertGreaterEqual(len(changed), 3)
            modes.verify_tree(root)
            self.assertEqual(file.stat().st_mode & 0o022, 0)
            self.assertEqual(directory.stat().st_mode & 0o022, 0)
            self.assertEqual(root.stat().st_mode & 0o022, 0)
            self.assertEqual(outside.stat().st_mode & 0o022, 0o022)

    def test_verify_tree_rejects_group_or_world_writable_release_entries(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            root.mkdir()
            file = root / "writable"
            file.write_text("release data")
            file.chmod(0o664)
            with self.assertRaisesRegex(ValueError, "group/world-writable"):
                modes.verify_tree(root)

    def test_installed_release_hardener_is_confined_to_manifest_child(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "host-releases"
            release = root / ("a" * 40)
            release.mkdir(parents=True)
            (release / "release-manifest.json").write_text(json.dumps({"release_rev": "a" * 40}))
            orch_root = Path(temp) / "host-releases-orch"
            orch_release = orch_root / ("c" * 40)
            orch_release.mkdir(parents=True)
            (orch_release / "release-manifest.json").write_text(json.dumps({"release_rev": "c" * 40}))
            self.assertEqual(modes.validate_installed_release(release, (root, orch_root)), release.resolve())
            self.assertEqual(modes.validate_installed_release(orch_release, (root, orch_root)), orch_release.resolve())

            outside = Path(temp) / "unrelated"
            outside.mkdir()
            (outside / "release-manifest.json").write_text(json.dumps({"release_rev": "b" * 40}))
            with self.assertRaisesRegex(ValueError, "direct child"):
                modes.validate_installed_release(outside, root)
            alias = root / "alias"
            alias.symlink_to(release)
            with self.assertRaisesRegex(ValueError, "symlinks"):
                modes.validate_installed_release(alias, root)

    def test_switch_refuses_writable_release_before_reading_template_or_systemd(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "release"
            root.mkdir()
            (root / "writable").write_text("release data")
            (root / "writable").chmod(0o664)
            dropin = Path(temp) / "zz~~hub-desired-release.conf"
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "switch-host-release.py"),
                 "--release", str(root), "--template", str(Path(temp) / "missing-template"),
                 "--dropin", str(dropin)],
                capture_output=True, text=True, timeout=20,
            )
            self.assertNotEqual(proc.returncode, 0)
            self.assertIn("REFUSED: release contains group/world-writable", proc.stderr)
            self.assertFalse(dropin.exists())

    def test_build_under_umask_0002_produces_no_group_or_world_writable_files_or_dirs(self):
        with tempfile.TemporaryDirectory() as temp:
            releases = Path(temp) / "releases"
            releases.mkdir()
            proc = subprocess.run(
                [sys.executable, str(SCRIPTS / "build-host-release.py"),
                 "--rev", "HEAD", "--releases-dir", str(releases), "--source-repo", str(REPO)],
                capture_output=True, text=True, timeout=600,
                preexec_fn=lambda: os.umask(0o002),
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            release = releases / subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=REPO, text=True, timeout=20
            ).strip()
            checked = []
            for path in [release, *release.rglob("*")]:
                if path.is_symlink():
                    continue
                mode = path.stat(follow_symlinks=False).st_mode
                if stat.S_ISDIR(mode) or stat.S_ISREG(mode):
                    checked.append(path)
                    self.assertEqual(mode & 0o022, 0, str(path))
            self.assertTrue(checked)


if __name__ == "__main__":
    unittest.main()

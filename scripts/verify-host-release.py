#!/usr/bin/env python3
"""Verify a host release tree is complete and smoke-loads its routing config.

A host release is the directory a systemd drop-in points at. Required layout
is scripts/host-release-manifest.json. This check:

1. verifies every manifest file exists and matches its recorded sha256,
2. verifies the host package tree exists,
3. smoke-loads the release's routing config exactly like production code does:
   imports agent_hub_host.model_contract and native_admission from the release
   and loads policy models, routing rules and account aliases.

Exit 0 when the release is switchable, 1 otherwise. Only fixed messages are
printed; never release file contents.
"""
import hashlib
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MANIFEST_TEMPLATE = REPO / "scripts" / "host-release-manifest.json"
sys.path.insert(0, str(REPO / "scripts"))
from host_release_modes import verify_tree

SMOKE = (
    "import sys;"
    "from agent_hub_host import model_contract as mc, native_admission as na, heavy_test_admission as hta;"
    "models = mc.policy_models(); rules = na.routing_rules(); aliases = mc.account_aliases();"
    "assert isinstance(models, list) and models, 'no policy models';"
    "assert isinstance(rules, dict) and rules, 'no routing rules';"
    "assert isinstance(aliases, dict), 'aliases malformed';"
    "assert hta.ADMISSION_PROTOCOL_VERSION == 1, 'heavy admission protocol unsupported';"
    "import os;"
    "host_root = os.path.realpath(os.path.join(os.environ.get('RELEASE_DIR', ''), 'host'));"
    "assert os.path.realpath(mc.__file__).startswith(host_root), 'model_contract not from release';"
    "assert os.path.realpath(na.__file__).startswith(host_root), 'native_admission not from release';"
    "assert os.path.realpath(hta.__file__).startswith(host_root), 'heavy_test_admission not from release';"
    "print('smoke ok: models=' + str(len(models)) + ' rules=' + str(len(rules)) + ' aliases=' + str(len(aliases)))"
)

REQUIRED_ADMISSION_MODULE = "host/agent_hub_host/heavy_test_admission.py"


def fail(message):
    print("verify-host-release: FAIL: " + message)
    return 1


def release_template(release):
    """The manifest template a release is verified against (intake hubfail-t2596u).

    A release carries its own template at scripts/host-release-manifest.json;
    it always matches the release manifest version. The runner's template is
    only a fallback for trees predating it. Returns (template, source) where
    source is 'release' or 'runner'; (None, None) when neither is readable.
    """
    own = release / "scripts" / "host-release-manifest.json"
    if own.is_file() and not own.is_symlink():
        try:
            parsed = json.loads(own.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed, "release"
        except Exception:
            pass
    try:
        parsed = json.loads(MANIFEST_TEMPLATE.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            return parsed, "runner"
    except Exception:
        pass
    return None, None


def tree_hashes(release, tree):
    """sha256 of every source file under a manifest tree (bounded).

    Bytecode caches are derived artifacts, not sources: __pycache__/*.pyc
    are excluded so a verification run never invalidates the tree it checks.
    """
    root = release / tree
    if not root.is_dir():
        return None
    out = {}
    count = 0
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            return None
        if path.is_dir():
            continue
        if not path.is_file():
            return None
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        count += 1
        if count > 2000:
            return None
        data = path.read_bytes()
        if len(data) > 4 * 1024 * 1024:
            return None
        out[path.relative_to(release).as_posix()] = hashlib.sha256(data).hexdigest()
    return out


def main(argv):
    if len(argv) != 2 or argv[1] in ("-h", "--help"):
        print("usage: verify-host-release.py <release-dir>")
        return 2
    release = Path(argv[1])
    if not release.is_dir():
        return fail("release dir is missing")
    try:
        verify_tree(release)
    except (OSError, ValueError):
        return fail("release contains group/world-writable or unsupported entries")
    manifest_file = release / "release-manifest.json"
    if not manifest_file.is_file():
        return fail("release-manifest.json is missing (not built by build-host-release.py)")
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except Exception:
        return fail("release-manifest.json is unreadable")
    template, _source = release_template(release)
    if template is None:
        return fail("repo manifest template is unreadable")
    if manifest.get("manifest_version") != template.get("version"):
        return fail("manifest version mismatch")
    hashes = manifest.get("files_sha256")
    if not isinstance(hashes, dict):
        return fail("release-manifest.json has no file hashes")
    for name in template.get("files", []):
        target = release / name
        if not target.is_file() or target.is_symlink():
            return fail("required file is missing: " + name)
        if len(target.read_bytes()) > 4 * 1024 * 1024:
            return fail("required file is unexpectedly large: " + name)
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if hashes.get(name) != digest:
            return fail("required file changed after build: " + name)
    for tree in template.get("hashed_trees", []):
        if not (release / tree).is_dir():
            return fail("required package dir is missing: " + tree)
        actual = tree_hashes(release, tree)
        if actual is None:
            return fail("package tree unreadable: " + tree)
        for name, digest in actual.items():
            if hashes.get(name) != digest:
                return fail("package file changed after build: " + name)
        if "host/agent_hub_host/__init__.py" not in actual:
            return fail("host package entry is missing")
    if REQUIRED_ADMISSION_MODULE not in hashes:
        return fail("heavy-test admission module is missing from the manifest")
    venv_python = release / ".venv" / "bin" / "python"
    if not venv_python.is_file():
        return fail("release interpreter is missing (.venv/bin/python)")
    if not (release / "scripts" / "verify-host-release.py").is_file():
        return fail("release verifier is missing")
    if not (release / ".local" / "tmp").is_dir():
        return fail("release tmp dir is missing (.local/tmp)")
    env = {"PYTHONPATH": str(release / "host"), "PATH": "/usr/bin:/bin",
           "RELEASE_DIR": str(release), "PYTHONDONTWRITEBYTECODE": "1"}
    try:
        proc = subprocess.run(
            [str(venv_python), "-c", SMOKE],
            capture_output=True, text=True, timeout=60, cwd=str(release), env=env,
        )
    except Exception:
        return fail("smoke subprocess did not run")
    if proc.returncode != 0:
        return fail("routing config smoke load failed")
    line = (proc.stdout or "").strip().splitlines()
    print("verify-host-release: OK: " + release.name + (" (" + line[-1] + ")" if line else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

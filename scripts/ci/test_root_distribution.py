"""Build/install the root wheel with no source-tree imports."""
from pathlib import Path
import os, shutil, subprocess, tempfile, zipfile
ROOT = Path(__file__).resolve().parents[2]
def run(*args, **kwargs):
    return subprocess.run(args, check=True, text=True, **kwargs)
def main():
    scratch = ROOT / ".local/tmp"
    scratch.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="root-package-", dir=scratch) as temporary:
        base = Path(temporary)
        stage = base / "source"
        stage.mkdir()
        shutil.copy2(ROOT / "pyproject.toml", stage)
        shutil.copytree(ROOT / "hub2/host/agent_hub_host", stage / "hub2/host/agent_hub_host",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        wheels = base / "wheels"
        run("uv", "build", "--wheel", "--project", str(stage), "--out-dir", str(wheels))
        wheel, = wheels.glob("*.whl")
        with zipfile.ZipFile(wheel) as archive:
            names = set(archive.namelist())
            for module in ("native_launch", "provider_home", "cli", "client"):
                assert "agent_hub_host/" + module + ".py" in names
            assert "agent_hub_host/launcher.py" not in names
        envdir = base / "venv"
        run("uv", "venv", str(envdir))
        binary = envdir / ("Scripts" if os.name == "nt" else "bin")
        python = binary / ("python.exe" if os.name == "nt" else "python")
        run("uv", "pip", "install", "--python", str(python), str(wheel))
        home = base / "home"
        home.mkdir(mode=0o700)
        child = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
        for key in ("PYTHONPATH", "PYTHONHOME", "HUB2_TOKEN", "HUB2_SERVER"):
            child.pop(key, None)
        probe = """import importlib.metadata as m
import agent_hub_host
d=m.distribution('agent-hub-host')
assert d.version=='2.0.0'
assert d.requires==['websockets==16.0'], d.requires
assert 'site-packages' in agent_hub_host.__file__
assert {e.name for e in d.entry_points}=={'hub','hub2'}
for old in ('quse','aplexer-client'):
    try: m.distribution(old)
    except m.PackageNotFoundError: pass
    else: raise AssertionError(old)
"""
        run(str(python), "-I", "-c", probe, cwd=home, env=child)
        for command in ("hub", "hub2"):
            executable = binary / (command + ".exe" if os.name == "nt" else command)
            result = run(str(executable), "--help", cwd=home, env=child, capture_output=True)
            assert "usage:" in result.stdout.lower()
        print("PASS root wheel: isolated import, hub/hub2 help, only websockets16")
if __name__ == "__main__":
    main()

"""Build/install the root wheel with no source-tree imports."""
from pathlib import Path
import json, os, shutil, subprocess, tempfile, zipfile
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
assert {e.name for e in d.entry_points}=={'hub'}, d.entry_points  # no hub2 console script
assert {e.value for e in d.entry_points}=={'agent_hub_host.wheel_cli:main'}, d.entry_points
for old in ('quse','aplexer-client'):
    try: m.distribution(old)
    except m.PackageNotFoundError: pass
    else: raise AssertionError(old)
"""
        run(str(python), "-I", "-c", probe, cwd=home, env=child)
        assert not (binary / ("hub2.exe" if os.name == "nt" else "hub2")).exists()
        executable = binary / ("hub.exe" if os.name == "nt" else "hub")
        # The wheel console `hub` is the host namespace, not a relabeled daemon
        # parser and not the operator zipapp.
        guide = run(str(executable), "--help", cwd=home, env=child, capture_output=True)
        assert guide.returncode == 0
        assert "wheel console entry" in guide.stdout and "hub host" in guide.stdout
        refused = subprocess.run([str(executable), "sessions", "list"], cwd=home, env=child,
                                 capture_output=True, text=True)
        assert refused.returncode == 2 and "hub host" in refused.stderr, refused
        host_help = run(str(executable), "host", "--help", cwd=home, env=child, capture_output=True)
        assert "usage:" in host_help.stdout.lower() and "serve" in host_help.stdout, host_help
        config = home / "v2.json"
        config.write_text('{"machine_id": "wheel-probe"}')
        version = run(str(executable), "host", "--config", str(config), "version",
                      cwd=home, env=child, capture_output=True)
        payload = json.loads(version.stdout)
        assert payload["machine_id"] == "wheel-probe" and payload["credential_mode"] == "local", payload
        # Isolated direct module invocation stays the internal daemon surface.
        module_help = run(str(python), "-I", "-m", "agent_hub_host.cli", "--help",
                          cwd=home, env=child, capture_output=True)
        assert "usage:" in module_help.stdout.lower(), module_help
        with zipfile.ZipFile(wheel) as archive:
            entry_points = [n for n in archive.namelist() if n.endswith(".dist-info/entry_points.txt")]
            assert entry_points and "wheel_cli" in archive.read(entry_points[0]).decode()
        print("PASS root wheel: isolated import, wheel hub host namespace, no hub2, only websockets16")
if __name__ == "__main__":
    main()

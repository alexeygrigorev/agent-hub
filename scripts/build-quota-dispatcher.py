#!/usr/bin/env python3
"""Deterministic quota archive, optionally assembling a verified Host release.

Host mode consumes the normal CI-manifest-pinned release and the same Git
commit's business payloads. It does not build/install dependencies or a Host.
"""
import argparse
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tomllib
import zipfile

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "hub2" / "host" / "agent_hub_host"
RELEASE_VERIFIER = REPO_ROOT / 'scripts' / 'build-hub2-release.py'
FIXED_DATE_TIME = (2026, 1, 1, 0, 0, 0)
SOURCE_CLOSURE = (
    "__init__.py",
    "alert_authority.py",
    "alert_broker.py",
    "alert_collector.py",
    "quota_dispatch.py",
    "quota_dispatch_manager.py",
    "quota_dispatch_entry.py",
    "quota_natural_primary.py",
    "quota_natural_resources.py",
    "quota_natural_startup.py",
)
NATURAL_PAYLOADS = ("hub-quota-publish.py", "hub_alert_client.py", "natural-quota-prelude.py")
MAIN_STUB = (
    "from agent_hub_host.quota_dispatch_entry import main\n"
    "raise SystemExit(main())\n"
)


def _zip_info(arcname, *, mode=0o644):
    info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = (mode & 0xFFFF) << 16
    return info


def _write_archive(output_path, contents, *, manifest_out=None, host_release=None):
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = {}
    tmp_path = output.with_name(f".{output.name}.tmp.{os.getpid()}")
    try:
        with zipfile.ZipFile(tmp_path, "w") as zf:
            for arcname, data in sorted(contents.items()):
                sources[arcname] = hashlib.sha256(data).hexdigest()
                zf.writestr(_zip_info(arcname), data)
            main_bytes = MAIN_STUB.encode("utf-8")
            sources["__main__.py"] = hashlib.sha256(main_bytes).hexdigest()
            zf.writestr(_zip_info("__main__.py"), main_bytes)
            manifest = {"v": 1, "entry": "agent_hub_host.quota_dispatch_entry:main", "sources": sources}
            if host_release is not None:
                manifest['host_release'] = host_release
            manifest_bytes = json.dumps(
                manifest,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            zf.writestr(_zip_info("MANIFEST.json"), manifest_bytes)
        os.chmod(tmp_path, 0o444)
        os.replace(tmp_path, output)
    except BaseException:
        if tmp_path.exists():
            tmp_path.unlink()
        raise
    archive_bytes = output.read_bytes()
    result = {
        "output": str(output),
        "size": len(archive_bytes),
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "sources": sources,
    }
    if host_release is not None:
        result['host_release'] = host_release
    if manifest_out is not None:
        Path(manifest_out).write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def build_archive(output_path, *, manifest_out=None):
    contents = {'agent_hub_host/' + rel: (PKG_ROOT / rel).read_bytes()
                for rel in SOURCE_CLOSURE}
    contents.update({'natural/' + name: (REPO_ROOT / 'scripts' / name).read_bytes()
                     for name in NATURAL_PAYLOADS})
    return _write_archive(output_path, contents, manifest_out=manifest_out)


def _committed(commit, path):
    result = subprocess.run(['git', '-C', str(REPO_ROOT), 'show', commit + ':' + path],
                            capture_output=True, timeout=15, check=True)
    if not 0 < len(result.stdout) <= 262144:
        raise ValueError('committed payload size')
    return result.stdout


def _release_blob(directory, name, record):
    # Recheck the exact bytes consumed after the normal bundle verifier. A
    # changed path between verification and assembly cannot change authority.
    with (directory / name).open('rb') as stream:
        raw = stream.read(16 * 1024 * 1024 + 1)
    if (len(raw) > 16 * 1024 * 1024 or len(raw) != record['size']
            or hashlib.sha256(raw).hexdigest() != record['sha256']):
        raise ValueError('release bytes changed before assembly')
    return raw


def _wheel_python(raw, package):
    contents = {}
    total = 0
    with zipfile.ZipFile(io.BytesIO(raw)) as wheel:
        entries = wheel.infolist()
        names = [entry.filename for entry in entries]
        if len(entries) > 512 or len(set(names)) != len(names):
            raise ValueError('wheel member ambiguity')
        for entry in entries:
            name = entry.filename
            path = PurePosixPath(name)
            if (path.is_absolute() or '..' in path.parts or '\\' in name
                    or name.rstrip('/') != str(path)
                    or any(ord(char) < 32 for char in name)
                    or stat.S_ISLNK(entry.external_attr >> 16)):
                raise ValueError('unsafe wheel member')
            if entry.is_dir() or not name.startswith(package + '/') or not name.endswith('.py'):
                continue
            total += entry.file_size
            if not 0 <= entry.file_size <= 1024 * 1024 or total > 16 * 1024 * 1024:
                raise ValueError('wheel source size')
            contents[name] = wheel.read(entry)
    if package + '/__init__.py' not in contents:
        raise ValueError('wheel package missing')
    return contents


def _assemble_host_archive(output_path, host_bytes, transport_bytes, lock_bytes, natural_sources,
                           *, host_release_provenance, manifest_out=None):
    """Assemble and validate host/transport wheel closures and natural payloads without executing them."""
    lock = tomllib.loads(lock_bytes.decode('utf-8'))
    transport_name = 'websockets-16.0-py3-none-any.whl'
    dependency = next(p for p in lock['package'] if p['name'] == 'websockets' and p['version'] == '16.0')
    locked = [w for w in dependency['wheels'] if w['url'].endswith('/' + transport_name)]
    if (len(locked) != 1 or locked[0]['hash'] != 'sha256:' + hashlib.sha256(transport_bytes).hexdigest()
            or locked[0]['size'] != len(transport_bytes)):
        raise ValueError('transport differs from committed lock')
    contents = _wheel_python(host_bytes, 'agent_hub_host')
    required = {f'agent_hub_host/{name}' for name in SOURCE_CLOSURE}
    required.update('agent_hub_host/' + name for name in ('agent.py', 'cli.py', 'transport.py', '_release.py'))
    if not required <= contents.keys():
        raise ValueError('Host release lacks natural startup closure')
    contents.update(_wheel_python(transport_bytes, 'websockets'))
    if not {'websockets/exceptions.py', 'websockets/sync/client.py'} <= contents.keys():
        raise ValueError('transport closure missing')
    for name in NATURAL_PAYLOADS:
        if name not in natural_sources:
            raise ValueError('missing natural payload: ' + name)
        contents['natural/' + name] = natural_sources[name]
    return _write_archive(output_path, contents, manifest_out=manifest_out, host_release=host_release_provenance)


def build_normal_host_archive(output_path, *, host_wheel_path, transport_wheel_path, commit,
                              uv_lock_bytes, natural_sources, manifest_out=None):
    """Assemble inert natural Host archive during normal CI build with honest input provenance."""
    host_bytes = Path(host_wheel_path).read_bytes()
    transport_bytes = Path(transport_wheel_path).read_bytes()
    if len(host_bytes) > 16 * 1024 * 1024 or len(transport_bytes) > 16 * 1024 * 1024:
        raise ValueError('release wheel exceeds size limit')
    provenance = {
        'commit': commit,
        'host_wheel_sha256': hashlib.sha256(host_bytes).hexdigest(),
        'transport_wheel_sha256': hashlib.sha256(transport_bytes).hexdigest(),
        'natural_payloads': {
            name: hashlib.sha256(natural_sources[name]).hexdigest()
            for name in NATURAL_PAYLOADS
        },
    }
    return _assemble_host_archive(
        output_path, host_bytes, transport_bytes, uv_lock_bytes, natural_sources,
        host_release_provenance=provenance, manifest_out=manifest_out,
    )


def build_host_archive(output_path, release_directory, commit, manifest_sha256, *, manifest_out=None):
    """Assemble existing verified normal artifacts; no user site or wheel exec."""
    spec = importlib.util.spec_from_file_location('quota_normal_release_verifier', RELEASE_VERIFIER)
    verifier = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(verifier)
    directory = Path(release_directory)
    manifest = verifier.verify_bundle(directory, commit, manifest_sha256)
    artifacts = manifest['artifacts']
    wheels = [name for name in artifacts if name.startswith('agent_hub_host_v2-') and name.endswith('.whl')]
    if len(wheels) != 1:
        raise ValueError('Host wheel ambiguity')
    host_name = wheels[0]
    transport_name = 'websockets-16.0-py3-none-any.whl'
    host = _release_blob(directory, host_name, artifacts[host_name])
    transport = _release_blob(directory, transport_name, artifacts[transport_name])
    lock_bytes = _committed(commit, 'uv.lock')
    natural_sources = {name: _committed(commit, 'scripts/' + name) for name in NATURAL_PAYLOADS}
    provenance = {'commit': commit, 'manifest_sha256': manifest_sha256,
        'host_wheel_sha256': artifacts[host_name]['sha256'],
        'transport_wheel_sha256': artifacts[transport_name]['sha256']}
    return _assemble_host_archive(
        output_path, host, transport, lock_bytes, natural_sources,
        host_release_provenance=provenance, manifest_out=manifest_out,
    )


def main(argv=None):
    parser = argparse.ArgumentParser(prog="build-quota-dispatcher")
    parser.add_argument(
        "--output",
        type=Path,
        default=REPO_ROOT / ".local" / "dist" / "agent-hub-quota-dispatcher.pyz",
    )
    parser.add_argument("--manifest-out", type=Path, default=None)
    parser.add_argument('--host-release-dir', type=Path)
    parser.add_argument('--host-commit')
    parser.add_argument('--host-manifest-sha256')
    args = parser.parse_args(argv)
    host = (args.host_release_dir, args.host_commit, args.host_manifest_sha256)
    if any(value is not None for value in host):
        if not all(value is not None for value in host):
            parser.error('Host archive requires release directory, full commit and CI manifest SHA-256')
        result = build_host_archive(args.output, *host, manifest_out=args.manifest_out)
    else:
        result = build_archive(args.output, manifest_out=args.manifest_out)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

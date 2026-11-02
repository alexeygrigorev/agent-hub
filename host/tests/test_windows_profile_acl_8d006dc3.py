"""Windows aplexer profile dir with a foreign (MSYS/Cygwin POSIX-mapped) ACL (intake 8d006dc3).

On DESKTOP-FP4OP26, %APPDATA%\\aplexer carried Everyone/NULL-SID ACEs. profiles()
only verified the ACL on Windows (POSIX fchmods it to 0700), so every engine sync
raised PermissionError before installing Antigravity or reporting host state.
"""
from contextlib import contextmanager
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent_hub_host import provisioning as p, win32_fs as win


class _FakeAPI:
    """Simulated Win32: the leaf is foreign until restrict() replaces its DACL."""

    def __init__(self):
        self.events = []
        self.restricted = set()

    def GetDriveTypeW(self, root):
        return 3

    def GetVolumeInformationW(self, root, *args):
        args[-2].value = 'NTFS'
        return 1

    def check(self, value):
        if not value:
            raise OSError('simulated API failure')
        return value

    def open(self, path, access, disposition=3, directory=False, security=None):
        handle = 0x100000000 + len([e for e in self.events if e[0] == 'open']) + 1
        self.events.append(('open', str(path), access, handle))
        return handle

    def CloseHandle(self, handle):
        return 1

    def restrict(self, handle, directory=False):
        self.events.append(('restrict', handle))
        self.restricted.add(handle)

    def private(self, handle, **kwargs):
        self.events.append(('private', handle))
        if handle not in self.restricted:
            raise PermissionError('Exactly one owner-only full-control ACE required')
        return True

    @contextmanager
    def security(self, directory=False):
        yield win.SecurityAttributes()

    def CreateDirectoryW(self, path, security):
        return 1


def test_owned_parent_restricts_leaf_before_native_proof():
    fake = _FakeAPI()
    path = r'C:\Users\U\AppData\Roaming\aplexer\config.toml'
    with patch.object(win, 'api', return_value=fake):
        with pytest.raises(PermissionError):
            with win.parent(path, create=True, private=False, native=True):
                pass  # unchanged behaviour without owned: foreign ACL refused
        fake.events.clear()
        with win.parent(path, create=True, private=False, native=True, owned=True):
            pass
    opens = [e for e in fake.events if e[0] == 'open']
    leaf = opens[-1]
    assert leaf[1].endswith('aplexer') and leaf[2] == win.WRITE_DAC
    assert all(e[2] == 0 for e in opens[:-1])  # ancestors are never rewritten
    kinds = [e[0] for e in fake.events if e[0] != 'open']
    assert kinds == ['restrict', 'private']
    assert fake.events[-2] == ('restrict', leaf[3])


def test_owned_refuses_drive_root():
    with patch.object(win, 'api', return_value=_FakeAPI()):
        with pytest.raises(PermissionError):
            with win.parent(r'C:\config.toml', owned=True):
                pass


@pytest.mark.skipif(os.name != 'nt', reason='real NTFS ACL evidence')
def test_real_profiles_tighten_foreign_aplexer_acl(monkeypatch):
    monkeypatch.delenv('APLEXER_CONFIG', raising=False)
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / 'Roaming'
        cfg = base / 'aplexer' / 'config.toml'
        cfg.parent.mkdir(parents=True)
        # Inheritable Everyone read, as an MSYS/Cygwin mkdir leaves behind.
        subprocess.run(['icacls', str(cfg.parent), '/grant', '*S-1-1-0:(OI)(CI)(RX)'],
                       check=True, capture_output=True)
        cfg.write_text('version = 1\n\n[profiles.mine]\nengine = "codex"\n')
        a = win.api()
        with win.parent(cfg, private=False, native=False) as _:
            h = a.open(cfg.parent, 0, directory=True)
            try:
                with pytest.raises(PermissionError):
                    a.private(h, directory=True)
            finally:
                a.CloseHandle(h)

        p.profiles([{'label': 'claude', 'provider': 'claude'}], path=cfg)

        text = cfg.read_text()
        assert '[profiles.mine]' in text and '[profiles.hub-claude]' in text
        everyone = subprocess.run(['icacls', str(cfg.parent)], capture_output=True, text=True).stdout
        assert 'Everyone' not in everyone and 'Jeder' not in everyone
        # Idempotent: a second sync keeps the operator profile and one block.
        p.profiles([{'label': 'claude', 'provider': 'claude'}], path=cfg)
        assert cfg.read_text().count(p.BEGIN) == 1

"""Bounded, reversible Windows package-update hold (intake e3e6bade).

DESKTOP-FP4OP26 was rolled back to ff295a5 because the served build (680642,
then 799cec4) was Host-canary red. The self-updater had no supported way to
pause: only its 12 h throttle stood between the host and reinstalling a red
build, and moving that marker would fake a timestamp. A hold file now pauses
scheduling for a stated reason, at most 72 h, and is released by deleting it.
"""
import json
import time

from agent_hub_host import package_update as u


def make_release(tmp_path, sha):
    release = tmp_path / 'releases' / (sha[:16] + '-' + 'c' * 12)
    release.mkdir(parents=True)
    (release / 'runtime-ready.json').write_text(json.dumps({'archive_sha256': sha}))
    return release


def step(tmp_path, now, registered):
    release = make_release(tmp_path, 'd' * 64) if not (tmp_path / 'releases').exists() else next((tmp_path / 'releases').iterdir())
    return u.step_once('https://hub.example', tmp_path, release=release, now=now,
                       fetch=lambda url: 'e' * 64 + '\n', register=lambda *a: registered.append(a), free_gb=500)


def test_active_hold_blocks_scheduling_and_leaves_throttle_marker_untouched(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 24, 'canary-red-680642', now=now)
    registered = []
    assert step(tmp_path, now + 60, registered) == 'held'
    assert registered == []
    assert not (tmp_path / 'package-update.json').exists()
    assert u.active_hold(tmp_path, now + 60)['reason'] == 'canary-red-680642'


def test_hold_expires_and_release_resumes_normal_scheduling(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 1, 'canary-red', now=now)
    registered = []
    assert step(tmp_path, now + 3600 + 1, registered) == 'scheduled'  # expired: normal path
    assert len(registered) == 1
    u.set_hold(tmp_path, 1, 'canary-red', now=now)
    assert u.clear_hold(tmp_path) is True and u.clear_hold(tmp_path) is False
    assert u.active_hold(tmp_path, now) is None


def test_current_build_is_reported_current_even_while_held(tmp_path):
    now = time.time()
    u.set_hold(tmp_path, 1, 'x', now=now)
    release = make_release(tmp_path, 'd' * 64)
    assert u.step_once('https://hub.example', tmp_path, release=release, now=now,
                       fetch=lambda url: 'd' * 64, register=lambda *a: None) == 'current'


def test_hold_is_bounded_and_invalid_holds_are_ignored(tmp_path):
    now = time.time()
    for hours in (0, -1, 72.01, 1000):
        try:
            u.set_hold(tmp_path, hours, 'x', now=now)
        except ValueError:
            pass
        else:
            raise AssertionError(f'{hours} h accepted')
    for reason in ('', 'Has Spaces', 'x' * 65):
        try:
            u.set_hold(tmp_path, 1, reason, now=now)
        except ValueError:
            pass
        else:
            raise AssertionError(f'reason {reason!r} accepted')
    path = tmp_path / u.HOLD_FILE
    bad = [
        'not json',
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00Z'}),                       # no until
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-10T00:00:00Z'}),  # > 72 h
        json.dumps({'reason': 'BAD REASON', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-01T01:00:00Z'}),
        json.dumps({'reason': 'x', 'set_at': '2099-01-01T00:00:00Z', 'until': '2099-01-02T00:00:00Z'}),  # set in future
        json.dumps({'reason': 'x', 'set_at': '2026-11-01T00:00:00+00:00', 'until': '2026-11-01T01:00:00+00:00'}),  # non-Z
    ]
    import datetime as dt
    t = dt.datetime(2026, 11, 1, 0, 40, tzinfo=dt.timezone.utc).timestamp()
    for text in bad:
        path.write_text(text)
        assert u.active_hold(tmp_path, t) is None, text
    path.write_text(json.dumps({'reason': 'ok', 'set_at': '2026-11-01T00:00:00Z', 'until': '2026-11-01T01:00:00Z'}))
    assert u.active_hold(tmp_path, t) == {'until': '2026-11-01T01:00:00Z', 'reason': 'ok'}


def test_cli_hold_show_release(tmp_path, monkeypatch, capsys):
    from agent_hub_host import bootstrap
    monkeypatch.setattr(bootstrap, 'user_install_root', lambda: tmp_path)
    assert u.main(['hold', '12', 'canary-red']) == 0
    assert json.loads(capsys.readouterr().out)['hold']['reason'] == 'canary-red'
    assert u.main(['show']) == 0
    assert json.loads(capsys.readouterr().out)['hold']['reason'] == 'canary-red'
    assert u.main(['release']) == 0
    assert json.loads(capsys.readouterr().out) == {'released': True}

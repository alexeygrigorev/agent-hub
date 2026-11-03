"""Intake 06728269 (LAPTOP-PHA6TCNC): the Hub stamps admission observed_at on
its own clock mid-gate, and a host wall clock skewed against the Hub shifts
the wall age in both directions — negative on a lagging clock, past 30 s on a
leading clock. The old wall-only check (0 <= age <= 30, no tolerance) refused
every dispatch on that laptop while placement kept working (the projection's
own cold gate run aged the stamp past the skew). The 30 s verdict window is
now mirrored skew-free from measured gate moments, with a bounded garbage-
stamp sanity bound and measured numeric evidence on the refusal."""
import json
import tempfile
import time
from datetime import timedelta
from unittest.mock import patch

import pytest

from agent_hub_host import fleet
from agent_hub_host.dispatch_diagnostics import AdmissionRefusedError, classify, GATE_EVIDENCE_KEYS

try:
    from host.tests.test_fleet import settings
except ImportError:  # pragma: no cover - direct host/tests invocation
    from test_fleet import settings

SUB = {'admission_argv': ['maintained-gate'], 'account_label': 'shared-codex'}
PAYLOAD = {'mode': 'headless', 'engine': 'codex', 'profile': None,
           'account': 'shared-codex', 'workspace': '/w', 'selection': None}


def stamp(offset_s):
    """Hub stamp as the host wall clock sees it after skewing by offset_s."""
    return (fleet.dt.datetime.now(fleet.dt.timezone.utc) + timedelta(seconds=offset_s)).isoformat()


def allowed_gate(observed_at, **extra):
    return {'launch_allowed': True, 'profile_supported': True,
            'account_label': 'shared-codex', 'observed_at': observed_at, **extra}


def dispatcher(tmp, runner):
    return fleet.Dispatcher(settings(tmp), lambda *a, **k: {}, runner, journal_dir=tmp)


def share_key():
    return json.dumps({"argv": SUB["admission_argv"],
                       **{k: PAYLOAD[k] for k in ("mode", "engine", "profile", "account", "selection")}},
                      sort_keys=True, default=str)


def test_lagging_clock_admits_fresh_verdict():
    # Hub stamp 40 s in the host's future: the wall age is -40 and the old
    # check refused every dispatch (this is the LAPTOP-PHA6TCNC symptom).
    observed = stamp(40)
    with tempfile.TemporaryDirectory() as tmp:
        d = dispatcher(tmp, lambda argv, **kw: allowed_gate(observed))
        assert d._dispatch_admission(SUB, dict(PAYLOAD), None, {}) == observed


def test_leading_clock_admits_fresh_verdict():
    # Hub stamp that looks 40 s old on a clock leading the Hub by 40 s.
    observed = stamp(-40)
    with tempfile.TemporaryDirectory() as tmp:
        d = dispatcher(tmp, lambda argv, **kw: allowed_gate(observed))
        assert d._dispatch_admission(SUB, dict(PAYLOAD), None, {}) == observed


def test_real_stale_verdict_refused_despite_fresh_wall_age():
    # The window is not weakened: a verdict whose real age is 45 s (measured
    # monotonic gate meta) is refused even while the wall age lies "fresh" —
    # the pre-fix check would have passed it on a lagging host.
    with tempfile.TemporaryDirectory() as tmp:
        runs = [0]

        def runner(argv, **kw):
            runs[0] += 1
            return allowed_gate(stamp(0))
        d = dispatcher(tmp, runner)
        now = time.monotonic()
        gate = allowed_gate(stamp(0), gate_post_stamp_s=0.05,
                            _gate_meta={'started_mono': now - 95, 'finished_mono': now - 45,
                                        'duration_s': 50.0})
        d._admission_shared[share_key()] = (time.monotonic(), dict(gate))
        with pytest.raises(AdmissionRefusedError) as exc:
            d._dispatch_admission(SUB, dict(PAYLOAD), None, {})
        assert exc.value.gate_reason == 'admission_timestamp_stale'
        assert runs[0] == 0  # refused from the shared observation, no new gate run
        diagnostic = classify(exc.value)
        assert diagnostic['gate_duration_s'] == 50.0
        assert diagnostic['gate_post_stamp_s'] == 0.05


def test_garbage_stamp_refused_with_evidence():
    with tempfile.TemporaryDirectory() as tmp:
        next_gate = [None]

        def runner(argv, **kw):
            return next_gate[0]
        d = dispatcher(tmp, runner)
        for offset in (-7200, 7200):
            next_gate[0] = allowed_gate(stamp(offset))
            with pytest.raises(AdmissionRefusedError) as exc:
                d._dispatch_admission(SUB, dict(PAYLOAD), None, {})
            assert exc.value.gate_reason == 'admission_timestamp_stale'
            diagnostic = classify(exc.value)
            assert abs(diagnostic['gate_stamp_age_s']) >= 7200 - 5
            assert diagnostic['gate_duration_s'] >= 0


def test_unparseable_stamp_refused():
    with tempfile.TemporaryDirectory() as tmp:
        d = dispatcher(tmp, lambda argv, **kw: allowed_gate('not-a-timestamp'))
        with pytest.raises(AdmissionRefusedError) as exc:
            d._dispatch_admission(SUB, dict(PAYLOAD), None, {})
        assert exc.value.gate_reason == 'admission_timestamp_stale'


def test_projection_admits_skewed_clock_and_refuses_garbage():
    with tempfile.TemporaryDirectory() as tmp:
        gates = [allowed_gate(stamp(40)), allowed_gate(stamp(-7200))]

        def runner(argv, **kw):
            return gates.pop(0)
        d = dispatcher(tmp, runner)
        caps = {'fleet_modes': ['headless'], 'fleet_route_rejections': []}
        # Wall age -40 on a lagging clock: the old projection denied placement.
        allowed, engines = d._admission_projection(caps)
        assert allowed is True and engines == {'codex'}
        allowed, engines = d._admission_projection(caps)
        assert allowed is False and engines == set()


def test_refresh_due_triggers_on_monotonic_age_when_wall_is_skew_blind():
    at = stamp(0)
    now = time.monotonic()
    with patch.object(fleet, '_iso_age_s', lambda value: 0.0):
        # A lagging host clock keeps the wall age negative and would never
        # refresh; real elapsed time since the gate must trigger the re-run.
        assert fleet._admission_refresh_due(at, now - fleet.CLAIM_ADMISSION_REFRESH_S - 1) is True
        assert fleet._admission_refresh_due(at, now) is False
    with patch.object(fleet, '_iso_age_s', lambda value: 20.0):
        assert fleet._admission_refresh_due(at, now) is True


def test_refusal_evidence_is_bounded_numeric_only():
    error = AdmissionRefusedError('Fresh admission timestamp required', 'admission_timestamp_stale',
                                  {'gate_stamp_age_s': -40.0021, 'gate_duration_s': 'x',
                                   'gate_post_stamp_s': 10 ** 9, 'gate_stamp_offset_s': -39.9,
                                   'host_path': '/home/alexey'})
    diagnostic = classify(error)
    assert diagnostic['gate_reason'] == 'admission_timestamp_stale'
    assert diagnostic['gate_stamp_age_s'] == -40.002
    assert diagnostic['gate_stamp_offset_s'] == -39.9
    assert 'gate_duration_s' not in diagnostic and 'gate_post_stamp_s' not in diagnostic
    assert 'host_path' not in diagnostic
    assert set(diagnostic) <= {'reason', 'message', 'stage', 'error_type', 'gate_reason'} | set(GATE_EVIDENCE_KEYS)
    plain = classify(AdmissionRefusedError('Fresh admission refused', 'gate_refused'))
    assert not (set(plain) & set(GATE_EVIDENCE_KEYS))


def test_admission_gate_records_measured_stamp_evidence(monkeypatch, capsys):
    # Intake 06728269 AC1: the gate records its measured post-stamp duration
    # and the host-vs-Hub offset (negative on the lagging laptop clock).
    import io
    import sys
    from agent_hub_host import provisioning
    observed = stamp(2)  # lagging host: the Hub stamp sits 2 s in its future
    seen = []

    def fake_request(method, path, payload=None):
        seen.append((method, path))
        return {'launch_allowed': True, 'profile_supported': True,
                'account_label': 'shared-codex', 'observed_at': observed}
    monkeypatch.setattr(provisioning, 'host_request', fake_request)
    monkeypatch.setattr(sys, 'argv', ['provisioning', 'shared-codex'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    provisioning.admission()
    gate = json.loads(capsys.readouterr().out)
    assert seen == [('POST', '/api/engines/admission')]
    assert gate['observed_at'] == observed
    assert type(gate['gate_post_stamp_s']) is float and 0 <= gate['gate_post_stamp_s'] < 10
    assert -3 <= gate['gate_stamp_offset_s'] <= -1


def test_admission_gate_omits_evidence_when_hub_unreachable(monkeypatch, capsys):
    import io
    import sys
    from agent_hub_host import provisioning

    def fake_request(method, path, payload=None):
        raise OSError('down')
    monkeypatch.setattr(provisioning, 'host_request', fake_request)
    monkeypatch.setattr(sys, 'argv', ['provisioning', 'shared-codex'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    provisioning.admission()
    gate = json.loads(capsys.readouterr().out)
    assert gate['reason'] == 'hub_unreachable'
    # Post-response work can be measured without a Hub stamp; the wall-clock
    # offset cannot be measured when the Hub never returned observed_at.
    assert gate['gate_post_stamp_s'] >= 0
    assert 'gate_stamp_offset_s' not in gate

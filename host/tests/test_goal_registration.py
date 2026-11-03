import json
import sqlite3
from pathlib import Path
import pytest

from agent_hub_host.goal_registration import GoalRegistrationError, register_goal


def fake_goal_store(tmp_path: Path, engine: str):
    home = tmp_path / ('.' + engine) / 'home'
    rollout = home / 'sessions' / '2026' / '11' / '03' / 'rollout-1.jsonl'
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({'type': 'session_meta', 'payload': {
        'id': 'thread-123', 'cwd': '/workspace/demo', 'timestamp': '2026-11-03T12:00:00Z',
    }}) + '\n')
    sidecar = tmp_path / 'native-history' / 'native-123'
    sidecar.mkdir(parents=True)
    (sidecar / 'transcript.json').write_text(json.dumps({'path': str(rollout)}))
    goal_db = home / 'goals_1.sqlite'
    with sqlite3.connect(goal_db) as db:
        db.execute('CREATE TABLE thread_goals (thread_id TEXT PRIMARY KEY, goal_id TEXT, objective TEXT, status TEXT, '
                   'token_budget INTEGER, tokens_used INTEGER, time_used_seconds INTEGER, created_at_ms INTEGER, updated_at_ms INTEGER)')
    row = {
        'id': 'native-123', 'tag': 'hub-offer-1', 'workspace': '/workspace/demo',
        'engine': engine, 'created_at_ms': 1793707200000,
        'history_path': str(sidecar / 'history.jsonl'),
    }
    return row, goal_db


@pytest.mark.parametrize('engine', ['codex', 'zcodex'])
def test_register_goal_delivers_slash_command_and_reads_exact_fake_thread_store(tmp_path, engine):
    row, goal_db = fake_goal_store(tmp_path, engine)
    calls = []

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1:3] == ['message', 'send']:
            return {'id': 'envelope-1'}
        if argv[1:3] == ['message', 'deliver']:
            with sqlite3.connect(goal_db) as db:
                db.execute("INSERT INTO thread_goals VALUES ('thread-123', 'goal-1', 'Fix it', 'active', NULL, 0, 0, 1, 2)")
            return {'status': 'submitted'}
        return [row]

    verified = register_goal(
        'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
        engine=engine, goal_command='/goal Fix the test', runner=runner,
    )
    assert verified == {'status': 'active', 'thread_id': 'thread-123'}
    send = next(argv for argv, _ in calls if argv[1:3] == ['message', 'send'])
    assert send[-1] == '/goal Fix the test'
    assert send[send.index('--workspace') + 1] == row['workspace']
    assert not any(argv[-1] == '/goal Fix the test' for argv, _ in calls if argv[1:3] != ['message', 'send'])
    delivery = next(argv for argv, _ in calls if argv[1:3] == ['message', 'deliver'])
    assert delivery[-1] == 'envelope-1'


def test_goal_not_ready_timeout_sends_once_and_does_not_retry_launch(tmp_path):
    row, _goal_db = fake_goal_store(tmp_path, 'codex')
    now = [0.0]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['message', 'send']:
            return {'id': 'envelope-1'}
        if argv[1:3] == ['message', 'deliver']:
            return {'status': 'not-ready'}
        return [row]

    with pytest.raises(GoalRegistrationError, match='goal_registration_failed'):
        register_goal(
            'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
            engine='codex', goal_command='/goal Fix the test', runner=runner,
            timeout_s=2.0, progress=object(), monotonic=lambda: now[0],
            sleep=lambda delay: now.__setitem__(0, now[0] + delay),
        )
    assert sum(argv[1:3] == ['message', 'send'] for argv in calls) == 1
    assert 1 <= sum(argv[1:3] == ['message', 'deliver'] for argv in calls) <= 5


def test_goal_store_timeout_after_single_delivery_is_fail_loud(tmp_path):
    row, _goal_db = fake_goal_store(tmp_path, 'zcodex')
    now = [0.0]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1:3] == ['message', 'send']:
            return {'id': 'envelope-1'}
        if argv[1:3] == ['message', 'deliver']:
            return {'status': 'submitted'}
        return [row]

    with pytest.raises(GoalRegistrationError, match='goal_registration_failed'):
        register_goal(
            'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
            engine='zcodex', goal_command='/goal Fix the test', runner=runner,
            timeout_s=1.5, progress=type('NoStore', (), {'locate': lambda *_: None})(),
            monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
        )
    assert sum(argv[1:3] == ['message', 'send'] for argv in calls) == 1
    assert sum(argv[1:3] == ['message', 'deliver'] for argv in calls) == 1


def test_engine_without_goal_support_is_refused_before_native_effect():
    calls = []
    with pytest.raises(GoalRegistrationError):
        register_goal('aplexer', native_id='native-123', tag='hub-offer', workspace='/workspace/demo',
                      engine='gemini', goal_command='/goal Fix it', runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []

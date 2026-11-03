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
    return row, goal_db, rollout


def delivery_receipt(row, *, ready=True, landed=True, submitted=True):
    return {
        'id': row['id'],
        'delivery': {
            'engine': row['engine'], 'status': 'submitted', 'ready': ready,
            'landed': landed, 'submitted': submitted, 'sends': 1, 'enter_presses': 1,
        },
    }


@pytest.mark.parametrize('engine', ['codex', 'zcodex'])
def test_register_goal_submits_native_command_and_reads_exact_fake_thread_store(tmp_path, engine):
    row, goal_db, rollout = fake_goal_store(tmp_path, engine)
    calls = []
    sent = [False]
    lookup_count = [0]

    class DelayedStore:
        def locate(self, _row):
            lookup_count[0] += 1
            # The exact thread row appears on the second post-send lookup.
            if sent[0] and lookup_count[0] >= 3:
                with sqlite3.connect(goal_db) as db:
                    db.execute("INSERT OR IGNORE INTO thread_goals VALUES ('thread-123', 'goal-1', 'Fix it', 'active', NULL, 0, 0, 1, 2)")
            return rollout

    def runner(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[1] == 'list':
            return [row]
        if argv[1] == 'send':
            sent[0] = True
            return delivery_receipt(row)
        raise AssertionError(f'unexpected native operation: {argv[1:3]}')

    verified = register_goal(
        'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
        engine=engine, goal_command='/goal Fix the test', runner=runner,
        progress=DelayedStore(),
    )
    assert verified == {'status': 'active', 'thread_id': 'thread-123'}
    send, kwargs = next((argv, kwargs) for argv, kwargs in calls if argv[1] == 'send')
    assert send[2:4] == [row['id'], '/goal Fix the test']
    assert '--enter' in send
    assert '--raw' not in send
    assert '--json' in send
    assert send[send.index('--wait-ack') + 1] == '0'
    assert send[send.index('--resends') + 1] == '0'
    assert send[send.index('--ready-timeout') + 1] == '25'
    assert kwargs['cwd'] == row['workspace']
    assert kwargs['timeout'] <= 30
    assert not any(argv[1] == 'message' for argv, _ in calls)


def test_goal_tui_readiness_failure_is_loud_and_is_not_retried(tmp_path):
    row, _goal_db, _rollout = fake_goal_store(tmp_path, 'codex')
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'list':
            return [row]
        if argv[1] == 'send':
            return delivery_receipt(row, ready=False)
        raise AssertionError(f'unexpected native operation: {argv[1:3]}')

    with pytest.raises(GoalRegistrationError, match='goal_registration_failed'):
        register_goal(
            'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
            engine='codex', goal_command='/goal Fix the test', runner=runner,
            progress=type('NoStore', (), {'locate': lambda *_: None})(),
        )
    assert sum(argv[1] == 'send' for argv in calls) == 1


def test_goal_store_timeout_after_single_native_submission_is_fail_loud(tmp_path):
    row, _goal_db, _rollout = fake_goal_store(tmp_path, 'zcodex')
    now = [0.0]
    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'list':
            return [row]
        if argv[1] == 'send':
            return delivery_receipt(row)
        raise AssertionError(f'unexpected native operation: {argv[1:3]}')

    with pytest.raises(GoalRegistrationError, match='goal_registration_failed'):
        register_goal(
            'aplexer', native_id=row['id'], tag=row['tag'], workspace=row['workspace'],
            engine='zcodex', goal_command='/goal Fix the test', runner=runner,
            timeout_s=1.5, progress=type('NoStore', (), {'locate': lambda *_: None})(),
            monotonic=lambda: now[0], sleep=lambda delay: now.__setitem__(0, now[0] + delay),
        )
    assert sum(argv[1] == 'send' for argv in calls) == 1


def test_engine_without_goal_support_is_refused_before_native_effect():
    calls = []
    with pytest.raises(GoalRegistrationError):
        register_goal('aplexer', native_id='native-123', tag='hub-offer', workspace='/workspace/demo',
                      engine='gemini', goal_command='/goal Fix it', runner=lambda *args, **kwargs: calls.append(args))
    assert calls == []

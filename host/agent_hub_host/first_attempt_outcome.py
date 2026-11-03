"""Normalized first-attempt outcome ledger (intake 95fd7f1a).

Unclassified session failures pile up in a residual ``other`` bucket because
each layer (dispatch, launcher, runtime, stall, finish report) records its own
signal in its own shape, and a retry success erases the initial cause. This
module normalizes one outcome row per qualifying *first* attempt:

- session/intake identity, lifecycle state, safe reason code,
  launch/dispatch stage, engine family, report receipt, timestamps;
- retry linkage (later sessions in the same intake group) preserved on the
  first-attempt row, so a retry success never erases the initial cause;
- a per-cause count + examples report for a selected Berlin-day window.

Privacy: only Hub UUIDs, fixed reason/stage/engine enums, bounded integers
and booleans ever cross into an outcome row. Free-form text (tails, logs,
reports, prompts, paths, credential shapes) is never copied.
"""
import datetime as _dt
import re as _re

SCHEMA = 'first_attempt_outcome/v1'

UUID_RE = _re.compile(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', _re.IGNORECASE)

# Lifecycle states that qualify on their own (failed/cancelled/held/refused
# per the RCA inventory, plus the derived stalled projection).
QUALIFYING_STATES = frozenset({'failed', 'cancelled', 'held', 'refused', 'stalled'})

# Terminal-ish host launch-journal states stamped at persist time.
QUALIFYING_LAUNCH_STATES = frozenset({
    'failed', 'exited', 'held', 'claim_refused', 'dispatch_refused', 'cancelled',
})

ENGINES = frozenset({'codex', 'zcodex', 'opencode', 'go', 'antigravity', 'claude', 'gemini', 'shell'})

ENGINE_FAMILIES = {
    'codex': 'codex', 'zcodex': 'codex',
    'opencode': 'opencode', 'go': 'opencode',
    'antigravity': 'antigravity', 'claude': 'claude',
    'gemini': 'gemini', 'shell': 'shell',
}

MODES = frozenset({'headless', 'interactive'})

REPORT_STATES = frozenset({'valid_report_seen', 'invalid_or_missing', 'not_observed'})

# Canonical safe reason codes. Each maps to (stage, cause bucket) for the
# per-cause report. Reasons come from dispatch_diagnostics, launch_diagnostics,
# runtime_diagnostics, session_stall and the finish-report overlay; anything
# else collapses to 'other' (still counted, never raw text).
REASONS = {
    # Dispatch / claim failures (stage dispatch_route).
    'dispatch_local_command_failed': ('dispatch_route', 'dispatch_failure'),
    'dispatch_claim_refused': ('dispatch_route', 'dispatch_failure'),
    'dispatch_worktree_refused': ('dispatch_route', 'dispatch_failure'),
    'dispatch_route_unconfigured': ('dispatch_route', 'dispatch_failure'),
    'dispatch_startup_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_cwd_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_goal_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_prompt_unsupported': ('dispatch_route', 'dispatch_failure'),
    'dispatch_offer_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_capability_unavailable': ('dispatch_route', 'dispatch_failure'),
    'dispatch_internal_error': ('dispatch_route', 'dispatch_failure'),
    'dispatch_value_error': ('dispatch_route', 'dispatch_failure'),
    'dispatch_result_path_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_result_invalid': ('dispatch_route', 'dispatch_failure'),
    'dispatch_credential_unavailable': ('dispatch_route', 'dispatch_failure'),
    'claim_timeout': ('dispatch_route', 'dispatch_failure'),
    'workspace_unavailable': ('dispatch_route', 'dispatch_failure'),
    'repo_clone_failed': ('dispatch_route', 'dispatch_failure'),
    'repo_clone_auth_failed': ('dispatch_route', 'dispatch_failure'),
    'repo_not_found': ('dispatch_route', 'dispatch_failure'),
    'native_profile_mismatch': ('dispatch_route', 'dispatch_failure'),
    'native_account_mismatch': ('dispatch_route', 'dispatch_failure'),
    'native_lifecycle_incomplete': ('dispatch_route', 'dispatch_failure'),
    'native_execution_unconfirmed': ('dispatch_route', 'dispatch_failure'),
    'native_receipt_mismatch': ('dispatch_route', 'dispatch_failure'),
    'native_cwd_mismatch': ('dispatch_route', 'dispatch_failure'),
    'native_session_identity_invalid': ('dispatch_route', 'dispatch_failure'),
    'placement_unknown': ('dispatch_route', 'dispatch_failure'),
    'dispatch_refused': ('dispatch_route', 'dispatch_failure'),
    # Launcher failures (stage launcher_run).
    'launcher_command_failed': ('launcher_run', 'dispatch_failure'),
    'launcher_model_unsupported': ('launcher_run', 'model_unsupported'),
    'launcher_plan_refused': ('launcher_run', 'dispatch_failure'),
    'launcher_admission_refused': ('launcher_run', 'dispatch_failure'),
    'launcher_quota_gate_refused': ('launcher_run', 'quota_blocked'),
    'launcher_worktree_lock_failed': ('launcher_run', 'dispatch_failure'),
    'launcher_worktree_identity_failed': ('launcher_run', 'dispatch_failure'),
    'launcher_not_started': ('launcher_run', 'dispatch_failure'),
    'launcher_unknown_profile': ('launcher_run', 'dispatch_failure'),
    'launcher_unknown_engine': ('launcher_run', 'dispatch_failure'),
    'launcher_binary_missing': ('launcher_run', 'dispatch_failure'),
    'launcher_codex_wrapper_unavailable': ('launcher_run', 'dispatch_failure'),
    'launcher_worker_auth_unavailable': ('launcher_run', 'auth_expired'),
    'launcher_host_path_unavailable': ('launcher_run', 'dispatch_failure'),
    'launcher_receipt_mismatch': ('launcher_run', 'dispatch_failure'),
    'launcher_cwd_mismatch': ('launcher_run', 'dispatch_failure'),
    'launcher_model_mismatch': ('launcher_run', 'model_unsupported'),
    'launcher_tmpdir_unavailable': ('launcher_run', 'dispatch_failure'),
    'launcher_disk_reserve': ('launcher_run', 'memory_pressure'),
    'launcher_memory_reserve': ('launcher_run', 'memory_pressure'),
    'launcher_worker_memory': ('launcher_run', 'memory_pressure'),
    # Headless engine exits (stage runtime).
    'engine_exit_code': ('runtime', 'headless_exit'),
    'engine_signal_exit': ('runtime', 'headless_exit'),
    'engine_exited_without_result': ('runtime', 'headless_exit'),
    'engine_oom_killed': ('runtime', 'headless_exit'),
    'engine_runtime_timeout': ('runtime', 'headless_exit'),
    'engine_children_running': ('runtime', 'headless_exit'),
    'engine_auth_expired': ('runtime', 'auth_expired'),
    'engine_tool_permission_refused': ('runtime', 'headless_exit'),
    'engine_interactive_prompt': ('runtime', 'headless_exit'),
    # Model support (stage dispatch_route).
    'model_unsupported': ('dispatch_route', 'model_unsupported'),
    'model_unverified': ('dispatch_route', 'model_unsupported'),
    'goal_unsupported': ('dispatch_route', 'goal_unsupported'),
    'goal_startup_unconfigured': ('dispatch_route', 'goal_unsupported'),
    # Stall Penciled by the native-progress classifier (stage stall).
    'hung_tool_call': ('stall', 'hung_call'),
    'no_native_progress': ('stall', 'hung_call'),
    'agent_exited': ('stall', 'hung_call'),
    'agent_exited_children_running': ('stall', 'hung_call'),
    'codex_reconnect_failed': ('stall', 'hung_call'),
    'zcodex_agent_exited': ('stall', 'hung_call'),
    'zcodex_reconnect_failed': ('stall', 'hung_call'),
    'goal_rejected': ('stall', 'hung_call'),
    # Capacity / quota / host pressure (stage dispatch_route).
    'host_overload': ('dispatch_route', 'memory_pressure'),
    'host_load_high': ('dispatch_route', 'memory_pressure'),
    'host_oom': ('dispatch_route', 'memory_pressure'),
    'host_runtime_choking': ('dispatch_route', 'memory_pressure'),
    'memory_pressure': ('dispatch_route', 'memory_pressure'),
    'cpu_headroom': ('dispatch_route', 'memory_pressure'),
    'cpu_pressure': ('dispatch_route', 'memory_pressure'),
    'cpu_usage_unknown': ('dispatch_route', 'memory_pressure'),
    'admission_refused': ('dispatch_route', 'quota_blocked'),
    'admission_unconfigured': ('dispatch_route', 'quota_blocked'),
    'admission_quota_unverified': ('dispatch_route', 'quota_blocked'),
    'account_blocked': ('dispatch_route', 'quota_blocked'),
    'account_unknown': ('dispatch_route', 'quota_blocked'),
    'account_capacity': ('dispatch_route', 'quota_blocked'),
    'account_concurrency': ('dispatch_route', 'quota_blocked'),
    'codex_quota_gate_quota_unavailable': ('dispatch_route', 'quota_blocked'),
    'codex_quota_gate_codex_reserve_required': ('dispatch_route', 'quota_blocked'),
    'codex_quota_gate_stale_window_evidence': ('dispatch_route', 'quota_blocked'),
    'codex_quota_gate_quota_check_failed': ('dispatch_route', 'quota_blocked'),
    'not_logged_in': ('dispatch_route', 'auth_expired'),
    # Finish-report overlay (stage report).
    'missing_report': ('report', 'missing_report'),
    # Bare lifecycle fallbacks (stage lifecycle).
    'state_failed': ('lifecycle', 'other'),
    'state_cancelled': ('lifecycle', 'other'),
    'state_held': ('lifecycle', 'other'),
    'state_refused': ('lifecycle', 'other'),
    'state_stopped': ('lifecycle', 'other'),
    'state_stalled': ('lifecycle', 'other'),
    'other': ('lifecycle', 'other'),
}

CAUSE_ORDER = ('dispatch_failure', 'headless_exit', 'model_unsupported', 'hung_call',
               'missing_report', 'quota_blocked', 'auth_expired', 'goal_unsupported',
               'memory_pressure', 'other')


def _as_uuid(value):
    if isinstance(value, str) and UUID_RE.fullmatch(value.strip()):
        return value.strip().lower()
    return None


def _as_int(value):
    return value if type(value) is int and abs(value) <= 9007199254740991 else None


def _as_ms(value):
    """Accept epoch millis ints or ISO-8601 strings; anything else is None."""
    if type(value) is int and 0 < value <= 9007199254740991:
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = _dt.datetime.fromisoformat(value.strip().replace('Z', '+00:00'))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        try:
            return int(parsed.timestamp() * 1000)
        except (OverflowError, OSError, ValueError):
            return None
    return None


def _berlin_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo('Europe/Berlin')
    except Exception:
        # CET (UTC+1) covers the Oct/Nov RCA windows; never raise.
        return _dt.timezone(_dt.timedelta(hours=1))


def berlin_day_range(day):
    """UTC millisecond window [start, end) for a Berlin calendar day."""
    if not isinstance(day, str) or not _re.fullmatch(r'\d{4}-\d{2}-\d{2}', day):
        raise ValueError('day must be YYYY-MM-DD')
    year, month, dom = int(day[0:4]), int(day[5:7]), int(day[8:10])
    tz = _berlin_tz()
    try:
        start = _dt.datetime(year, month, dom, tzinfo=tz)
    except ValueError:
        raise ValueError('day must be YYYY-MM-DD')
    end = start + _dt.timedelta(days=1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


def berlin_day(ms):
    """Berlin calendar day for epoch millis, or None."""
    if type(ms) is not int:
        return None
    try:
        return _dt.datetime.fromtimestamp(ms / 1000, tz=_berlin_tz()).strftime('%Y-%m-%d')
    except (OverflowError, OSError, ValueError):
        return None


def engine_family(engine):
    """Fixed engine family; unknown engines collapse (never forwarded raw)."""
    if isinstance(engine, str):
        return ENGINE_FAMILIES.get(engine.strip().lower(), 'unknown')
    return 'unknown'


def safe_engine(engine):
    if isinstance(engine, str) and engine.strip().lower() in ENGINES:
        return engine.strip().lower()
    return 'unknown'


def safe_mode(mode):
    if isinstance(mode, str) and mode.strip().lower() in MODES:
        return mode.strip().lower()
    return 'unknown'


def normalize_reason(raw):
    """Collapse any raw reason/diagnostic text to a fixed safe code."""
    if not isinstance(raw, str):
        return 'other'
    text = raw.strip()
    if text in REASONS:
        return text
    # Diagnostics sometimes prefix the slug ("model_unsupported: ...").
    head = text.split(':', 1)[0].strip()
    if head in REASONS:
        return head
    lowered = text.lower()
    for slug in REASONS:
        if slug in lowered and slug not in ('other',):
            # Substring fallback only for unambiguous multi-word slugs.
            if len(slug) > 12:
                return slug
    return 'other'


def _raw_reason(session):
    """First fixed-looking signal across the known session shapes."""
    if not isinstance(session, dict):
        return None
    for key in ('failure_reason', 'exit_reason', 'stall_reason'):
        value = session.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != 'waiting_until':
            return value.strip()
    for key in ('reason',):
        value = session.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    diagnostic = session.get('diagnostic')
    if isinstance(diagnostic, dict) and isinstance(diagnostic.get('reason'), str):
        return diagnostic['reason'].strip()
    launch_error = session.get('launch_error')
    if isinstance(launch_error, dict) and isinstance(launch_error.get('reason'), str):
        return launch_error['reason'].strip()
    rejected = session.get('rejected')
    if isinstance(rejected, list):
        for entry in rejected:
            if isinstance(entry, dict) and isinstance(entry.get('reason'), str) and entry['reason'].strip():
                return entry['reason'].strip()
    runtime = session.get('runtime')
    if isinstance(runtime, dict):
        for key in ('reason', 'engine_outcome'):
            if isinstance(runtime.get(key), str) and runtime[key].strip():
                return runtime[key].strip()
    return None


def report_state(session):
    """Finish-report receipt: seen / explicitly missing / not observed."""
    if not isinstance(session, dict):
        return 'not_observed'
    if session.get('worker_report_received') is True:
        return 'valid_report_seen'
    status = session.get('report_status')
    if status == 'done':
        return 'valid_report_seen'
    if status in ('failed', 'missing', 'invalid'):
        return 'invalid_or_missing'
    finish = session.get('finish_report')
    if finish == 'valid_report_seen':
        return 'valid_report_seen'
    if finish == 'invalid_or_missing' or session.get('worker_report_received') is False:
        return 'invalid_or_missing'
    if session.get('worker_status') == 'done':
        return 'valid_report_seen'
    return 'not_observed'


def lifecycle_state(session):
    """Effective lifecycle state, honoring the derived stalled projection."""
    if not isinstance(session, dict):
        return 'unknown'
    raw = session.get('state')
    state = raw.strip().lower() if isinstance(raw, str) else ''
    if state == 'running' and session.get('stalled') is True:
        return 'stalled'
    if state in ('failed', 'cancelled', 'held', 'refused', 'stalled',
                 'stopped', 'exited', 'done', 'running', 'queued',
                 'offered', 'starting', 'uncertain'):
        return state
    return 'unknown'


def is_qualifying(session):
    """A first-attempt ledger row is emitted when the session failed, was
    cancelled, is held/refused/stalled, carries a failure/stall signal, or
    has an explicitly invalid/missing finish report. A pending scheduled
    wake (waiting on an external event) is never a failure."""
    if not isinstance(session, dict):
        return False
    if session.get('waiting_until'):
        return False
    state = lifecycle_state(session)
    if state in QUALIFYING_STATES:
        return True
    if state in ('stopped', 'exited', 'failed'):
        if _raw_reason(session) is not None:
            return True
        if report_state(session) == 'invalid_or_missing':
            return True
    return False


def classify(session):
    """Normalized (reason_code, stage, cause) for one session dict."""
    raw = _raw_reason(session)
    code = normalize_reason(raw) if raw is not None else 'other'
    if code == 'other' and report_state(session) == 'invalid_or_missing':
        # No layer named a cause, but the finish report is explicitly
        # missing: that is the actionable residual signal.
        code = 'missing_report'
    if code == 'other':
        state = lifecycle_state(session)
        fallback = 'state_' + state
        code = fallback if fallback in REASONS else 'other'
    stage, cause = REASONS[code]
    return code, stage, cause


def _session_id(session):
    for key in ('id', 'session_id'):
        found = _as_uuid(session.get(key))
        if found:
            return found
    return None


def _intake_id(session):
    for key in ('intake_id', 'intake_task_id', 'linked_intake'):
        found = _as_uuid(session.get(key))
        if found:
            return found
    return None


def _started_ms(session):
    for key in ('created_at', 'started_at', 'started_at_ms', 'created_at_ms'):
        found = _as_ms(session.get(key))
        if found is not None:
            return found
    return None


def _ended_ms(session):
    for key in ('updated_at', 'finished_at', 'finished_at_ms', 'ended_at', 'ended_at_ms'):
        found = _as_ms(session.get(key))
        if found is not None:
            return found
    return None


def group_sessions(sessions):
    """Group by intake (unlinked sessions stand alone), oldest first.

    Returns {group_key: [session, ...]} with a stable oldest-first order so
    index 0 is the first attempt. Sessions without an id are dropped (they
    cannot be evidenced or retried).
    """
    groups = {}
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        sid = _session_id(session)
        if sid is None:
            continue
        intake = _intake_id(session)
        key = intake if intake is not None else 'unlinked:' + sid
        groups.setdefault(key, []).append(session)
    for key in groups:
        groups[key].sort(key=lambda s: (_started_ms(s) or 0, _session_id(s) or ''))
    return groups


def build_outcome(first, group):
    """One normalized outcome row for a group's first session.

    `group` is the oldest-first session list; retry linkage (later sessions)
    is preserved on the row so retry success never erases the initial cause.
    """
    sid = _session_id(first) or 'unknown'
    intake = _intake_id(first)
    code, stage, cause = classify(first)
    state = lifecycle_state(first)
    started = _started_ms(first)
    retries = []
    retry_states = []
    for later in group[1:9]:
        lid = _session_id(later)
        if lid is None or lid == sid:
            continue
        retries.append(lid)
        retry_states.append(lifecycle_state(later))
    return {
        'schema': SCHEMA,
        'session_id': sid,
        'intake_id': intake,
        'attempt_index': 1,
        'group_size': len(group),
        'lifecycle_state': state,
        'reason_code': code,
        'stage': stage,
        'cause': cause,
        'engine_family': engine_family(first.get('engine')),
        'engine': safe_engine(first.get('engine')),
        'mode': safe_mode(first.get('mode')),
        'report': report_state(first),
        'retry_session_ids': retries,
        'retry_states': retry_states,
        'started_at_ms': started,
        'ended_at_ms': _ended_ms(first),
        'berlin_day': berlin_day(started) if started is not None else None,
    }


def build_first_attempt_outcomes(sessions):
    """Emit one outcome row per qualifying first attempt (AC2).

    Only groups whose oldest session qualifies are emitted; the row keeps
    the retry linkage so later success does not erase the initial cause.
    """
    outcomes = []
    for _key, group in group_sessions(sessions).items():
        if not group:
            continue
        if is_qualifying(group[0]):
            outcomes.append(build_outcome(group[0], group))
    outcomes.sort(key=lambda o: (o['started_at_ms'] or 0, o['session_id']))
    return outcomes


def summarize_by_cause(outcomes, examples_per_cause=3):
    """Per-cause counts plus bounded example rows (AC5 building block)."""
    counts = {}
    examples = {}
    for outcome in outcomes:
        cause = outcome.get('cause', 'other')
        if cause not in {c for _s, c in REASONS.values()}:
            cause = 'other'
        counts[cause] = counts.get(cause, 0) + 1
        if len(examples.get(cause, [])) < max(0, examples_per_cause):
            examples.setdefault(cause, []).append({
                'session_id': outcome.get('session_id'),
                'intake_id': outcome.get('intake_id'),
                'lifecycle_state': outcome.get('lifecycle_state'),
                'reason_code': outcome.get('reason_code'),
                'stage': outcome.get('stage'),
                'engine_family': outcome.get('engine_family'),
                'mode': outcome.get('mode'),
                'report': outcome.get('report'),
            })
    ordered = [cause for cause in CAUSE_ORDER if cause in counts]
    ordered += sorted(c for c in counts if c not in CAUSE_ORDER)
    return [{'cause': cause, 'count': counts[cause],
             'examples': examples.get(cause, [])} for cause in ordered]


def report_for_day(sessions, day, examples_per_cause=3):
    """Query/report for one Berlin-day window (AC5).

    Only first attempts whose Berlin start day falls inside the window are
    counted. Sessions without a parseable start timestamp are excluded from
    the day inventory (counted as `undated_sessions`, never guessed).
    """
    start_ms, end_ms = berlin_day_range(day)
    in_day = []
    undated = 0
    for session in sessions or []:
        if not isinstance(session, dict):
            continue
        started = _started_ms(session)
        if started is None:
            undated += 1
            continue
        if start_ms <= started < end_ms:
            in_day.append(session)
    groups = group_sessions(in_day)
    outcomes = []
    for group in groups.values():
        if group and is_qualifying(group[0]):
            outcomes.append(build_outcome(group[0], group))
    outcomes.sort(key=lambda o: (o['started_at_ms'] or 0, o['session_id']))
    import datetime as _dt2
    return {
        'schema': SCHEMA,
        'day': day,
        'window_utc': {
            'start': _dt2.datetime.fromtimestamp(start_ms / 1000, tz=_dt2.timezone.utc).isoformat(),
            'end': _dt2.datetime.fromtimestamp(end_ms / 1000, tz=_dt2.timezone.utc).isoformat(),
        },
        'sessions_in_day': len(in_day),
        'first_attempts_in_day': sum(1 for group in groups.values() if group),
        'qualifying_first_attempts': len(outcomes),
        'undated_sessions': undated,
        'causes': summarize_by_cause(outcomes, examples_per_cause),
    }


def stamp_launch_record(record):
    """Attach durable per-attempt outcome evidence to a host launch record.

    Called on the fleet persist path for terminal-ish states; never raises,
    never copies free-form text, and never overwrites an existing stamp from
    a newer revision (persist() merges before writing, so the freshest stamp
    wins by construction).
    """
    try:
        if not isinstance(record, dict):
            return record
        if record.get('state') not in QUALIFYING_LAUNCH_STATES:
            return record
        diagnostic = record.get('diagnostic')
        raw = diagnostic.get('reason') if isinstance(diagnostic, dict) else None
        if not isinstance(raw, str):
            raw = record.get('state')
        code = normalize_reason(raw)
        if code == 'other' and record.get('worker_report_received') is False:
            code = 'missing_report'
        stage, cause = REASONS.get(code, REASONS['other'])
        report = 'invalid_or_missing' if record.get('worker_report_received') is False else (
            'valid_report_seen' if record.get('worker_report_received') is True else 'not_observed')
        record['first_attempt_outcome'] = {
            'schema': SCHEMA,
            'reason_code': code,
            'stage': stage,
            'cause': cause,
            'engine_family': engine_family(record.get('engine')),
            'mode': safe_mode(record.get('mode')),
            'report': report,
            'retry_id': record.get('retry_id') if isinstance(record.get('retry_id'), str) else None,
            'superseded_by': record.get('superseded_by') if isinstance(record.get('superseded_by'), str) else None,
        }
        return record
    except Exception:
        return record

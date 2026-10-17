"""Maintained host status runtime: native collection + probes in one rate-limited step.

Owns ONLY the runtime wiring. Classification stays in
:mod:`agent_hub_host.status_collect` (``collect_once``/``classify_session``),
probe evidence in :mod:`agent_hub_host.status_probe` (``collect_probes``),
native access in :mod:`agent_hub_host.native_sessions` (``list_sessions``),
and lifecycle text hygiene in :mod:`agent_hub_host.session_lifecycle`.
This module adds no timer, controller, store, or classifier: :meth:`StatusCollector.step`
is a synchronous, rate-limited, isolated-error round that head (or the
``--once`` CLI) calls. Head wires it into the existing session_host loop;
this module never touches peer files.

ABI (stable for head):
    collector = StatusCollector(
        allowlist_path=None,   # explicit trusted path (preferred)
        allowlist=None,        # dict override (tests); wins over path
        machine_id=None,       # override; default allowlist machine_id
        min_interval_s=60,     # rate-limit floor for step(); 0 disables
        list_sessions_fn=None, # default: native aplexer list via adapter
        probe_runner=None,     # passed to collect_probes (fake in tests)
        submit_fn=None,        # default: status_collect.submit
        time_fn=None,          # default: time.monotonic
    )
    result = collector.step(force=False, submit=False, prepared_sessions=None)

    - ``allowlist_path`` explicit is preferred. When omitted, resolution is:
      ``$HUB_STATUS_COLLECT_ALLOWLIST`` then ``<cwd>/.local/session/
      status-allowlist.json`` then ``<cwd>/journal/status-allowlist.json``.
      Absent/invalid allowlist fails closed (no collection, error recorded).
    - ``prepared_sessions`` retains ``--sessions-json`` compatibility: a list
      used INSTEAD of the native list. ``None`` (default) means real native
      collection through the existing adapter.
    - ``step`` never raises for collection failures: every sub-phase is
      isolated and recorded in ``errors``/``skipped``. ``rate_limited`` is True
      when the call was skipped by ``min_interval_s`` (use ``force=True``).
    - Return keys: ``machine_id, observed_at, observations, skipped, errors,
      sender, rate_limited, submit``. ``sender`` is the qualified native
      sender binding (``qualify_sender_binding``) or ``None`` with an error
      when unbound; collection still proceeds read-only.
    - Reported session states are ``working/waiting/idle`` only when the
      native row carries that EXACT ``agent_state`` (or an explicit True
      boolean in prepared input); ``limit_hit/context_low`` only on explicit
      True markers; anything else is ``unknown``. No synthetic draft/limit/
      finished flags are ever emitted here.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable


def resolve_allowlist_path(explicit: str | None = None) -> str:
    """Resolve the trusted allowlist path. Explicit wins; then env; then siblings."""
    if explicit:
        return explicit
    env = os.environ.get('HUB_STATUS_COLLECT_ALLOWLIST')
    if env:
        return env
    cwd = os.getcwd()
    for candidate in (os.path.join(cwd, '.local', 'session', 'status-allowlist.json'),
                      os.path.join(cwd, 'journal', 'status-allowlist.json')):
        if os.path.isfile(candidate):
            return candidate
    return os.path.join(cwd, '.local', 'session', 'status-allowlist.json')


def utc_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def native_to_entry(item: Any) -> dict | None:
    """Project one native row to a collect_once entry. None when identity missing.

    Honest mapping only: working/waiting/idle set ONLY on exact agent_state;
    limit/context only on explicit True booleans (prepared input); never
    draft/finished synthesis; no free-text inference.
    """
    if item is None:
        return None
    # NativeSession dataclass (preferred) or raw dict (prepared/native JSON).
    get = (lambda k: getattr(item, k, None)) if not isinstance(item, dict) else item.get
    sid = get('session_id') or get('native_id') or get('id')
    ws = get('workspace')
    if not sid or not ws:
        return None
    entry: dict = {'native_id': str(sid), 'workspace': str(ws)}
    agent_state = get('agent_state')
    if isinstance(agent_state, str) and agent_state in ('working', 'waiting', 'idle'):
        entry[agent_state] = True
    for key, marker in (('limit_exhausted', 'limit_hit'), ('context_low', 'context_low')):
        value = item.get(key) if isinstance(item, dict) else getattr(item, key, None)
        if value is True:
            entry[key] = True
    # Prepared input may carry an explicit working/waiting/idle boolean set;
    # honor ONLY explicit True (never strings/phase guesses).
    if isinstance(item, dict):
        for key in ('working', 'waiting', 'idle'):
            if item.get(key) is True:
                entry[key] = True
        note = item.get('note')
        if isinstance(note, str) and note.strip():
            entry['note'] = note
    return entry


class StatusCollector:
    """One rate-limited collection round: native sessions + allowlisted reports + probes."""

    def __init__(self, allowlist_path: str | None = None, allowlist: dict | None = None,
                 machine_id: str | None = None, min_interval_s: int = 60,
                 list_sessions_fn: Callable | None = None,
                 probe_runner: Callable | None = None,
                 submit_fn: Callable | None = None,
                 time_fn: Callable | None = None) -> None:
        self.allowlist_path = allowlist_path
        self.allowlist = allowlist
        self.machine_id_override = machine_id
        try:
            self.min_interval_s = max(0, min(int(min_interval_s), 3600))
        except (TypeError, ValueError):
            self.min_interval_s = 60
        self.list_sessions_fn = list_sessions_fn
        self.probe_runner = probe_runner
        self.submit_fn = submit_fn
        self.time_fn = time_fn or time.monotonic
        self._last_step: float | None = None

    def _load_allowlist(self) -> tuple[dict | None, str | None]:
        from .status_collect import load_allowlist
        if self.allowlist is not None:
            if not isinstance(self.allowlist, dict):
                return None, 'invalid_allowlist'
            return self.allowlist, None
        path = resolve_allowlist_path(self.allowlist_path)
        try:
            return load_allowlist(path), None
        except (OSError, ValueError) as exc:
            return None, 'allowlist_unreadable:' + type(exc).__name__

    def _list_native(self) -> tuple[list | None, str | None]:
        fn = self.list_sessions_fn
        if fn is None:
            from . import native_sessions as _ns
            fn = _ns.list_sessions
        try:
            rows = fn()
        except TypeError:
            # Adapter signature list_sessions(executable=..., workspace=..., ...)
            # with no required args; retry with no args already done. Fail closed.
            return None, 'native_list_failed:TypeError'
        except Exception as exc:  # noqa: BLE001 - isolated, recorded
            return None, 'native_list_failed:' + type(exc).__name__
        if not isinstance(rows, list):
            return None, 'native_list_failed:non_list'
        return rows, None

    def step(self, force: bool = False, submit: bool = False,
             prepared_sessions: list | None = None) -> dict:
        from .status_collect import collect_once

        now = self.time_fn()
        if not force and self._last_step is not None and self.min_interval_s > 0:
            if now - self._last_step < self.min_interval_s:
                return {'machine_id': None, 'observed_at': utc_now(), 'observations': [],
                        'skipped': [], 'errors': ['rate_limited'], 'sender': None,
                        'rate_limited': True, 'submit': None}
        self._last_step = now

        errors: list = []
        allowlist, allow_err = self._load_allowlist()
        if allowlist is None:
            return {'machine_id': None, 'observed_at': utc_now(), 'observations': [],
                    'skipped': [], 'errors': [allow_err or 'allowlist_unreadable'],
                    'sender': None, 'rate_limited': False, 'submit': None}
        machine_id = self.machine_id_override or allowlist.get('machine_id')
        if not machine_id:
            return {'machine_id': None, 'observed_at': utc_now(), 'observations': [],
                    'skipped': [], 'errors': ['allowlist_missing_machine_id'],
                    'sender': None, 'rate_limited': False, 'submit': None}
        machine_id = str(machine_id)
        observed_at = utc_now()

        # Sender identity (read-only whoami via existing adapter). Unbound is
        # recorded, never forged; collection proceeds read-only regardless.
        sender: dict | None = None
        try:
            from . import native_sessions as _ns
            binding = _ns.qualify_sender_binding('aplexer')
            sender = {'session_id': binding.session_id, 'tag': binding.tag,
                      'workspace': binding.workspace, 'qualified': True}
        except Exception as exc:  # noqa: BLE001 - isolated
            errors.append('sender_unbound:' + type(exc).__name__)
            sender = None

        # Sessions: prepared JSON (compat) or real native list via adapter.
        if prepared_sessions is not None:
            sessions_input = prepared_sessions
        else:
            rows, list_err = self._list_native()
            if rows is None:
                errors.append(list_err or 'native_list_failed')
                sessions_input = []
            else:
                sessions_input = []
                for row in rows[:500]:
                    try:
                        entry = native_to_entry(row)
                    except Exception:  # noqa: BLE001 - one bad row never kills the round
                        continue
                    if entry is not None:
                        sessions_input.append(entry)

        try:
            result = collect_once(sessions_input, allowlist, machine_id, observed_at)
            observations = result.get('observations', [])
            skipped = result.get('skipped', [])
        except Exception as exc:  # noqa: BLE001 - isolated
            errors.append('collect_failed:' + type(exc).__name__)
            observations, skipped = [], []

        # Probes: explicit configured probes only (allowlist repo_roots + probes).
        try:
            from .status_probe import collect_probes
            probes = allowlist.get('probes') or []
            repo_roots = allowlist.get('repo_roots') or []
            if isinstance(probes, list) and probes and isinstance(repo_roots, list) and repo_roots:
                spec = {'machine_id': machine_id, 'observed_at': observed_at,
                        'repo_roots': [str(r) for r in repo_roots], 'probes': probes}
                probe_result = collect_probes(spec, self.probe_runner)
                observations = (observations + probe_result.get('observations', []))
                skipped = (skipped + probe_result.get('skipped', []))
        except Exception as exc:  # noqa: BLE001 - isolated
            errors.append('probes_failed:' + type(exc).__name__)

        submit_result = None
        if submit:
            try:
                fn = self.submit_fn
                if fn is None:
                    from .status_collect import submit as _submit
                    fn = _submit
                from . import api, auth, config
                cfg = config.load_config()
                base = (os.environ.get('HUB_URL') or os.environ.get('HUB_BASE_URL')
                        or cfg.get('base_url'))
                api.check_url(base)
                token = (os.environ.get('HUB_TOKEN') or os.environ.get('HUB_JOB_TOKEN')
                         or auth.get_access_token())
                submit_result = fn(observations, base, token)
            except Exception as exc:  # noqa: BLE001 - isolated, no secrets
                errors.append('submit_failed:' + type(exc).__name__)
                submit_result = None

        return {'machine_id': machine_id, 'observed_at': observed_at,
                'observations': observations, 'skipped': skipped, 'errors': errors,
                'sender': sender, 'rate_limited': False, 'submit': submit_result}

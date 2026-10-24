"""Observe the exact stop receipt; an old terminal session row is insufficient."""
import re


def wait_for_confirmed_stop(session_ref, command_id, timeout, interval, *, wait_command, request):
    try:
        entry = wait_command(session_ref, command_id, timeout, interval)
    except ValueError as exc:
        reason = getattr(exc, 'code', 'stop_host_unreachable')
        raise ValueError(f'{reason}; command {command_id}; reconcile with commands before retrying') from None
    ack = entry.get('ack') or {}
    if entry.get('state') != 'acked' or ack.get('outcome') != 'delivered' or ack.get('confirmed_exit') is not True:
        reason = ack.get('reason') or entry.get('reason')
        if not isinstance(reason, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,63}', reason):
            reason = 'stop_' + str(entry.get('state', 'unconfirmed'))
        raise ValueError(f'{reason}; command {command_id}; stop was not confirmed')
    view = request('GET', '/api/sessions/' + session_ref)
    if view.get('state') != 'stopped':
        raise ValueError(f'stop_state_unconfirmed; command {command_id}; reconcile with commands')
    return view

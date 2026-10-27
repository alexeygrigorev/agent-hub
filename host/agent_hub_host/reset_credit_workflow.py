"""Operator workflow connecting actual Hub decisions to native reset receipts.

No fallback fabricates Hub evidence. A failed prepare never reaches redemption.
After a provider response, a failed Hub write is retried from the native journal.
"""
import argparse
import hashlib
import json
import time
from . import codex_reset_credit as native
from . import session_control

PATH = '/api/quota/reset-credits'


def _event(response):
    if not isinstance(response, dict) or not isinstance(response.get('decision'), dict) or not response.get('event_id'):
        raise native.CreditError('hub_decision_unavailable')
    return response


def prepare(rpc, hub, account, credit_id):
    before = native.snapshot(rpc.call(native.READ))
    native._eligible(before, credit_id, int(time.time()))
    credit = native._credit(before, credit_id)
    event = _event(hub('POST', PATH, {'action': 'prepare', 'account': account,
        'credit_id': credit_id, 'provider_account_id': before['accountId'],
        'expires_at_ms': credit['expiresAt'] * 1000,
        'observed_at_ms': before['observedAt'] * 1000, 'available_count': before['availableCount']}))
    decision = event['decision']
    if (decision.get('account') != account or decision.get('credit_id') != credit_id or
            decision.get('provider_account_id') != before['accountId'] or
            decision.get('expires_at_ms') != credit['expiresAt'] * 1000):
        raise native.CreditError('hub_credit_identity_mismatch')
    return event


def _get_event(hub, event_id):
    native._identifier(event_id)
    response = hub('GET', PATH + '?event_id=' + event_id)
    events = response.get('events', []) if isinstance(response, dict) else []
    if len(events) != 1 or events[0].get('event_id') != event_id:
        raise native.CreditError('hub_decision_unavailable')
    return _event(events[0])


def consume(rpc, hub, event_id, journal=None):
    event = _get_event(hub, event_id)
    d = event['decision']
    if event.get('consumption_ready') is not True:
        raise native.CreditError('hub_consumption_not_ready')
    now_ms = int(time.time() * 1000)
    if not isinstance(d.get('captured_at_ms'), int) or not 0 <= now_ms - d['captured_at_ms'] <= 300000:
        raise native.CreditError('hub_decision_expired')
    key = native._identifier(d.get('idempotency_key'))
    receipt = native.redeem(rpc, native.journal_path() if journal is None else journal,
                            d.get('provider_account_id'), d.get('credit_id'), idempotency_key=key)
    return record(hub, event_id, receipt)


def record(hub, event_id, receipt):
    """Publish an actual durable receipt, including retry after a Hub outage.

    The server labels this an operator attestation and independently correlates
    stored Hub state. Uploading a file is never provider verification by itself.
    """
    event = _get_event(hub, event_id)
    d = event['decision']
    if (receipt.get('accountId') != d.get('provider_account_id') or receipt.get('creditId') != d.get('credit_id') or
            receipt.get('idempotencyKey') != d.get('idempotency_key')):
        raise native.CreditError('receipt_identity_mismatch')
    before, after = receipt.get('before'), receipt.get('after')
    if not isinstance(before, dict) or (receipt.get('outcome') in ('reset', 'alreadyRedeemed') and not isinstance(after, dict)):
        raise native.CreditError('fresh_provider_read_required')
    selected = native._credit(after, receipt['creditId']) if isinstance(after, dict) else None
    evidence = {'account_id': receipt['accountId'], 'credit_id': receipt['creditId'],
        'idempotency_key': receipt['idempotencyKey'], 'outcome': receipt['outcome'],
        'before_at_ms': before['observedAt'] * 1000, 'after_at_ms': after['observedAt'] * 1000 if after else None,
        'before_count': before['availableCount'], 'after_count': after['availableCount'] if after else None,
        'selected_credit_state': 'unknown' if not after else selected['status'] if selected and selected['status'] in ('available', 'redeemed') else 'unknown' if selected else 'absent',
        'sha256': hashlib.sha256(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode()).hexdigest()}
    return _event(hub('POST', PATH, {'action': 'receipt', 'event_id': event_id, 'receipt': evidence}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare')
    p.add_argument('--account', required=True)
    p.add_argument('--credit-id', required=True)
    p = commands.add_parser('consume')
    p.add_argument('--event-id', required=True)
    p = commands.add_parser('record')
    p.add_argument('--event-id', required=True)
    p.add_argument('--receipt-file', required=True)
    p = commands.add_parser('link-work')
    p.add_argument('--event-id', required=True)
    p.add_argument('--routing-job-id', required=True)
    args = parser.parse_args()
    try:
        hub = session_control.request
        if args.command in ('prepare', 'consume'):
            with native.AppServer() as rpc:
                result = prepare(rpc, hub, args.account, args.credit_id) if args.command == 'prepare' else consume(rpc, hub, args.event_id)
        elif args.command == 'record':
            with open(args.receipt_file, 'rb') as file:
                data = file.read(65537)
            if len(data) > 65536:
                raise native.CreditError('receipt_too_large')
            result = record(hub, args.event_id, json.loads(data))
        else:
            result = hub('POST', PATH, {'action': 'link-work', 'event_id': args.event_id, 'routing_job_id': args.routing_job_id})
        print(json.dumps(result, sort_keys=True))
        if args.command == 'consume':
            r = result.get('receipt') or {}
            if (r.get('outcome') not in ('reset', 'alreadyRedeemed') or type(r.get('after_count')) is not int or
                    r['after_count'] >= r.get('before_count', 0) or r.get('selected_credit_state') not in ('absent', 'redeemed')):
                return 2
        return 0
    except Exception:
        # HTTP/provider exception bodies can contain private data. Never print them.
        print(json.dumps({'error': 'reset_workflow_failed_closed', 'provider_outcome_may_require_journal_reconciliation': args.command == 'consume'}))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

"""Publish complete native censuses across an older Hub progress schema."""
from .bus import HttpRequestError


def report_catalog(request, rows):
    """Retry one schema refusal without the two newer advisory progress keys.

    Identity, lifecycle and the complete row set are unchanged. Never retry
    authorization, network or server failures, and never remove other fields
    to make a malformed census pass validation.
    """
    try:
        return request('PUT', '/api/fleet/sessions', {'sessions': rows})
    except HttpRequestError as exc:
        if (exc.http_status != 400 or
                str(exc) != 'HTTP request failed with status 400: invalid_session'):
            raise
        legacy = []
        changed = False
        for row in rows:
            progress = row.get('progress')
            if isinstance(progress, dict) and any(k in progress for k in ('sampled_at', 'goal_status')):
                row = {**row, 'progress': {k: v for k, v in progress.items()
                                         if k not in ('sampled_at', 'goal_status')}}
                changed = True
            legacy.append(row)
        if not changed:
            raise
    return request('PUT', '/api/fleet/sessions', {'sessions': legacy})

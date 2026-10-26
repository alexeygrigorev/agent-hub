"""Fixed pre-start diagnostics. Never forward arbitrary exception text."""
MESSAGES = {
    'workspace_unavailable': 'The requested workspace is not available on this host.',
    'dispatch_route_unconfigured': 'The offered account, engine and profile do not match a local route.',
    'goal_startup_unconfigured': 'This account has no trusted interactive goal startup command.',
    'dispatch_startup_invalid': 'The configured trusted startup command is invalid.',
    'dispatch_cwd_invalid': 'The requested working directory is not confined to the workspace.',
    'dispatch_goal_invalid': 'Goal work requires an interactive Codex-family route and nonempty text.',
    'dispatch_prompt_unsupported': 'Interactive prompt injection requires an explicit goal start.',
    'dispatch_offer_invalid': 'The offered launch or task identifier is invalid.',
    'dispatch_capability_unavailable': 'The required task capabilities are unavailable on this host.',
    'dispatch_local_command_failed': 'A local dispatch prerequisite command failed before native start.',
    'dispatch_internal_error': 'An unexpected host dispatch error occurred before native start.',
    'dispatch_claim_refused': 'The Hub refused the dispatch claim before native start.',
    'model_unsupported': 'The selected model or effort is unsupported by this local route.',
    'admission_unconfigured': 'This local route has no maintained fresh admission adapter.',
    'admission_refused': 'Fresh admission refused this account or could not verify its timestamp and model.',
    'engine_auth_expired': 'The engine authentication is unavailable or expired.',
    'codex_quota_gate_quota_unavailable': 'The managed Codex account quota gate refused launch before execution.',
    'codex_quota_gate_codex_reserve_15_percent_required': 'The managed Codex account is below the 15% weekly reserve.',
    'codex_quota_gate_stale_window_evidence': 'The managed Codex quota evidence is stale.',
    'codex_quota_gate_quota_check_failed': 'The managed Codex quota check failed.',
}
EXACT = {
    'Local account/profile/workspace is not configured': 'dispatch_route_unconfigured',
    'Goal startup requires a trusted local command': 'goal_startup_unconfigured',
    'Invalid trusted startup argv': 'dispatch_startup_invalid',
    'Goal work is interactive only': 'dispatch_goal_invalid',
    'Interactive goal requires a Codex-family engine and goal text': 'dispatch_goal_invalid',
    'Interactive task injection is unsupported; start an empty native session': 'dispatch_prompt_unsupported',
    'Invalid offer id': 'dispatch_offer_invalid', 'Invalid task id': 'dispatch_offer_invalid',
    'Required capabilities are no longer available locally': 'dispatch_capability_unavailable',
    'Offer cwd is not a workspace-relative path': 'dispatch_cwd_invalid',
    'Offer cwd escapes or is malformed': 'dispatch_cwd_invalid',
    'Offer cwd escapes the workspace root': 'dispatch_cwd_invalid',
    'Fresh admission refused': 'admission_refused',
    'Fresh admission timestamp required': 'admission_refused',
    'Selected model/effort requires verified admission': 'admission_refused',
}


def classify(exc, *, workspace_available=True):
    reason = 'dispatch_internal_error'
    if isinstance(exc, ValueError):
        value = str(exc)
        prefix = value.split(':', 1)[0]
        if prefix.startswith('codex_quota_gate_'):
            slug = ''.join(c if (c.isalnum() or c == '_') else '_' for c in prefix)[:64]
            if slug in MESSAGES:
                reason = slug
            else:
                reason = 'codex_quota_gate_quota_unavailable'
        else:
            reason = prefix if prefix in {'model_unsupported', 'admission_unconfigured', 'engine_auth_expired'} else EXACT.get(value, reason)
        if value.startswith('HTTP request failed with status '):
            reason = 'dispatch_claim_refused'
    elif isinstance(exc, (RuntimeError, OSError)):
        reason = 'dispatch_local_command_failed'
    if not workspace_available:
        reason = 'workspace_unavailable'
    return {'reason': reason, 'message': MESSAGES[reason], 'stage': 'dispatch_route',
            'error_type': type(exc).__name__ if type(exc).__name__.isalpha() and len(type(exc).__name__) <= 64 else 'Error'}

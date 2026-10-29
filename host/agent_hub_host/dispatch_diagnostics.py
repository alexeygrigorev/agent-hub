"""Fixed pre-start reasons plus bounded, redacted typed command evidence."""
MESSAGES = {
    'host_overload': 'Host 1-minute load is above the configured multiple of core count; new launches stay queued.',
    'cpu_headroom': 'Host CPU usage plus expected session demand exceeds the CPU target.',
    'cpu_pressure': 'Host CPU pressure exceeds the safety cap.',
    'memory_pressure': 'Host memory full pressure exceeds the safety cap.',
    'host_oom': 'A recent host OOM kill requires a placement cooldown.',
    'host_runtime_choking': 'Recent session or service failures require a placement cooldown.',
    'cpu_usage_unknown': 'Fresh real CPU usage is unavailable.',
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
    'claim_timeout': 'The launch claim timed out before a confirmed response; the launch is held for an idempotent same-host retry.',
    'model_unsupported': 'The selected model or effort is unsupported by this local route.',
    'admission_unconfigured': 'This local route has no maintained fresh admission adapter.',
    'admission_refused': 'Fresh admission refused this account or could not verify its timestamp and model.',
    'engine_auth_expired': 'The engine authentication is unavailable or expired.',
    'codex_quota_gate_quota_unavailable': 'The managed Codex account quota gate refused launch before execution.',
    'codex_quota_gate_codex_reserve_required': 'The managed Codex account is below the configured weekly reserve.',
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
    if isinstance(exc, ClaimRefusedError):
        return dict(exc.diagnostic)
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
            reason = prefix if prefix in {'model_unsupported', 'admission_unconfigured', 'engine_auth_expired', 'host_overload', 'cpu_headroom', 'cpu_pressure', 'cpu_usage_unknown', 'memory_pressure', 'host_oom', 'host_runtime_choking'} else EXACT.get(value, reason)
        if value.startswith('HTTP request failed with status '):
            reason = 'dispatch_claim_refused'
    elif isinstance(exc, (RuntimeError, OSError)):
        reason = 'dispatch_local_command_failed'
    if not workspace_available:
        reason = 'workspace_unavailable'
    value = {'reason': reason, 'message': MESSAGES[reason], 'stage': 'dispatch_route',
            'error_type': type(exc).__name__ if type(exc).__name__.isalpha() and len(type(exc).__name__) <= 64 else 'Error'}
    from .launch_diagnostics import LocalCommandError
    if isinstance(exc, LocalCommandError):
        value.update(exc.command_diagnostic)
        if type(exc.diagnostic.get('exit_code')) is int:
            value['exit_code'] = exc.diagnostic['exit_code']
    return value


# Fixed protocol enums only: token-shaped arbitrary strings can still be secrets.
HTTP_SLUGS = frozenset({
    'fresh_admission_required', 'account_capacity', 'account_concurrency',
    'machine_capacity', 'account_unavailable', 'provider_disabled',
    'quota_unavailable', 'no_quota_evidence', 'quota_unknown', 'quota_stale',
    'quota_exhausted', 'reserve_required', 'limit_reached', 'stale_window_evidence',
    'malformed', 'invalid_observed_at', 'future_observation', 'stale_observation',
    'route_not_ok', 'route_stale', 'cooldown_active', 'grok_entitlement',
    'window_unavailable', 'reset_passed_since_observation', 'reserve', 'no_available_window',
    'cpu_headroom', 'cpu_pressure', 'cpu_usage_unknown', 'memory_pressure',
    'host_oom', 'host_runtime_choking', 'model_unsupported', 'already_claimed',
    'invalid_claim', 'claim_inactive', 'claim_conflict', 'claim_authority_changed',
    'claim_required', 'claim_workspace_unconfigured', 'claim_project_mismatch',
    'claim_scope_mismatch', 'claim_authority_unsupported', 'claim_actor_ambiguous',
    'claim_holder_mismatch', 'worker_scope_forbidden', 'forbidden', 'not_found', 'unauthorized',
})
RETRYABLE_CLAIM_ERRORS = frozenset({
    'fresh_admission_required', 'account_capacity', 'account_concurrency',
    'machine_capacity', 'cpu_headroom', 'cpu_pressure', 'memory_pressure',
    'host_oom', 'host_runtime_choking',
})


class ClaimRefusedError(ValueError):
    """Definitive claim response; retains only fixed, bounded protocol fields."""
    def __init__(self, exc, *, no_effects=True):
        import re
        status = getattr(exc, 'http_status', None)
        error = getattr(exc, 'hub_error', None)
        reason = getattr(exc, 'hub_reason', None)
        # Compatibility with the installed 4398ce2a transport and test adapters.
        if status is None:
            match = re.fullmatch(r'HTTP request failed with status ([45][0-9]{2})(?::\s*([A-Za-z0-9_.:-]{1,129}))?', str(exc))
            if match:
                status = int(match[1])
                parts = (match[2] or '').split(':')
                error = parts[0]
                reason = parts[1] if len(parts) == 2 else None
        self.diagnostic = {
            'reason': 'dispatch_claim_refused', 'message': MESSAGES['dispatch_claim_refused'],
            'stage': 'dispatch_route', 'error_type': 'ValueError',
        }
        if type(status) is int and 400 <= status <= 599:
            self.diagnostic['http_status'] = status
        if isinstance(error, str) and error in HTTP_SLUGS:
            self.diagnostic['hub_error'] = error
        if isinstance(reason, str) and reason in HTTP_SLUGS:
            self.diagnostic['hub_reason'] = reason
        self.diagnostic['retryable'] = no_effects and status == 409 and isinstance(error, str) and error in RETRYABLE_CLAIM_ERRORS
        super().__init__('HTTP request failed with status ' + str(status or 'unknown') + ': ' + (error if isinstance(error, str) and error in HTTP_SLUGS else 'unknown'))

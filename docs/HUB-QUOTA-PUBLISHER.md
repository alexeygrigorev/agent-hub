# Maintained Hub quota publication

A user-level publisher refreshes Hub quota, and a separate monitor checks each
expected account even when the publisher stops. Disabled Hub accounts stay disabled.

The publisher invokes the installed release CLI as `~/.local/bin/hub quota
publish`, without `--enable`. The command owns measurement and publication to
Hub. The wrapper accepts only successful JSON with `published` known account
labels and a `skipped` mapping. It records an invocation timestamp only for
published accounts. A skipped account keeps its previous timestamp. Failed,
malformed or unknown-account receipts cannot refresh any account.

These local receipts prove successful CLI publication, not continued Hub quota
validity, provider credential health, or available quota. No quota percentages,
provider errors, quse details or tokens enter the helper's output or alert text.

## Configuration and state

The default expected set is `claude,codex,codex-orin,gemini,grok,copilot,shared-go,zai-central`.
Review this against the intended pools before installation. Use the same explicit
`--accounts` list in both unit overrides if only a subset is expected. Gemini and
ZAI use labels `gemini` and `zai-central`. Disabled pools are not enabled by this
helper; omit deliberately unmonitored pools explicitly rather than assuming a
skipped result means fresh evidence.

Supported options include `--state /absolute/path`, `--hub /absolute/path`,
`--alert-command /absolute/path`, `--stale-seconds 1..900`,
`--publish-timeout 1..120`, and `--alert-timeout 1..30`. Defaults are a private
`~/.local/state/agent-hub-quota-publisher/state.json`, installed release Hub,
`~/.local/bin/principal-alert.sh`, 840 seconds, 120 seconds and 30 seconds.
The state's immediate directory must be owned by the user and private (0700).
State and lock are 0600, locked and replaced atomically. Symlink/nonprivate state
cannot prove freshness. Corrupt state is treated as missing evidence by the
monitor; the publisher refuses to update it until repaired by the monitor or
reviewed recovery. A monitor that cannot safely persist state alerts without a
sent marker, so it may repeat until storage is repaired.

Concurrent publisher subprocesses do not hold the state lock during Hub requests;
the monitor can detect their failure independently. State updates merge per
account, and an older invocation cannot overwrite a newer timestamp. The monitor
holds the lock during its bounded alert call to serialize incident delivery.
Successful alert delivery is deduplicated per stale account. A successful fresh publication or fresh check clears
that account's incident marker, allowing a later stale incident to alert again.
Sink failure leaves no sent marker and the next monitor retries.

## Reviewed installation, owned by deployment

The deployment owner must install from an exact reviewed commit. Copy the helper
into a private versioned directory such as
`~/.local/lib/agent-hub-quota-publisher/<full-commit>/hub-quota-publish.py`, verify
its hash against that source, and atomically select it with the `current` symlink.
The units use only this installed path; they do not execute a mutable checkout.
Keep the previous version available for rollback. Install the four reviewed
`deploy/agent-hub-quota-{publish,monitor}.{service,timer}` files into
`~/.config/systemd/user/`, validate their expanded paths, and verify that the Hub
binary is the intended installed release and the principal alert hook works.

The deployment owner validates the configuration, reloads the user manager,
run each oneshot deliberately, inspect its private receipt and delivery evidence,
and enable the two timers. Ensure the user manager survives logout if this host
requires lingering; do not silently assume this. Avoid starting another live
publisher while an existing cron/timer publisher is active. Disable the old quota
cron entry only after the replacement is verified, using an acknowledged cutover
that avoids duplicate live publication.

Both timers run on five-minute calendar boundaries, have no randomized delay,
and the monitor has no dependency on the publisher service. The default 14-minute
staleness threshold leaves allowance for a five-minute check interval, one-second
timer accuracy and a 30-second alert timeout within the 20-minute drill target.
An operator setting the threshold to the allowed 15-minute maximum consumes that
allowance; do not claim the 20-minute delivery target without measuring it.
Scheduler pauses, a stopped user manager, or a broken mail path can defeat delivery;
source unit definitions alone are not production evidence.

## Independent stopped-publisher drill

Under deployment-owner control, retain the independent monitor timer and its mail
path while stopping only the replacement publisher. Record the last successful
per-account receipt time. Verify a `quota_stale` message is delivered to
`dev@dtcdev.click` within 20 minutes, not merely logged or queued. The helper forces
that recipient through `PRINCIPAL_ALERT_TO` and never accepts a recipient option.
Restore the publisher, verify successful publication and a fresh monitor check,
then confirm a later synthetic incident can alert again. Preserve private evidence
of times, expected accounts, exact installed commit and delivery acknowledgment.
Record the measured result separately from source validation.

Exit status is 0 for published/fresh, 2 for stale (including a deduplicated incident),
3 for stale with a failed alert sink, and 1 for other helper failure. Alert text
contains only `quota_stale` and validated account labels. Investigate failures from
private Hub/provider logs; raw errors are deliberately absent from mail.

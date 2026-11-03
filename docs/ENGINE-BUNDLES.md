# Managed engine bundles

The managed Codex package version comes from
`host/agent_hub_host/bootstrap_pins.py` (`ENGINE_PACKAGES['codex']`). Bump that
reviewed release pin and ship the host release through CI/CD. Per-account
bundle versions cannot override the pin, so a sync from a stale account record
will still install the current managed version.

Local `bundle.json` files are receipts, not credential stores. A receipt points
to the fixed relative path `credentials.json`; its `files` and `env` maps are
empty. The referenced file contains the account's credential maps and is
written atomically with private owner-only permissions (0600 on POSIX). The
path is fixed by the host and cannot be selected by bundle data. The existing
private-directory and owner-DACL checks apply on Windows.

On the next successful sync, legacy receipts migrate by writing
`credentials.json` before replacing `bundle.json`. The host continues using
the existing managed home during migration, so a live Codex login is not
replaced or revoked. The migration does not rotate credentials.

Never display bundle receipts, credential files, raw sync responses, or engine
auth files. Bounded diagnostic output goes through the shared redactor, which
removes token-field values, bearer values, and recognizable JWT fragments
before output truncation.

For intake 935b266b, Alexey owns deciding and performing rotation of the shared
Codex login and checking whether the second Codex account is affected.

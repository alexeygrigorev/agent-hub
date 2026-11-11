"""Fresh all-registry observations for legacy launch-failure reconciliation."""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import time

MAX_CENSUS_BYTES = 16 * 1024 * 1024
MAX_CENSUS_ROWS = 5000
CENSUS_TIMEOUT_SECONDS = 5
MAX_CENSUS_AGE_SECONDS = 5
_UUID = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate native census field')
        result[key] = value
    return result


def _workspace(value):
    # Missing/deleted worktrees are supported. This comparison grants no native effect.
    if value.endswith(' (deleted)'):
        value = value[:-10]
    try:
        return os.path.normcase(os.path.realpath(value))
    except (ValueError, OSError):
        raise RuntimeError('native_census_workspace_invalid') from None


@dataclass(frozen=True)
class NativeInventory:
    identities: tuple
    started_at_ms: int
    observed_at_ms: int
    observed_monotonic: float
    digest: str

    def fresh(self):
        return 0 <= time.monotonic() - self.observed_monotonic <= MAX_CENSUS_AGE_SECONDS

    def absence_evidence(self, workspace, tag):
        if not self.fresh():
            raise RuntimeError('native_census_stale')
        if not isinstance(workspace, str) or not Path(workspace).is_absolute():
            raise RuntimeError('native_census_workspace_invalid')
        expected = _workspace(workspace)
        for row in self.identities:
            if row['tag'] == tag and _workspace(row['workspace']) == expected:
                return None
        return {
            'source': 'aplexer_all_registry_census',
            'started_at_ms': self.started_at_ms,
            'observed_at_ms': self.observed_at_ms,
            'row_count': len(self.identities),
            'sha256': self.digest,
            'complete': True,
            'match_count': 0,
            'workspace': workspace,
            'tag': tag,
            'dispatch_provenance': 'legacy_unknown',
        }


def collect_native_inventory(query):
    """query must invoke a list --all --json with no filters or cached result.

    The native JSON API returns one array for the full registry. Registry
    observation cannot establish process exit or whether a dispatch occurred.
    Oversized/malformed observations are rejected rather than truncated.
    """
    started_at_ms = round(time.time() * 1000)
    started = time.monotonic()
    try:
        response = query()
        raw = response.stdout
        if response.returncode != 0 or not isinstance(raw, str) or len(raw.encode('utf-8')) > MAX_CENSUS_BYTES:
            raise ValueError('invalid native census response')
        rows = json.loads(raw, object_pairs_hook=_object)
        if not isinstance(rows, list) or len(rows) > MAX_CENSUS_ROWS:
            raise ValueError('invalid native census array')
        identities, seen = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise ValueError('invalid native census row')
            native_id, tag, workspace = row.get('id'), row.get('tag'), row.get('workspace')
            if (not isinstance(native_id, str) or not _UUID.fullmatch(native_id) or native_id.lower() in seen
                    or not isinstance(tag, str) or not tag or '\0' in tag or len(tag.encode('utf-8')) > 512
                    or not isinstance(workspace, str) or not Path(workspace).is_absolute()
                    or '\0' in workspace or len(workspace.encode('utf-8')) > 8192
                    or type(row.get('schema_version')) is not int or row['schema_version'] < 1):
                raise ValueError('invalid native census identity')
            seen.add(native_id.lower())
            identities.append({'id': native_id.lower(), 'tag': tag, 'workspace': workspace})
        observed = time.monotonic()
        observed_at_ms = round(time.time() * 1000)
        if not 0 <= observed - started <= CENSUS_TIMEOUT_SECONDS + 1 or not 0 <= observed_at_ms - started_at_ms <= 6000:
            raise ValueError('native census observation exceeded deadline')
        identities.sort(key=lambda row: row['id'])
        encoded = json.dumps(identities, sort_keys=True, separators=(',', ':'), ensure_ascii=True).encode()
        return NativeInventory(tuple(identities), started_at_ms, observed_at_ms, observed,
                               hashlib.sha256(encoded).hexdigest())
    except (ValueError, TypeError, OSError):
        raise RuntimeError('native_census_unavailable') from None

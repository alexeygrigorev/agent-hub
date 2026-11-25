"""Tests for Hub2 disk admission backpressure and one-shot hygiene trigger (AC3)."""
import sqlite3
import tempfile
import unittest
from collections import namedtuple
from pathlib import Path
from types import SimpleNamespace

from agent_hub_host import resource_admission

DiskUsage = namedtuple('DiskUsage', ('total', 'used', 'free'))


def _make_bridge(tmp_dir: Path, floor_gb: float):
    db = sqlite3.connect(':memory:')
    db.row_factory = sqlite3.Row
    db.execute(
        'CREATE TABLE launches(operation_id TEXT PRIMARY KEY, session_id TEXT, epoch INTEGER, '
        'digest TEXT, workspace TEXT, tag TEXT, native_id TEXT, state TEXT, interactive INTEGER, '
        'runtime_deadline INTEGER, native_dispatch_started INTEGER)'
    )
    db.execute('CREATE TABLE stop_fences(session_id TEXT, epoch INTEGER)')
    db.execute(
        'CREATE TABLE resource_reservations(operation_id TEXT PRIMARY KEY, session_id TEXT, '
        'epoch INTEGER, digest TEXT, attempt TEXT, allowance_bytes INTEGER, updated_at_ms INTEGER, native_id TEXT)'
    )
    db.execute(
        "INSERT INTO launches VALUES('op1','s1',1,'d1',?, 'tag1', NULL, 'launching', 0, NULL, 0)",
        (str(tmp_dir),),
    )
    db.commit()
    return SimpleNamespace(
        journal=SimpleNamespace(db=db),
        launcher=SimpleNamespace(config={'reserved_floor_mb': 128, 'disk_floor_gb': floor_gb}),
        attempt_label=lambda s, o, e: f'{s}:{o}:{e}',
        resource_sample=lambda deadline: 64 * 1024 ** 3,
    )


class DiskAdmissionBackpressureTests(unittest.TestCase):
    def test_low_disk_refuses_with_disk_below_floor_triggers_hygiene_once_and_resumes(self):
        resource_admission._LAST_HYGIENE_TRIGGER_MONO = 0.0
        with tempfile.TemporaryDirectory() as tmp:
            bridge = _make_bridge(Path(tmp), floor_gb=20.0)
            triggers = []
            low_disk = lambda _p: DiskUsage(100 * 1024 ** 3, 95 * 1024 ** 3, 5 * 1024 ** 3)
            with self.assertRaisesRegex(RuntimeError, 'disk_below_floor'):
                resource_admission.admit_dispatch(
                    bridge, 'op1', 's1', 1, {'memory_mb': 512, 'pids': 64},
                    census=[], disk_usage_fn=low_disk, hygiene_trigger_fn=lambda: triggers.append(1),
                )
            with self.assertRaisesRegex(RuntimeError, 'disk_below_floor'):
                resource_admission.admit_dispatch(
                    bridge, 'op1', 's1', 1, {'memory_mb': 512, 'pids': 64},
                    census=[], disk_usage_fn=low_disk, hygiene_trigger_fn=lambda: triggers.append(1),
                )
            self.assertEqual(len(triggers), 1)

            # Space returns -> admission succeeds
            ok_disk = lambda _p: DiskUsage(100 * 1024 ** 3, 50 * 1024 ** 3, 50 * 1024 ** 3)
            permit = resource_admission.admit_dispatch(
                bridge, 'op1', 's1', 1, {'memory_mb': 512, 'pids': 64},
                census=[], disk_usage_fn=ok_disk, hygiene_trigger_fn=lambda: triggers.append(1),
            )
            self.assertEqual(permit.operation_id, 'op1')


if __name__ == '__main__':
    unittest.main()

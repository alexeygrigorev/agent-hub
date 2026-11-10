"""Offline tests: real helper CLI, fake installed Hub and principal alert sink."""
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / 'scripts/hub-quota-publish.py'
spec = importlib.util.spec_from_file_location('quota_publisher_helper', SCRIPT)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


class PublisherTests(unittest.TestCase):
    def setUp(self):
        parent = ROOT / '.local/tmp'
        parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='quota-publisher-test-', dir=parent)
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.cfg = self.home / 'fixture.json'
        self.log = self.home / 'alerts.jsonl'
        self.argvlog = self.home / 'hub-argv.jsonl'
        self.state = self.home / 'state.json'
        self.hub = self.home / 'hub'
        self.sink = self.home / 'principal-alert.sh'
        self.config()
        self.hub.write_text('''#!/usr/bin/env python3
import json,sys,time
from pathlib import Path
cfg=json.loads(Path(%r).read_text())
with Path(%r).open('a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
assert sys.argv[1:]==['quota','publish']
time.sleep(cfg.get('sleep',0))
if cfg.get('failure'):
 print('SECRET provider/token/quse details',file=sys.stderr);sys.exit(1)
print(cfg.get('raw',json.dumps(cfg['receipt'])))
''' % (str(self.cfg), str(self.argvlog)))
        self.sink.write_text('''#!/usr/bin/env python3
import json,sys,os
from pathlib import Path
cfg=json.loads(Path(%r).read_text())
entry={'recipient':os.environ.get('PRINCIPAL_ALERT_TO'),'message':os.environ.get('PING_ALERT_MESSAGE'),
       'argv':sys.argv[1:],'alternate_to':os.environ.get('ALERT_TO'),'hub_token_forwarded':'HUB_TOKEN' in os.environ}
with Path(%r).open('a') as f:f.write(json.dumps(entry)+'\\n')
sys.exit(1 if cfg.get('sink_failure') else 0)
''' % (str(self.cfg), str(self.log)))
        self.hub.chmod(0o700); self.sink.chmod(0o700)

    def config(self, **kw):
        value = {'receipt': {'published': ['gemini', 'zai-central'],
                            'skipped': {a: 'no measured window' for a in helper.ACCOUNTS - {'gemini', 'zai-central'}}}}
        value.update(kw)
        self.cfg.write_text(json.dumps(value))

    def invoke(self, check=False, **env):
        args = [sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub),
                '--alert-command', str(self.sink), '--accounts', 'gemini,zai-central']
        if check: args.append('--check')
        done = subprocess.run(args, capture_output=True, text=True, timeout=8,
                              env={**os.environ, 'HOME': str(self.home), **env})
        self.assertNotIn('SECRET', done.stdout + done.stderr)
        self.assertEqual(done.stderr, '', done.stderr)
        return done.returncode, json.loads(done.stdout)

    def read(self):
        return json.loads(self.state.read_text())

    def seed(self, **accounts):
        self.state.write_text(json.dumps({'version': 1, 'accounts': accounts, 'alerted': []}))
        self.state.chmod(0o600)

    def alerts(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_actual_publish_argv_and_gemini_zai_private_receipts(self):
        before = int(time.time() * 1000)
        rc, result = self.invoke()
        self.assertEqual(rc, 0, result)
        self.assertEqual(json.loads(self.argvlog.read_text()), ['quota', 'publish'])
        self.assertEqual(set(self.read()['accounts']), {'gemini', 'zai-central'})
        self.assertTrue(all(before <= at <= int(time.time() * 1000) for at in self.read()['accounts'].values()))
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.invoke(check=True)[0], 0)
        self.assertEqual(self.alerts(), [])

    def test_partial_success_preserves_skipped_times(self):
        old = int(time.time() * 1000) - 900000
        self.seed(gemini=old, **{'zai-central': old})
        self.config(receipt={'published': ['gemini'], 'skipped': {'zai-central': 'SECRET private provider error'}})
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read()['accounts']['zai-central'], old)
        self.assertGreater(self.read()['accounts']['gemini'], old)
        rc, result = self.invoke(check=True)
        self.assertEqual(rc, 2)
        self.assertEqual(result['accounts'], ['zai-central'])
        self.assertNotIn('SECRET', json.dumps(self.alerts()))

    def test_invalid_or_failed_result_cannot_refresh_state(self):
        old = int(time.time() * 1000) - 900000
        cases = [{'failure': True}, {'raw': 'not json'}, {'raw': '{"published":["unknown"],"published":["gemini"],"skipped":{}}'}, {'receipt': {'published': ['unknown'], 'skipped': {}}},
                 {'receipt': {'published': ['gemini', 'gemini'], 'skipped': {}}},
                 {'receipt': {'published': ['gemini'], 'skipped': {'gemini': 'conflict'}}},
                 {'receipt': {'published': [], 'skipped': {}}},
                 {'receipt': {'published': ['gemini'], 'skipped': {}, 'unexpected': 'SECRET'}}]
        for case in cases:
            with self.subTest(case=case):
                self.seed(gemini=old, **{'zai-central': old}); self.config(**case)
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.read()['accounts'], {'gemini': old, 'zai-central': old})

    def test_monitor_detects_stopped_publisher_independently_and_dedupes(self):
        self.hub.unlink()  # monitor cannot depend on hub or publisher being runnable
        for _ in range(2):
            rc, result = self.invoke(check=True)
            self.assertEqual(rc, 2, result)
        self.assertEqual(len(self.alerts()), 1)
        self.assertEqual(self.read()['alerted'], ['gemini', 'zai-central'])
        self.assertFalse(self.argvlog.exists())

    def test_recovery_resets_incident_then_new_staleness_alerts(self):
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.invoke(check=True)[0], 0)
        self.assertEqual(self.read()['alerted'], [])
        stale = int(time.time() * 1000) - 900000
        data = self.read(); data['accounts'] = dict(gemini=stale, **{'zai-central': stale})
        self.state.write_text(json.dumps(data))
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)

    def test_sink_failure_never_marks_sent_and_recipient_is_fixed(self):
        self.config(sink_failure=True)
        self.assertEqual(self.invoke(check=True, PRINCIPAL_ALERT_TO='wrong@example.test', ALERT_TO='wrong@example.test',
                                     PING_ALERT_MESSAGE='SECRET', HUB_TOKEN='SECRET')[0], 3)
        self.assertEqual(self.read()['alerted'], [])
        self.config()
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)
        for alert in self.alerts():
            self.assertEqual(alert['recipient'], 'dev@dtcdev.click')
            self.assertEqual(alert['alternate_to'], 'dev@dtcdev.click')
            self.assertEqual(alert['argv'], [alert['message']])
            self.assertIn('quota_stale', alert['message'])
            self.assertFalse(alert['hub_token_forwarded'])

    def test_corrupt_or_future_state_never_proves_fresh(self):
        for raw in ('{broken', json.dumps({'version': 1, 'accounts': {'gemini': int(time.time()*1000)+60000}, 'alerted': []}),
                    json.dumps({'version': 1, 'accounts': {}, 'alerted': [{}]})):
            with self.subTest(raw=raw):
                self.state.write_text(raw); self.state.chmod(0o600)
                self.assertEqual(self.invoke()[0], 1)
                self.assertEqual(self.state.read_text(), raw)
                rc, result = self.invoke(check=True)
                self.assertEqual(rc, 2, result)
                self.assertEqual(result['accounts'], ['gemini', 'zai-central'])
                self.assertEqual(self.read()['accounts'], {})

    def test_concurrent_partial_publishers_preserve_both_receipts(self):
        # Each subprocess has an immutable fake receipt, with the slower older
        # publication completing after the newer one.
        hubs = []
        for account, delay in [('gemini', 0.2), ('zai-central', 0)]:
            binary = self.home / ('hub-' + account)
            binary.write_text('#!/usr/bin/env python3\nimport json,time,sys\nassert sys.argv[1:]==["quota","publish"]\ntime.sleep(%r)\nprint(%r)\n' %
                              (delay, json.dumps({'published': [account], 'skipped': {}})))
            binary.chmod(0o700); hubs.append(binary)
        def run(binary):
            return subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(binary)],
                                  capture_output=True, text=True, timeout=5).returncode
        with ThreadPoolExecutor(2) as pool:
            self.assertEqual(list(pool.map(run, hubs)), [0, 0])
        self.assertEqual(set(self.read()['accounts']), {'gemini', 'zai-central'})
        self.assertEqual(self.invoke(check=True)[0], 0)

    def test_older_overlapping_receipt_never_overwrites_newer_same_account(self):
        marker = self.home / 'slow-entered'
        slow = self.home / 'slow-hub'
        fast = self.home / 'fast-hub'
        response = json.dumps({'published': ['gemini'], 'skipped': {}})
        slow.write_text('#!/usr/bin/env python3\nfrom pathlib import Path\nimport time\nPath(%r).write_text("entered")\ntime.sleep(0.3)\nprint(%r)\n' % (str(marker), response))
        fast.write_text('#!/usr/bin/env python3\nprint(%r)\n' % response)
        slow.chmod(0o700); fast.chmod(0o700)
        args = [sys.executable, str(SCRIPT), '--state', str(self.state), '--hub']
        child = subprocess.Popen(args + [str(slow)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not marker.exists() and time.monotonic() < deadline: time.sleep(0.01)
            self.assertTrue(marker.exists())
            done = subprocess.run(args + [str(fast)], capture_output=True, text=True, timeout=3)
            self.assertEqual(done.returncode, 0, done.stderr)
            newer = self.read()['accounts']['gemini']
            stdout, stderr = child.communicate(timeout=3)
            self.assertEqual(child.returncode, 0, (stdout, stderr))
            self.assertEqual(self.read()['accounts']['gemini'], newer)
        finally:
            if child.poll() is None: child.kill(); child.communicate()

    def test_state_symlink_and_nonprivate_state_cannot_claim_freshness(self):
        outside = self.home / 'unrelated-data'
        outside.write_text('SECRET unrelated data')
        self.state.symlink_to(outside)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(outside.read_text(), 'SECRET unrelated data')
        self.assertFalse(self.state.is_symlink())
        self.assertEqual(self.read()['accounts'], {})
        self.seed(gemini=int(time.time()*1000), **{'zai-central': int(time.time()*1000)})
        self.state.chmod(0o644)
        self.assertEqual(self.invoke()[0], 1)
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o600)
        self.assertEqual(self.read()['accounts'], {})

    def test_successful_publication_recovers_incident_without_an_intervening_check(self):
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(self.invoke()[0], 0)
        self.assertEqual(self.read()['alerted'], [])
        data = self.read(); data['accounts'] = {a: int(time.time()*1000)-900000 for a in data['accounts']}
        self.state.write_text(json.dumps(data))
        self.assertEqual(self.invoke(check=True)[0], 2)
        self.assertEqual(len(self.alerts()), 2)

    def test_monitor_does_not_wait_for_inflight_hub_process(self):
        self.config(sleep=2)
        process = subprocess.Popen([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            deadline = time.monotonic() + 2
            while not self.argvlog.exists() and time.monotonic() < deadline: time.sleep(0.01)
            self.assertTrue(self.argvlog.exists())
            start = time.monotonic(); self.assertEqual(self.invoke(check=True)[0], 2)
            self.assertLess(time.monotonic() - start, 1)
            self.assertIsNone(process.poll())
            stdout, stderr = process.communicate(timeout=5)
            self.assertEqual(process.returncode, 0, (stdout, stderr))
            self.assertEqual(self.invoke(check=True)[0], 0)
        finally:
            if process.poll() is None: process.kill(); process.communicate()

    def test_safe_slug_and_timeout(self):
        self.config(sleep=2)
        result = subprocess.run([sys.executable, str(SCRIPT), '--state', str(self.state), '--hub', str(self.hub),
                                 '--publish-timeout', '1'], capture_output=True, text=True, timeout=4)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn('SECRET', result.stdout + result.stderr)
        self.assertFalse(self.state.exists())


class ContractTests(unittest.TestCase):
    def test_config_bounds_and_known_default_accounts(self):
        self.assertTrue({'gemini', 'zai-central'}.issubset(helper.ACCOUNTS))
        for raw in ('', 'gemini,gemini', 'gemini,unknown', '../bad', 'Gemini'):
            with self.assertRaises(Exception): helper.account_set(raw)
        for value in ('relative', '/a\nsecret'):
            with self.assertRaises(Exception): helper.absolute_path(value)
        for stale in ('0', '901'):
            with mock.patch('sys.stderr'):
                with self.assertRaises(SystemExit): helper.main(['--stale-seconds', stale])

    def test_timers_independent_five_minute_cadence_and_versioned_path(self):
        for name in ('publish', 'monitor'):
            timer = (ROOT / ('deploy/agent-hub-quota-' + name + '.timer')).read_text()
            service = (ROOT / ('deploy/agent-hub-quota-' + name + '.service')).read_text()
            self.assertIn('OnCalendar=*-*-* *:0/5:00', timer)
            self.assertIn('AccuracySec=1s', timer)
            self.assertIn('RandomizedDelaySec=0', timer)
            self.assertIn('UMask=0077', service)
            self.assertIn('%h/.local/lib/agent-hub-quota-publisher/current/hub-quota-publish.py', service)
            self.assertNotIn('Requires=', service)
            self.assertNotIn('--enable', service)
            self.assertEqual('--check' in service, name == 'monitor')
        self.assertLessEqual(helper.DEFAULT_STALE_SECONDS + 300 + 1 + 30, 1200)


if __name__ == '__main__':
    unittest.main()

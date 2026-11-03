"""Fast `hub intake summary` path (intake ef429827): cheap imports, strict base
parsing, static-profile resolution that stays equivalent to load_config."""
import json
import os
import stat
import subprocess
import sys
import unittest
from unittest.mock import patch


class TestSummaryFastImportCost(unittest.TestCase):
    def test_summary_fast_imports_no_heavy_modules(self):
        code = (
            "import sys; "
            "from agent_hub_host import summary_fast; "
            "mods = set(sys.modules); "
            "heavy = sorted(m for m in ("
            "'agent_hub_host.config', 'agent_hub_host.worker_auth', "
            "'agent_hub_host.api', 'agent_hub_host.auth', "
            "'agent_hub_host.cli', 'agent_hub_host.intake', "
            "'urllib.parse', 'urllib.request', 'http.client', "
            "'argparse', 'ssl', 'tempfile', 'pathlib', 'email.utils'"
            ") if m in mods); "
            "print(','.join(heavy))"
        )
        host_dir = os.path.join(os.path.dirname(__file__), '..')
        child_env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        child_env['PYTHONPATH'] = os.path.abspath(host_dir)
        proc = subprocess.run(
            [sys.executable, '-c', code],
            capture_output=True, text=True, timeout=60,
            cwd=os.path.abspath(host_dir),
            env=child_env,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertEqual(proc.stdout.strip(), '', 'heavy modules imported: ' + proc.stdout.strip())


class TestParseHttpsBase(unittest.TestCase):
    def test_parity_with_urllib_reference(self):
        from urllib.parse import urlparse
        from agent_hub_host.summary_fast import _parse_https_base
        valid = [
            'https://hub.dtcdev.click',
            'https://hub.dtcdev.click/',
            'https://hub.dtcdev.click///',
            'https://hub.dtcdev.click:8443',
            'https://hub.dtcdev.click:443',
            'https://HOST.DTCDEV.CLICK',
            'https://hub.dtcdev.click/prefix',
            'https://hub.dtcdev.click/prefix/',
            'https://h:1',
            'https://h:65535',
            'https://h:0443',
            'https://example.com/a.b_c-d',
        ]
        for base in valid:
            with self.subTest(base=base):
                parsed = urlparse(base.rstrip('/'))
                try:
                    port = int(parsed.port) if parsed.port is not None else 443
                except ValueError:
                    self.fail('reference rejects valid case ' + base)
                expect = (parsed.hostname, port,
                          parsed.path, parsed.hostname if port == 443 else f'{parsed.hostname}:{port}')
                self.assertEqual(_parse_https_base(base), expect)
        invalid = [
            '', '   ', 'http://hub.dtcdev.click', 'ftp://h', 'https://',
            'https://user@h', 'https://user:pass@h', 'https://h/x#frag',
            'https://h?x=1', 'https://h\\path',
            'https://h:0', 'https://h:65536',
            'https://h:abc', 'https://h:+443', 'https://h:443:1',
            'https://h:bär', ' https://h', 'https://h ',
            'https://', 'notaurl', 'https://h/x\ty',
        ]
        conservative_rejects = [
            # The reference parser accepts these, but the fast path
            # deliberately falls back to the full transport instead:
            # IPv6 literals and inner-space paths never occur in Hub
            # base URLs and are not worth hand-rolled support.
            'https://[::1]/', 'https://h/a b',
        ]
        for base in invalid + conservative_rejects:
            with self.subTest(base=base):
                self.assertIsNone(_parse_https_base(base), 'fast parser must reject ' + base)


class TestFastLoadPosixConfig(unittest.TestCase):
    def _write_profile(self, root, name='cli.json', token='tok-1', base='https://hub.test'):
        folder = os.path.join(root, 'agent-hub')
        os.makedirs(folder, mode=0o700, exist_ok=True)
        os.chmod(folder, 0o700)
        path = os.path.join(folder, name)
        with open(path, 'w') as fp:
            json.dump({'access_token': token, 'base_url': base}, fp)
        os.chmod(path, 0o600)
        return path

    def test_static_read_without_session_vars(self):
        from agent_hub_host import summary_fast
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(tmp)
            env = {'XDG_CONFIG_HOME': tmp}
            with patch.dict(os.environ, env, clear=False):
                for var in ('HUB_CONFIG_FILE', 'HUB_WORKER_CONFIG_FILE', 'APLEXER_TAG',
                            'APLEXER_WORKSPACE', 'APLEXER_SESSION_ID'):
                    os.environ.pop(var, None)
                cfg = summary_fast._fast_load_posix_config()
        self.assertEqual(cfg['access_token'], 'tok-1')
        self.assertEqual(cfg['base_url'], 'https://hub.test')

    def test_session_env_without_profile_still_fast(self):
        from agent_hub_host import summary_fast
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(tmp)
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp,
                                         'APLEXER_TAG': 'drain-ef429827',
                                         'APLEXER_WORKSPACE': '/home/alexey/git/drain-ef429827'}):
                for var in ('HUB_CONFIG_FILE', 'HUB_WORKER_CONFIG_FILE', 'APLEXER_SESSION_ID'):
                    os.environ.pop(var, None)
                # drain-ef429827 is not a launch identity and no per-launch
                # profile exists, so the static XDG read stays valid.
                cfg = summary_fast._fast_load_posix_config()
        self.assertEqual(cfg['access_token'], 'tok-1')

    def test_launch_session_profile_falls_back(self):
        from agent_hub_host import summary_fast
        import tempfile
        launch = 'hub-' + '12345678-1234-1234-1234-1234567890ab'
        with tempfile.TemporaryDirectory() as tmp:
            self._write_profile(tmp)
            ws = os.path.join(tmp, 'ws')
            profdir = os.path.join(ws, '.local', 'hub-worker-auth')
            os.makedirs(profdir)
            with open(os.path.join(profdir, launch[4:] + '.json'), 'w') as fp:
                json.dump({'access_token': 'session-tok'}, fp)
            with patch.dict(os.environ, {'XDG_CONFIG_HOME': tmp,
                                         'APLEXER_TAG': launch,
                                         'APLEXER_WORKSPACE': ws}):
                for var in ('HUB_CONFIG_FILE', 'HUB_WORKER_CONFIG_FILE', 'APLEXER_SESSION_ID'):
                    os.environ.pop(var, None)
                self.assertIsNone(summary_fast._fast_load_posix_config())


class TestTryFastSummaryHermetic(unittest.TestCase):
    def test_returns_false_immediately_under_test_runner(self):
        from agent_hub_host import summary_fast
        # Must never touch the network from a test process.
        with patch.object(summary_fast, '_fast_summary_get',
                           side_effect=AssertionError('network touched')):
            self.assertFalse(summary_fast.try_fast_summary(
                ['hub', 'intake', 'summary']))
            self.assertFalse(summary_fast.try_fast_summary(
                ['hub', 'intake', 'summary', '--project', 'demo']))

    def test_rejects_non_summary_forms_without_network(self):
        from agent_hub_host import summary_fast
        with patch.object(summary_fast, '_fast_summary_get',
                           side_effect=AssertionError('network touched')):
            self.assertFalse(summary_fast.try_fast_summary(
                ['hub', 'intake', 'summary', '--unknown']))
            self.assertFalse(summary_fast.try_fast_summary(
                ['hub', 'intake', 'summary', '--project', '../evil']))
            self.assertFalse(summary_fast.try_fast_summary(
                ['hub', 'intake', 'summary', '--project=']))

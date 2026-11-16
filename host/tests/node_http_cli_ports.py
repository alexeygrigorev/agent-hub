"""Two real HTTP/CLI ports from test_http_cli; no proxy, provider or SQL seed.

This module is loaded by the existing required Node intake e2e module. Its
TestCase is also exported by the original module for unittest discovery.
"""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import unittest


class TestCurrentNodeHttpCli(unittest.TestCase):
    def setUp(self):
        repo = Path(__file__).resolve().parents[2]
        spec = importlib.util.spec_from_file_location(
            "mailbox_http_node_harness", repo / "hub2/tests/e2e/harness.py")
        self.h = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.h)
        self.root = self.h.scratch_root()
        self.server = self.h.Server(self.root)
        self.addCleanup(self.close)
        self.zipapp = self.h.build_cli(self.root / 'cli')
        self.server.start()
        self.cli = self.h.Cli(self.zipapp, self.root, self.server)
        # Public setup only. No provider credentials, host, machine or native
        # session is needed for a mailbox with no forwarding target.
        created = self.server.ok('POST', '/api/named-agents', {
            'name': 'http-recipient', 'display_name': 'HTTP test recipient',
            'enabled': True})
        self.assertEqual(created['agent']['id'], 'agent:http-recipient')

    def close(self):
        # If server settlement fails, retain its files rather than removing
        # scratch beneath a potentially live process.
        self.server.stop()
        shutil.rmtree(self.root)

    def run_cli_stdin(self, *args, body):
        # The existing harness Cli.run has no stdin parameter. Use the same
        # actual zipapp and isolated operator credential, with input supplied
        # only by this test. No credential/config discovery from the real HOME.
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('HUB', 'AGENT_HUB', 'APLEXER', 'XDG'))}
        env.update(HUB_URL=self.server.origin, HUB_TOKEN=self.h.OPERATOR_TOKEN,
                   HOME=str(self.cli.home), XDG_CONFIG_HOME=str(self.cli.home / '.config'),
                   HUB_CONFIG_FILE=str(self.cli.home / '.config/agent-hub/cli.json'))
        return subprocess.run([sys.executable, str(self.zipapp), *args],
                              input=body, env=env, capture_output=True,
                              text=True, timeout=30)

    def test_real_worker_malformed_and_utf8_bounds(self):
        # Preserve the original independent legacy stdin byte-limit concern.
        over = self.run_cli_stdin('msg', 'send', '--to', 'agent:http-recipient',
                                  '--body', '-', '--key', 'over-stdin', body='X' * 16385)
        self.assertEqual(over.returncode, 1)
        self.assertIn('exceeds 16 KiB limit', over.stderr)
        self.assertEqual(self.server.ok('GET', '/api/messages?folder=sent')['items'], [])

        # Current public wire has a separate 12000-character bound. Exercise
        # both the real CLI refusal and direct real HTTP validation, without
        # changing either limit to retain the obsolete Hub1 wire contract.
        chars = self.cli.run('msg', 'send', '--to', 'agent:http-recipient',
                             '--body', 'X' * 12001, '--key', 'over-characters')
        self.assertEqual(chars.returncode, 2)
        self.assertIn('12000-character message limit', chars.stderr)
        bad = self.server.api('POST', '/api/messages', {
            'recipient': 'agent:http-recipient', 'body': 'X' * 12001,
            'key': 'over-http'})
        self.assertEqual((bad.status, bad.body.get('error')), (400, 'invalid_message'))
        self.assertEqual(self.server.ok('GET', '/api/messages?folder=sent')['items'], [])

        utf8_text = 'Unicode test: 🚀 🤖 äöü €'
        sent = self.cli.json('msg', 'send', '--to', 'agent:http-recipient',
                             '--body', utf8_text, '--key', 'utf8-key-1')
        self.assertTrue(sent['id'])
        self.assertEqual(sent['to'], 'agent:http-recipient')
        shown = self.cli.json('msg', 'show', sent['id'])
        self.assertEqual(shown['body'], utf8_text)
        self.assertEqual((shown['id'], shown['recipient']),
                         (sent['id'], 'agent:http-recipient'))
        rows = self.server.ok('GET', '/api/messages?folder=sent')['items']
        self.assertEqual([r['id'] for r in rows], [sent['id']])
        # Boundary accepted by the actual Node HTTP route, not only the CLI.
        boundary = self.server.ok('POST', '/api/messages', {
            'recipient': 'agent:http-recipient', 'body': 'X' * 12000,
            'key': 'exact-http-boundary'})['message']
        self.assertEqual(len(boundary['body']), 12000)

    def test_real_worker_unknown_addresses(self):
        missing = 'agent:nonexistent-agent-xyz'
        result = self.cli.run('msg', 'send', '--to', missing,
                               '--body', 'Hello nowhere', '--key', 'unknown-cli')
        # Current documented messaging exit code is 3, not a generic failure.
        self.assertEqual(result.returncode, 3)
        self.assertIn('404', result.stderr)
        self.assertIn('recipient_unknown', result.stderr)
        denied = self.server.api('POST', '/api/messages', {
            'recipient': missing, 'body': 'Hello nowhere', 'key': 'unknown-http'})
        self.assertEqual((denied.status, denied.body.get('error')), (404, 'recipient_unknown'))
        self.assertEqual(self.server.ok('GET', '/api/messages?folder=sent')['items'], [])
        # An unknown-recipient error must not silently create a directory row.
        directory = self.server.ok('GET', '/api/named-agents')['items']
        self.assertEqual([a['id'] for a in directory], ['agent:http-recipient'])

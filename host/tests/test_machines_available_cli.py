"""Available-machine API envelopes must render in both CLI output modes."""
import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch

from agent_hub_host import cli


class MachinesAvailableCliTests(unittest.TestCase):
    def invoke(self, response, *, as_json=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ['hub', 'machines', 'available'] + (['--json'] if as_json else [])
        with patch.object(sys, 'argv', argv), \
                patch.object(cli.config, 'load_config', return_value={'base_url': 'https://hub.test'}), \
                patch.object(cli.api, 'get_available_machines', return_value=response) as get, \
                redirect_stdout(stdout), redirect_stderr(stderr):
            cli.main()
        get.assert_called_once_with('https://hub.test')
        return stdout.getvalue(), stderr.getvalue()

    def test_canonical_api_envelope_and_legacy_shapes(self):
        machines = [{'id': 'machine-1', 'hostname': 'host-1'}, {'hostname': 'host-2'}]
        expected = {'reachable': machines, 'count': 2}
        for response in ({'machines': machines}, machines, expected):
            with self.subTest(response=response):
                self.assertEqual(self.invoke(response), ('machine-1\nhost-2\n', ''))
                stdout, stderr = self.invoke(response, as_json=True)
                self.assertEqual(json.loads(stdout), expected)
                self.assertEqual(stderr, '')

    def test_empty_canonical_envelope(self):
        self.assertEqual(self.invoke({'machines': []}),
                         ('', 'reachable: none (all probes failed or unknown)\n'))
        stdout, stderr = self.invoke({'machines': []}, as_json=True)
        self.assertEqual(json.loads(stdout), {'reachable': [], 'count': 0})
        self.assertEqual(stderr, '')

    def test_unknown_response_is_an_error_not_an_empty_inventory(self):
        with self.assertRaises(SystemExit) as caught:
            self.invoke({'machines': None})
        self.assertEqual(caught.exception.code, 1)

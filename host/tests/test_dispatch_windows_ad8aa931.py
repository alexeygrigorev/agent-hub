"""Pre-start refusals on DESKTOP-FP4OP26 are named and a clean gate exit is not a failure (intake ad8aa931).

Claude headless launches there failed as a bare dispatch_internal_error
(35cf62ff, 8151fb8f) and as LocalCommandError on an exit-0 admission gate
whose complete JSON was captured (abe6bf8d).
"""
import json
import sys
import unittest
from unittest.mock import patch

from agent_hub_host import fleet
from agent_hub_host.dispatch_diagnostics import classify
from agent_hub_host.launch_diagnostics import LocalCommandError

# Prints the gate JSON and exits 0 while a child keeps the inherited stdout
# open, the way a starved reader thread outlives the 1 s join.
LINGER = ("import json, subprocess, sys\n"
          "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(%s)'])\n"
          "print(json.dumps({'launch_allowed': True}))\n")


class TestNamedPreStartRefusals(unittest.TestCase):
    def reason(self, message):
        return classify(ValueError(message))['reason']

    def test_fixed_messages_map_to_existing_slugs(self):
        self.assertEqual(self.reason('Selected model/effort is not configured locally'), 'model_unsupported')
        self.assertEqual(self.reason('Maintained launcher route is not configured'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Invalid provider profile'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Headless provider profile is not qualified'), 'dispatch_route_unconfigured')
        self.assertEqual(self.reason('Unsupported launch mode'), 'dispatch_offer_invalid')
        self.assertEqual(self.reason('workspace path exists and is not a git checkout'), 'workspace_unavailable')

    def test_variable_tails_map_by_prefix_and_are_never_forwarded(self):
        cases = {"Launcher provider 'x' is not supported for headless execution": 'dispatch_route_unconfigured',
                 'repo clone failed: fatal: could not read Username for https://github.com': 'workspace_unavailable',
                 'git fetch failed: exit 128': 'workspace_unavailable',
                 'git checkout failed: error: pathspec': 'workspace_unavailable'}
        for message, slug in cases.items():
            diag = classify(ValueError(message))
            self.assertEqual(diag['reason'], slug, message)
            self.assertNotIn('Username', json.dumps(diag))

    def test_unknown_messages_stay_internal(self):
        self.assertEqual(self.reason('Native lifecycle receipt is incomplete'), 'dispatch_internal_error')
        self.assertEqual(self.reason('repo clone failedX'), 'dispatch_internal_error')


class TestCleanExitReaderGrace(unittest.TestCase):
    def test_clean_exit_with_slow_reader_returns_the_json(self):
        self.assertEqual(fleet.run_json([sys.executable, '-c', LINGER % 2]), {'launch_allowed': True})

    def test_reader_held_past_the_grace_is_still_a_failure(self):
        with patch.object(fleet, 'READER_GRACE_S', 0.5), self.assertRaises(LocalCommandError):
            fleet.run_json([sys.executable, '-c', LINGER % 4])

    def test_nonzero_exit_gets_no_grace(self):
        with self.assertRaises(LocalCommandError) as ctx:
            fleet.run_json([sys.executable, '-c', 'import sys; print("{}"); sys.exit(3)'])
        self.assertEqual(ctx.exception.diagnostic.get('exit_code'), 3)


if __name__ == '__main__':
    unittest.main()

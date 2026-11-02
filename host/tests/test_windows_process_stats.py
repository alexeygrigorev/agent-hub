import os
import unittest

from agent_hub_host import windows_process_stats as w


class TreeTests(unittest.TestCase):
    def test_tree_walks_descendants_and_ignores_unrelated(self):
        parents = {10: 1, 11: 10, 12: 11, 20: 1, 21: 20}
        self.assertEqual(w.tree_pids([10], parents), [10, 11, 12])
        self.assertEqual(w.tree_pids([10, 20], parents), [10, 11, 12, 20, 21])
        self.assertEqual(w.tree_pids([], parents), [])
        self.assertEqual(w.tree_pids([-1, 0], parents), [])

    def test_sample_sums_memory_and_cpu_and_skips_unreadable(self):
        parents = {10: 1, 11: 10, 12: 10}
        rows = {10: {"working_set_bytes": 100 << 20, "private_bytes": 60 << 20, "cpu_seconds": 1.5},
                11: {"working_set_bytes": 50 << 20, "private_bytes": 40 << 20, "cpu_seconds": 0.5},
                12: None}
        result = w.sample_tree([10], parents, stats=lambda pid: rows[pid])
        self.assertEqual(result["rss_mb"], 150)
        self.assertEqual(result["pss_mb"], 100)
        self.assertEqual(result["cpu_seconds"], 2.0)

    def test_nothing_readable_is_unknown_not_zero(self):
        self.assertIsNone(w.sample_tree([10], {10: 1}, stats=lambda pid: None))

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_live_self_sample(self):
        result = w.sample_tree([os.getpid()])
        self.assertGreater(result["rss_mb"], 1)
        self.assertGreaterEqual(result["cpu_seconds"], 0)

    @unittest.skipUnless(os.name == "nt", "Windows only")
    def test_session_sampler_uses_windows_tree(self):
        from agent_hub_host.resource_management import sample_session_tree
        measured = sample_session_tree({"worker_pid": os.getpid(), "created_at_ms": 1}, 10_000)
        self.assertIsNotNone(measured)
        self.assertGreater(measured["rss_mb"], 1)
        self.assertIsNotNone(measured["cpu_seconds"])
        self.assertTrue(measured["cpu_identity"].startswith("tree:"))

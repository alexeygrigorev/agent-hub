import sys, os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
import unittest

class TestAgent(unittest.TestCase):
    def test_agent(self):
        self.assertTrue(True)

"""board/health.py: the public box-health file for the /board panel."""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "board"))

import health  # noqa: E402

STAT_A = """cpu  100 0 100 800 0 0 0 0 0 0
cpu0 10 0 10 80 0 0 0 0 0 0
cpu12 10 0 10 80 0 0 0 0 0 0
intr 1 2 3
"""
STAT_B = """cpu  200 0 200 900 0 0 0 0 0 0
cpu0 60 0 40 100 0 0 0 0 0 0
cpu12 10 0 10 180 0 0 0 0 0 0
"""
MEMINFO = """MemTotal:       65625088 kB
MemFree:         1290240 kB
MemAvailable:   63836160 kB
Buffers:          102400 kB
Cached:         62000000 kB
"""


class HealthTest(unittest.TestCase):
    def test_busy_split_by_core_group(self):
        a, b = health.read_cpu_times(STAT_A), health.read_cpu_times(STAT_B)
        self.assertEqual(sorted(a), [0, 12])
        self.assertEqual(health.busy_pct(a, b, health.P_CORES), 80.0)     # 80 busy of 100 jiffies
        self.assertEqual(health.busy_pct(a, b, health.E_CORES), 0.0)
        self.assertIsNone(health.busy_pct(a, a, health.P_CORES))

    def test_memory_used_excludes_page_cache(self):
        used, cache, total = health.read_mem(MEMINFO)
        self.assertEqual((used, total), (1747, 64087))
        self.assertEqual(cache, 60647)

    def test_document_is_public_safe_and_atomic(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "health.json")
            doc = health.document([[1, 5.0, 1.0, 1749, 60645, 0.5, 41.0]], 5.0, 64087, 1793350000)
            health.write_atomic(path, doc)
            self.assertEqual(os.listdir(d), ["health.json"])
            with open(path) as f:
                back = json.load(f)
        self.assertEqual(back["fields"][:3], ["t", "p_pct", "e_pct"])
        self.assertEqual(len(back["points"][0]), len(back["fields"]))
        self.assertEqual(set(back), {"generated_at", "interval_s", "box", "fields", "points"})


if __name__ == "__main__":
    unittest.main()

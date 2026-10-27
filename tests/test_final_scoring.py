"""Logic tests for the final-scoring driver's drift-gate state machine and helpers (no Docker).

The clone/build/run/measure-S path is proven by a small real bracket on the box; here we drive
score_track with fake run_team / measure_ref callables to pin the accept / rescore / halt behaviour.
"""
import os
import sys
import types
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "board"))

import final_scoring as fs  # noqa: E402


def teams(n):
    return [types.SimpleNamespace(slug=f"t{i}", name=f"Team {i}") for i in range(n)]


def scripted(seq):
    """A measure_ref() that yields each value in turn; an Exception value is raised."""
    it = iter(seq)

    def measure():
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return measure


def run_ok(team):
    return {"slug": team.slug, "name": team.name, "ok": True, "points": 1.0}


class HelperTest(unittest.TestCase):
    def test_partition(self):
        self.assertEqual(fs.partition([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]])
        self.assertEqual(fs.partition([], 10), [])
        self.assertEqual(fs.partition([1, 2], 5), [[1, 2]])

    def test_drift_fraction(self):
        self.assertAlmostEqual(fs.drift_fraction(100, 102), 0.02)
        self.assertAlmostEqual(fs.drift_fraction(100, 98), 0.02)

    def test_reference_ref_block(self):
        self.assertEqual(fs.reference_ref_block({"T_ref_s": 30.0, "M_ref_bytes": 2e8}, 56),
                         {"T_s": 30.0, "M_bytes": 2e8, "S": 56, "D": 1})


class ScoreTrackTest(unittest.TestCase):
    def _run(self, measure_seq, n_teams=2, batch_size=1, ref0=100.0, rescore_limit=2):
        return fs.score_track("a", teams(n_teams), ref0, run_ok, scripted(measure_seq),
                              batch_size=batch_size, threshold=0.02, rescore_limit=rescore_limit)

    def test_all_clean(self):
        results, drift_log, halt = self._run([100.5, 101.0])
        self.assertIsNone(halt)
        self.assertEqual(set(results), {"t0", "t1"})
        self.assertTrue(all(r["drift_ok"] for r in results.values()))
        self.assertEqual([r["batch"] for r in results.values()], [0, 1])
        self.assertEqual(len(drift_log), 2)
        self.assertEqual(drift_log[1]["ref_before_s"], 100.5)      # shared boundary: after(0) == before(1)

    def test_rescore_recovers(self):
        # batch 1 drifts (104 vs 100.2), then a rescore's fresh before/after (100.5/100.6) is clean.
        results, drift_log, halt = self._run([100.2, 104.0, 100.5, 100.6])
        self.assertIsNone(halt)
        self.assertEqual(set(results), {"t0", "t1"})
        self.assertEqual(len(drift_log), 3)
        self.assertEqual([e["attempt"] for e in drift_log], [0, 0, 1])
        self.assertEqual([e["ok"] for e in drift_log], [True, False, True])

    def test_persistent_drift_halts_after_two_rescores(self):
        # batch 0 drifts on all three attempts (1 initial + 2 rescores) -> halt, nothing accepted.
        results, drift_log, halt = self._run([104.0, 103.0, 107.0, 102.0, 106.0])
        self.assertIsNotNone(halt)
        self.assertTrue(halt["halted"])
        self.assertEqual((halt["track"], halt["batch"]), ("a", 0))
        self.assertEqual(results, {})
        self.assertEqual(len(drift_log), 3)                        # 1 initial + 2 rescores, all failed
        self.assertTrue(all(not e["ok"] for e in drift_log))

    def test_reference_failure_halts_gracefully(self):
        results, drift_log, halt = self._run([RuntimeError("reference will not run on track a")])
        self.assertIsNotNone(halt)
        self.assertEqual(results, {})
        self.assertIn("reference will not run", halt["reason"])

    def test_earlier_batches_survive_a_later_halt(self):
        # batch 0 clean (100.1), batch 1 drifts forever -> halt, but t0 is already scored.
        results, drift_log, halt = self._run([100.1, 105.0, 104.0, 108.0, 103.0, 109.0], n_teams=2, batch_size=1)
        self.assertIsNotNone(halt)
        self.assertEqual((halt["track"], halt["batch"]), ("a", 1))
        self.assertEqual(set(results), {"t0"})                     # batch 0 banked before the halt


if __name__ == "__main__":
    unittest.main()

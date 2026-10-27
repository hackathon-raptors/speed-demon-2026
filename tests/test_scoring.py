import math
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "harness"))

import scoring  # noqa: E402

# Illustrative Track A run used on the site until measured numbers land Oct 28.
NIGHTSHADE = {"T": 41.20 / 1.31, "M": 14.0, "S": 64 / 109, "D": 1.0}


class ScoringTest(unittest.TestCase):
    def test_exponents_sum_to_one(self):
        self.assertAlmostEqual(sum(scoring.EXPONENTS.values()), 1.0, places=12)

    def test_curve_anchors(self):
        self.assertEqual(scoring.speed_points(1), 0.0)
        self.assertAlmostEqual(scoring.speed_points(10), 50.0, places=9)
        self.assertAlmostEqual(scoring.speed_points(100), 100.0, places=9)
        self.assertEqual(scoring.speed_points(1000), 100.0)
        self.assertEqual(scoring.speed_points(0.5), 0.0)
        self.assertEqual(scoring.speed_points(0), 0.0)

    def test_nightshade_vector(self):
        k = scoring.k_from_ratios(NIGHTSHADE)
        self.assertAlmostEqual(k, 8.7773, places=3)
        self.assertAlmostEqual(scoring.speed_points(k), 47.17, places=2)
        self.assertAlmostEqual(scoring.track_total(scoring.speed_points(k), 82.0), 59.36, places=2)

    def test_wall_time_is_uncapped(self):
        # Track A: 0.8 s and 0.2 s used to score the same under the 50x cap.
        base = {"M": 50.0, "S": 0.6, "D": 1.0}
        slow = scoring.k_from_ratios({**base, "T": 41.2 / 0.8})
        fast = scoring.k_from_ratios({**base, "T": 41.2 / 0.2})
        self.assertAlmostEqual(fast / slow, 2.0, places=9)
        gap = scoring.speed_points(fast) - scoring.speed_points(slow)
        self.assertAlmostEqual(gap, 50 * math.log10(2), places=9)

    def test_memory_cap(self):
        at_cap = scoring.k_from_ratios({"T": 10, "M": 50, "S": 1, "D": 1})
        past_cap = scoring.k_from_ratios({"T": 10, "M": 750, "S": 1, "D": 1})
        self.assertEqual(at_cap, past_cap)

    def test_source_size_cannot_pay(self):
        golfed = scoring.k_from_ratios({"T": 10, "M": 10, "S": 50, "D": 1})
        equal = scoring.k_from_ratios({"T": 10, "M": 10, "S": 1, "D": 1})
        self.assertEqual(golfed, equal)

    def test_library_wrapper_loses_to_inline(self):
        # A purpose-built library counts as source, so wrapping it changes
        # nothing on S and costs one dependency on D.
        inline = scoring.k_from_ratios({"T": 30, "M": 14, "S": 64 / 109, "D": 1.0})
        wrapped = scoring.k_from_ratios(
            {"T": 30, "M": 14, "S": 64 / 109, "D": 1 / scoring.dependency_weight(1)}
        )
        self.assertLess(wrapped, inline)
        self.assertAlmostEqual(wrapped / inline, 2 ** -0.15, places=12)

    def test_overall_best_three(self):
        self.assertIsNone(scoring.overall([80, 70]))
        self.assertAlmostEqual(scoring.overall([80, 70, 60, 20]), (80 * 70 * 60) ** (1 / 3), places=9)
        self.assertEqual(scoring.overall([80, 70, 60, 20]), scoring.overall([80, 70, 60]))

    def test_k_from_measurements(self):
        ref = {"T": 41.2, "M": 2940e6, "S": 64, "D": 1}
        team = {"T": 1.31, "M": 210e6, "S": 109, "D": 1}
        self.assertAlmostEqual(
            scoring.k_from_measurements(ref, team), scoring.k_from_ratios(NIGHTSHADE), places=12
        )


class ZeroSourceTest(unittest.TestCase):
    def test_zero_lines_sit_at_the_s_cap(self):
        ref = {"T": 32.05, "M": 228e6, "S": 56, "D": 1}
        zero = scoring.k_from_measurements(ref, {"T": 1.0, "M": 40e6, "S": 0, "D": 1})
        equal = scoring.k_from_measurements(ref, {"T": 1.0, "M": 40e6, "S": 56, "D": 1})
        self.assertEqual(zero, equal)
        with self.assertRaises(ValueError):
            scoring.k_from_measurements(ref, {"T": 0, "M": 40e6, "S": 56, "D": 1})


if __name__ == "__main__":
    unittest.main()

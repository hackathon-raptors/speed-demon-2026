import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "board"))
import tape  # noqa: E402


def ev(slug, tk, move, gain, at):
    return {"at": at, "slug": slug, "team": slug.upper(), "tk": tk, "pts": 1.0, "gain": gain, "move": move, "rank": 1}


class Climbers(unittest.TestCase):
    NOW = tape._epoch("2026-10-31T12:00:00Z")

    def test_sums_places_within_the_hour(self):
        events = [ev("a", "a", 3, 0.5, "2026-10-31T11:10:00Z"), ev("a", "b", 2, 0.2, "2026-10-31T11:50:00Z"),
                  ev("b", "c", 4, 1.0, "2026-10-31T11:30:00Z"), ev("c", "a", 9, 2.0, "2026-10-31T10:30:00Z")]
        out = tape.climbers(events, self.NOW)
        self.assertEqual([c["slug"] for c in out], ["a", "b"])
        self.assertEqual((out[0]["climb"], out[0]["gain"], out[0]["tracks"]), (5, 0.7, ["a", "b"]))

    def test_ignores_fails_overall_and_no_moves(self):
        events = [{"at": "2026-10-31T11:30:00Z", "slug": "x", "team": "X", "tk": "a", "fail": "correctness"},
                  ev("y", "all", 5, None, "2026-10-31T11:30:00Z"), ev("z", "a", None, None, "2026-10-31T11:30:00Z"),
                  ev("w", "a", 0, 0.1, "2026-10-31T11:30:00Z")]
        self.assertEqual(tape.climbers(events, self.NOW), [])

    def test_top_three_tie_broken_by_gain(self):
        events = [ev(s, "a", 2, g, "2026-10-31T11:30:00Z") for s, g in (("p", 0.1), ("q", 0.3), ("r", 0.2), ("s", 0.05))]
        self.assertEqual([c["slug"] for c in tape.climbers(events, self.NOW)], ["q", "r", "p"])



class Crowns(unittest.TestCase):
    def row(self, slug, tk, pts, at):
        return {"team_slug": slug, "team": slug.upper(), "track": tk, "ok": True, "points": pts, "K": pts, "ended_at": at}

    def test_holder_changes_and_time_held(self):
        rows = [self.row("x", "a", 50, "2026-10-31T10:00:00Z"), self.row("y", "a", 40, "2026-10-31T10:30:00Z"),
                self.row("y", "a", 60, "2026-10-31T11:00:00Z"), self.row("x", "a", 55, "2026-10-31T11:10:00Z")]
        crowns = {}
        tape.replay(rows, keep=0, crowns=crowns)
        self.assertEqual([(c["slug"], c["from"]) for c in crowns["a"]], [("x", "2026-10-31T10:00:00Z"), ("y", "2026-10-31T11:00:00Z")])
        out = tape.crown_summary(crowns, tape._epoch("2026-10-31T12:00:00Z"))
        self.assertEqual([(h["slug"], h["s"]) for h in out["a"]["held"]], [("x", 3600), ("y", 3600)])
        self.assertNotIn("all", crowns)          # nobody has three tracks yet


if __name__ == "__main__":
    unittest.main()

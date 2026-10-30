"""records.build_stats and steps.track_language: the public stats for the /board panel."""
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "board"))
sys.path.insert(0, os.path.join(REPO, "harness"))

import axes  # noqa: E402
import records  # noqa: E402
import steps  # noqa: E402

START, END = "2026-10-30T15:00:00Z", "2026-11-02T15:00:00Z"
T0 = records.epoch(START)


def rec(team, track, at, ok=True, K=None, stage=None, langs=None, commit="c1"):
    return {"team": team.upper(), "team_slug": team, "track": track, "commit": commit, "ok": ok,
            "stage": stage or ("scored" if ok else "run"), "K": K if ok else None, "T_s": 1.0, "points": 50.0,
            "recorded_at": records.iso(T0 + at), "langs": langs if langs is not None else {"Rust": 100}}


class BuildStatsTest(unittest.TestCase):
    def stats(self, recs, now_after=3 * 3600):
        return records.build_stats(T0 + now_after, [object()] * 4, recs, START, END)

    def test_counters_hours_records_languages(self):
        recs = [rec("a", "a", 60, K=5.0), rec("a", "c", 120, K=2.0),
                rec("b", "a", 3700, K=9.0, langs={"Go": 300}, commit="c2"), rec("b", "a", 3800, ok=False, commit="c3"),
                rec("c", "a", 7300, stage="unchanged", ok=False, langs={}),
                {"team": "A", "team_slug": "a", "stage": "fetch", "ok": False, "recorded_at": records.iso(T0)}]
        s = self.stats(recs)
        self.assertEqual(s["counters"], {"teams": 4, "teams_scored": 2, "teams_active_1h": 1, "pushes": 4, "runs": 4})
        self.assertEqual(s["runs_per_hour"]["points"], [[T0, 2, 0], [T0 + 3600, 1, 1], [T0 + 7200, 0, 0],
                                                        [T0 + 10800, 0, 0]])
        self.assertEqual((s["records"]["a"]["K"], s["records"]["a"]["team"]), (9.0, "B"))
        self.assertEqual(s["records"]["c"]["K"], 2.0)
        self.assertEqual(s["languages"], [["Go", 1], ["Rust", 1]])      # teams, not entries: a's two tracks are one

    def test_active_and_no_bins_past_freeze(self):
        s = records.build_stats(T0 + 5 * 86400, [], [rec("a", "a", 3 * 86400 - 60, K=1.0)], START, END)
        self.assertEqual(s["runs_per_hour"]["points"][-1][0], T0 + 72 * 3600)
        self.assertEqual(s["counters"]["teams_active_1h"], 0)
        s = records.build_stats(T0 + 600, [], [rec("a", "a", 300, K=1.0)], START, END)
        self.assertEqual(s["counters"]["teams_active_1h"], 1)

    def test_baseline_is_not_counted(self):
        import types
        teams = [types.SimpleNamespace(name="A"), types.SimpleNamespace(name="REF")]
        recs = [rec("a", "a", 60, K=5.0), rec("ref", "a", 70, K=1.0, commit="r1"), rec("ref", "b", 80, K=1.0, commit="r1")]
        s = records.build_stats(T0 + 3 * 3600, teams, recs, START, END, exclude=["REF"])
        self.assertEqual((s["counters"]["teams"], s["counters"]["teams_scored"], s["counters"]["pushes"], s["counters"]["runs"]), (1, 1, 1, 1))
        self.assertEqual(s["records"]["a"]["team"], "A")

    def test_feed_carries_language_names_not_line_counts(self):
        out = records.summarise(rec("a", "a", 60, K=5.0, langs={"Rust": 900, "C": 120, "Shell": 5}))
        self.assertNotIn("langs", out)
        self.assertEqual(out["lang"], ["Rust", "C"])          # biggest first, under 10% dropped
        self.assertNotIn("900", json.dumps(out))
        self.assertNotIn("lang", records.summarise(rec("a", "a", 60, K=5.0, langs={})))

    def test_ten_percent_rule(self):
        self.assertEqual(sorted(records.entry_languages({"Rust": 900, "Python": 120})), ["Python", "Rust"])
        self.assertEqual(records.entry_languages({"C": 2000, "Shell": 15}), ["C"])
        self.assertEqual(records.entry_languages({"Go": 0}), ["Go"])           # build-image fallback, no lines
        self.assertEqual(records.entry_languages(None), [])
        s = self.stats([rec("a", "a", 60, K=1.0, langs={"C++": 800, "Python": 200}),
                        rec("b", "c", 70, K=1.0, langs={"C++": 50, "Shell": 2})])
        self.assertEqual(s["languages"], [["C++", 2], ["Python", 1]])


class CodeLanguagesTest(unittest.TestCase):
    def test_from_scc_per_language(self):
        per = {"C": {"code": 400, "counted": True}, "C Header": {"code": 40, "counted": True},
               "C++ Header": {"code": 10, "counted": True}, "Dockerfile": {"code": 9, "counted": True},
               "JSON": {"code": 500, "counted": False}, "Shell": {"code": 0, "counted": True}}
        self.assertEqual(axes.code_languages(per), {"C": 440, "C++": 10})


class TrackLanguageTest(unittest.TestCase):
    def lang(self, text):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "Dockerfile"), "w") as f:
            f.write(text)
        return steps.track_language(d)

    def test_build_stage_names_the_language(self):
        self.assertEqual(self.lang("FROM gcc:15@sha256:x AS build\nFROM debian:13-slim@sha256:y\n"), "C/C++")
        self.assertEqual(self.lang("# c\nFROM \\\n  rust:1-trixie@sha256:x\n"), "Rust")
        self.assertEqual(self.lang("from eclipse-temurin:25-jdk@sha256:x AS b\nFROM eclipse-temurin:25-jre@sha256:y\n"),
                         "JVM")
        self.assertEqual(self.lang("FROM debian:13-slim@sha256:y\n"), "")
        self.assertEqual(steps.track_language("/nonexistent"), "")


if __name__ == "__main__":
    unittest.main()

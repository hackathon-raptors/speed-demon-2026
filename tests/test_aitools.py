import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "board"))
import aitools  # noqa: E402

HUMAN = ("Ann <ann@example.com>", "Ann <ann@example.com>")


class Fingerprint(unittest.TestCase):
    def test_claude_trailer_and_model(self):
        t, m = aitools.fingerprint(HUMAN, "Speed up parse\n\nCo-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>")
        self.assertEqual((t, m), ({"Claude Code"}, {"Claude Opus 5.5"}))

    def test_claude_code_is_not_a_model(self):
        t, m = aitools.fingerprint(HUMAN, "x\n\n🤖 Generated with [Claude Code](https://claude.com/claude-code)\n"
                                          "Co-Authored-By: Claude Code <noreply@anthropic.com>")
        self.assertEqual((t, m), ({"Claude Code"}, set()))

    def test_aider_model(self):
        t, m = aitools.fingerprint(("Bob (aider) <bob@x.io>", "Bob <bob@x.io>"), "aider: tweak\n\nCo-authored-by: aider (openai/gpt-5) <aider@aider.chat>")
        self.assertEqual((t, m), ({"aider"}, {"openai/gpt-5"}))

    def test_bot_author(self):
        t, _ = aitools.fingerprint(("Copilot <198982749+Copilot@users.noreply.github.com>", "GitHub <noreply@github.com>"), "Fix")
        self.assertEqual(t, {"Copilot"})

    def test_free_text_mention_is_ignored(self):
        t, _ = aitools.fingerprint(HUMAN, "Compare with what claude suggested and cursor position fix")
        self.assertEqual(t, set())


class Team(unittest.TestCase):
    def test_primary_from_commits_then_files(self):
        commits = [(HUMAN, "a\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"),
                   (HUMAN, "b\n\nCo-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"),
                   (HUMAN, "c\n\nCo-authored-by: Codex <codex@openai.com>")]
        r = aitools.team_result(commits, {"track-a/main.rs", "AGENTS.md", ".cursor/rules/x.mdc"})
        self.assertEqual(r["tools"], ["Claude Code", "Codex", "Cursor"])
        self.assertEqual(r["primary"], "Claude Code")
        self.assertEqual(r["models"], ["Claude Sonnet 5.5"])
        self.assertEqual(aitools.team_result([(HUMAN, "x")], {"CLAUDE.md"})["primary"], "Claude Code")
        self.assertIsNone(aitools.team_result([(HUMAN, "x")], {"README.md"})["primary"])


class Aggregate(unittest.TestCase):
    def test_fold_and_stats_without_names(self):
        res = {"A": {"tools": ["Claude Code"], "models": ["Claude Opus 5.5"]},
               "B": {"tools": ["Claude Code", "Kiro"], "models": ["Claude Opus 5.5"]},
               "C": {"tools": ["Cursor"], "models": []},
               "D": {"tools": [], "models": []}}
        overall = {"A": 80.0, "B": 70.0, "C": 60.0, "D": 50.0}
        out = aitools.aggregate(res, overall)
        names = [r["name"] for r in out["tools"]]
        self.assertEqual(names, ["Claude Code", "other"])            # Kiro and Cursor fold into other (2 teams)
        cc = out["tools"][0]
        self.assertEqual((cc["teams"], cc["median_overall"], cc["best_overall"], cc["share"]), (2, 75.0, 80.0, 0.5))
        self.assertEqual(out["models"], [{"name": "Claude Opus 5.5", "teams": 2, "median_overall": 75.0, "best_overall": 80.0}])
        self.assertEqual((out["teams_detected"], out["none"]["teams"]), (3, 1))
        self.assertNotIn('"A"', str(out))

    def test_overall_by_team(self):
        board = {"teams": [{"team": "X", "tracks": {k: {"best": {"ok": True, "points": p, "K": 1}} for k, p in zip("abc", (80, 80, 80))}},
                           {"team": "Y", "tracks": {"a": {"best": {"ok": True, "points": 90, "K": 1}}}}]}
        o = aitools.overall_by_team(board)
        self.assertAlmostEqual(o["X"], 80.0)
        self.assertIsNone(o["Y"])


if __name__ == "__main__":
    unittest.main()

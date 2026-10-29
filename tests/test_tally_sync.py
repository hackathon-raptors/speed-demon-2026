"""tally_sync: Tally submissions -> the board's registry.json."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "board"))

import sources  # noqa: E402
import tally_sync  # noqa: E402

# Shapes copied from a real Ar7g9z submission read back through the API (Oct 28).
QUESTIONS = [
    {"id": "N49eaO", "type": "INPUT_TEXT", "title": "Team name"},
    {"id": "qY729g", "type": "INPUT_EMAIL", "title": "Contact email"},
    {"id": "96gz85", "type": "INPUT_LINK", "title": "GitHub repository"},
    {"id": "eNjdVl", "type": "CHECKBOXES", "title": "Tracks you are entering"},
]


def read(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def sub(sid, at, team, repo, tracks, completed=True):
    return {"id": sid, "submittedAt": at, "isCompleted": completed, "responses": [
        {"questionId": "N49eaO", "answer": team},
        {"questionId": "qY729g", "answer": "x@example.com"},
        {"questionId": "96gz85", "answer": repo},
        {"questionId": "eNjdVl", "answer": tracks},
    ]}


class BuildRegistry(unittest.TestCase):
    def test_real_shape(self):
        entries, problems = tally_sync.build_registry(QUESTIONS, [
            sub("vX8e7Z0", "2026-10-28T11:13:12.000Z", "Swift C",
                "https://github.com/acme/speed", ["A · Ingest", "C · Join"])])
        self.assertEqual(problems, [])
        self.assertEqual(entries[0]["team"], "Swift C")
        self.assertEqual(entries[0]["tracks"], ["a", "c"])

    def test_latest_submission_per_team_wins(self):
        entries, _ = tally_sync.build_registry(QUESTIONS, [
            sub("s2", "2026-10-29T10:00:00Z", "Swift C", "https://github.com/acme/fixed", ["B · Index"]),
            sub("s1", "2026-10-28T10:00:00Z", "swift c", "https://github.com/acme/typo", ["A · Ingest"]),
        ])
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repo"], "https://github.com/acme/fixed")
        self.assertEqual(entries[0]["tracks"], ["b"])

    def test_resubmission_to_another_owner_is_held(self):
        held = []
        entries, problems = tally_sync.build_registry(QUESTIONS, [
            sub("s1", "2026-10-28T10:00:00Z", "Swift C", "https://github.com/acme/fast", ["A · Ingest"]),
            sub("s2", "2026-10-29T10:00:00Z", "SWIFT C", "https://github.com/mallory/fast", ["A · Ingest"]),
            sub("s3", "2026-10-29T11:00:00Z", "Swift C", "https://github.com/ACME/fast2", ["C · Join"]),
        ], held=held)
        self.assertEqual(entries[0]["repo"], "https://github.com/ACME/fast2")     # same owner, any case: applied
        self.assertEqual([h["submission"] for h in held], ["s2"])
        self.assertEqual((held[0]["pinned_owner"], held[0]["new_owner"]), ("acme", "mallory"))
        self.assertTrue(any("held" in p for p in problems))

    def test_extra_releases_a_held_owner_change(self):
        entries, _ = tally_sync.build_registry(QUESTIONS, [
            sub("s1", "2026-10-28T10:00:00Z", "Swift C", "https://github.com/acme/fast", ["A · Ingest"]),
            sub("s2", "2026-10-29T10:00:00Z", "Swift C", "https://github.com/neworg/fast", ["A · Ingest"]),
        ], extra=[{"team": "Swift C", "repo": "https://github.com/neworg/fast", "tracks": ["a"]}])
        self.assertEqual(entries[0]["repo"], "https://github.com/neworg/fast")

    def test_team_names_are_cleaned(self):
        entries, _ = tally_sync.build_registry(QUESTIONS, [
            sub("s1", "2026-10-28T10:00:00Z", "Evil‮Team​\x07  X" + "y" * 200, "https://github.com/a/b",
                ["A · Ingest"]),
        ])
        name = entries[0]["team"]
        self.assertEqual(name[:11], "EvilTeam Xy")
        self.assertEqual(len(name), sources.NAME_MAX_CHARS)
        self.assertEqual(sources.clean_name("did it run?"), "did it run?")

    def test_held_submission_is_a_security_event_once(self):
        d = tempfile.mkdtemp()
        subs = [sub("s1", "2026-10-28T10:00:00Z", "T", "https://github.com/a/r", ["A · Ingest"]),
                sub("s2", "2026-10-29T10:00:00Z", "T", "https://github.com/m/r", ["A · Ingest"])]
        out = os.path.join(d, "registry.json")
        with mock.patch.object(tally_sync, "fetch_submissions", return_value=(QUESTIONS, subs)), \
                mock.patch.dict(os.environ, {"TALLY_API_KEY": "k"}):
            for _ in range(2):
                self.assertEqual(tally_sync.main(["--out", out, "--security-dir", d]), 0)
        with open(os.path.join(d, "security.jsonl"), encoding="utf-8") as f:
            events = [json.loads(line) for line in f]
        self.assertEqual([(e["kind"], e["severity"]) for e in events], [("registry_takeover", "high")])
        self.assertEqual(read(out)[0]["repo"], "https://github.com/a/r")

    def test_refused_entries_are_reported_not_written(self):
        entries, problems = tally_sync.build_registry(QUESTIONS, [
            sub("s1", "1", "No Repo", "https://gitlab.com/x/y", ["A · Ingest"]),
            sub("s2", "2", "No Tracks", "https://github.com/x/y", []),
            sub("s3", "3", "", "https://github.com/x/z", ["A · Ingest"]),
            sub("s4", "4", "Partial", "https://github.com/x/p", ["A · Ingest"], completed=False),
        ])
        self.assertEqual(entries, [])
        self.assertEqual(len(problems), 3)

    def test_exclude_by_team_or_submission_id(self):
        subs = [sub("s1", "1", "ZZ Sync Test", "https://github.com/x/a", ["A · Ingest"]),
                sub("s2", "2", "Keep", "https://github.com/x/b", ["A · Ingest"]),
                sub("s3", "3", "Gone", "https://github.com/x/c", ["A · Ingest"])]
        entries, _ = tally_sync.build_registry(QUESTIONS, subs, exclude=["zz sync test", "S3"])
        self.assertEqual([e["team"] for e in entries], ["Keep"])

    def test_extra_overrides_the_form(self):
        entries, _ = tally_sync.build_registry(
            QUESTIONS, [sub("s1", "1", "Keep", "https://github.com/x/b", ["A · Ingest"])],
            extra=[{"team": "Keep", "repo": "https://github.com/x/override", "tracks": ["d"]},
                   {"team": "Organiser", "repo": "https://github.com/x/o", "tracks": ["a", "b"]}])
        by = {e["team"]: e for e in entries}
        self.assertEqual(by["Keep"]["repo"], "https://github.com/x/override")
        self.assertIn("Organiser", by)

    def test_renamed_question_fails_loudly(self):
        qs = [q for q in QUESTIONS if q["title"] != "GitHub repository"]
        with self.assertRaises(RuntimeError):
            tally_sync.build_registry(qs, [])

    def test_output_loads_in_the_board(self):
        entries, _ = tally_sync.build_registry(QUESTIONS, [
            sub("s1", "1", "Swift C", "https://github.com/acme/speed", ["A · Ingest", "D · Traverse"]),
            sub("s2", "2", "Ünïcode Team", "github.com/acme/other/", ["B · Index"])])
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            tally_sync.write_if_changed(path, entries)
            teams, problems = sources.load_registry(path)
        self.assertEqual(problems, [])
        self.assertEqual(sorted(t.name for t in teams), ["Swift C", "Ünïcode Team"])
        self.assertEqual({t.name: t.tracks for t in teams}["Swift C"], ["a", "d"])


class WriteAndMain(unittest.TestCase):
    def test_write_only_when_changed(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            self.assertTrue(tally_sync.write_if_changed(path, [{"team": "A"}]))
            self.assertFalse(tally_sync.write_if_changed(path, [{"team": "A"}]))
            self.assertTrue(tally_sync.write_if_changed(path, [{"team": "B"}]))
            self.assertEqual(os.listdir(d), ["registry.json"])  # no temp files left behind

    def run_main(self, path, questions, subs, err=None):
        def fetch(form, key):
            if err:
                raise err
            return questions, subs
        with mock.patch.object(tally_sync, "fetch_submissions", fetch), \
                mock.patch.dict(os.environ, {"TALLY_API_KEY": "k"}):
            return tally_sync.main(["--out", path])

    def test_empty_form_never_wipes_a_filled_registry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            tally_sync.write_if_changed(path, [{"team": "A", "repo": "https://github.com/x/a", "tracks": ["a"]}])
            self.assertEqual(self.run_main(path, QUESTIONS, []), 1)
            self.assertEqual(len(read(path)), 1)

    def test_fetch_failure_leaves_registry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            tally_sync.write_if_changed(path, [{"team": "A"}])
            self.assertEqual(self.run_main(path, QUESTIONS, [], err=OSError("down")), 1)
            self.assertEqual(read(path), [{"team": "A"}])

    def test_main_writes_registry(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            rc = self.run_main(path, QUESTIONS, [sub("s1", "1", "T", "https://github.com/x/t", ["C · Join"])])
            self.assertEqual(rc, 0)
            self.assertEqual(read(path)[0]["tracks"], ["c"])


if __name__ == "__main__":
    unittest.main()

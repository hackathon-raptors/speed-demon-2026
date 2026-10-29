"""board/security.py: private, alert-only security events."""
import json
import os
import stat
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "board"))

import fairqueue  # noqa: E402
import records  # noqa: E402
import security  # noqa: E402
import sources  # noqa: E402


class FlagsTest(unittest.TestCase):
    def test_dockerfile_reach_vs_slip(self):
        self.assertEqual(security.dockerfile_flags("Dockerfile check: base image is not on the allowlist (pin it by digest): x"), [])
        for reason in ("Dockerfile check: the `# escape=` parser directive is not allowed; remove it",
                       "Dockerfile check: COPY --from must name an earlier stage, not an image: busybox",
                       "Dockerfile check: RUN --network is not allowed: 'RUN --network=host x'",
                       "Dockerfile check: ADD may not fetch a URL or git repo: http://x",
                       "track-a/Dockerfile must be a regular file, not a symlink"):
            with self.subTest(reason):
                (f,) = security.dockerfile_flags(reason)
                self.assertEqual((f["kind"], f["severity"]), ("dockerfile_reach", "medium"))

    def test_suspicious_files(self):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "main.rs"), "w") as f:
            f.write("fn main() {}\n")
        with open(os.path.join(d, "helper"), "wb") as f:
            f.write(b"\x7fELF" + b"\0" * 100)
        with open(os.path.join(d, "code.tar.gz"), "wb") as f:
            f.write(b"\x1f\x8b" + b"\0" * 100)
        with open(os.path.join(d, "blob.bin"), "wb") as f:
            f.write(b"\xff\xfe" * (security.BLOB_BYTES // 2 + 10))
        with open(os.path.join(d, "big.txt"), "w") as f:
            f.write("x" * (security.BLOB_BYTES + 10))
        found = security.suspicious_files(d)
        self.assertEqual(sorted(found), ["blob.bin (binary blob over 1 MB)", "code.tar.gz (gzip archive)",
                                         "helper (ELF binary)"])

    def test_run_flags(self):
        kinds = lambda r, floor=None: [f["kind"] for f in security.run_flags(r, floor)]  # noqa: E731
        self.assertEqual(kinds({"ok": True, "wall_s": 1.0, "timing_gap_s": 0.4}, 0.05), [])
        self.assertEqual(kinds({"ok": False, "tamper": True, "container_rc": 125}), ["timing_tamper"])
        self.assertEqual(kinds({"ok": False, "fs_diff": ["A /x"]}), ["sandbox_write"])
        self.assertEqual(kinds({"ok": False, "reason": "summary.txt must be a regular file of at most 2 GB (x)"}),
                         ["output_not_regular"])
        self.assertEqual(kinds({"ok": True, "wall_s": 1.0, "timing_gap_s": 9.0}), ["timing_gap"])
        self.assertEqual(kinds({"ok": True, "wall_s": 0.002}, 0.05), ["too_fast"])
        self.assertEqual(kinds(None), [])


class SecurityLogTest(unittest.TestCase):
    def test_emit_dedupe_summary_and_private_modes(self):
        d = tempfile.mkdtemp()
        log = security.SecurityLog(d)
        self.assertTrue(log.emit("timing_tamper", "high", "x", team="A", key="k1", now=0))
        self.assertFalse(log.emit("timing_tamper", "high", "x", team="A", key="k1", now=1))
        log.emit_flags([security.flag("review_blob", "low", "y")], team="B", track="a", run_id="r", commit="c")
        with open(log.summary_path, encoding="utf-8") as f:
            summary = json.load(f)
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["by_severity"], {"low": 1, "medium": 0, "high": 1})
        self.assertEqual(summary["by_team"], {"A": {"timing_tamper": 1}, "B": {"review_blob": 1}})
        self.assertEqual(summary["recent"][0]["kind"], "review_blob")
        for p in (log.path, log.summary_path):
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), 0o600)

    def test_feed_never_carries_security_flags(self):
        team = sources.Team(name="A", slug="a-1", repo="https://github.com/a/b.git", tracks=["a"])
        rec = {"team": "A", "team_slug": "a-1", "track": "a", "run_id": "r1", "ok": False, "stage": "run",
               "failure": "x" * 1000, "stderr_tail": ["secret-ish"],
               "security": [security.flag("timing_tamper", "high", "evidence")]}
        feed = records.build_feed(10, [team], fairqueue.FairQueue(900), {"running": None}, [rec], {})
        text = json.dumps(feed)
        for needle in ("timing_tamper", "evidence", "secret-ish"):
            self.assertNotIn(needle, text)
        self.assertLessEqual(len(feed["teams"][0]["tracks"]["a"]["latest"]["failure"]), records.FAILURE_MAX_CHARS)


if __name__ == "__main__":
    unittest.main()

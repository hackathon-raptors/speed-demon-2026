"""The live board: scheduling rules, sources, checks, records, guards, and one Docker end-to-end run.

    python3.13 -m unittest tests.test_board -v

The Docker test builds the naive Track A reference from a bare git repo on the
board's own buildx builder and runs it once on the tiny dataset. It is skipped
without Docker, and needs root (or Colima) to read the run's memory.peak.
"""
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, os.path.join(REPO, "board"))

import board  # noqa: E402
import fairqueue  # noqa: E402
import guards  # noqa: E402
import records  # noqa: E402
import sources  # noqa: E402
import steps  # noqa: E402

PY = "python:3.13-slim@sha256:d98cc29d3266e535dd9da1099688d9cb3ca4753813571e071763d17725460962"
TINY_A = os.path.join(REPO, ".data", "a", "tiny-seed1")
TINY_A_SHA = "c24fa507c963eaeac9224fd8435019b4f4e8c002ece7e32f316681a3957eb92f"
REF_DOCKERFILE = f'FROM {PY}\nCOPY a_ingest.py /app/a_ingest.py\nENTRYPOINT ["python3", "/app/a_ingest.py"]\n'
STOLEN_DOCKERFILE = f"FROM {PY}\nCOPY --from=postgres:17 /usr/lib/postgresql /pg\nCOPY a_ingest.py /app/\n"


def reference_text(name):
    with open(os.path.join(REPO, "reference", name), encoding="utf-8") as f:
        return f.read()


def git(*args, cwd=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.com", GIT_COMMITTER_NAME="t",
               GIT_COMMITTER_EMAIL="t@example.com", GIT_CONFIG_NOSYSTEM="1", HOME=cwd or os.getcwd())
    return subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True).stdout.strip()


class FakeRepo:
    """A bare repo plus a working clone that pushes to its speed-demon branch."""

    def __init__(self, root, name):
        self.bare = os.path.join(root, f"{name}.git")
        self.work = os.path.join(root, f"{name}-work")
        os.makedirs(self.work)
        git("init", "--quiet", "--bare", self.bare, cwd=root)
        git("init", "--quiet", "-b", "speed-demon", cwd=self.work)
        git("remote", "add", "origin", self.bare, cwd=self.work)

    def push(self, files, message="push"):
        for rel, content in files.items():
            path = os.path.join(self.work, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(content)
        git("add", "-A", cwd=self.work)
        git("commit", "--quiet", "--allow-empty", "-m", message, cwd=self.work)
        git("push", "--quiet", "-f", "origin", "speed-demon", cwd=self.work)
        return git("rev-parse", "HEAD", cwd=self.work)


class FairQueueTest(unittest.TestCase):
    def test_one_queued_run_per_team_newest_wins(self):
        q = fairqueue.FairQueue(900)
        self.assertTrue(q.observe("a", "1" * 40, 100))
        self.assertTrue(q.observe("a", "2" * 40, 110))
        self.assertEqual(q.teams["a"].pending["commit"], "2" * 40)
        self.assertFalse(q.observe("a", "2" * 40, 120))       # unchanged head queues nothing
        self.assertEqual(len([s for s in q.teams.values() if s.pending]), 1)

    def test_second_push_inside_interval_waits(self):
        q = fairqueue.FairQueue(900)
        q.observe("a", "1" * 40, 0)
        q.dispatch("a", 10, "j1")
        q.finish("a")
        q.observe("a", "2" * 40, 300)
        self.assertIsNone(q.next_eligible(600))
        self.assertEqual(q.eligible_at(q.teams["a"]), 910)
        self.assertEqual(q.next_wakeup(600), 910)
        self.assertEqual(q.next_eligible(910).slug, "a")

    def test_least_recently_served_first(self):
        q = fairqueue.FairQueue(900)
        for slug, t in (("old", 0), ("new", 50)):
            q.observe(slug, "1" * 40, t)
            q.dispatch(slug, t, "j")
            q.finish(slug)
        q.observe("new", "2" * 40, 2000)
        q.observe("old", "2" * 40, 2001)
        q.observe("never", "3" * 40, 2002)
        order = []
        while (st := q.next_eligible(3000)) is not None:
            order.append(st.slug)
            q.dispatch(st.slug, 3000, "j")
        self.assertEqual(order, ["never", "old", "new"])

    def test_inflight_blocks_and_its_commit_does_not_requeue(self):
        q = fairqueue.FairQueue(0)
        q.observe("a", "1" * 40, 0)
        q.dispatch("a", 0, "j1")
        self.assertTrue(q.observe("a", "2" * 40, 5))
        self.assertIsNone(q.next_eligible(10))                 # one job in flight per team
        self.assertFalse(q.observe("a", "1" * 40, 6))          # force-push back to the running commit
        self.assertIsNone(q.teams["a"].pending)

    def test_clone_saw_newer_head(self):
        q = fairqueue.FairQueue(0)
        q.observe("a", "1" * 40, 0)
        q.dispatch("a", 0, "j1")
        q.observe("a", "2" * 40, 1)
        q.set_commit("a", "2" * 40)                            # the clone already got the newer push
        self.assertIsNone(q.teams["a"].pending)
        q.finish("a")
        self.assertFalse(q.observe("a", "2" * 40, 2))

    def test_interrupted_job_requeued_once(self):
        q = fairqueue.FairQueue(900)
        q.observe("a", "1" * 40, 0)
        q.dispatch("a", 5, "j1")
        inf, requeued = q.requeue_interrupted("a", max_attempts=2)
        self.assertTrue(requeued)
        self.assertEqual(q.next_eligible(6).slug, "a")         # the lost dispatch does not count
        q.dispatch("a", 6, "j2")
        inf, requeued = q.requeue_interrupted("a", max_attempts=2)
        self.assertFalse(requeued)
        self.assertEqual(inf["attempt"], 2)
        self.assertIsNone(q.teams["a"].pending)

    def test_queue_order(self):
        q = fairqueue.FairQueue(900)
        q.observe("served", "1" * 40, 0)
        q.dispatch("served", 0, "j")
        q.finish("served")
        q.observe("served", "2" * 40, 100)
        q.observe("fresh", "3" * 40, 200)
        self.assertEqual([s for s, _ in q.queue_order(300)], ["fresh", "served"])

    def test_state_round_trips(self):
        st = fairqueue.TeamState(slug="a", head="1" * 40, pending={"commit": "2" * 40, "pushed_at": 1.0, "attempt": 1})
        self.assertEqual(fairqueue.TeamState.from_json(json.loads(json.dumps(st.to_json()))), st)


class RegistryTest(unittest.TestCase):
    def test_github_urls_normalise(self):
        want = "https://github.com/owner/repo.git"
        for url in ("https://github.com/owner/repo", "https://github.com/owner/repo.git", "github.com/owner/repo/",
                    "http://www.github.com/owner/repo", "git@github.com:owner/repo.git",
                    "https://github.com/owner/repo/tree/speed-demon"):
            self.assertEqual(sources.normalise_repo(url), (want, ""), url)

    def test_other_urls_rejected(self):
        for url in ("https://gitlab.com/o/r", "file:///etc", "/srv/x", "ext::sh -c id", "https://github.com/o", ""):
            self.assertEqual(sources.normalise_repo(url)[0], "", url)
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(sources.normalise_repo(d, allow_local=True), (d, ""))

    def test_load_registry(self):
        entries = [
            {"team": "Raptors", "repo": "https://github.com/a/b", "tracks": ["A", "track-c", "e"], "discord": "x"},
            {"team": "raptors", "repo": "https://github.com/a/c", "tracks": ["a"]},
            {"team": "火箭队", "repo": "https://github.com/a/d", "tracks": ["d"]},
            {"team": "", "repo": "https://github.com/a/e"},
            {"team": "Bad", "repo": "https://example.com/x"},
        ]
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "registry.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
            teams, problems = sources.load_registry(path)
        self.assertEqual([t.name for t in teams], ["Raptors", "火箭队"])
        self.assertEqual(teams[0].tracks, ["a", "c"])
        self.assertTrue(teams[1].slug.startswith("team-"))
        self.assertEqual(len(problems), 4)


class GitTest(unittest.TestCase):
    def test_token_only_in_environment(self):
        token = "ghp_" + "A1b2" * 9
        g = sources.Git(token)
        env = g.env()
        values = [v for k, v in env.items() if k.startswith("GIT_CONFIG_VALUE_")]
        self.assertTrue(any(v.startswith("Authorization: Basic ") for v in values))
        self.assertFalse(any(token in v for v in values))      # base64 of user:token, not the token
        self.assertEqual(g.redact(f"fatal: https://x:{token}@github.com {values[-1]}").count("***"), 2)
        self.assertNotIn(token, g.redact(f"oops {token}"))

    def test_ls_remote_and_clone_local_repo(self):
        with tempfile.TemporaryDirectory() as d:
            repo = FakeRepo(d, "team")
            g = sources.Git()
            self.assertEqual(g.ls_remote(repo.bare, "speed-demon"), ("", ""))   # readable, no branch yet
            sha = repo.push({"README": "hi\n"})
            self.assertEqual(g.ls_remote(repo.bare, "speed-demon"), (sha, ""))
            got, err = g.clone(repo.bare, "speed-demon", os.path.join(d, "co"))
            self.assertEqual((got, err), (sha, ""))
            self.assertTrue(g.ls_remote(os.path.join(d, "missing.git"), "speed-demon")[1])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.allow = [{"image": "python:3.13-slim", "digest": PY.split("@")[1]}]

    def tearDown(self):
        shutil.rmtree(self.dir)

    def write(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_reference_style_dockerfile_passes(self):
        self.write("track-a/Dockerfile", REF_DOCKERFILE)
        self.assertEqual(steps.present_tracks(self.dir), ["a"])
        self.assertEqual(steps.check_track(self.dir, "a", self.allow), "")

    def test_copy_from_foreign_image_rejected(self):
        self.write("track-a/Dockerfile", STOLEN_DOCKERFILE)
        reason = steps.check_track(self.dir, "a", self.allow)
        self.assertIn("--from must name an earlier stage", reason)
        self.assertIn("postgres:17", reason)

    def test_symlinks_and_missing(self):
        self.assertIn("missing", steps.check_track(self.dir, "b", self.allow))
        os.makedirs(os.path.join(self.dir, "track-c"))
        self.assertIn("missing", steps.check_track(self.dir, "c", self.allow))
        secret = self.write("outside/secret", "FROM ubuntu\n")
        os.symlink(secret, os.path.join(self.dir, "track-c", "Dockerfile"))
        self.assertIn("not a symlink", steps.check_track(self.dir, "c", self.allow))
        os.symlink(os.path.dirname(secret), os.path.join(self.dir, "track-d"))
        self.assertIn("not a symlink", steps.check_track(self.dir, "d", self.allow))


class UnchangedReferenceTest(unittest.TestCase):
    """The starter as shipped is the reference itself: the board reports it and does not run it."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        for name in ("Dockerfile", "run.py", "a_ingest.py"):
            shutil.copy(os.path.join(REPO, "reference", name), self.dir)
        for name, text in ((".speed-demon.toml", 'track = "a"\nteam = "x"\n'), ("PERF.md", "# log\n"),
                           ("README.md", "# A\n")):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(text)

    def tearDown(self):
        shutil.rmtree(self.dir)

    def append(self, name, text):
        with open(os.path.join(self.dir, name), "a", encoding="utf-8") as f:
            f.write(text)

    def test_starter_as_shipped_is_unchanged(self):
        self.assertTrue(steps.unchanged_reference(self.dir))

    def test_edited_docs_are_still_unchanged(self):
        self.append("PERF.md", "| 1 | tried a dict | | | | |\n")
        self.append(".speed-demon.toml", 'language = "Python"\n')
        self.assertTrue(steps.unchanged_reference(self.dir))

    def test_a_code_change_runs(self):
        self.append("run.py", "# tweak\n")
        self.assertFalse(steps.unchanged_reference(self.dir))

    def test_a_dockerfile_change_runs(self):
        self.append("Dockerfile", "ENV PYTHONOPTIMIZE=2\n")
        self.assertFalse(steps.unchanged_reference(self.dir))

    def test_a_new_file_runs(self):
        os.makedirs(os.path.join(self.dir, "src"))
        with open(os.path.join(self.dir, "src", "fast.py"), "w", encoding="utf-8") as f:
            f.write("print(1)\n")
        self.assertFalse(steps.unchanged_reference(self.dir))

    def test_a_symlinked_copy_runs(self):
        os.remove(os.path.join(self.dir, "run.py"))
        os.symlink(os.path.join(REPO, "reference", "run.py"), os.path.join(self.dir, "run.py"))
        self.assertFalse(steps.unchanged_reference(self.dir))


class StepsTest(unittest.TestCase):
    def test_line_tail_keeps_only_the_end(self):
        t = steps.LineTail(replacements=[("/srv/speeddemon/board/checkouts/x-1", ".")])
        for i in range(100):
            t.feed(f"\x1b[32m#{i} COPY /srv/speeddemon/board/checkouts/x-1/a.py\x1b[0m\n")
        lines = t.get()
        self.assertEqual(len(lines), steps.LOG_TAIL_LINES)
        self.assertEqual(lines[0], "#60 COPY ./a.py")
        self.assertEqual(lines[-1], "#99 COPY ./a.py")

    def test_gates_match_run_submission(self):
        ok = {"ok": True, "sha256": "ab", "fs_diff": [], "wall_s": 1.0}
        self.assertEqual(steps.gates_of(ok), {"correctness": True, "timeout": True, "sandbox": True})
        self.assertFalse(steps.gates_of({**ok, "ok": False, "reason": "hash mismatch"})["correctness"])
        g = steps.gates_of({"ok": False, "reason": "timeout", "fs_diff": []})
        self.assertEqual((g["correctness"], g["timeout"]), (False, False))
        self.assertIn("3x", steps.failure_of({"reason": "timeout"}, g, 30.0, 3.0))
        g = steps.gates_of({**ok, "fs_diff": ["A /etc/x"]})
        self.assertFalse(g["sandbox"])
        self.assertEqual(steps.failure_of({**ok, "fs_diff": ["A /etc/x"]}, g, 30.0, 3.0), "wrote outside /out: /etc/x")

    def test_score(self):
        ref = {"T_s": 10.0, "M_bytes": 100.0, "S": 50, "D": 1}
        k, points = steps.score(5.0, 100.0, 50, 1, ref)
        self.assertAlmostEqual(k, 2 ** 0.5)
        self.assertAlmostEqual(points, 50 * 0.5 * 0.30103, places=3)
        self.assertEqual(steps.score(10.0, 100.0, 50, 1, ref), (1.0, 0.0))


class DepotFallbackTest(unittest.TestCase):
    """The on-box fallback fires ONLY on a Depot pre-flight/infra failure, never on a team build
    error, so an untrusted build can't be steered onto the box by emitting infra-looking output."""

    @staticmethod
    def _tail(lines):
        t = steps.LineTail()
        for l in lines:
            t.feed(l + "\n")
        return t

    def test_bad_token_falls_back(self):
        tail = self._tail(["unauthenticated: Invalid token"])
        self.assertTrue(steps._should_fallback("", 1, tail, True))

    def test_depot_machine_launch_failure_falls_back(self):
        # only Depot's own control lines, then an infra error, before any real build step
        tail = self._tail(["#1 [depot] build: https://depot.dev/orgs/x/projects/y/builds/z",
                           "#2 [depot] launching amd64 machine",
                           "rpc error: code = Unavailable desc = connection reset"])
        self.assertTrue(steps._should_fallback("", 1, tail, True))

    def test_team_build_error_does_not_fall_back(self):
        # a RUN step printing infra-looking text must NOT be retried on the box
        tail = self._tail(["#9 [build 1/4] FROM docker.io/library/python",
                           "#13 [build 3/4] RUN pip install requests",
                           "#13 1.20 ERROR: Connection error: connection reset by peer"])
        self.assertFalse(steps._should_fallback("", 1, tail, True))

    def test_success_does_not_fall_back(self):
        self.assertFalse(steps._should_fallback("", 0, self._tail(["#13 DONE 2.1s"]), True))

    def test_killed_does_not_fall_back(self):
        self.assertFalse(steps._should_fallback("timeout", 1, self._tail(["unauthenticated"]), True))

    def test_disabled_does_not_fall_back(self):
        self.assertFalse(steps._should_fallback("", 1, self._tail(["unauthenticated: Invalid token"]), False))

    def test_missing_depot_cli_falls_back(self):
        calls = []

        def fake_run(cmd, timeout_s, procs, key, replacements):
            calls.append(cmd[0])
            if cmd[0] == "/nonexistent/depot":
                raise FileNotFoundError(cmd[0])
            return "", 0, 0.1, self._tail(["#1 DONE"])

        bcfg = {"backend": "depot", "depot_bin": "/nonexistent/depot", "timeout_s": 60,
                "image_max_bytes": 10**9, "name": "b"}
        with mock.patch.object(steps, "_run_build", fake_run), \
             mock.patch.object(steps, "_local_build_cmd", lambda *a: ["docker", "buildx"]), \
             mock.patch.object(steps, "image_bytes", lambda tag: 1000):
            seen = []
            res = steps.build_image("/ctx", "t:x", bcfg, {}, None, "k", on_backend=seen.append)
        self.assertEqual(calls, ["/nonexistent/depot", "docker"])
        self.assertEqual(seen, ["depot", "local"])
        self.assertTrue(res["ok"], res)

    def test_non_infra_preflight_does_not_fall_back(self):
        # pre-flight, no buildkit, but no known infra marker -> a real failure, not a fallback
        self.assertFalse(steps._should_fallback("", 1, self._tail(["some unexpected depot cli crash"]), True))


class NativeCpuTest(unittest.TestCase):
    """A track that compiles for the build host's CPU builds on the box; a Depot-built SIGILL reruns there."""

    def _dir(self, files):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        for rel, body in files.items():
            path = os.path.join(d, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as f:
                f.write(body if isinstance(body, bytes) else body.encode())
        return d

    def test_native_flags_found(self):
        cases = {
            "Dockerfile": "RUN gcc -O3 -march=native a.c\n",
            ".cargo/config.toml": '[build]\nrustflags = ["-C", "target-cpu=native"]\n',
            "build.sh": "RUSTFLAGS='-Ctarget-cpu=native' cargo build --release\n",
            "app.csproj": "<IlcInstructionSet>native</IlcInstructionSet>\n",
            "Makefile": "CFLAGS += -mcpu=native\n",
            "CMakeLists.txt": 'set(CMAKE_CXX_FLAGS "-march = native")\n',
        }
        for rel, body in cases.items():
            self.assertEqual(steps.native_cpu_flags(self._dir({rel: body})), [rel], rel)

    def test_native_flags_not_found(self):
        d = self._dir({"Dockerfile": "RUN gcc -O3 -march=alderlake -mtune=native a.c && native-image -march=x86-64-v3 App\n",
                       "target/release/build.sh": "gcc -march=native\n",
                       "blob.bin": b"\0\0-march=native",
                       "README.md": "we use target-cpu=alderlake\n"})
        self.assertEqual(steps.native_cpu_flags(d), [])

    def test_is_sigill(self):
        self.assertTrue(steps.is_sigill({"signal": 4, "exit_code": 0}))
        self.assertTrue(steps.is_sigill({"signal": 0, "exit_code": 132}))
        self.assertFalse(steps.is_sigill({"signal": 11, "exit_code": 139}))
        self.assertFalse(steps.is_sigill(None))

    def test_requeue_rebuild_is_immediate_and_free(self):
        q = fairqueue.FairQueue(900)
        q.observe("t", "1" * 40, 0)
        q.dispatch("t", 0, "j1")
        q.finish("t")
        q.observe("t", "2" * 40, 1000)
        q.dispatch("t", 1000, "j2")
        self.assertTrue(q.requeue_rebuild("t"))
        q.finish("t")
        st = q.teams["t"]
        self.assertEqual(st.pending["commit"], "2" * 40)
        self.assertEqual(q.eligible_at(st), 1000)          # not 1000 + 900: the rebuild is on us
        self.assertIs(q.next_eligible(1001), st)

    def test_requeue_rebuild_yields_to_a_newer_push(self):
        q = fairqueue.FairQueue(900)
        q.observe("t", "1" * 40, 0)
        q.dispatch("t", 0, "j1")
        q.observe("t", "2" * 40, 10)
        self.assertFalse(q.requeue_rebuild("t"))
        self.assertEqual(q.teams["t"].pending["commit"], "2" * 40)

    def test_force_local_skips_depot(self):
        calls = []

        def fake_run(cmd, timeout_s, procs, key, replacements):
            calls.append(cmd[0])
            return "", 0, 0.1, LineTailStub()

        class LineTailStub:
            def get(self):
                return []

        bcfg = {"backend": "depot", "timeout_s": 60, "image_max_bytes": 10**9, "name": "b"}
        with mock.patch.object(steps, "_run_build", fake_run), \
             mock.patch.object(steps, "_local_build_cmd", lambda *a: ["docker", "buildx"]), \
             mock.patch.object(steps, "image_bytes", lambda tag: 1000):
            res = steps.build_image("/ctx", "t:x", bcfg, {}, None, "k", force_local=True)
        self.assertEqual(calls, ["docker"])
        self.assertEqual(res["builder"], "local")

    def test_sigill_on_depot_rebuilds_on_the_box_and_reruns(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, "data")
            os.makedirs(data)
            tracks = {"a": {"data": data, "output": "summary.txt", "expected_sha256": "0" * 64,
                            "ref": {"T_s": 1.0, "M_bytes": 1e6, "S": 40, "D": 1}}}
            cfg = board.load_config(write_config(root, tracks))
            cfg["builder"]["backend"] = "depot"
            repo = FakeRepo(root, "delta")
            dockerfile = ("FROM debian:13-slim@sha256:62bd423aeee912639ca8ee681c2b762cb3e25137e0a92b20be455898a9f034ad\n"
                          "COPY run /run\nENTRYPOINT [\"/run\"]\n")
            commit = repo.push({"track-a/Dockerfile": dockerfile, "track-a/run": "#!/bin/sh\necho 1\n"})
            write_registry(root, [{"team": "Delta", "repo": repo.bare, "tracks": ["a"], "discord": "d#1"}])
            builds, runs = [], []

            def fake_build(ctx, tag, bcfg, labels, procs, key, replacements=(), on_backend=None, force_local=False):
                used = "local" if force_local else "depot"
                builds.append(used)
                return {"ok": True, "stage": "built", "reason": "", "build_s": 1.0, "image_bytes": 1000,
                        "log_tail": [], "builder": used}

            def fake_run(tag, track, cmd, tcfg, cfg_, cgroups):
                runs.append(tag)
                if len(runs) == 1:
                    return {"ok": False, "signal": 4, "exit_code": 0, "reason": "exit code 0 signal 4",
                            "wall_s": 0.01, "peak_bytes": 1000}, 3.0
                return {"ok": True, "sha256": "0" * 64, "signal": 0, "exit_code": 0, "wall_s": 0.5,
                        "peak_bytes": 1e6, "fs_diff": []}, 3.0

            with mock.patch.object(steps, "build_image", fake_build), \
                 mock.patch.object(steps, "image_entry", lambda tag: (["/run"], "")), \
                 mock.patch.object(steps, "run_track", fake_run), \
                 mock.patch.object(steps, "prune_images", lambda *a, **k: []), \
                 mock.patch.object(board.axes, "static_axes", lambda *a, **k: {"S": 40, "D": 1, "source": "measured"}):
                b = board.Board(cfg)
                b.run_until_idle(wait_pending=True, max_seconds=60)
            recs = [r for r in b.history.snapshot() if r.get("track") == "a"]
            self.assertEqual(builds, ["depot", "local"])
            self.assertEqual([r["commit"] for r in recs], [commit, commit])
            self.assertEqual(recs[0]["failure"], board.SIGILL_REBUILD)
            self.assertEqual((recs[1]["ok"], recs[1]["builder"]), (True, "local"))
            self.assertEqual([st.box_tracks for st in b.fq.teams.values()], [["a"]])


class RecordsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_history_is_append_only_and_survives_a_cut_line(self):
        path = os.path.join(self.dir, "history.jsonl")
        h = records.History(path)
        h.append({"run_id": "r1"})
        with open(path, "a", encoding="utf-8") as f:
            f.write('{"run_id": "cut')                         # a write the box died in the middle of
        h2 = records.History(path)
        h2.append({"run_id": "r2"})
        self.assertEqual([r["run_id"] for r in records.History(path).records], ["r1", "r2"])
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read().splitlines()[0], '{"run_id":"r1"}')

    def test_atomic_write(self):
        path = os.path.join(self.dir, "pub", "board.json")
        records.atomic_write_json(path, {"a": 1})
        records.atomic_write_json(path, {"a": 2})
        with open(path, encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"a": 2})
        self.assertEqual(os.listdir(os.path.dirname(path)), ["board.json"])

    def test_mirror_runs_after_trigger(self):
        out = os.path.join(self.dir, "mirrored")
        m = records.Mirror(f'cp "$SD_HISTORY" "{out}"', {"SD_HISTORY": __file__})
        m.trigger()
        self.assertTrue(m.flush(30))
        self.assertTrue(os.path.exists(out))
        self.assertEqual(m.failures, 0)
        records.Mirror(None, {}).trigger()                       # off by default: nothing to run

    def test_feed_best_latest_and_queue(self):
        team = sources.Team(name="Alpha", slug="alpha-1", repo="https://github.com/a/b.git", tracks=["a", "b"],
                            discord="secret-handle")
        base = {"team": "Alpha", "team_slug": "alpha-1", "track": "a"}
        recs = [
            {**base, "run_id": "r1", "ok": True, "points": 10.0, "K": 1.6, "queue_latency_s": 5.0},
            {**base, "run_id": "r2", "ok": True, "points": 12.0, "K": 1.7, "queue_latency_s": 7.0},
            {**base, "run_id": "r3", "ok": False, "stage": "build", "failure": "docker build failed",
             "build_log_tail": ["error: x"]},
        ]
        q = fairqueue.FairQueue(900)
        q.observe("alpha-1", "1" * 40, 0)
        q.dispatch("alpha-1", 0, "j1")
        q.finish("alpha-1")
        q.observe("alpha-1", "2" * 40, 100)
        feed = records.build_feed(200, [team], q, {"running": None}, recs, {"dataset": "tiny"})
        entry = feed["teams"][0]
        self.assertEqual(entry["tracks"]["a"]["best"]["run_id"], "r2")
        self.assertEqual(entry["tracks"]["a"]["latest"]["run_id"], "r3")
        self.assertNotIn("build_log_tail", entry["tracks"]["a"]["latest"])     # tails stay in the private history
        self.assertNotIn("build_log_tail", entry["tracks"]["a"]["best"])
        self.assertNotIn("error: x", json.dumps(feed))
        self.assertIsNone(entry["tracks"]["b"]["latest"])
        self.assertEqual(entry["queue"]["queued"]["position"], 1)
        self.assertEqual(entry["queue"]["queued"]["eligible_at"], records.iso(900))
        self.assertEqual(entry["tracks"]["a"]["queue_latency_s"], 100.0)
        self.assertEqual(feed["queue"]["recent_latency_s"], 6.0)
        self.assertNotIn("secret-handle", json.dumps(feed))

        q.dispatch("alpha-1", 1000, "j2")
        for builder, want in (("depot", "depot"), ("local", "local"), (True, None)):
            f2 = records.build_feed(1001, [team], q, {"running": None, "building": {"alpha-1": builder}}, recs, {"dataset": "tiny"})
            inf = f2["teams"][0]["queue"]["in_flight"]
            self.assertEqual(inf["state"], "building")
            self.assertEqual(inf.get("builder"), want)


class GuardsTest(unittest.TestCase):
    def test_watchdog_kills_pauses_and_resumes(self):
        class Probe:
            values = [500, 90, 95, 105, 120]

            def free_bytes(self):
                return self.values.pop(0)

        kills, changes = [], []
        w = guards.DiskWatchdog(Probe(), 100, 110, 1, on_low=kills.append,
                                on_change=lambda ok, free: changes.append(ok))
        self.assertEqual([w.check() for _ in range(5)], [True, False, False, False, True])
        self.assertEqual(kills, [90, 95, 105])                  # every check while low kills builds
        self.assertEqual(changes, [False, True])

    def test_probe_failure_keeps_state(self):
        class Broken:
            def free_bytes(self):
                raise RuntimeError("df failed")

        w = guards.DiskWatchdog(Broken(), 100, 110, 1, on_low=lambda f: None)
        self.assertTrue(w.check())


def write_config(root, tracks, **extra):
    timer = os.path.join(REPO, "harness", "timer", "sd-timer.c")
    cfg = {
        "registry": "registry.json", "state_dir": "state", "history": "state/history.jsonl",
        "feed": "public/board.json", "checkouts": "checkouts", "work": "work", "timer": timer,
        "cgroups": "host", "allow_local_repos": True, "poll_interval_s": 1, "min_interval_s": 900,
        "builder": {"name": ""}, "disk": {"paths": [root], "min_free_gb": 0, "resume_free_gb": 0},
        "tracks": tracks,
    }
    board.deep_merge(cfg, extra)
    path = os.path.join(root, "board.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f)
    return path


def write_registry(root, teams):
    with open(os.path.join(root, "registry.json"), "w", encoding="utf-8") as f:
        json.dump(teams, f)


class CalibrationTest(unittest.TestCase):
    def test_refs_come_from_calibrate_py_output(self):
        with tempfile.TemporaryDirectory() as root:
            for t in "abc":
                os.makedirs(os.path.join(root, t))
            cal = {"tracks": {
                "a": {"output": "summary.txt", "output_sha256": ["a" * 64], "T_ref_s": 26.1, "M_ref_bytes": 250e6,
                      "gates": {"correctness": True, "timeout": True, "sandbox": True}},
                "b": {"output": "results.txt", "output_sha256": ["b" * 64, "c" * 64], "T_ref_s": 50.0,
                      "M_ref_bytes": 2.3e9, "gates": {"correctness": True, "timeout": True, "sandbox": True}},
                "c": {"output": "aggregates.txt", "output_sha256": ["d" * 64], "T_ref_s": 12.0, "M_ref_bytes": 4e8,
                      "gates": {"correctness": False, "timeout": True, "sandbox": True}},
            }}
            with open(os.path.join(root, "calibration.json"), "w", encoding="utf-8") as f:
                json.dump(cal, f)
            tracks = {"a": {"data": "a", "ref": {"S": 40}},
                      "b": {"data": "b", "ref": {"S": 90}},
                      "c": {"data": "c", "ref": {"S": 60, "T_s": 11.0}}}
            path = write_config(root, tracks, calibration="calibration.json")
            with self.assertRaises(SystemExit) as caught:
                board.load_config(path)
            message = str(caught.exception)
            self.assertNotIn("track a", message)                       # filled from the calibration
            self.assertIn("track b: expected_sha256", message)         # five runs disagreed: never guess
            self.assertIn("track c: expected_sha256", message)         # a failed gate fills nothing
            del tracks["b"], tracks["c"]
            cfg = board.load_config(write_config(root, tracks, calibration="calibration.json"))
            a = cfg["tracks"]["a"]
            self.assertEqual((a["output"], a["expected_sha256"], a["ref"]), ("summary.txt", "a" * 64,
                             {"S": 40, "D": 1, "T_s": 26.1, "M_bytes": 250e6}))


class BoardWithoutDockerTest(unittest.TestCase):
    """A rejected Dockerfile never reaches docker, so the whole job path runs without it."""

    def test_rejected_dockerfile_and_held_second_push(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, "data")
            os.makedirs(data)
            tracks = {"a": {"data": data, "output": "summary.txt", "expected_sha256": "0" * 64,
                            "ref": {"T_s": 1.0, "M_bytes": 1e6, "S": 40, "D": 1}}}
            cfg = board.load_config(write_config(root, tracks))
            repo = FakeRepo(root, "bravo")
            first = repo.push({"track-a/Dockerfile": STOLEN_DOCKERFILE, "track-a/a_ingest.py": "print(1)\n"})
            write_registry(root, [{"team": "Bravo", "repo": repo.bare, "tracks": ["a"], "discord": "b#1"}])

            b = board.Board(cfg)
            b.run_until_idle(max_seconds=60)
            recs = b.history.snapshot()
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual((rec["team"], rec["track"], rec["commit"], rec["stage"], rec["ok"]),
                             ("Bravo", "a", first, "check", False))
            self.assertIn("postgres:17", rec["failure"])
            self.assertEqual(rec["gates"]["check"], False)
            self.assertIsNone(rec["gates"]["build"])

            second = repo.push({"track-a/Dockerfile": REF_DOCKERFILE})
            b2 = board.Board(cfg)
            b2.run_until_idle(max_seconds=60)
            self.assertEqual(len(b2.history.records), 1)           # held by the 15-minute rule
            with open(cfg["feed"], encoding="utf-8") as f:
                feed = json.load(f)
            queued = feed["teams"][0]["queue"]["queued"]
            self.assertEqual((queued["commit"], queued["position"]), (second, 1))
            self.assertGreater(board.epoch(queued["eligible_at"]), time.time() + 800)
            self.assertEqual(feed["teams"][0]["tracks"]["a"]["latest"]["stage"], "check")
            self.assertNotIn("b#1", json.dumps(feed))

    def test_unchanged_starter_is_reported_not_run(self):
        with tempfile.TemporaryDirectory() as root:
            data = os.path.join(root, "data")
            os.makedirs(data)
            tracks = {"a": {"data": data, "output": "summary.txt", "expected_sha256": "0" * 64,
                            "ref": {"T_s": 1.0, "M_bytes": 1e6, "S": 40, "D": 1}}}
            cfg = board.load_config(write_config(root, tracks))
            repo = FakeRepo(root, "starter")
            files = {f"track-a/{n}": reference_text(n) for n in ("Dockerfile", "run.py", "a_ingest.py")}
            files.update({"track-a/.speed-demon.toml": 'track = "a"\nteam = "Starter"\n', "track-a/PERF.md": "# log\n"})
            sha = repo.push(files)
            write_registry(root, [{"team": "Starter", "repo": repo.bare, "tracks": ["a"]}])

            b = board.Board(cfg)
            b.run_until_idle(max_seconds=60)
            recs = b.history.snapshot()
            self.assertEqual(len(recs), 1)
            rec = recs[0]
            self.assertEqual((rec["commit"], rec["stage"], rec["ok"]), (sha, "unchanged", False))
            self.assertEqual((rec["gates"]["check"], rec["gates"]["build"]), (True, None))
            self.assertIn("unchanged starter", rec["failure"])


class ReferenceCanaryTest(unittest.TestCase):
    """B2: the board-side reference-drift canary. Pure logic; the docker run path is proven on the box."""

    CALIB = 32.047276

    def _board(self, root, rc=None):
        for name in ("data-a", "data-c"):
            os.makedirs(os.path.join(root, name))
        tracks = {"a": {"data": os.path.join(root, "data-a"), "output": "summary.txt", "expected_sha256": "a" * 64,
                        "ref": {"T_s": self.CALIB, "M_bytes": 2.3e8, "S": 40, "D": 1}},
                  "c": {"data": os.path.join(root, "data-c"), "output": "aggregates.txt", "expected_sha256": "c" * 64,
                        "ref": {"T_s": 14.44, "M_bytes": 3.6e8, "S": 60, "D": 1}}}
        extra = {"reference_check": rc} if rc is not None else {}
        return board.Board(board.load_config(write_config(root, tracks, **extra)))

    def _run_json(self, path, gates, T_s, walls, spread=True):
        obj = {"track": "a", "gates": gates, "batches": [[{"wall_s": w} for w in walls]], "spread_ok": spread}
        if T_s is not None:
            obj["T_s"] = T_s
        with open(path, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        return path

    def test_drift_math_and_failure_modes(self):
        allok = {"correctness": True, "timeout": True, "sandbox": True}
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            p = os.path.join(root, "r.json")
            # healthy: +1.1% is under the 2% threshold
            self._run_json(p, allok, 32.40, [31.9, 32.3, 32.4, 32.5, 32.6])
            r = b._reference_result("a", self.CALIB, 1.7e9, p, 0, "", "")
            self.assertTrue(r["ok"])
            self.assertEqual(r["T_measured_s"], 32.4)
            self.assertAlmostEqual(r["drift_pct"], round((32.40 - self.CALIB) / self.CALIB * 100, 2))
            self.assertNotIn("OVER", r["summary"])
            # drift over threshold
            self._run_json(p, allok, 33.60, [33.4, 33.5, 33.6, 33.7, 33.8])
            r = b._reference_result("a", self.CALIB, 1.7e9, p, 0, "", "")
            self.assertFalse(r["ok"])
            self.assertGreater(r["drift_pct"], 2)
            self.assertIn("OVER THRESHOLD", r["summary"])
            # a failed gate is a failed canary, not a drift reading
            self._run_json(p, {"correctness": False, "timeout": True, "sandbox": True}, None, [])
            r = b._reference_result("a", self.CALIB, 1.7e9, p, 0, "", "")
            self.assertFalse(r["ok"])
            self.assertIsNone(r["T_measured_s"])
            self.assertIn("clean timed run", r["summary"])
            # no result file at all: box or harness problem, stderr kept
            r = b._reference_result("a", self.CALIB, 1.7e9, os.path.join(root, "nope.json"), 1, "", "boom\ntrace")
            self.assertFalse(r["ok"])
            self.assertIn("no result", r["summary"])
            self.assertTrue(r["stderr_tail"])
            # killed on stop
            r = b._reference_result("a", self.CALIB, 1.7e9, p, -9, "stop", "")
            self.assertFalse(r["ok"])
            self.assertEqual(r["summary"], "canary stopped (stop)")

    def test_public_view_is_numbers_free(self):
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            self.assertEqual(b._public_reference_check()["note"], "no reference check yet")
            b.reference_check = {"ok": True, "checked_at": "t", "track": "a", "drift": 0.011,
                                 "drift_pct": 1.1, "T_measured_s": 32.4, "T_calib_s": self.CALIB, "walls_s": [1, 2]}
            pub = b._public_reference_check()
            self.assertEqual((pub["ok"], pub["note"]), (True, "box timing nominal"))
            for leak in ("drift", "drift_pct", "T_measured_s", "T_calib_s", "walls_s"):
                self.assertNotIn(leak, pub)
            b.reference_check = {"ok": False, "checked_at": "t", "track": "a", "drift": 0.045}
            self.assertEqual(b._public_reference_check()["note"], "reference timing drift over threshold")
            b.reference_check = {"ok": False, "checked_at": "t", "track": "a", "drift": None}
            self.assertEqual(b._public_reference_check()["note"], "reference run failed")
            # and it lands in board.json
            b.load_registry(force=True)
            feed = b.write_feed()
            self.assertEqual(feed["reference_check"]["note"], "reference run failed")

    def test_pick_rotates_and_skips_uncalibrated(self):
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root, rc={"tracks": ["a", "c"]})
            self.assertEqual([b._canary_pick() for _ in range(4)], ["a", "c", "a", "c"])
            # an uncalibrated track is never picked
            b.cfg["tracks"]["c"]["ref"]["T_s"] = None
            b._canary_idx = 0
            self.assertEqual([b._canary_pick() for _ in range(3)], ["a", "a", "a"])
            self.assertTrue(b._canary_calibrated("a"))
            self.assertFalse(b._canary_calibrated("c"))

    def test_due_gating_and_wait_bounds(self):
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            b.disk.ok = True
            self.assertFalse(b._canary_due())                 # not armed
            b._canary_next = time.monotonic() - 1
            self.assertTrue(b._canary_due())
            b._canary_next = time.monotonic() + 10000
            self.assertFalse(b._canary_due())                 # not yet due
            b._canary_next = time.monotonic() - 1
            b.disk.ok = False
            self.assertFalse(b._canary_due())                 # yields while the disk guard holds
            b.disk.ok = True
            self.assertEqual(b._runner_wait(), max(0.2, min(5.0, b._canary_next - time.monotonic())))
            b._canary_next = None
            self.assertEqual(b._runner_wait(), 5.0)

    def test_building_does_not_gate_a_due_canary(self):
        # Builds run on the E-cores and never contend with the P-core canary, so an in-flight
        # build must not stop a due check firing (else it starves for the whole event under load).
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            b.disk.ok = True
            b._canary_next = time.monotonic() - 1
            b.building = {"someteam": True}
            b.ready = ["pretend-job"]
            self.assertTrue(b._canary_due())

    def test_once_does_not_arm_the_canary(self):
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            b.load_registry(force=True)
            b.start(poll_thread=False)
            try:
                self.assertIsNone(b._canary_next)
            finally:
                b.stop()

    def test_serve_runner_takes_the_canary_branch(self):
        # Exercises the in-serve path: the runner picking the canary slot, running it, re-arming.
        # _run_canary itself (docker/subprocess) is proven on the box; here it is stubbed.
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            b.load_registry(force=True)
            fired = []
            b._run_canary = lambda track: fired.append(track)
            b.start()                                   # poll_thread=True -> arms the canary
            try:
                with b.cv:
                    b._canary_next = time.monotonic() - 1   # force due now
                    b.cv.notify_all()
                deadline = time.monotonic() + 5
                while not fired and time.monotonic() < deadline:
                    time.sleep(0.02)
            finally:
                b.stop()
            self.assertEqual(fired[:1], ["a"])
            self.assertGreater(b._canary_next, time.monotonic())   # re-armed one interval forward
            self.assertIsNone(b.running)

    def test_validate_rejects_bad_reference_check(self):
        with tempfile.TemporaryDirectory() as root:
            os.makedirs(os.path.join(root, "data-a"))
            os.makedirs(os.path.join(root, "data-c"))
            tracks = {"a": {"data": os.path.join(root, "data-a"), "output": "summary.txt", "expected_sha256": "a" * 64,
                            "ref": {"T_s": 1.0, "M_bytes": 1e6, "S": 40, "D": 1}}}
            with self.assertRaises(SystemExit) as caught:
                board.load_config(write_config(root, tracks,
                                               reference_check={"drift_threshold": 5, "runs": 2, "tracks": ["z"]}))
            msg = str(caught.exception)
            self.assertIn("drift_threshold", msg)
            self.assertIn("runs must be", msg)
            self.assertIn("not a configured track", msg)

    def test_paths_resolve_with_defaults(self):
        with tempfile.TemporaryDirectory() as root:
            b = self._board(root)
            rc = b.cfg["reference_check"]
            self.assertTrue(os.path.isabs(rc["context"]) and rc["context"].endswith("/reference"))
            self.assertEqual(rc["log"], os.path.join(b.cfg["state_dir"], "reference_checks.jsonl"))
            self.assertEqual(rc["work"], os.path.join(b.cfg["work"], "reference-canary"))


_DOCKER_ENV = []


def docker_env():
    """(cgroups mode, timer path, cpu count) for the Docker test, or a reason to skip. Probed once."""
    if not _DOCKER_ENV:
        _DOCKER_ENV.append(_probe_docker())
    return _DOCKER_ENV[0]


def _probe_docker():
    try:
        if not shutil.which("docker") or subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode:
            return None, "Docker is not available"
        ncpu = int(subprocess.run(["docker", "info", "-f", "{{.NCPU}}"], capture_output=True, text=True,
                                  timeout=20).stdout.strip() or 1)
        context = subprocess.run(["docker", "context", "show"], capture_output=True, text=True, timeout=20).stdout
    except subprocess.TimeoutExpired:
        return None, "Docker is not responding"
    if not os.path.isdir(TINY_A):
        return None, "tiny Track A dataset missing: python3.13 generators/a_ingest.py --seed 1 --scale tiny --out .data/a/tiny-seed1"
    arch = {"arm64": "arm64", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine(), platform.machine())
    timer = os.environ.get("SD_TIMER") or next(
        (p for p in ("/srv/speeddemon/bin/sd-timer", os.path.join(REPO, "harness", "timer", f"sd-timer-{arch}"))
         if os.path.isfile(p)), "")
    if not timer:
        return None, "no sd-timer binary (set SD_TIMER)"
    if shutil.which("colima") and "colima" in context:
        return ("colima", timer, ncpu), ""
    if sys.platform.startswith("linux") and os.geteuid() == 0:
        return ("host", timer, ncpu), ""
    return None, "reading memory.peak needs root on Linux, or Colima"


@unittest.skipIf(docker_env()[0] is None, docker_env()[1])
class DockerEndToEndTest(unittest.TestCase):
    BUILDER = os.environ.get("SD_TEST_BUILDER", "sdboard-test")
    # Colima's Ubuntu VM blocks unprivileged user namespaces, which rootless BuildKit needs, so the
    # local default is buildx's docker-container driver; on the box run with SD_TEST_DRIVER=rootless.
    DRIVER = os.environ.get("SD_TEST_DRIVER", "docker-container")
    IMAGE = "moby/buildkit:v0.34.1" + ("-rootless" if DRIVER == "rootless" else "")

    @classmethod
    def tearDownClass(cls):
        if "SD_TEST_BUILDER" not in os.environ:
            subprocess.run(["docker", "buildx", "rm", cls.BUILDER], capture_output=True)
            subprocess.run(["docker", "rm", "-f", f"{cls.BUILDER}-buildkitd"], capture_output=True)

    def builder_cfg(self, **extra):
        (_, _, ncpu), _ = docker_env()
        cfg = {"name": self.BUILDER, "create": True, "driver": self.DRIVER, "image": self.IMAGE, "network": "host",
               "cpuset": f"2-{ncpu - 1}" if ncpu >= 4 else "0", "memory": "2g", "timeout_s": 600,
               "image_max_bytes": 2 * 1024**3, "buildkitd_config": board.DEFAULTS["builder"]["buildkitd_config"]}
        cfg.update(extra)
        steps.ensure_builder(cfg)
        return cfg

    def test_image_cap_and_hard_build_timeout(self):
        ctx = tempfile.mkdtemp()
        try:
            with open(os.path.join(ctx, "Dockerfile"), "w", encoding="utf-8") as f:
                f.write(f"FROM {PY}\nRUN echo built > /built\n")
            res = steps.build_image(ctx, "sdboard-test/cap:x", self.builder_cfg(image_max_bytes=1000), {},
                                    steps.ProcessRegistry(), "cap")
            self.assertEqual(res["stage"], "image-size", res)
            self.assertIsNone(steps.image_bytes("sdboard-test/cap:x"))   # an oversized image is not kept
            with open(os.path.join(ctx, "Dockerfile"), "w", encoding="utf-8") as f:
                f.write(f"FROM {PY}\nRUN sleep 300\n")
            started = time.monotonic()
            res = steps.build_image(ctx, "sdboard-test/slow:x", self.builder_cfg(timeout_s=8), {},
                                    steps.ProcessRegistry(), "slow")
            self.assertEqual((res["stage"], res["reason"]), ("build", "build exceeded 8 s"))
            self.assertLess(time.monotonic() - started, 8 + steps.KILL_GRACE_S + 10)
            self.assertLessEqual(len(res["log_tail"]), steps.LOG_TAIL_LINES)
        finally:
            shutil.rmtree(ctx, ignore_errors=True)

    def test_push_build_run_record_prune(self):
        (cgroups, timer, ncpu), _ = docker_env()
        run_cpus, build_cpus = ("0-1", f"2-{ncpu - 1}") if ncpu >= 4 else ("0", "0")
        root = tempfile.mkdtemp(dir=os.path.join(REPO, ".data"), prefix="board-test-")   # visible inside Colima
        try:
            tracks = {"a": {"data": TINY_A, "output": "summary.txt", "expected_sha256": TINY_A_SHA,
                            "ref": {"T_s": 2.0, "M_bytes": 20e6, "S": 40, "D": 1}}}
            path = write_config(root, tracks, cgroups=cgroups, timer=timer, run_cpuset=run_cpus, min_interval_s=0,
                                image_prefix="sdboard-test",
                                builder={"name": self.BUILDER, "cpuset": build_cpus, "memory": "2g",
                                         "driver": self.DRIVER, "image": self.IMAGE})
            cfg = board.load_config(path)
            alpha = FakeRepo(root, "alpha")
            with open(os.path.join(REPO, "reference", "a_ingest.py"), encoding="utf-8") as f:
                ref_code = f.read()
            first = alpha.push({"track-a/Dockerfile": REF_DOCKERFILE, "track-a/a_ingest.py": ref_code})
            write_registry(root, [{"team": "Alpha", "repo": alpha.bare, "tracks": ["a"]}])

            board.Board(cfg).run_until_idle(max_seconds=900)
            second = alpha.push({"track-a/NOTES": "second push\n"})
            b = board.Board(cfg)
            b.run_until_idle(max_seconds=900)

            recs = b.history.snapshot()
            self.assertEqual([r["commit"] for r in recs], [first, second], recs)
            for rec in recs:
                self.assertTrue(rec["ok"], rec)
                self.assertEqual(rec["stage"], "scored")
                self.assertEqual(rec["sha256"], TINY_A_SHA)
                self.assertGreater(rec["T_s"], 0)
                self.assertGreater(rec["M_bytes"], 0)
                self.assertGreater(rec["K"], 0)
                self.assertIn(rec["static_axes"], ("measured", "stubbed"))   # measured when the tools image is built
            tags = subprocess.run(["docker", "image", "ls", "--filter", "reference=sdboard-test/*",
                                   "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True).stdout.split()
            self.assertEqual(tags, [f"sdboard-test/{recs[0]['team_slug']}-a:{second[:12]}"])
            self.assertEqual(os.listdir(cfg["work"]), [])            # run_once's out/ and sd/ are cleared
            with open(cfg["feed"], encoding="utf-8") as f:
                feed = json.load(f)
            self.assertEqual(feed["teams"][0]["tracks"]["a"]["latest"]["commit"], second)
        finally:
            ids = subprocess.run(["docker", "image", "ls", "-q", "--filter", "reference=sdboard-test/*"],
                                 capture_output=True, text=True).stdout.split()
            if ids:
                subprocess.run(["docker", "image", "rm", "-f", *sorted(set(ids))], capture_output=True)
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class GitHubAppTest(unittest.TestCase):
    """The app signs its own JWT with openssl and hands git one token per repo."""

    def test_jwt_is_signed_rs256_by_the_app_key(self):
        import base64, json as _json
        sys.path.insert(0, os.path.join(REPO, "board"))
        import github_app
        with tempfile.TemporaryDirectory() as d:
            key = os.path.join(d, "app.pem")
            subprocess.run(["openssl", "genrsa", "-out", key, "2048"], check=True, capture_output=True)
            token = github_app.GitHubApp(12345, key).jwt()
            header, claims, sig = token.split(".")
            pad = lambda s: s + "=" * (-len(s) % 4)
            self.assertEqual(_json.loads(base64.urlsafe_b64decode(pad(header)))["alg"], "RS256")
            body = _json.loads(base64.urlsafe_b64decode(pad(claims)))
            self.assertEqual(body["iss"], "12345")
            self.assertLess(body["exp"] - body["iat"], 660)
            pub = os.path.join(d, "app.pub")
            subprocess.run(["openssl", "rsa", "-in", key, "-pubout", "-out", pub], check=True, capture_output=True)
            sigfile = os.path.join(d, "sig")
            with open(sigfile, "wb") as f:
                f.write(base64.urlsafe_b64decode(pad(sig)))
            ok = subprocess.run(["openssl", "dgst", "-sha256", "-verify", pub, "-signature", sigfile],
                                input=f"{header}.{claims}".encode(), capture_output=True)
            self.assertEqual(ok.returncode, 0)

    def test_git_uses_the_provider_token_per_repo_and_redacts_it(self):
        calls = []

        def provider(owner, repo):
            calls.append((owner, repo))
            return ("ghs_" + "Z9y8" * 9, "") if repo == "ok" else ("", "the Speed Demon Board app is not installed")

        g = sources.Git(token_provider=provider)
        basic, err = g._auth_for("https://github.com/team/ok.git")
        self.assertEqual((calls[-1], err), (("team", "ok"), ""))
        values = [v for k, v in g.env(basic).items() if k.startswith("GIT_CONFIG_VALUE_")]
        self.assertTrue(any(v == f"Authorization: Basic {basic}" for v in values))
        self.assertEqual(g.redact(f"error {basic} and ghs_{'Z9y8' * 9}").count("***"), 2)
        self.assertEqual(g.ls_remote("https://github.com/team/missing.git", "speed-demon"),
                         ("", "the Speed Demon Board app is not installed"))

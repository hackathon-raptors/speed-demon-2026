"""run_submission.run_once against images that try to break the run contract.

Needs Docker (Colima on macOS, or root on Linux) and the tiny Track A dataset; skips otherwise.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "harness"))

import run_submission  # noqa: E402
from test_board import PY, TINY_A, docker_env  # noqa: E402

TAG = "sdcontract-test/{}:x"


@unittest.skipIf(docker_env()[0] is None, docker_env()[1])
class RunContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        (cls.cgroups_mode, cls.timer, ncpu), _ = docker_env()
        cls.cpuset = "0-1" if ncpu >= 2 else "0"
        cls.root = tempfile.mkdtemp(dir=os.path.join(REPO, ".data"), prefix="contract-test-")   # visible in Colima
        cls.built = []

    @classmethod
    def tearDownClass(cls):
        if cls.built:
            subprocess.run(["docker", "image", "rm", "-f", *cls.built], capture_output=True)
        shutil.rmtree(cls.root, ignore_errors=True)

    def image(self, name, body):
        ctx = os.path.join(self.root, f"ctx-{name}")
        os.makedirs(ctx, exist_ok=True)
        with open(os.path.join(ctx, "Dockerfile"), "w", encoding="utf-8") as f:
            f.write(f"FROM {PY}\n{body}\n")
        tag = TAG.format(name)
        proc = subprocess.run(["docker", "build", "--network=none", "-q", "-t", tag, ctx], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.built.append(tag)
        return tag

    def run_image(self, tag, timeout_s=60, timer=None):
        work = tempfile.mkdtemp(dir=self.root, prefix="work-")
        args = types.SimpleNamespace(work=work, cpuset=self.cpuset, data=TINY_A, output="summary.txt", expected_sha256="")
        cmd = run_submission.image_command(tag)
        started = time.monotonic()
        r = run_submission.run_once(tag, "a", cmd, args, timer or self.timer,
                                    run_submission.CgroupReader(self.cgroups_mode), timeout_s)
        return r, time.monotonic() - started

    def test_healthcheck_cannot_plant_a_timing_record(self):
        tag = self.image("health", 'HEALTHCHECK --interval=1s --start-period=0s --timeout=1s CMD ["sh","-c",'
                         '"test -e /sd/timing.json || { echo {\\"wall_ns\\":1000,\\"exit_code\\":0,\\"signal\\":0} '
                         '> /sd/timing.json; chmod 0444 /sd/timing.json; }"]\n'
                         'CMD ["sh","-c","sleep 3; echo hi > /out/summary.txt"]')
        r, _ = self.run_image(tag)
        self.assertTrue(r["ok"], r)
        self.assertGreater(r["wall_s"], 2.5)

    def test_record_that_disagrees_with_the_exit_status_is_refused(self):
        # A stand-in "timer" that writes a perfect record but exits 125, as the real timer does when
        # something else owns /sd/timing.json.
        bindir = tempfile.mkdtemp(dir=self.root, prefix="faketimer-")
        fake = os.path.join(bindir, "sd-timer")
        with open(fake, "w", encoding="utf-8") as f:
            f.write('#!/bin/sh\necho hi > /out/summary.txt\n'
                    'echo \'{"wall_ns": 1000, "exit_code": 0, "signal": 0}\' > "$1"\nexit 125\n')
        os.chmod(fake, 0o755)
        tag = self.image("tamper", 'CMD ["true"]')
        r, _ = self.run_image(tag, timer=fake)
        self.assertFalse(r["ok"], r)
        self.assertTrue(r.get("tamper"), r)
        self.assertEqual(r["container_rc"], 125)

    def test_volume_image_is_refused_before_it_runs(self):
        tag = self.image("volume", 'VOLUME /scratch\nCMD ["true"]')
        with self.assertRaises(run_submission.ImageRefused):
            run_submission.image_command(tag)

    def test_fifo_or_symlink_output_fails_fast(self):
        for name, cmd in (("fifo", "mkfifo /out/summary.txt"), ("zero", "ln -s /dev/zero /out/summary.txt")):
            with self.subTest(name):
                tag = self.image(name, f'CMD ["sh","-c","{cmd}"]')
                r, took = self.run_image(tag)
                self.assertFalse(r["ok"], r)
                self.assertIn("regular file", r["reason"])
                self.assertLess(took, 30)

    def test_stderr_flood_is_bounded(self):
        tag = self.image("flood", 'CMD ["sh","-c","head -c 300000000 /dev/zero | tr \\"\\\\0\\" x >&2; echo hi > /out/summary.txt"]')
        r, _ = self.run_image(tag)
        self.assertTrue(r["ok"], {k: v for k, v in r.items() if k != "stderr"})
        self.assertNotIn("stderr", r)

    def test_image_user_does_not_pick_the_timer_user(self):
        tag = self.image("user", 'USER 65534\nCMD ["sh","-c","echo hi > /out/summary.txt"]')
        r, _ = self.run_image(tag)
        self.assertTrue(r["ok"], r)


if __name__ == "__main__":
    unittest.main()

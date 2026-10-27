"""Keeping one box alive for the whole event.

docker builds have no disk quota, so a build that writes without limit could
fill the disk under /var/lib/docker and take the board, the runs and the
evidence with it. The watchdog checks free space every few seconds; below the
threshold it kills every running build (and clone) and refuses to start new
ones until free space is back above a higher resume mark.
"""
import logging
import shutil
import subprocess
import threading
import time

log = logging.getLogger("sdboard")
GB = 1000**3


class DiskProbe:
    """Free bytes on the fullest of the watched filesystems: on this host, or inside the Colima VM."""

    def __init__(self, mode, paths):
        self.mode = mode
        self.paths = list(paths)

    def free_bytes(self):
        if self.mode == "colima":
            proc = subprocess.run(["colima", "ssh", "--", "df", "-Pk", *self.paths],
                                  capture_output=True, text=True, timeout=30)
            lines = proc.stdout.strip().splitlines()[1:]
            free = [int(line.split()[3]) * 1024 for line in lines if len(line.split()) >= 6]
            if not free:
                raise RuntimeError(f"df gave no usable lines: {proc.stderr.strip()[:200]}")
            return min(free)
        free = []
        for p in self.paths:
            try:
                free.append(shutil.disk_usage(p).free)
            except FileNotFoundError:
                continue
        if not free:
            raise RuntimeError(f"none of {self.paths} exist")
        return min(free)


class DiskWatchdog:
    def __init__(self, probe, min_free_bytes, resume_free_bytes, interval_s, on_low, on_change=None):
        self.probe = probe
        self.min_free = int(min_free_bytes)
        self.resume_free = max(int(resume_free_bytes), self.min_free)
        self.interval_s = interval_s
        self.on_low = on_low            # called (free_bytes) on every check while low: kill builds
        self.on_change = on_change      # called (ok, free_bytes) when the state flips
        self.ok = True
        self.free = None
        self.checked_at = None
        self.stop = threading.Event()
        self.thread = None

    def check(self):
        """One probe. Returns whether builds may run."""
        try:
            free = self.probe.free_bytes()
        except Exception as exc:  # a failed probe keeps the last state rather than flapping
            log.warning("disk probe failed: %s", exc)
            return self.ok
        self.free, self.checked_at = free, time.time()
        was = self.ok
        if self.ok and free < self.min_free:
            self.ok = False
        elif not self.ok and free >= self.resume_free:
            self.ok = True
        if not self.ok:
            self.on_low(free)
        if was != self.ok:
            log.log(logging.INFO if self.ok else logging.ERROR,
                    "disk %s: %.1f GB free (stop below %.1f GB, resume at %.1f GB)",
                    "recovered, builds resume" if self.ok else "LOW, builds killed and paused",
                    free / GB, self.min_free / GB, self.resume_free / GB)
            if self.on_change:
                self.on_change(self.ok, free)
        return self.ok

    def start(self):
        self.check()
        self.thread = threading.Thread(target=self._loop, name="disk-watchdog", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stop.wait(self.interval_s):
            self.check()

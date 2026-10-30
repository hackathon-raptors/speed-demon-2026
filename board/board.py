#!/usr/bin/env python3
"""Speed Demon 2026 live board: watch, queue, build, run once, record, publish.

    python3 board/board.py serve --config /etc/speeddemon/board.json
    python3 board/board.py once --config board.local.json [--wait-pending]
    python3 board/board.py setup-builder --config ...
    python3 board/board.py feed --config ...        # rewrite board.json from the history

Each team's `speed-demon` branch is watched with git ls-remote. A new commit
queues one job for the team (a newer push replaces a queued one); a team gets
at most one job per 15 minutes, least-recently-served first. A job clones the
commit, and for every track-a..track-d directory checks the Dockerfile,
builds it on the board's own builder (E-cores, offline, 10 minutes, 2 GB),
then runs it once on the full public dataset under run_submission.py's
contract, capped at 3x the reference's T, on the P-cores. Runs are strictly
one at a time. Every attempt goes to history.jsonl; board.json is the public
feed. Private repos are read through the Speed Demon Board GitHub App
(github_app_id and github_app_key in the config); BOT_TOKEN is for local tests.
"""
import argparse
import calendar
import concurrent.futures
import copy
import dataclasses
import json
import logging
import os
import re
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "harness"))
sys.path.insert(0, HERE)

import run_submission  # noqa: E402

import axes  # noqa: E402
import github_app  # noqa: E402
import fairqueue  # noqa: E402
import guards  # noqa: E402
import records  # noqa: E402
import security  # noqa: E402
import sources  # noqa: E402
import steps  # noqa: E402

log = logging.getLogger("sdboard")
GB = guards.GB

DEFAULTS = {
    "event": "Speed Demon 2026",
    "dataset_label": "full public dataset (kickoff release)",
    "event_starts": "2026-10-30T15:00:00Z",       # kickoff and code freeze: the stats' runs-per-hour window
    "event_ends": "2026-11-02T15:00:00Z",
    "registry": "/srv/speeddemon/board/registry.json",
    "state_dir": "/srv/speeddemon/board/state",
    "history": "/srv/speeddemon/board/state/history.jsonl",
    "feed": "/srv/speeddemon/board/public/board.json",
    "checkouts": "/srv/speeddemon/board/checkouts",
    "work": "/srv/speeddemon/work/board",        # on the 8 GB tmpfs from setup-box.sh; the board's own subdirectory
    "timer": "/srv/speeddemon/bin/sd-timer",
    "allowlist": os.path.join(REPO, "harness", "allowlist.json"),
    "cgroups": "host",                           # "colima" for local tests
    "branch": "speed-demon",
    "github_app_id": "",                         # the Speed Demon Board app; empty = BOT_TOKEN or public repos only
    "github_app_key": "/etc/speeddemon/github-app.pem",
    "allow_local_repos": False,                  # tests only: lets the registry name local bare repos
    "image_prefix": "sdboard",
    "poll_interval_s": 60,
    "poll_workers": 8,
    "min_interval_s": 900,
    "run_cpuset": "0-11",
    "run_timeout_factor": 3.0,
    "skip_unchanged_reference": True,            # an untouched starter track is the reference: report it, never run it
    "too_fast_floor_s": 0.05,                    # a scored run under this T raises a private "too_fast" flag (alert only)
    "push_churn_per_hour": 20,                   # new commits per team per hour before a private "push_churn" flag
    "max_ready": None,                           # built jobs waiting for the runner; None = builder.max_concurrent
    "restart_attempts": 2,
    "feed_interval_s": 10,
    "mirror_command": None,                      # sh -c, run in the config file's directory; off when null
    "mirror_timeout_s": 120,
    "builder": {
        "name": "speeddemon-builder",            # "" = the default builder (no isolation; local use only)
        "create": True,
        "driver": "rootless",                    # "rootless" (unprivileged, our own container) or "docker-container"
        "image": "moby/buildkit:v0.34.1-rootless",
        "network": "host",                       # the BuildKit container's, for base-image pulls only
        "cpuset": "12-19",
        "memory": "16g",
        "pids": 1024,                            # builder-container fork-storm cap (docker update --pids-limit)
        "max_concurrent": 2,
        "timeout_s": 600,
        "backend": "local",                      # "local" (on-box buildx) or "depot" (off-box, isolated)
        "depot_project": "",                     # Depot project id; empty = $DEPOT_PROJECT_ID (needs $DEPOT_TOKEN)
        "depot_bin": "",                         # empty = `depot` on PATH, else /root/.depot/bin/depot
        "depot_platform": "linux/amd64",
        "fallback_local": True,                  # on a Depot outage, fall back to the on-box builder
        "image_max_bytes": 2 * 1024**3,
        "cache_max_bytes": 100 * GB,
        "buildkitd_config": os.path.join(HERE, "buildkitd.toml"),
    },
    "disk": {
        "paths": ["/var/lib/docker", "/srv/speeddemon"],
        "min_free_gb": 100,
        "resume_free_gb": 110,
        "probe_interval_s": 5,
    },
    "git": {"ls_remote_timeout_s": 30, "clone_timeout_s": 300},
    "calibration": None,                         # harness/calibrate.py output; fills refs, outputs and hashes
    "reference_check": {                         # B2: periodic box-timing canary; ALERT-ONLY, never re-anchors scoring
        "enabled": True,
        "interval_s": 10800,                     # re-run the reference every 3 h
        "initial_delay_s": 300,                  # first check this long after serve starts
        "tracks": ["a"],                         # track(s) to re-run, rotated one per cycle; a is cheapest (~32 s x runs)
        "runs": 5,                               # middle-3 mean of 5: identical estimator to calibrate.py's T_ref_s
        "drift_threshold": 0.02,                 # alert if |measured - calibrated| / calibrated exceeds this
        "context": os.path.join(REPO, "reference"),  # the reference implementation, scored exactly like a submission
        "tag": "speeddemon/reference:2026",
        "log": None,                             # defaults to <state_dir>/reference_checks.jsonl (on-box, private)
        "work": None,                            # defaults to <work>/reference-canary
    },
    "tracks": {},
}
PATH_KEYS = ("registry", "state_dir", "history", "feed", "checkouts", "work", "timer", "allowlist")
UNCHANGED = ("this folder is the unchanged starter, which is the published reference and scores about 0, "
             "so the board does not run it; change the code and push again")
SIGILL_REBUILD = ("illegal instruction: the image was compiled for a CPU feature the scoring box does not have; "
                  "this track now builds on the scoring box, and the same commit reruns automatically")


def deep_merge(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def set_dotted(cfg, assignment):
    key, _, raw = assignment.partition("=")
    try:
        value = json.loads(raw)
    except ValueError:
        value = raw
    node = cfg
    parts = key.split(".")
    for p in parts[:-1]:
        node = node.setdefault(p, {})
    node[parts[-1]] = value


def load_config(path, sets=()):
    with open(path, encoding="utf-8") as f:
        cfg = deep_merge(copy.deepcopy(DEFAULTS), json.load(f))
    for s in sets:
        set_dotted(cfg, s)
    base = os.path.dirname(os.path.abspath(path))

    def resolve(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    for k in PATH_KEYS:
        cfg[k] = resolve(cfg[k])
    if cfg.get("calibration"):
        cfg["calibration"] = resolve(cfg["calibration"])
        apply_calibration(cfg)
    if cfg["builder"].get("buildkitd_config"):
        cfg["builder"]["buildkitd_config"] = resolve(cfg["builder"]["buildkitd_config"])
    for tcfg in cfg["tracks"].values():
        tcfg["data"] = resolve(tcfg["data"])
    rc = cfg["reference_check"]
    rc["context"] = resolve(rc["context"])
    rc["log"] = resolve(rc["log"]) if rc.get("log") else os.path.join(cfg["state_dir"], "reference_checks.jsonl")
    rc["work"] = resolve(rc["work"]) if rc.get("work") else os.path.join(cfg["work"], "reference-canary")
    cfg["config_dir"] = base
    problems = validate(cfg)
    if problems:
        raise SystemExit("config problems:\n  " + "\n  ".join(problems))
    return cfg


def apply_calibration(cfg):
    """Fill each track's output, expected_sha256, ref.T_s and ref.M_bytes from harness/calibrate.py's JSON.

    Values written in the config itself win. A track whose calibration did not pass every gate, or whose
    five runs disagreed on the output hash, is left alone, so validation reports it.
    """
    with open(cfg["calibration"], encoding="utf-8") as f:
        cal = json.load(f).get("tracks", {})
    for t, tc in cfg["tracks"].items():
        c = cal.get(t)
        tc.setdefault("ref", {}).setdefault("D", 1)
        if not c or not all((c.get("gates") or {"none": False}).values()):
            continue
        tc.setdefault("output", c.get("output"))
        shas = [x for x in c.get("output_sha256") or [] if x]
        if len(shas) == 1:
            tc.setdefault("expected_sha256", shas[0])
        tc["ref"].setdefault("T_s", c.get("T_ref_s"))
        tc["ref"].setdefault("M_bytes", c.get("M_ref_bytes"))


def validate(cfg):
    p = []
    if cfg["cgroups"] not in ("host", "colima"):
        p.append("cgroups must be host or colima")
    if not cfg["tracks"]:
        p.append("no tracks configured")
    for t, tc in cfg["tracks"].items():
        if t not in sources.TRACKS:
            p.append(f"unknown track {t!r}")
            continue
        if not os.path.isdir(tc.get("data", "")):
            p.append(f"track {t}: data directory {tc.get('data')!r} does not exist")
        if not tc.get("output") or "/" in tc["output"]:
            p.append(f"track {t}: output must be the spec's single file name")
        if not re.fullmatch(r"[0-9a-f]{64}", str(tc.get("expected_sha256", ""))):
            p.append(f"track {t}: expected_sha256 must be the 64-hex sha256 of the reference output on this dataset")
        ref = tc.get("ref", {})
        for axis, low in (("T_s", 0), ("M_bytes", 0), ("S", 0), ("D", 0)):
            if not isinstance(ref.get(axis), (int, float)) or not ref[axis] > low:
                p.append(f"track {t}: ref.{axis} must be a positive number")
    if not os.path.isfile(cfg["timer"]):
        p.append(f"timer {cfg['timer']} does not exist (setup-box.sh builds /srv/speeddemon/bin/sd-timer)")
    b = cfg["builder"]
    if int(b["max_concurrent"]) < 1 or float(b["timeout_s"]) <= 0 or int(b["image_max_bytes"]) <= 0:
        p.append("builder: max_concurrent >= 1, timeout_s > 0 and image_max_bytes > 0")
    if float(cfg["run_timeout_factor"]) <= 0 or float(cfg["min_interval_s"]) < 0:
        p.append("run_timeout_factor must be > 0 and min_interval_s >= 0")
    rc = cfg["reference_check"]
    if rc["enabled"]:
        if float(rc["interval_s"]) <= 0 or float(rc["initial_delay_s"]) < 0:
            p.append("reference_check.interval_s must be > 0 and initial_delay_s >= 0")
        if not 0 < float(rc["drift_threshold"]) < 1:
            p.append("reference_check.drift_threshold must be between 0 and 1")
        if int(rc["runs"]) < 3:
            p.append("reference_check.runs must be >= 3 (5 matches calibration's middle-3 mean)")
        for t in rc["tracks"]:
            if t not in cfg["tracks"]:
                p.append(f"reference_check track {t!r} is not a configured track")
    return p


def new_id(prefix):
    return f"{prefix}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{secrets.token_hex(3)}"


def epoch(iso_text):
    return float(calendar.timegm(time.strptime(iso_text, "%Y-%m-%dT%H:%M:%SZ"))) if iso_text else None


@dataclasses.dataclass
class TrackJob:
    track: str
    tag: str = ""
    stage: str = ""                 # check | build | image-size | image | built
    reason: str = ""
    build_s: float | None = None
    image_bytes: int | None = None
    log_tail: list = dataclasses.field(default_factory=list)
    cmd: list | None = None
    axes: dict | None = None
    build_started_at: float | None = None
    build_ended_at: float | None = None
    builder: str = ""               # backend that produced the image: "depot" or "local"
    flags: list = dataclasses.field(default_factory=list)   # private security flags (security.flag), never public
    lang: str = ""                  # build-image language: the stats' fallback when S is stubbed

    @property
    def ok(self):
        return self.stage == "built"


@dataclasses.dataclass
class Job:
    job_id: str
    slug: str
    team: str
    repo: str
    commit: str
    pushed_at: float
    dispatched_at: float
    attempt: int = 1
    observed_commit: str = ""
    tracks: list = dataclasses.field(default_factory=list)
    failure_stage: str = ""
    failure: str = ""
    builder: str = ""                 # backend building right now ("depot" or "local"), for the feed
    rebuild_on_box: bool = False      # a Depot-built run hit SIGILL: requeue the commit for an on-box build


class Board:
    def __init__(self, cfg, token=""):
        self.cfg = cfg
        provider = None
        if cfg.get("github_app_id"):
            provider = github_app.GitHubApp(cfg["github_app_id"], cfg["github_app_key"]).token_for
        self.git = sources.Git(token, "x-access-token", cfg["git"]["ls_remote_timeout_s"],
                               cfg["git"]["clone_timeout_s"], token_provider=provider)
        self.allowlist = steps.load_allowlist(cfg["allowlist"])
        self.cgroups = run_submission.CgroupReader(cfg["cgroups"])
        self.lock = threading.RLock()
        self.cv = threading.Condition(self.lock)
        self.fq = fairqueue.FairQueue(cfg["min_interval_s"])
        self.teams = {}
        self.registry_sig = None
        os.makedirs(cfg["state_dir"], exist_ok=True)
        self.history = records.History(cfg["history"])
        self.security = security.SecurityLog(cfg["state_dir"])
        self._pushes = {}             # slug -> monotonic times of recent new commits, for the churn flag
        self.watch_path = os.path.join(cfg["state_dir"], "watch.json")
        self.mirror = records.Mirror(cfg["mirror_command"], {"SD_HISTORY": cfg["history"], "SD_FEED": cfg["feed"],
                                                             "SD_WATCH": self.watch_path,
                                                             "SD_SECURITY": self.security.path,
                                                             "SD_SECURITY_SUMMARY": self.security.summary_path},
                                     cfg["mirror_timeout_s"],
                                     cwd=cfg.get("config_dir"))
        self.building = {}            # slug -> Job being fetched/built
        self.ready = []               # built Jobs waiting for the runner
        self.running = None           # {"slug", "team", "track", "commit", "since"}
        self.procs = steps.ProcessRegistry()
        d = cfg["disk"]
        probe = guards.DiskProbe("colima" if cfg["cgroups"] == "colima" else "host", d["paths"])
        self.disk = guards.DiskWatchdog(probe, d["min_free_gb"] * GB, d["resume_free_gb"] * GB, d["probe_interval_s"],
                                        on_low=self._disk_low, on_change=self._disk_changed)
        self.max_ready = int(cfg["max_ready"] or cfg["builder"]["max_concurrent"])
        self.stopping = threading.Event()
        self.feed_dirty = threading.Event()
        self.feed_lock = threading.Lock()
        self.threads = []
        self.reference_check = None        # latest canary result (full detail, kept in memory and in reference_check.log)
        self._canary_next = None           # monotonic deadline for the next canary; armed in start()
        self._canary_idx = 0               # rotates through reference_check.tracks, one per cycle
        self._load_last_reference_check()

    # -- state ----------------------------------------------------------------

    def load_registry(self, force=False):
        path = self.cfg["registry"]
        try:
            st = os.stat(path)
        except FileNotFoundError:
            if force:
                log.error("registry %s does not exist yet", path)
            return
        sig = (st.st_mtime_ns, st.st_size)
        if not force and sig == self.registry_sig:
            return
        try:
            teams, problems = sources.load_registry(path, self.cfg["allow_local_repos"])
        except (OSError, ValueError) as exc:
            log.error("registry %s unreadable, keeping the previous one: %s", path, exc)
            return
        for p in problems:
            log.warning("registry: %s", p)
        with self.lock:
            self.teams = {t.slug: t for t in teams}
            for t in teams:
                self.fq.team(t.slug)
            self.registry_sig = sig
        log.info("registry: %d teams", len(teams))

    def save_watch(self):
        with self.lock:
            data = {"schema": 1, "saved_at": records.iso(time.time()),
                    "teams": {slug: st.to_json() for slug, st in sorted(self.fq.teams.items())}}
        records.atomic_write_json(self.watch_path, data, mode=0o600)

    def load_watch(self):
        if os.path.exists(self.watch_path):
            with open(self.watch_path, encoding="utf-8") as f:
                data = json.load(f)
            for slug, st in data.get("teams", {}).items():
                self.fq.teams[slug] = fairqueue.TeamState.from_json(st)
            return
        # No saved state (first start, or it was lost): rebuild the rate-limit state from the history.
        for rec in self.history.snapshot():
            st = self.fq.team(rec["team_slug"])
            t = epoch(rec.get("dispatched_at"))
            if t and (st.last_dispatched_at is None or t >= st.last_dispatched_at):
                st.last_dispatched_at, st.last_dispatched_commit = t, rec.get("commit", "")

    def recover_interrupted(self):
        """A job in flight when the board stopped is requeued once, then recorded as interrupted."""
        for slug in list(self.fq.teams):
            inf, requeued = self.fq.requeue_interrupted(slug, self.cfg["restart_attempts"])
            if inf is None:
                continue
            if requeued:
                log.warning("requeued %s commit %s: the board stopped while it was in flight", slug, inf["commit"][:12])
            else:
                team = self.teams.get(slug)
                self.record({
                    **self._base_record(team.name if team else slug, slug, inf["job_id"], inf["commit"],
                                        inf["pushed_at"], inf["dispatched_at"]),
                    "track": None, "stage": "board", "ok": False,
                    "failure": "the board stopped while this commit was in flight, twice; push again to re-run",
                })
        self.save_watch()

    # -- lifecycle ------------------------------------------------------------

    def start(self, poll_thread=True):
        self.load_registry(force=True)
        self.load_watch()
        self.recover_interrupted()
        if self.cfg["builder"]["name"] and self.cfg["builder"]["create"]:
            log.info(steps.ensure_builder(self.cfg["builder"]))
        elif self.cfg["builder"]["name"] and not steps.builder_exists(self.cfg["builder"]["name"]):
            raise SystemExit(f"builder {self.cfg['builder']['name']} does not exist; run: board.py setup-builder")
        self.disk.start()
        rc = self.cfg["reference_check"]
        if poll_thread and rc["enabled"] and any(self._canary_calibrated(t) for t in rc["tracks"]):
            self._canary_next = time.monotonic() + float(rc["initial_delay_s"])
            log.info("reference canary armed: tracks %s every %s (first in %s)", ",".join(rc["tracks"]),
                     records.window(float(rc["interval_s"])), records.window(float(rc["initial_delay_s"])))
        targets = [("runner", self._runner), ("feed", self._feed_loop)]
        targets += [(f"build-{i}", self._build_worker) for i in range(int(self.cfg["builder"]["max_concurrent"]))]
        if poll_thread:
            targets.append(("poll", self._poll_loop))
        for name, fn in targets:
            t = threading.Thread(target=fn, name=name, daemon=True)
            t.start()
            self.threads.append(t)
        self.write_feed()

    def stop(self):
        self.stopping.set()
        self.feed_dirty.set()
        killed = self.procs.kill_all("stop")
        with self.lock:
            running = self.running
        if running:
            steps.kill_running_containers()
        with self.cv:
            self.cv.notify_all()
        for t in self.threads:
            t.join(timeout=90)
        self.disk.stop.set()
        self.save_watch()
        self.write_feed()
        self.mirror.flush(timeout=self.cfg["mirror_timeout_s"])
        log.info("stopped (%d build(s) killed%s)", killed, ", run interrupted" if running else "")

    def run_until_idle(self, wait_pending=False, max_seconds=3600):
        """Poll once, work through everything that may start, then stop (the `once` command)."""
        self.start(poll_thread=False)
        try:
            self.poll_once()
            deadline = time.monotonic() + max_seconds
            while not self.idle(wait_pending) and time.monotonic() < deadline:
                with self.cv:
                    self.cv.wait(1.0)
        finally:
            self.stop()

    def idle(self, wait_pending=False):
        with self.lock:
            now = time.time()
            if self.building or self.ready or self.running or any(st.inflight for st in self.fq.teams.values()):
                return False
            nxt = self.fq.next_eligible(now)
            if nxt and nxt.slug in self.teams:
                return False
            if wait_pending and any(st.pending for slug, st in self.fq.teams.items() if slug in self.teams):
                return False
            return True

    # -- watching -------------------------------------------------------------

    def _poll_loop(self):
        while not self.stopping.is_set():
            try:
                self.poll_once()
            except Exception:
                log.exception("poll failed")
            self.stopping.wait(self.cfg["poll_interval_s"])

    def poll_once(self):
        self.load_registry()
        with self.lock:
            teams = list(self.teams.values())
        results = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=int(self.cfg["poll_workers"])) as ex:
            futures = {ex.submit(self.git.ls_remote, t.repo, self.cfg["branch"]): t for t in teams}
            for fut in concurrent.futures.as_completed(futures):
                team = futures[fut]
                try:
                    results[team.slug] = fut.result()
                except Exception as exc:
                    results[team.slug] = ("", f"ls-remote failed ({type(exc).__name__})")
        now = time.time()
        queued, new_heads = [], []
        with self.cv:
            for slug, (sha, err) in results.items():
                st = self.fq.team(slug)
                if err:
                    st.watch_error, st.checked_at = err, now
                    continue
                if self.fq.observe(slug, sha, now):
                    queued.append((self.teams[slug].name if slug in self.teams else slug, sha))
                    new_heads.append((slug, sha))
                if not sha:
                    st.watch_error = f"no {self.cfg['branch']} branch yet"
            self.cv.notify_all()
        self.save_watch()
        for name, sha in queued:
            log.info("queued %s at %s", name, sha[:12])
        self._note_churn([slug for slug, _ in new_heads], now)
        self.feed_dirty.set()
        return queued

    def _note_churn(self, slugs, now):
        """A private low flag when a team pushes more new commits in an hour than anyone iterates."""
        limit = int(self.cfg["push_churn_per_hour"])
        for slug in slugs:
            times = [t for t in self._pushes.get(slug, []) if now - t < 3600] + [now]
            self._pushes[slug] = times
            if len(times) > limit:
                team = self.teams[slug].name if slug in self.teams else slug
                self.security.emit("push_churn", "low", f"{len(times)} new commits in the last hour", team=team,
                                   key=f"push_churn:{slug}:{int(now // 3600)}")

    # -- building -------------------------------------------------------------

    def _take_job(self):
        with self.cv:
            while not self.stopping.is_set():
                now = time.time()
                if self.disk.ok and len(self.ready) < self.max_ready:
                    st = self.fq.next_eligible(now)
                    while st and st.slug not in self.teams:
                        log.warning("dropping the queued commit of %s: no longer in the registry", st.slug)
                        st.pending = None
                        st = self.fq.next_eligible(now)
                    if st:
                        team = self.teams[st.slug]
                        job_id = new_id("job")
                        inf = self.fq.dispatch(st.slug, now, job_id)
                        job = Job(job_id=job_id, slug=st.slug, team=team.name, repo=team.repo, commit=inf["commit"],
                                  pushed_at=inf["pushed_at"], dispatched_at=now, attempt=inf["attempt"])
                        self.building[st.slug] = job
                        self.save_watch()
                        self.feed_dirty.set()
                        log.info("dispatched %s commit %s (pushed %.0f s ago)", team.name, job.commit[:12], now - job.pushed_at)
                        return job
                wake = self.fq.next_wakeup(now)
                self.cv.wait(5.0 if wake is None else min(5.0, max(0.05, wake - now)))
        return None

    def _wait_disk(self):
        with self.cv:
            while not self.disk.ok and not self.stopping.is_set():
                self.cv.wait(5.0)
            return not self.stopping.is_set()

    def _build_worker(self):
        while True:
            job = self._take_job()
            if job is None:
                return
            try:
                self._build_job(job)
            except Exception:
                log.exception("building %s failed inside the board", job.team)
                job.failure_stage = "board"
                job.failure = "the board hit an internal error on this commit (logged for the organisers); push again"
            if self.stopping.is_set():
                return                       # left in flight: requeued on the next start
            self._built(job)

    def _build_job(self, job):
        cfg = self.cfg
        checkout = os.path.join(cfg["checkouts"], f"{job.slug}-{job.job_id}")
        shutil.rmtree(checkout, ignore_errors=True)
        os.makedirs(cfg["checkouts"], exist_ok=True)
        try:
            if not self._wait_disk():
                return
            key = f"clone:{job.job_id}"
            sha, err = self.git.clone(job.repo, cfg["branch"], checkout, register=lambda p: self.procs.add(key, p))
            killed = self.procs.remove(key)
            if self.stopping.is_set():
                return
            if killed == "disk":
                err = "fetch stopped by the board's disk guard (the box ran low on disk); push again to retry"
            if err:
                job.failure_stage, job.failure = "fetch", err
                return
            if sha != job.commit:
                job.observed_commit, job.commit = job.commit, sha
                with self.lock:
                    self.fq.set_commit(job.slug, sha)
                self.save_watch()
            tracks = steps.present_tracks(checkout)
            if not tracks:
                job.failure_stage = "checkout"
                job.failure = f"no track-a, track-b, track-c or track-d directory on {cfg['branch']} at {sha[:12]}"
                return
            for t in tracks:
                if self.stopping.is_set():
                    return
                job.tracks.append(self._build_track(job, checkout, t))
        finally:
            shutil.rmtree(checkout, ignore_errors=True)

    def _build_track(self, job, checkout, track):
        tj = TrackJob(track=track, build_started_at=time.time())
        try:
            tcfg = self.cfg["tracks"].get(track)
            if not tcfg:
                tj.stage, tj.reason = "check", f"track {track} is not on the live board yet"
                return tj
            reason = steps.check_track(checkout, track, self.allowlist)
            if reason:
                tj.stage, tj.reason = "check", reason
                tj.flags += security.dockerfile_flags(reason)
                return tj
            if self.cfg["skip_unchanged_reference"] and steps.unchanged_reference(os.path.join(checkout, f"track-{track}")):
                tj.stage, tj.reason = "unchanged", UNCHANGED
                return tj
            if not self._wait_disk():
                tj.stage, tj.reason = "build", "board stopping"
                return tj
            tj.tag = f"{self.cfg['image_prefix']}/{job.slug}-{track}:{job.commit[:12]}"
            labels = {"dev.speeddemon.board": "1", "dev.speeddemon.team": job.slug,
                      "dev.speeddemon.track": track, "dev.speeddemon.commit": job.commit}
            scrub = [(os.path.realpath(checkout), "."), (os.path.abspath(checkout), ".")]
            track_dir = os.path.join(checkout, f"track-{track}")
            with self.lock:
                sticky = track in self.fq.team(job.slug).box_tracks
            native = [] if sticky else steps.native_cpu_flags(track_dir)
            if native:
                log.info("%s track %s asks for the native CPU (%s): building on the box", job.team, track, ", ".join(native))
            res = steps.build_image(track_dir, tj.tag, self.cfg["builder"], labels,
                                    self.procs, f"build:{job.job_id}:{track}", replacements=scrub,
                                    on_backend=lambda b: setattr(job, "builder", b),
                                    force_local=sticky or bool(native))
            tj.build_s, tj.image_bytes, tj.builder = res["build_s"], res["image_bytes"], res.get("builder", "")
            if not res["ok"]:
                tj.stage, tj.reason, tj.log_tail = res["stage"], res["reason"], res["log_tail"]
                return tj
            cmd, why = steps.image_entry(tj.tag)
            if cmd is None:
                steps.remove_image(tj.tag)
                tj.stage, tj.reason = "image", why
                if "VOLUME" in why:
                    tj.flags.append(security.flag("image_volume", "medium", why))
                return tj
            tj.cmd = cmd
            if steps.image_has_healthcheck(tj.tag):
                tj.flags.append(security.flag("image_healthcheck", "medium",
                                              "image declares a HEALTHCHECK (disabled at run time)"))
            tj.lang = steps.track_language(track_dir)
            blobs = security.suspicious_files(track_dir)
            if blobs:
                tj.flags.append(security.flag("review_blob", "low", "S review: " + ", ".join(blobs)))
            tj.axes = axes.static_axes(checkout, track, tcfg["ref"], cpuset=self.cfg["builder"]["cpuset"])
            tj.stage = "built"
            return tj
        finally:
            tj.build_ended_at = time.time()

    def _built(self, job):
        if job.failure:
            self.record({**self._job_record(job), "track": None, "stage": job.failure_stage, "ok": False,
                         "failure": job.failure})
        for tj in job.tracks:
            if not tj.ok:
                self.record(self._track_record(job, tj))
        runnable = [tj for tj in job.tracks if tj.ok]
        with self.cv:
            self.building.pop(job.slug, None)
            if runnable:
                job.tracks = runnable
                self.ready.append(job)
            else:
                self.fq.finish(job.slug)
            self.cv.notify_all()
        self.save_watch()
        self.feed_dirty.set()

    # -- running --------------------------------------------------------------

    def _runner(self):
        while True:
            job = canary_track = None
            with self.cv:
                while not self.ready and not self.stopping.is_set() and not self._canary_due():
                    self.cv.wait(self._runner_wait())
                if self.stopping.is_set():
                    return
                if self._canary_due():            # a due canary takes the next slot, even under load
                    canary_track = self._canary_pick()
                    if canary_track is None:      # nothing calibrated after all: re-arm and wait
                        self._canary_next = time.monotonic() + float(self.cfg["reference_check"]["interval_s"])
                        continue
                    self.running = {"slug": "_reference", "team": "reference check", "track": canary_track,
                                    "commit": None, "since": time.time()}
                else:
                    job = min(self.ready, key=lambda j: j.dispatched_at)
                    self.ready.remove(job)
                    self.running = {"slug": job.slug, "team": job.team, "track": job.tracks[0].track,
                                    "commit": job.commit, "since": time.time()}
                self.cv.notify_all()          # a built slot opened: another build may start
            if job is not None:
                try:
                    self._run_job(job)
                except Exception:
                    log.exception("running %s failed inside the board", job.team)
                if self.stopping.is_set():
                    return                        # left in flight: requeued on the next start
                with self.cv:
                    self.running = None
                    if job.rebuild_on_box:
                        self.fq.requeue_rebuild(job.slug)
                    self.fq.finish(job.slug)
                    self.cv.notify_all()
                self.save_watch()
                self.feed_dirty.set()
            else:
                try:
                    self._run_canary(canary_track)
                except Exception as exc:
                    log.exception("reference canary failed inside the board")
                    self.reference_check = {"schema": 1, "kind": "reference_check", "track": canary_track,
                                            "checked_at": records.iso(time.time()), "ok": False,
                                            "summary": f"canary raised {type(exc).__name__}"}
                finally:
                    self._canary_next = time.monotonic() + float(self.cfg["reference_check"]["interval_s"])
                if self.stopping.is_set():
                    return
                with self.cv:
                    self.running = None
                    self.cv.notify_all()
                self.feed_dirty.set()

    def _run_job(self, job):
        cfg = self.cfg
        for tj in job.tracks:
            if self.stopping.is_set():
                return
            tcfg = cfg["tracks"][tj.track]
            started = time.time()
            with self.lock:
                self.running = {"slug": job.slug, "team": job.team, "track": tj.track, "commit": job.commit, "since": started}
            self.write_feed()
            r, harness_error = None, ""
            timeout_s = round(float(cfg["run_timeout_factor"]) * float(tcfg["ref"]["T_s"]), 3)
            try:
                r, timeout_s = steps.run_track(tj.tag, tj.track, tj.cmd, tcfg, cfg, self.cgroups)
            except Exception as exc:
                log.exception("run of %s track %s failed inside the harness", job.team, tj.track)
                harness_error = f"the board could not complete this run ({type(exc).__name__}; logged for the organisers)"
            ended = time.time()
            steps.cleanup_work(cfg["work"])
            if self.stopping.is_set():
                return
            rec = self._track_record(job, tj, r, started, ended, timeout_s, harness_error)
            if tj.builder == "depot" and not rec["ok"] and steps.is_sigill(r):
                with self.lock:
                    st = self.fq.team(job.slug)
                    if tj.track not in st.box_tracks:
                        st.box_tracks.append(tj.track)
                job.rebuild_on_box = True
                rec["failure"] = SIGILL_REBUILD
                rec.setdefault("security", []).append(security.flag(
                    "sigill_rebuild", "low", "a Depot-built run died on SIGILL; the track now builds on the box"))
                log.warning("%s track %s hit SIGILL on a Depot-built image: rebuilding on the box", job.team, tj.track)
            self.record(rec)
            log.info("%s track %s at %s: %s", job.team, tj.track, job.commit[:12],
                     f"T {rec['T_s']:.3f} s, M {rec['M_bytes'] / 1e6:.1f} MB, K {rec['K']:.4f}, {rec['points']:.2f} points"
                     if rec["ok"] else f"failed: {rec['failure']}")
            repo = tj.tag.rsplit(":", 1)[0]
            removed = steps.prune_images(repo, keep_tag=tj.tag)
            if removed:
                log.info("pruned %s", ", ".join(removed))

    # -- reference canary (B2) ------------------------------------------------
    # Periodically re-run the reference on the P-cores through run_submission's exact
    # contract and compare T to calibration. It runs ONLY through the runner, so it is
    # serialized with team runs (never overlaps one). It is ALERT-ONLY: it never changes
    # T_ref, the timeout cap, or any team's score. The site promises drift handling for
    # final scoring (B1), not the board; this is box-health early warning for A4.

    def _canary_calibrated(self, track):
        tc = self.cfg["tracks"].get(track, {})
        return tc.get("ref", {}).get("T_s") is not None and bool(tc.get("output"))

    def _canary_due(self):
        rc = self.cfg["reference_check"]
        if not rc["enabled"] or self._canary_next is None or not self.disk.ok:
            return False
        if time.monotonic() < self._canary_next:
            return False
        return any(self._canary_calibrated(t) for t in rc["tracks"])

    def _canary_pick(self):
        cands = [t for t in self.cfg["reference_check"]["tracks"] if self._canary_calibrated(t)]
        if not cands:
            return None
        track = cands[self._canary_idx % len(cands)]
        self._canary_idx += 1
        return track

    def _runner_wait(self):
        if self._canary_next is None:
            return 5.0
        return max(0.2, min(5.0, self._canary_next - time.monotonic()))

    def _run_canary(self, track):
        cfg, rc = self.cfg, self.cfg["reference_check"]
        tcfg = cfg["tracks"][track]
        t_calib = float(tcfg["ref"]["T_s"])
        started = time.time()
        runs = int(rc["runs"])
        log.info("reference canary: re-running track %s x%d to check box timing (calibrated T %.3f s)",
                 track, runs, t_calib)
        work = rc["work"]
        os.makedirs(work, exist_ok=True)
        results_path = os.path.join(work, f"canary-{track}.json")
        timeout_s = max(60.0, 10.0 * t_calib)        # per-run wall cap; the reference sits at ~t_calib
        cmd = [sys.executable, os.path.join(REPO, "harness", "run_submission.py"),
               "--context", rc["context"], "--track", track, "--data", tcfg["data"],
               "--output", tcfg["output"], "--timeout-s", str(timeout_s), "--timer", cfg["timer"],
               "--work", work, "--tag", rc["tag"], "--runs", str(runs), "--cgroups", cfg["cgroups"],
               "--results", results_path]
        if tcfg.get("expected_sha256"):
            cmd += ["--expected-sha256", tcfg["expected_sha256"]]
        key = f"canary:{track}:{uuid.uuid4().hex[:8]}"
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                start_new_session=True)
        self.procs.add(key, proc)
        stderr, timed_out = "", False
        try:
            _, stderr = proc.communicate(timeout=timeout_s * runs * 2 + 300)
        except subprocess.TimeoutExpired:
            timed_out = True
            steps.kill_group(proc)
            _, stderr = proc.communicate()
        killed = self.procs.remove(key)
        if killed or timed_out:
            steps.kill_running_containers()       # kill_group ends the client, not the sd-run-* it started
        steps.cleanup_work(work)
        result = self._reference_result(track, t_calib, started, results_path, proc.returncode, killed, stderr)
        self.reference_check = result
        self._append_reference_log(result)
        (log.info if result["ok"] else log.warning)("reference canary track %s: %s", track, result["summary"])

    def _reference_result(self, track, t_calib, checked_at, results_path, returncode, killed, stderr):
        threshold = float(self.cfg["reference_check"]["drift_threshold"])
        out = {"schema": 1, "kind": "reference_check", "track": track, "checked_at": records.iso(checked_at),
               "T_calib_s": round(t_calib, 6), "T_measured_s": None, "drift": None, "drift_pct": None,
               "threshold": threshold, "spread_ok": None, "gates": None, "walls_s": None, "ok": False}
        if killed:
            out["summary"] = f"canary stopped ({killed})"
            return out
        run = None
        try:
            with open(results_path, encoding="utf-8") as f:
                run = json.load(f)
        except (OSError, ValueError):
            pass
        if run is None:
            out["summary"] = f"reference produced no result (exit {returncode}); box or harness problem"
            if stderr:
                out["stderr_tail"] = steps.tail_lines(stderr, steps.STDERR_TAIL_LINES)
            return out
        out["gates"] = run.get("gates")
        t_meas = run.get("T_s")
        if t_meas is None or not run.get("gates") or not all(run["gates"].values()):
            out["summary"] = f"reference did not produce a clean timed run (gates {run.get('gates')})"
            return out
        out["walls_s"] = [r.get("wall_s") for r in run.get("batches", [[]])[-1]]
        out["spread_ok"] = run.get("spread_ok")
        drift = (t_meas - t_calib) / t_calib
        out.update(T_measured_s=round(t_meas, 6), drift=round(drift, 5), drift_pct=round(drift * 100, 2),
                   ok=abs(drift) <= threshold)
        out["summary"] = (f"T {t_meas:.3f} s vs calibrated {t_calib:.3f} s, drift {drift * 100:+.2f}% "
                          f"(threshold {threshold * 100:.0f}%) -> {'ok' if out['ok'] else 'OVER THRESHOLD'}")
        return out

    def _append_reference_log(self, result):
        path = self.cfg["reference_check"]["log"]
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(result) + "\n")
        except OSError:
            log.exception("could not append the reference-check log %s", path)

    def _load_last_reference_check(self):
        path = self.cfg["reference_check"].get("log")
        if not path or not os.path.exists(path):
            return
        try:
            last = None
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        last = line
            if last:
                self.reference_check = json.loads(last)
        except (OSError, ValueError):
            pass

    def _public_reference_check(self):
        """A numbers-free health view for the PUBLIC feed and A4; full detail stays in reference_check.log."""
        rc = self.cfg["reference_check"]
        if not rc["enabled"]:
            return None
        stale_after = int(float(rc["interval_s"]) * 2 + float(rc["initial_delay_s"]) + 600)
        latest = self.reference_check
        if not latest:
            return {"ok": None, "checked_at": None, "track": None,
                    "note": "no reference check yet", "stale_after_s": stale_after}
        note = ("box timing nominal" if latest["ok"]
                else "reference timing drift over threshold" if latest.get("drift") is not None
                else "reference run failed")
        return {"ok": latest["ok"], "checked_at": latest.get("checked_at"), "track": latest.get("track"),
                "note": note, "stale_after_s": stale_after}

    # -- records --------------------------------------------------------------

    def _base_record(self, team, slug, job_id, commit, pushed_at, dispatched_at):
        return {"schema": records.SCHEMA, "run_id": f"run-{uuid.uuid4().hex[:12]}", "job_id": job_id,
                "team": team, "team_slug": slug, "commit": commit,
                "pushed_at": records.iso(pushed_at), "dispatched_at": records.iso(dispatched_at),
                "recorded_at": records.iso(time.time())}

    def _job_record(self, job):
        rec = self._base_record(job.team, job.slug, job.job_id, job.commit, job.pushed_at, job.dispatched_at)
        if job.observed_commit:
            rec["observed_commit"] = job.observed_commit
        if job.attempt > 1:
            rec["attempt"] = job.attempt
        return rec

    def _track_record(self, job, tj, r=None, started=None, ended=None, timeout_s=None, harness_error=""):
        tcfg = self.cfg["tracks"].get(tj.track, {})
        rec = {**self._job_record(job), "track": tj.track,
               "build_started_at": records.iso(tj.build_started_at), "build_ended_at": records.iso(tj.build_ended_at),
               "build_s": tj.build_s, "image_bytes": tj.image_bytes,
               "started_at": None, "ended_at": None, "queue_latency_s": None,
               "gates": {"check": tj.stage != "check",
                         "build": None if tj.stage in ("check", "unchanged") else tj.stage not in ("build", "image-size", "image"),
                         "correctness": None, "timeout": None, "sandbox": None},
               "T_s": None, "M_bytes": None, "S": None, "D": None, "K": None, "points": None,
               "static_axes": tj.axes["source"] if tj.axes else None, "timeout_s": timeout_s, "builder": tj.builder or None,
               "langs": (tj.axes or {}).get("langs") or ({tj.lang: 0} if tj.lang else None),
               "board": {"runs": 1, "run_cpuset": self.cfg["run_cpuset"], "dataset": self.cfg["dataset_label"],
                         "timeout_factor": self.cfg["run_timeout_factor"]}}
        flags = list(tj.flags) + security.run_flags(r, float(self.cfg["too_fast_floor_s"] or 0))
        if flags:
            rec["security"] = flags                 # private: history only, never in board.json
        if not tj.ok:
            rec.update(stage=tj.stage, ok=False, failure=tj.reason)
            if tj.log_tail:
                rec["build_log_tail"] = tj.log_tail[-steps.LOG_TAIL_LINES:]
            return rec
        rec.update(started_at=records.iso(started), ended_at=records.iso(ended),
                   queue_latency_s=round(started - job.pushed_at, 1), S=tj.axes["S"], D=tj.axes["D"])
        if r is None:
            rec.update(stage="run", ok=False, failure=harness_error, harness_error=True)
            return rec
        gates = steps.gates_of(r)
        rec["gates"].update(gates)
        if "wall_s" in r:
            rec["T_s"] = r["wall_s"]
        rec["M_bytes"] = r.get("peak_bytes")
        if r.get("sha256"):
            rec["sha256"] = r["sha256"]
        rec["run_id"] = r.get("run_id") or rec["run_id"]
        if all(gates.values()):
            k, points = steps.score(r["wall_s"], r["peak_bytes"], tj.axes["S"], tj.axes["D"], tcfg["ref"])
            rec.update(stage="scored", ok=True, failure=None, K=round(k, 6), points=round(points, 6))
        else:
            rec.update(stage="run", ok=False, failure=steps.failure_of(r, gates, timeout_s, float(self.cfg["run_timeout_factor"])))
            if r.get("stderr"):
                rec["stderr_tail"] = steps.tail_lines(r["stderr"], steps.STDERR_TAIL_LINES,
                                                      replacements=[(self.cfg["work"], "<work>")])
        return rec

    def record(self, rec):
        """Append to the history (fsynced), refresh the feed, then mirror both off the box."""
        self.history.append(rec)
        try:
            self.security.emit_flags(rec.get("security"), team=rec.get("team"), track=rec.get("track"),
                                     run_id=rec.get("run_id"), commit=rec.get("commit"))
        except Exception:
            log.exception("could not write a security event")
        self.write_feed()
        self.mirror.trigger()

    # -- feed -----------------------------------------------------------------

    def _feed_loop(self):
        while not self.stopping.is_set():
            self.feed_dirty.wait(self.cfg["feed_interval_s"])
            self.feed_dirty.clear()
            if self.stopping.is_set():
                return
            try:
                self.write_feed()
            except Exception:
                log.exception("writing the feed failed")

    def write_feed(self):
        with self.feed_lock:
            with self.lock:
                runtime = {"running": dict(self.running) if self.running else None,
                           "building": {slug: (getattr(j, "builder", "") or True) for slug, j in self.building.items()},
                           "ready": [j.slug for j in self.ready], "disk_ok": self.disk.ok}
                teams = list(self.teams.values())
                fq = copy.deepcopy(self.fq)
            info = {"event": self.cfg["event"], "dataset": self.cfg["dataset_label"],
                    "reference_check": self._public_reference_check(),
                    "rules": {"runs_per_push": 1, "timeout_factor": self.cfg["run_timeout_factor"],
                              "min_interval_s": self.cfg["min_interval_s"], "branch": self.cfg["branch"],
                              "build_timeout_s": self.cfg["builder"]["timeout_s"],
                              "image_max_bytes": self.cfg["builder"]["image_max_bytes"]}}
            feed = records.build_feed(time.time(), teams, fq, runtime, self.history.snapshot(), info)
            records.atomic_write_json(self.cfg["feed"], feed)
            stats = records.build_stats(time.time(), teams, self.history.snapshot(),
                                        self.cfg["event_starts"], self.cfg["event_ends"])
            records.atomic_write_json(os.path.join(os.path.dirname(self.cfg["feed"]), "stats.json"), stats)
            return feed

    # -- disk -----------------------------------------------------------------

    def _disk_low(self, free):
        n = self.procs.kill_all("disk")
        if n:
            log.error("disk guard: killed %d build(s)/clone(s) at %.1f GB free", n, free / GB)

    def _disk_changed(self, ok, free):
        with self.cv:
            self.cv.notify_all()
        self.feed_dirty.set()
        name = self.cfg["builder"]["name"]
        if not ok and name:
            # Trim the builder's cache back under its cap and towards the resume mark; images and running
            # containers are untouched. BuildKit's own gc (buildkitd.toml) normally keeps it there.
            proc = steps.sh(["docker", "buildx", "prune", "--builder", name, "--force",
                             "--max-used-space", str(int(self.cfg["builder"]["cache_max_bytes"])),
                             "--min-free-space", str(int(self.cfg["disk"]["resume_free_gb"] * GB))], timeout=600)
            log.error("disk guard: pruned builder cache (exit %d) %s", proc.returncode, proc.stdout.strip()[-200:])


class _SystemdNotifier:
    """Minimal sd_notify over $NOTIFY_SOCKET, stdlib only (python3-sdnotify is not installed
    on the box). A no-op when not run under systemd Type=notify, so `once` and local runs
    are unaffected."""

    def __init__(self):
        self.sock = None
        self.addr = None
        self.watchdog_interval = None
        addr = os.environ.get("NOTIFY_SOCKET")
        if not addr:
            return
        if addr[0] == "@":                       # abstract namespace
            addr = "\0" + addr[1:]
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        except OSError:
            self.sock = None
            return
        self.addr = addr
        usec = os.environ.get("WATCHDOG_USEC", "")
        if usec.isdigit() and int(usec) > 0:
            self.watchdog_interval = int(usec) / 2_000_000.0   # ping at half the deadline (seconds)

    def _send(self, msg):
        if not self.sock:
            return
        try:
            self.sock.sendto(msg, self.addr)
        except OSError:
            pass

    def ready(self):
        self._send(b"READY=1")

    def watchdog(self):
        self._send(b"WATCHDOG=1")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Speed Demon 2026 live board")
    ap.add_argument("command", choices=["serve", "once", "setup-builder", "feed", "reference-check"])
    ap.add_argument("--config", required=True)
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                    help="override a config value, e.g. --set min_interval_s=30 --set builder.cpuset='\"2-3\"'")
    ap.add_argument("--prewarm", action="store_true", help="setup-builder: pull every allowlisted base into the builder")
    ap.add_argument("--wait-pending", action="store_true", help="once: also wait for queued commits held by the interval")
    ap.add_argument("--max-seconds", type=float, default=3600, help="once: give up after this long")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(threadName)s %(message)s")
    cfg = load_config(args.config, args.set)
    # Taken out of the environment so no child (git, buildx, docker run, the mirror command) inherits it;
    # git gets it back only as an HTTP header in its own environment config (sources.Git.env).
    token = os.environ.pop("BOT_TOKEN", "")
    if not token and not cfg.get("github_app_id") and args.command in ("serve", "once"):
        log.warning("no github_app_id and no BOT_TOKEN: only public (or local) repos can be read")

    if args.command == "setup-builder":
        print(steps.ensure_builder(cfg["builder"]))
        if args.prewarm:
            work = os.path.join(cfg["state_dir"], "prewarm")
            for image, ok, seconds in steps.prewarm_builder(cfg["builder"], steps.load_allowlist(cfg["allowlist"]), work):
                print(f"{'ok  ' if ok else 'FAIL'} {image} ({seconds} s)")
            shutil.rmtree(work, ignore_errors=True)
        return 0
    board = Board(cfg, token)
    if args.command == "feed":
        board.load_registry(force=True)
        board.load_watch()
        feed = board.write_feed()
        print(f"wrote {cfg['feed']}: {len(feed['teams'])} teams, {len(board.history.records)} records")
        return 0
    if args.command == "once":
        board.run_until_idle(args.wait_pending, args.max_seconds)
        return 0
    if args.command == "reference-check":
        # Run one canary cycle per calibrated track and exit. Non-disruptive to the serve loop,
        # but it runs on the P-cores: only run it when the board is idle, or it will contend with a live run.
        tracks = [t for t in cfg["reference_check"]["tracks"] if board._canary_calibrated(t)]
        if not tracks:
            raise SystemExit("reference-check: no calibrated tracks among reference_check.tracks")
        drifted = False
        for t in tracks:
            board._run_canary(t)
            print(json.dumps(board.reference_check, indent=2))
            drifted = drifted or not board.reference_check["ok"]
        return 1 if drifted else 0

    done = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: done.set())
    board.start()
    log.info("serving: %d teams, tracks %s, run cpuset %s, builder %s", len(board.teams),
             ",".join(sorted(cfg["tracks"])), cfg["run_cpuset"], cfg["builder"]["name"] or "(default)")
    # systemd watchdog: ping WATCHDOG=1 only while every worker thread is alive, so a dead
    # worker (not just a hung main thread) lets WatchdogSec fire and restart us. No-op off systemd.
    notifier = _SystemdNotifier()
    notifier.ready()
    ping_every = notifier.watchdog_interval
    last_ping = 0.0
    while not done.wait(1.0):
        if ping_every is not None and board.threads and all(t.is_alive() for t in board.threads):
            now = time.monotonic()
            if now - last_ping >= ping_every:
                notifier.watchdog()
                last_ping = now
    board.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

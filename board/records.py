"""The board's evidence and its public face.

history.jsonl is append-only: one JSON object per team-track attempt, written
and fsynced before anything else learns about it. board.json is derived from
it (plus the live queue) and replaced atomically, so a reader never sees half
a file. Neither ever holds more than the last 40 lines of a team's own build
output, and only inside that team's record.
"""
import json
import logging
import os
import statistics
import subprocess
import tempfile
import threading
import time

log = logging.getLogger("sdboard")

SCHEMA = 1
RECENT_RUNS = 20
PUBLIC_FIELDS = ("run_id", "commit", "pushed_at", "dispatched_at", "started_at", "ended_at", "queue_latency_s",
                 "ok", "stage", "gates", "T_s", "M_bytes", "S", "D", "K", "points", "static_axes",
                 "timeout_s", "build_s", "image_bytes", "failure")


def iso(t):
    if t is None:
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def window(seconds):
    seconds = int(seconds)
    return f"{seconds // 60} minutes" if seconds % 60 == 0 and seconds >= 120 else f"{seconds} seconds"


def atomic_write_json(path, obj, mode=0o644):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=1, ensure_ascii=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    try:
        dfd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class History:
    """Append-only JSONL. Records are kept in memory too, for the feed."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.records = []
        self._load()

    def _load(self):
        if not os.path.exists(self.path):
            os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
            return
        with open(self.path, "rb") as f:
            data = f.read()
        for n, raw in enumerate(data.split(b"\n"), 1):
            if not raw.strip():
                continue
            try:
                self.records.append(json.loads(raw))
            except ValueError:
                log.warning("history line %d is not JSON (a write cut short?); kept in the file, skipped", n)
        if data and not data.endswith(b"\n"):
            with open(self.path, "ab") as f:   # close the cut-short line so the next record starts clean
                f.write(b"\n")
                f.flush()
                os.fsync(f.fileno())

    def append(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())
            self.records.append(record)

    def snapshot(self):
        with self.lock:
            return list(self.records)


class Mirror:
    """Runs a configured shell command after each history append (e.g. `aws s3 cp`), off when None.

    Appends that land while a copy is running coalesce into one more run, so the
    remote copy always catches up with the last record. It never blocks the board.
    """

    def __init__(self, command, env, timeout_s=120, cwd=None):
        self.command = command or ""
        self.env = env
        self.cwd = cwd
        self.timeout_s = timeout_s
        self.pending = threading.Event()
        self.idle = threading.Event()
        self.idle.set()
        self.thread = None
        self.lock = threading.Lock()
        self.runs = 0
        self.failures = 0

    def trigger(self):
        if not self.command:
            return
        with self.lock:
            self.idle.clear()
            self.pending.set()
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._loop, name="mirror", daemon=True)
                self.thread.start()

    def _loop(self):
        while True:
            with self.lock:
                if not self.pending.is_set():
                    self.idle.set()
                    self.thread = None
                    return
                self.pending.clear()
            try:
                proc = subprocess.run(["sh", "-c", self.command], env={**os.environ, **self.env}, cwd=self.cwd,
                                      capture_output=True, text=True, timeout=self.timeout_s)
                self.runs += 1
                if proc.returncode != 0:
                    self.failures += 1
                    log.error("mirror command failed (exit %d): %s", proc.returncode, proc.stderr.strip()[-500:])
            except subprocess.TimeoutExpired:
                self.failures += 1
                log.error("mirror command timed out after %d s", self.timeout_s)

    def flush(self, timeout=None):
        return self.idle.wait(timeout)


# -- the feed -------------------------------------------------------------------

def summarise(rec, with_logs=False):
    out = {k: rec.get(k) for k in PUBLIC_FIELDS if k in rec}
    if with_logs:
        for k in ("build_log_tail", "stderr_tail"):
            if rec.get(k):
                out[k] = rec[k]
    return out


def best_and_latest(records):
    """{(team_slug, track): {"best": record | None, "latest": record}} over the whole history."""
    out = {}
    for rec in records:
        if not rec.get("track"):
            continue
        slot = out.setdefault((rec["team_slug"], rec["track"]), {"best": None, "latest": None})
        slot["latest"] = rec
        if rec.get("ok") and rec.get("points") is not None:
            best = slot["best"]
            if best is None or (rec["points"], rec["K"]) > (best["points"], best["K"]):
                slot["best"] = rec
    return out


def latest_job_failures(records):
    """{team_slug: record} for the latest whole-job failure (fetch, no track directories)."""
    out = {}
    for rec in records:
        if not rec.get("track"):
            out[rec["team_slug"]] = rec
    return out


def build_feed(now, teams, fq, runtime, records, info):
    """Assemble board.json. teams: registry Teams; fq: FairQueue; runtime: the board's live state."""
    by_slot = best_and_latest(records)
    job_failures = latest_job_failures(records)
    order = fq.queue_order(now)
    position = {slug: i for i, (slug, _) in enumerate(order, 1)}
    running = runtime.get("running")
    building = runtime.get("building", {})
    built = set(runtime.get("ready", []))
    oldest = 0.0

    out_teams = []
    for team in sorted(teams, key=lambda t: t.name.casefold()):
        st = fq.teams.get(team.slug)
        in_flight = queued = None
        if st and st.inflight:
            inf = st.inflight
            if running and running["slug"] == team.slug:
                state = "running"
            elif team.slug in built:
                state = "built, waiting for the runner"
            elif team.slug in building:
                state = "building"
            else:
                state = "dispatched"
            in_flight = {"state": state, "commit": inf["commit"], "pushed_at": iso(inf["pushed_at"]),
                         "dispatched_at": iso(inf["dispatched_at"]), "waiting_s": round(now - inf["pushed_at"], 1)}
            if state == "running":
                in_flight["track"] = running["track"]
            oldest = max(oldest, now - inf["pushed_at"])
        if st and st.pending:
            eligible = fq.eligible_at(st)
            queued = {"commit": st.pending["commit"], "pushed_at": iso(st.pending["pushed_at"]),
                      "position": position.get(team.slug), "eligible_at": iso(eligible),
                      "waiting_s": round(now - st.pending["pushed_at"], 1)}
            if st.inflight:
                queued["note"] = "waits for the job in flight"
            elif eligible and eligible > now:
                queued["note"] = f"waits out the {window(fq.min_interval_s)} between a team's runs"
            oldest = max(oldest, now - st.pending["pushed_at"])
        latency = None
        if in_flight:
            latency = in_flight["waiting_s"]
        elif queued:
            latency = queued["waiting_s"]

        tracks = {}
        names = set(team.tracks) | {t for (slug, t) in by_slot if slug == team.slug}
        for t in sorted(names):
            slot = by_slot.get((team.slug, t), {"best": None, "latest": None})
            tracks[t] = {
                "registered": t in team.tracks,
                "best": summarise(slot["best"]) if slot["best"] else None,
                "latest": summarise(slot["latest"], with_logs=True) if slot["latest"] else None,
                "queue_position": queued["position"] if queued else None,
                "queue_latency_s": latency,
            }
        entry = {
            "team": team.name,
            "slug": team.slug,
            "watch": {"ok": not (st and st.watch_error), "error": (st.watch_error if st else "") or None,
                      "head": (st.head if st else "") or None, "checked_at": iso(st.checked_at) if st and st.checked_at else None},
            "queue": {"in_flight": in_flight, "queued": queued},
            "tracks": tracks,
        }
        if team.slug in job_failures:
            entry["last_job_failure"] = summarise(job_failures[team.slug])
        out_teams.append(entry)

    recent = [r["queue_latency_s"] for r in records if r.get("queue_latency_s") is not None][-RECENT_RUNS:]
    return {
        "schema": SCHEMA,
        "event": info.get("event", "Speed Demon 2026"),
        "generated_at": iso(now),
        "dataset": info.get("dataset"),
        "reference_check": info.get("reference_check"),
        "rules": info.get("rules", {}),
        "queue": {
            "queued": len(order),
            "in_flight": sum(1 for st in fq.teams.values() if st.inflight),
            "running": ({"team": runtime["running"]["team"], "track": runtime["running"]["track"],
                         "since": iso(runtime["running"]["since"])} if running else None),
            "oldest_wait_s": round(oldest, 1),
            "recent_latency_s": round(statistics.median(recent), 1) if recent else None,
            "builds_paused": not runtime.get("disk_ok", True),
        },
        "teams": out_teams,
    }

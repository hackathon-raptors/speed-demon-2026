#!/usr/bin/env python3
"""Speed Demon 2026 show driver: run the show roster team by team and stream events for the show page.

    show_driver.py --roster roster.json --freeze freeze.json [--overrides freeze.overrides.json] \
        --calibration calibration.json --seed N --name prescored|events [--keep-images] [--upload]

Order: one reference run per track (the baseline beat), then run_order from slot N down to slot 1
(each team: every entered track, 5 timed runs, score against the reference on this seed), then one
closing reference run per track (drift %). Tracks the rulings zeroed are not run; they are reported
as zeroed. Events are appended to <out-dir>/events.<name>.json (or events.json for --name events) and
uploaded to the private bucket after every event when --upload is given.

Reuses final_scoring.py: run_contract (build + 5 runs through run_submission.py, middle-3 T),
make_run_team (pinned clone, checks, measured S/D, steps.score). Machine side only.
"""
import argparse
import json
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "board"))
import final_scoring as fs  # noqa: E402
import sources  # noqa: E402
import steps  # noqa: E402

BUCKET = "s3://raptors-speeddemon-board-history/2026/show"


def now(t=None):
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int(t % 1 * 1000):03d}Z"


class Stream:
    """Append-only event file. Uploads run on a background thread, coalesced to at most one per second,
    so the driver never waits on S3 (an upload per log line cost ~30 s per track)."""
    def __init__(self, path, key, upload):
        import threading
        self.path, self.key, self.upload = path, key, upload
        self.doc = {"generated_at": now(), "events": []}
        self.lock, self.dirty, self.done = threading.Lock(), threading.Event(), False
        self._write()
        if upload:
            self.thread = threading.Thread(target=self._uploader, daemon=True)
            self.thread.start()
            self.dirty.set()

    def emit(self, **ev):
        ts = ev.pop("ts", None)
        ev = {"type": ev.pop("type"), "at": now(ts), **ev}
        with self.lock:
            self.doc["events"].append(ev)
            self.doc["generated_at"] = now()
            self._write()
        print(json.dumps(ev), flush=True)
        self.dirty.set()

    def _write(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.doc, f)
        os.replace(tmp, self.path)

    def _push(self):
        snap = self.path + ".up"
        with self.lock:
            with open(snap, "w", encoding="utf-8") as f:
                json.dump(self.doc, f)
        subprocess.run(["aws", "s3", "cp", snap, f"{BUCKET}/{self.key}", "--content-type",
                        "application/json", "--only-show-errors"], check=False, timeout=60)

    def _uploader(self):
        while not self.done:
            if self.dirty.wait(1.0):
                self.dirty.clear()
                self._push()
                time.sleep(1.0)

    def close(self):
        if self.upload:
            self.done = True
            self._push()


class DockerWatch:
    """Tails `docker events` for container create/start/die, so the show's terminal shows the real lifecycle."""
    def __init__(self):
        import threading
        self.events, self.lock = [], threading.Lock()
        self.proc = subprocess.Popen(["docker", "events", "--filter", "type=container", "--filter", "event=create",
                                      "--filter", "event=start", "--filter", "event=die", "--format", "{{json .}}"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        for line in self.proc.stdout:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            a = (e.get("Actor") or {}).get("Attributes") or {}
            with self.lock:
                self.events.append({"t": e.get("timeNano", 0) / 1e9, "action": e.get("Action") or e.get("status"),
                                    "id": e.get("id") or (e.get("Actor") or {}).get("ID"), "name": a.get("name", ""),
                                    "image": a.get("image") or e.get("from", ""), "exit": a.get("exitCode")})

    def runs_for(self, image, since):
        """The last five containers of `image` since `since`: [(create, start, die)] in run order."""
        time.sleep(0.3)                               # let the last die event arrive
        with self.lock:
            evs = [e for e in self.events if e["image"] == image and e["t"] >= since]
        by = {}
        for e in evs:
            by.setdefault(e["id"], {})[e["action"]] = e
        runs = sorted(by.values(), key=lambda d: (d.get("create") or d.get("start") or {}).get("t", 0))
        return runs[-5:]


LAST = {}
_run_contract = fs.run_contract


def _capturing_run_contract(*a, **k):
    res = _run_contract(*a, **k)
    LAST["res"] = res
    return res


fs.run_contract = _capturing_run_contract


def mb(b):
    return f"{b / 1e6:.1f} MB" if b else "?"


def ms(s):
    return f"{s * 1000:.1f} ms" if s is not None and s < 10 else (f"{s:.2f} s" if s is not None else "?")


def overall(points):
    best = sorted((p for p in points.values() if p and p > 0), reverse=True)[:3]
    return round((best[0] * best[1] * best[2]) ** (1 / 3), 3) if len(best) == 3 else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--registry", default="/srv/speeddemon/final/registry.frozen.json")
    ap.add_argument("--freeze", default="/srv/speeddemon/final/freeze.json")
    ap.add_argument("--overrides", default="/srv/speeddemon/final/freeze.overrides.json")
    ap.add_argument("--calibration", required=True)
    ap.add_argument("--seed", type=int, required=True)
    ap.add_argument("--name", choices=["prescored", "events"], required=True)
    ap.add_argument("--out-dir", default="/srv/speeddemon/final/show")
    ap.add_argument("--upload", action="store_true")
    ap.add_argument("--keep-images", action="store_true", help="leave team images in place (warm cache for the live run)")
    ap.add_argument("--only", default="", help="comma-separated slugs (rehearsal subsets)")
    ap.add_argument("--github-app-id", default="5456832")
    ap.add_argument("--github-app-key", default="/etc/speeddemon/github-app.pem")
    ap.add_argument("--data-root", default="/srv/speeddemon/data")
    ap.add_argument("--work", default="/srv/speeddemon/work/show")
    ap.add_argument("--checkouts", default="/srv/speeddemon/final/show-checkouts")
    ap.add_argument("--timer", default="/srv/speeddemon/bin/sd-timer")
    ap.add_argument("--allowlist", default=os.path.join(HERE, "allowlist.json"))
    ap.add_argument("--run-cpuset", default="0-11")
    ap.add_argument("--tools-cpuset", default="12-19")
    ap.add_argument("--timeout-factor", type=float, default=10.0)
    a = ap.parse_args()

    os.makedirs(a.out_dir, exist_ok=True)
    roster = json.load(open(a.roster))
    freeze = json.load(open(a.freeze))["teams"]
    if a.overrides and os.path.exists(a.overrides):
        for slug, o in json.load(open(a.overrides)).items():
            freeze.setdefault(slug, {})["commit"] = o["commit"]
    teams = {t.slug: t for t in sources.load_registry(a.registry)[0]}
    cal = json.load(open(a.calibration)).get("tracks", {})
    ref_axes = json.load(open(os.path.join(HERE, "reference_axes.json")))
    if a.keep_images:
        steps.remove_image = lambda tag: None

    cfg = {"timer": a.timer, "cgroups": "host", "run_cpuset": a.run_cpuset, "tools_cpuset": a.tools_cpuset,
           "work": a.work, "checkouts": a.checkouts, "allowlist": a.allowlist, "timeout_factor": a.timeout_factor,
           "ref_runs": 1, "team_runs": 5, "freeze": freeze}
    git = fs.make_git(argparse.Namespace(github_app_id=a.github_app_id, github_app_key=a.github_app_key))
    allowlist = steps.load_allowlist(a.allowlist)
    fs.ensure_tools_image(sorted(cal))

    per_track = {}
    for tr, c in cal.items():
        expected = [h for h in c["output_sha256"] if h][0]
        data = os.path.join(a.data_root, f"track-{tr}", f"full-seed{a.seed}")
        ref_block = fs.reference_ref_block(c, ref_axes[tr]["S_lines"])
        per_track[tr] = {
            "ref": fs.make_measure_ref(tr, data, c["output"], expected, round(fs.REF_TIMEOUT_FACTOR * c["T_ref_s"], 3), cfg),
            "team": fs.make_run_team(tr, data, c["output"], expected, ref_block,
                                     round(a.timeout_factor * c["T_ref_s"], 3), cfg, git, allowlist),
            "T_ref": c["T_ref_s"], "expected": expected}

    watch = DockerWatch()
    key = "events.json" if a.name == "events" else "events.prescored.json"
    out = Stream(os.path.join(a.out_dir, key), key, a.upload)
    tracks = sorted(per_track)

    opening = {}
    for tr in tracks:
        t0 = time.time()
        try:
            opening[tr] = per_track[tr]["ref"]()
        except Exception as exc:
            print(f"reference {tr} failed: {exc}", file=sys.stderr)
            opening[tr] = None
        out.emit(type="reference", track=tr, wall_s=round(opening[tr] or (time.time() - t0), 3),
                 T_ref_s=round(per_track[tr]["T_ref"], 3))

    only = {s for s in a.only.split(",") if s}
    order = sorted(roster["run_order"], key=lambda r: -r["slot"])
    for entry in order:
        slug = entry["slug"]
        if only and slug not in only:
            continue
        team = teams.get(slug)
        out.emit(type="team_start", slug=slug)
        fz = freeze.get(slug, {})
        repo = (team.repo if team else fz.get("repo", "")).replace("https://github.com/", "").removesuffix(".git")
        out.emit(type="log", slug=slug, track=None, i=None, stage="clone",
                 text=f"clone {repo} @ {(fz.get('commit') or '')[:7]}  (pinned at freeze)"[:110])
        points = {}
        for tr in entry["tracks"]:
            if tr in entry.get("zeroed", []):
                points[tr] = 0.0
                out.emit(type="log", slug=slug, track=tr, i=None, stage="verify",
                         text=f"track {tr.upper()}  skipped: built on another team's code, scored 0")
                out.emit(type="track_done", slug=slug, track=tr, ok=False, T_s=None, M_bytes=None, S=None, D=None,
                         K=None, points=0.0, failure="copied code", zeroed=True)
                continue
            if team is None or tr not in per_track:
                points[tr] = 0.0
                out.emit(type="track_done", slug=slug, track=tr, ok=False, T_s=None, M_bytes=None, S=None, D=None,
                         K=None, points=0.0, failure="not in the frozen registry", zeroed=False)
                continue
            t_start = time.time()
            LAST.clear()
            r = per_track[tr]["team"](team)
            res = LAST.get("res") or {}
            b = res.get("build") or {}
            tag = f"sdfinal/{slug}-{tr}:{(fz.get('commit') or '')[:12]}"
            if b.get("ok"):
                out.emit(type="log", slug=slug, track=tr, i=None, stage="build",
                         text=f"docker build track-{tr}  {mb(b.get('image_bytes'))}  {b.get('build_s', 0):.1f} s" + ("  cached" if b.get('build_s', 9) < 3 else ""))
            elif b:
                out.emit(type="log", slug=slug, track=tr, i=None, stage="build",
                         text=f"docker build track-{tr}  FAILED  ({b.get('stage', 'build')})")
            conts = watch.runs_for(tag, t_start)
            walls = r.get("walls_s") or []
            for i, w in enumerate(walls, 1):
                c = conts[i - 1] if i - 1 < len(conts) else {}
                nm = (c.get("create") or c.get("start") or {}).get("name", "")[:20]
                if c.get("create"):
                    out.emit(type="log", slug=slug, track=tr, i=i, stage="docker", ts=c["create"]["t"],
                             text=f"create {nm}  cpus {a.run_cpuset}  8g  pids 512  net none  ro")
                if c.get("start"):
                    out.emit(type="log", slug=slug, track=tr, i=i, stage="docker", ts=c["start"]["t"], text=f"start  {nm}")
                st0 = (c.get("start") or {}).get("t")
                out.emit(type="log", slug=slug, track=tr, i=i, stage="run", ts=(st0 + (w or 0)) if st0 else None,
                         text=f"run {i}/5  wall {ms(w)}")
                if c.get("die"):
                    d = c["die"]; st = (c.get("start") or d)
                    out.emit(type="log", slug=slug, track=tr, i=i, stage="docker", ts=d["t"],
                             text=f"die    {nm}  exit {d.get('exit')}  {(d['t'] - st['t']) * 1000:.0f} ms")
            for i, w in enumerate(walls, 1):
                out.emit(type="run", slug=slug, track=tr, i=i, wall_s=round(w, 4) if w else None, ok=bool(w))
            exp = (per_track[tr].get("expected") or "")[:6]
            if r.get("ok"):
                out.emit(type="log", slug=slug, track=tr, i=None, stage="verify",
                         text=f"memory.peak {mb(r.get('M_bytes'))}   sha256 {exp}… == reference ✓")
                out.emit(type="log", slug=slug, track=tr, i=None, stage="score",
                         text=f"T {ms(r.get('T_s'))}  M {mb(r.get('M_bytes'))}  S {r.get('S')}  D {r.get('D')}  K {r.get('K'):.2f}  ->  {r.get('points'):.2f} pts")
            else:
                out.emit(type="log", slug=slug, track=tr, i=None, stage="verify",
                         text=f"track {tr.upper()}  FAILED at {r.get('stage')}: {str(r.get('failure') or '')[:70]}")
            points[tr] = round(r["points"] or 0.0, 3) if r.get("ok") else 0.0
            out.emit(type="track_done", slug=slug, track=tr, ok=bool(r.get("ok")), T_s=r.get("T_s"),
                     M_bytes=r.get("M_bytes"), S=r.get("S"), D=r.get("D"), K=r.get("K"), points=points[tr],
                     failure=(r.get("failure") or None) and str(r["failure"])[:160], zeroed=False)
        out.emit(type="team_done", slug=slug, overall=overall(points), tracks=points)

    drift = {}
    for tr in tracks:
        try:
            after = per_track[tr]["ref"]()
            drift[tr] = round((after - opening[tr]) / opening[tr] * 100, 2) if opening[tr] else None
        except Exception as exc:
            print(f"closing reference {tr} failed: {exc}", file=sys.stderr)
            drift[tr] = None
    out.emit(type="drift", tracks=drift)
    out.close()


if __name__ == "__main__":
    main()

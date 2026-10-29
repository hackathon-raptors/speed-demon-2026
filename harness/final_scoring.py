#!/usr/bin/env python3
"""Speed Demon 2026 final scoring: score every team's FROZEN commit on a private holdout seed.

    final_scoring.py snapshot --registry R --out freeze.json
        At code freeze: pin each team's speed-demon tip so a later push cannot change what is scored.

    final_scoring.py score --registry R --freeze freeze.json --holdout-seed N --out final_scores.json
        After freeze, board stopped, box to itself: re-calibrate the reference on the holdout (the K
        anchor AND the output-hash oracle), then score each frozen commit on the holdout in batches.

The site's binding promise (speeddemon.dev harness page): the reference runs before and after every
scoring batch; a batch whose reference drifts more than 2% is thrown out and rescored. Here a drifted
batch is auto-rescored up to twice, then the run halts and flags for the operator (check the box's
clocks) rather than emit numbers from a drifting box. K is anchored to the reference re-measured on the
holdout. This is the MACHINE side only; judge scores (0.35) and blended totals combine after judging.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(REPO, "board"))

import axes  # noqa: E402      (board/)
import github_app  # noqa: E402
import sources  # noqa: E402
import steps  # noqa: E402

BRANCH = "speed-demon"
REF_CONTEXT = os.path.join(REPO, "reference")
REF_TAG = "speeddemon/reference:2026"
REF_TIMEOUT_FACTOR = 10.0          # headroom for the reference (it sits at ~1x); teams get --timeout-factor


# -- pure helpers (unit-tested without Docker) --------------------------------

def partition(items, size):
    return [items[i:i + size] for i in range(0, len(items), size)]


def drift_fraction(ref_before, ref_after):
    return abs(ref_after - ref_before) / ref_before


def reference_ref_block(cal_track, s_lines):
    """The scoring 'ref' for a track: T and M from the holdout calibration, S from reference_axes, D=1."""
    return {"T_s": cal_track["T_ref_s"], "M_bytes": cal_track["M_ref_bytes"], "S": s_lines, "D": 1}


def score_track(track, teams, ref0, run_team, measure_ref, *, batch_size, threshold, rescore_limit):
    """Score one track's teams in batches, bracketing each batch with a reference measurement.

    run_team(team) -> result dict (the machine side for that team-track).
    measure_ref()  -> the reference's T on this track now (float); raises if the reference will not run.
    ref0           -> the holdout-calibration reference T (batch 1's 'before').

    Returns (results{slug: result}, drift_log[list], halt|None). On a batch that still drifts after
    rescore_limit retries, returns the accepted results so far plus a halt marker; the caller exits non-zero.
    """
    results, drift_log = {}, []
    ref_prev = ref0
    for i, batch in enumerate(partition(teams, batch_size)):
        for attempt in range(1 + rescore_limit):
            try:
                ref_before = ref_prev if attempt == 0 else measure_ref()   # a rescore takes a fresh 'before'
                batch_results = {t.slug: run_team(t) for t in batch}
                ref_after = measure_ref()
            except RuntimeError as exc:                                     # the reference itself will not run: box emergency
                return results, drift_log, {"halted": True, "track": track, "batch": i, "reason": str(exc)}
            d = drift_fraction(ref_before, ref_after)
            ok = d <= threshold
            drift_log.append({"track": track, "batch": i, "attempt": attempt, "teams": [t.slug for t in batch],
                              "ref_before_s": round(ref_before, 6), "ref_after_s": round(ref_after, 6),
                              "drift": round(d, 5), "drift_pct": round(d * 100, 2), "ok": ok})
            if ok:
                for slug, r in batch_results.items():
                    r["batch"] = i
                    r["drift_ok"] = True
                results.update(batch_results)
                ref_prev = ref_after                                    # shared boundary: after(i) = before(i+1)
                break
        else:
            return results, drift_log, {"halted": True, "track": track, "batch": i,
                                        "last_drift_pct": round(d * 100, 2),
                                        "reason": f"track {track} batch {i} still drifted {d * 100:.2f}% "
                                                  f"after {rescore_limit} rescore(s); check the box clocks"}
    return results, drift_log, None


# -- real runners -------------------------------------------------------------

def run_contract(context, track, data, output, tag, timeout_s, work, results_path, cfg, expected_sha256="", runs=5):
    """One build + `runs` timed runs through the published contract. Returns the parsed results dict."""
    cmd = [sys.executable, os.path.join(REPO, "harness", "run_submission.py"), "--context", context,
           "--track", track, "--data", data, "--output", output, "--timeout-s", str(timeout_s),
           "--timer", cfg["timer"], "--work", work, "--tag", tag, "--runs", str(runs),
           "--cgroups", cfg["cgroups"], "--cpuset", cfg["run_cpuset"], "--allowlist", cfg["allowlist"],
           "--results", results_path]
    if expected_sha256:
        cmd += ["--expected-sha256", expected_sha256]
    os.makedirs(work, exist_ok=True)
    proc = subprocess.run(cmd, capture_output=True, text=True)
    steps.cleanup_work(work)
    try:
        with open(results_path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"build": {"ok": False, "stage": "harness", "problems": [proc.stderr[-2000:]]}, "gates": {}}


def make_measure_ref(track, data, output, expected_sha256, ref_cap_s, cfg):
    """A measure_ref() for score_track: run the reference `ref_runs` times, return its T; raise if it fails."""
    def measure():
        work = os.path.join(cfg["work"], "reference")
        res = run_contract(REF_CONTEXT, track, data, output, REF_TAG, ref_cap_s, work,
                             os.path.join(work, f"ref-{track}.json"), cfg,
                             expected_sha256=expected_sha256, runs=cfg["ref_runs"])
        t = res.get("T_s")
        if t is None or not res.get("gates") or not all(res["gates"].values()):
            raise RuntimeError(f"reference failed to produce a clean timed run on track {track}: gates {res.get('gates')}")
        return t
    return measure


def make_run_team(track, data, output, expected_sha256, ref_block, team_cap_s, cfg, git, allowlist):
    """A run_team(team) for score_track: clone the frozen commit, build+run, measure S/D, score."""
    def run(team):
        commit = cfg["freeze"][team.slug]["commit"]
        base = {"slug": team.slug, "name": team.name, "track": track, "commit": commit,
                "T_s": None, "M_bytes": None, "S": None, "D": None, "K": None, "points": None,
                "walls_s": None, "gates": None, "ok": False, "failure": None}
        checkout = os.path.join(cfg["checkouts"], f"{team.slug}-{track}")
        tag = None
        shutil.rmtree(checkout, ignore_errors=True)
        os.makedirs(cfg["checkouts"], exist_ok=True)
        try:
            landed, err = git.clone_commit(team.repo, commit, checkout, branch=BRANCH)
            if err:
                return {**base, "stage": "clone", "failure": err}
            track_dir = os.path.join(checkout, f"track-{track}")
            if not os.path.isdir(track_dir):
                return {**base, "stage": "absent", "failure": f"no track-{track} directory at {commit[:12]}"}
            reason = steps.check_track(checkout, track, allowlist)
            if reason:
                return {**base, "stage": "check", "failure": reason}
            if steps.unchanged_reference(track_dir):
                return {**base, "stage": "unchanged", "points": 0.0,
                        "failure": "unchanged starter (the published reference); scores ~0"}
            tag = f"sdfinal/{team.slug}-{track}:{commit[:12]}"
            work = os.path.join(cfg["work"], "team")
            res = run_contract(track_dir, track, data, output, tag, team_cap_s, work,
                                 os.path.join(work, f"{team.slug}-{track}.json"), cfg,
                                 expected_sha256=expected_sha256, runs=cfg["team_runs"])
            build = res.get("build") or {}
            if not build.get("ok"):
                return {**base, "stage": build.get("stage", "build"),
                        "failure": "; ".join(build.get("problems") or ["build failed"])[-500:]}
            gates = res.get("gates") or {}
            base["gates"] = gates
            final = (res.get("batches") or [[]])[-1]
            base["walls_s"] = [r.get("wall_s") for r in final]
            if not all(gates.values()) or res.get("T_s") is None:
                return {**base, "stage": "run", "failure": steps.failure_of(final[-1] if final else {}, gates,
                                                                            team_cap_s, cfg["timeout_factor"])}
            # S must be MEASURED for final scoring; a stub silently sets the S factor to 1.0 for the team.
            ax = axes.static_axes(checkout, track, {"S": ref_block["S"], "D": ref_block["D"]}, cpuset=cfg["tools_cpuset"])
            if ax["source"] != "measured":
                return {**base, "T_s": res["T_s"], "M_bytes": res["M_bytes"], "stage": "axes",
                        "failure": f"source size could not be measured ({ax['detail']}); flagged for manual review"}
            k, points = steps.score(res["T_s"], res["M_bytes"], ax["S"], ax["D"], ref_block)
            return {**base, "stage": "scored", "ok": True, "T_s": res["T_s"], "M_bytes": res["M_bytes"],
                    "S": ax["S"], "D": ax["D"], "K": round(k, 6), "points": round(points, 6)}
        except Exception as exc:                      # a broken team never crashes the whole run
            return {**base, "stage": "harness", "failure": repr(exc)[:500]}
        finally:
            if tag:
                steps.remove_image(tag)
            shutil.rmtree(checkout, ignore_errors=True)
    return run


# -- commands -----------------------------------------------------------------

def make_git(args):
    token = os.environ.pop("BOT_TOKEN", "")
    provider = None
    if args.github_app_id:
        provider = github_app.GitHubApp(args.github_app_id, args.github_app_key).token_for
    return sources.Git(token, "x-access-token", token_provider=provider)


def snapshot_cmd(args):
    teams, problems = sources.load_registry(args.registry, args.allow_local)
    for p in problems:
        print(f"registry: {p}", file=sys.stderr)
    git = make_git(args)
    frozen = {}
    for team in teams:
        sha, err = git.ls_remote(team.repo, BRANCH)
        frozen[team.slug] = {"name": team.name, "repo": team.repo, "tracks": team.tracks,
                             "commit": sha, "error": err or None}
        print(f"{'  ok' if sha else 'MISS'}  {team.name}: {sha[:12] or err}")
    out = {"captured_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "branch": BRANCH, "teams": frozen}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    missing = [s for s, v in frozen.items() if not v["commit"]]
    print(f"\nwrote {args.out}: {len(frozen)} teams, {len(missing)} with no {BRANCH} tip")
    return 1 if missing else 0


def holdout_calibration(args, tracks):
    """Reuse calibrate.py to generate the holdout data and measure the reference (the anchor + hash oracle)."""
    if args.calibration and os.path.exists(args.calibration):
        print(f"reusing holdout calibration {args.calibration}")
    else:
        cal_out = args.calibration or os.path.join(os.path.dirname(os.path.abspath(args.out)), "holdout_calibration.json")
        print(f"calibrating the reference on holdout seed {args.holdout_seed} (tracks {''.join(tracks)})...")
        subprocess.run([sys.executable, os.path.join(REPO, "harness", "calibrate.py"), "--seed", str(args.holdout_seed),
                        "--tracks", "".join(tracks), "--data-root", args.data_root, "--work", args.work,
                        "--timer", args.timer, "--out", cal_out], check=True)
        steps.cleanup_work(args.work)                   # calibrate leaves run-* scratch under --work
        args.calibration = cal_out
    with open(args.calibration, encoding="utf-8") as f:
        return json.load(f).get("tracks", {})


def score_cmd(args):
    teams, problems = sources.load_registry(args.registry, args.allow_local)
    for p in problems:
        print(f"registry: {p}", file=sys.stderr)
    with open(args.freeze, encoding="utf-8") as f:
        freeze = json.load(f).get("teams", {})
    teams = [t for t in teams if freeze.get(t.slug, {}).get("commit")]
    if not teams:
        raise SystemExit("no teams with a frozen commit in the freeze snapshot")
    tracks = sorted({t for team in teams for t in team.tracks})
    if args.tracks:
        tracks = [t for t in tracks if t in args.tracks]

    cal = holdout_calibration(args, tracks)
    ref_axes = json.load(open(os.path.join(HERE, "reference_axes.json"), encoding="utf-8"))
    ok_tracks = [t for t in tracks if cal.get(t) and all((cal[t].get("gates") or {"x": False}).values())
                 and len([h for h in (cal[t].get("output_sha256") or []) if h]) == 1]
    for t in tracks:
        if t not in ok_tracks:
            print(f"track {t}: holdout calibration unusable (gate/hash); skipped", file=sys.stderr)

    cfg = {"timer": args.timer, "cgroups": args.cgroups, "run_cpuset": args.run_cpuset,
           "tools_cpuset": args.tools_cpuset, "work": args.work, "checkouts": args.checkouts,
           "allowlist": args.allowlist, "timeout_factor": args.timeout_factor,
           "ref_runs": args.ref_runs, "team_runs": args.team_runs,
           "freeze": {s: v for s, v in freeze.items()}}
    git = make_git(args)
    allowlist = steps.load_allowlist(args.allowlist)
    ensure_tools_image(ok_tracks)

    out = {"holdout_seed": args.holdout_seed, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "batch_size": args.batch_size, "drift_threshold": args.drift_threshold, "rescore_limit": args.rescore_limit,
           "holdout_reference": {}, "tracks": {}, "teams": {}, "halted": None}
    halted = None
    for track in ok_tracks:
        c = cal[track]
        s_lines = ref_axes[track]["S_lines"]
        ref_block = reference_ref_block(c, s_lines)
        output = c["output"]
        data = os.path.join(args.data_root, f"track-{track}", f"full-seed{args.holdout_seed}")
        expected = [h for h in c["output_sha256"] if h][0]
        out["holdout_reference"][track] = {"T_ref_s": c["T_ref_s"], "M_ref_bytes": c["M_ref_bytes"],
                                           "S_ref": s_lines, "D_ref": 1, "output": output, "expected_sha256": expected}
        team_cap = round(args.timeout_factor * c["T_ref_s"], 3)
        ref_cap = round(REF_TIMEOUT_FACTOR * c["T_ref_s"], 3)
        track_teams = sorted([t for t in teams if track in t.tracks], key=lambda t: t.slug)
        print(f"\n=== track {track}: {len(track_teams)} teams, anchor T_ref {c['T_ref_s']:.3f} s, "
              f"batches of {args.batch_size} ===")
        measure_ref = make_measure_ref(track, data, output, expected, ref_cap, cfg)
        run_team = make_run_team(track, data, output, expected, ref_block, team_cap, cfg, git, allowlist)
        results, drift_log, halt = score_track(track, track_teams, c["T_ref_s"], run_team, measure_ref,
                                               batch_size=args.batch_size, threshold=args.drift_threshold,
                                               rescore_limit=args.rescore_limit)
        out["tracks"][track] = {"anchor_T_ref_s": c["T_ref_s"], "drift_log": drift_log, "halted": bool(halt)}
        for slug, r in results.items():
            out["teams"].setdefault(slug, {"name": r["name"], "tracks": {}})
            out["teams"][slug]["tracks"][track] = {k: r[k] for k in
                ("commit", "stage", "ok", "gates", "T_s", "M_bytes", "S", "D", "K", "points", "walls_s", "batch",
                 "drift_ok", "failure") if k in r}
        if halt:
            halted = halt
            print(f"HALTED: {halt['reason']}", file=sys.stderr)
            break
    out["halted"] = halted
    out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
    scored = sum(1 for s in out["teams"].values() for tr in s["tracks"].values() if tr.get("ok"))
    print(f"\nwrote {args.out}: {len(out['teams'])} teams, {scored} scored team-tracks, "
          f"tracks {''.join(out['tracks'])}{' (HALTED)' if halted else ''}")
    return 2 if halted else 0


def ensure_tools_image(tracks):
    """Fail loud if S cannot be measured: build the static-tools image used by axes.static_axes."""
    import static_axes
    if not tracks:
        raise SystemExit("no usable tracks to score")
    probe = subprocess.run(["docker", "image", "inspect", static_axes.TOOLS_IMAGE], capture_output=True, text=True)
    if probe.returncode != 0:
        print(f"building the static-tools image {static_axes.TOOLS_IMAGE} (needed to measure source size S)...")
        build = subprocess.run(["docker", "build", "-t", static_axes.TOOLS_IMAGE, os.path.join(REPO, "harness", "tools")],
                               capture_output=True, text=True)
        if build.returncode != 0:
            raise SystemExit(f"could not build the static-tools image; S cannot be measured:\n{build.stderr[-2000:]}")


def main(argv=None):
    ap = argparse.ArgumentParser(description="Speed Demon 2026 final scoring")
    sub = ap.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--registry", required=True)
    common.add_argument("--allow-local", action="store_true", help="tests only: let the registry name local repos")
    common.add_argument("--github-app-id", default=os.environ.get("SD_GITHUB_APP_ID", ""))
    common.add_argument("--github-app-key", default="/etc/speeddemon/github-app.pem")

    snap = sub.add_parser("snapshot", parents=[common], help="pin each team's frozen commit at code freeze")
    snap.add_argument("--out", required=True)

    sc = sub.add_parser("score", parents=[common], help="score the frozen commits on the holdout")
    sc.add_argument("--freeze", required=True)
    sc.add_argument("--holdout-seed", type=int, required=True)
    sc.add_argument("--out", required=True)
    sc.add_argument("--tracks", default="", help="limit to these track letters, e.g. 'a' (default: every track present)")
    sc.add_argument("--calibration", default="", help="reuse an existing holdout_calibration.json instead of re-running")
    sc.add_argument("--data-root", default="/srv/speeddemon/data")
    sc.add_argument("--work", default="/srv/speeddemon/work/final")
    sc.add_argument("--checkouts", default="/srv/speeddemon/final/checkouts")
    sc.add_argument("--timer", default="/srv/speeddemon/bin/sd-timer")
    sc.add_argument("--allowlist", default=os.path.join(HERE, "allowlist.json"))
    sc.add_argument("--cgroups", default="host", choices=["host", "colima"])
    sc.add_argument("--run-cpuset", default="0-11")
    sc.add_argument("--tools-cpuset", default="12-19")
    sc.add_argument("--batch-size", type=int, default=10)
    sc.add_argument("--timeout-factor", type=float, default=10.0, help="team run cap = this x T_ref (the published final-scoring gate, 10x; the board uses 3x)")
    sc.add_argument("--drift-threshold", type=float, default=0.02)
    sc.add_argument("--ref-runs", type=int, default=5)
    sc.add_argument("--team-runs", type=int, default=5)
    sc.add_argument("--rescore-limit", type=int, default=2)
    args = ap.parse_args(argv)
    if args.command == "snapshot":
        return snapshot_cmd(args)
    return score_cmd(args)


if __name__ == "__main__":
    sys.exit(main())

"""Measure the reference implementations on the scoring box.

  python3 harness/calibrate.py --seed 1 --out /srv/speeddemon/calibration.json

For each track: generate the full dataset, build the reference image, run it
through run_submission.py exactly like a submission (five runs, same limits),
and record T_ref, M_ref, the input and output hashes, and the timeouts derived
from T_ref (10x for scoring, 3x for the live board).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKS = {
    "a": ("a_ingest", "summary.txt"),
    "b": ("b_index", None),
    "c": ("c_join", "aggregates.txt"),
    "d": ("d_traverse", "distances.txt"),
}


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def output_name(module):
    """Ask the reference itself which file it writes, by running it on tiny data."""
    tiny = f"/tmp/sd-calibrate-tiny-{module}"
    out = tiny + "-out"
    subprocess.run([sys.executable, f"{REPO}/generators/{module}.py", "--seed", "1", "--scale", "tiny", "--out", tiny],
                   check=True, capture_output=True)
    os.makedirs(out, exist_ok=True)
    subprocess.run([sys.executable, f"{REPO}/reference/{module}.py", "--data", tiny, "--out", out],
                   check=True, capture_output=True)
    (name,) = os.listdir(out)
    return name


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--tracks", default="abcd")
    ap.add_argument("--data-root", default="/srv/speeddemon/data")
    ap.add_argument("--work", default="/srv/speeddemon/work")
    ap.add_argument("--timer", default="/srv/speeddemon/bin/sd-timer")
    ap.add_argument("--timeout-s", type=float, default=1800)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    results = {"seed": args.seed, "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "tracks": {}}
    for track in args.tracks:
        module, out_name = TRACKS[track]
        out_name = out_name or output_name(module)
        data = f"{args.data_root}/track-{track}/full-seed{args.seed}"
        t0 = time.monotonic()
        subprocess.run([sys.executable, f"{REPO}/generators/{module}.py", "--seed", str(args.seed),
                        "--scale", "full", "--out", data], check=True)
        gen_s = time.monotonic() - t0
        inputs = {name: sha256_file(os.path.join(data, name)) for name in sorted(os.listdir(data))}
        result_path = f"{args.work}/calibrate-{track}.json"
        subprocess.run([sys.executable, f"{REPO}/harness/run_submission.py", "--context", f"{REPO}/reference",
                        "--track", track, "--data", data, "--output", out_name, "--timeout-s", str(args.timeout_s),
                        "--timer", args.timer, "--work", args.work, "--tag", "speeddemon/reference:2026",
                        "--results", result_path], check=True)
        with open(result_path, encoding="utf-8") as f:
            run = json.load(f)
        final = run["batches"][-1]
        t_ref = run.get("T_s")
        results["tracks"][track] = {
            "generate_s": round(gen_s, 1),
            "inputs_sha256": inputs,
            "output": out_name,
            "output_sha256": sorted({r.get("sha256") for r in final}),
            "gates": run["gates"],
            "T_ref_s": t_ref,
            "M_ref_bytes": run.get("M_bytes"),
            "walls_s": [r.get("wall_s") for r in final],
            "outside_s": [r.get("outside_s") for r in final],
            "spread_ok": run.get("spread_ok"),
            "timeout_s": round(10 * t_ref, 1) if t_ref else None,
            "board_cap_s": round(3 * t_ref, 1) if t_ref else None,
        }
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2)
        print(f"track {track}: T_ref {t_ref} s, M_ref {run.get('M_bytes')} B, gates {run['gates']}", flush=True)
    results["finished"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)


if __name__ == "__main__":
    main()

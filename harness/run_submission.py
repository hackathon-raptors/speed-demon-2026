"""Build one submission for one track and run it under the published contract.

  python3 run_submission.py --context <dir with Dockerfile> --track a \
      --data /srv/speeddemon/data/track-a --expected-sha256 <hex> \
      --work /srv/speeddemon/work --results result.json

Wall time comes from sd-timer inside the container (exec to exit), memory
from the run's own parent cgroup, read after the container is gone. The
harness host must use Docker's cgroupfs driver on cgroup v2.
"""
import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dockerfile_check  # noqa: E402

BUILD_TIMEOUT_S = 600
IMAGE_MAX_BYTES = 2 * 1024**3
NOBODY = "65534"
CGROUP_PARENT = "speeddemon"
MOUNT_POINTS = {"/data", "/out", "/sd", "/sd-bin", "/tmp"}
STDERR_KEEP_BYTES = 64 * 1024
OUTPUT_MAX_BYTES = 2 * 1024**3
TIMING_MAX_BYTES = 4096


class ImageRefused(Exception):
    """The image itself breaks the contract (not a build failure)."""


def sh(args, timeout=None, check=True):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


class CgroupReader:
    """Reads a parent cgroup's memory.peak on the host, or inside a Colima VM for local tests."""

    def __init__(self, mode):
        self.mode = mode

    def _cmd(self, script):
        if self.mode == "colima":
            return ["colima", "ssh", "--", "sudo", "sh", "-c", script]
        return ["sh", "-c", script]

    def peak_and_remove(self, run_id):
        path = f"/sys/fs/cgroup/{CGROUP_PARENT}/{run_id}"
        out = sh(self._cmd(f"cat {path}/memory.peak && rmdir {path}")).stdout.strip()
        return int(out.splitlines()[0])


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def open_regular(path, max_bytes):
    """Open a file the submission wrote, refusing anything but a regular file of bounded size.

    O_NOFOLLOW + O_NONBLOCK: a symlink or a FIFO must never be followed or block the host."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise ValueError("not a regular file")
    if st.st_size > max_bytes:
        os.close(fd)
        raise ValueError(f"{st.st_size} bytes, over the {max_bytes}-byte cap")
    return os.fdopen(fd, "rb")


def sha256_output(path):
    h = hashlib.sha256()
    with open_regular(path, OUTPUT_MAX_BYTES) as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class TailReader(threading.Thread):
    """Drains a pipe, keeping only the last `keep` bytes, so a flood cannot grow the caller."""

    def __init__(self, pipe, keep=STDERR_KEEP_BYTES):
        super().__init__(daemon=True)
        self.pipe, self.keep, self.buf = pipe, keep, bytearray()

    def run(self):
        for chunk in iter(lambda: self.pipe.read(1 << 16), b""):
            self.buf += chunk
            if len(self.buf) > 2 * self.keep:
                del self.buf[:-self.keep]

    def text(self):
        return bytes(self.buf[-self.keep:]).decode("utf-8", errors="replace")


def build(context, tag, allowlist_path):
    with open(os.path.join(context, "Dockerfile"), encoding="utf-8") as f:
        dockerfile = f.read()
    with open(allowlist_path, encoding="utf-8") as f:
        allowlist = json.load(f)["images"]
    problems = dockerfile_check.check(dockerfile, allowlist)
    if problems:
        return {"ok": False, "stage": "allowlist", "problems": problems}
    started = time.monotonic()
    try:
        proc = sh(["docker", "build", "--network=none", "-t", tag, context], timeout=BUILD_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "stage": "build", "problems": [f"build exceeded {BUILD_TIMEOUT_S} s"]}
    if proc.returncode != 0:
        return {"ok": False, "stage": "build", "problems": [proc.stderr[-4000:]]}
    size = int(sh(["docker", "image", "inspect", "-f", "{{.Size}}", tag]).stdout.strip())
    if size > IMAGE_MAX_BYTES:
        return {"ok": False, "stage": "image-size", "problems": [f"image is {size} bytes, cap is {IMAGE_MAX_BYTES}"]}
    return {"ok": True, "build_s": round(time.monotonic() - started, 3), "image_bytes": size}


def image_command(tag):
    cfg = json.loads(sh(["docker", "image", "inspect", "-f", "{{json .Config}}", tag]).stdout)
    if cfg.get("Volumes"):
        # --read-only does not cover a VOLUME: it would be a writable, uncapped anonymous volume.
        raise ImageRefused(f"the image declares VOLUME {sorted(cfg['Volumes'])}; remove it, /out and /tmp are the writable places")
    cmd = (cfg.get("Entrypoint") or []) + (cfg.get("Cmd") or [])
    if not cmd:
        raise SystemExit(f"{tag} has no ENTRYPOINT or CMD")
    return cmd


def prewarm(data_dir):
    # /data must be resident before the run cgroup exists, or the submission faults it
    # in itself and the pages count toward its memory.peak (M). vmtouch failing is not a
    # reason to skip warming: fall through to the host-side read loop, which holds the
    # same guarantee. An unreadable file there raises, which is the intended loud failure.
    if shutil.which("vmtouch"):
        p = sh(["vmtouch", "-qt", data_dir], check=False)
        if p.returncode == 0:
            return
        print(f"prewarm: vmtouch failed rc={p.returncode}; falling back to read-loop", file=sys.stderr)
    for root, _, files in os.walk(data_dir):
        for name in files:
            with open(os.path.join(root, name), "rb") as f:
                while f.read(1 << 22):
                    pass


def run_once(tag, track, cmd, args, timer_bin, cgroups, timeout_s):
    run_id = f"run-{uuid.uuid4().hex[:12]}"
    out_dir = os.path.join(args.work, run_id, "out")
    sd_dir = os.path.join(args.work, run_id, "sd")
    os.makedirs(out_dir)
    os.makedirs(sd_dir)
    os.chmod(out_dir, 0o777)
    os.chmod(sd_dir, 0o700)
    name = f"sd-{run_id}"
    docker_run = [
        "docker", "run", "--name", name,
        # Nothing from the image may start a second process or pick the timer's user: the timer is
        # the only root process in the container, and only it may write /sd.
        "--no-healthcheck", "--user=0:0",
        f"--cgroup-parent=/{CGROUP_PARENT}/{run_id}",
        f"--cpuset-cpus={args.cpuset}", "--cpuset-mems=0",
        "--memory=8g", "--memory-swap=8g", "--pids-limit=512",
        "--network=none", "--read-only", "--tmpfs", "/tmp:rw,size=1g,exec",
        # sd-timer needs only SETUID/SETGID to drop the submission to uid 65534; nothing else.
        "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID", "--security-opt=no-new-privileges",
        "--ulimit", "nofile=8192:8192",
        "-v", f"{os.path.abspath(args.data)}:/data:ro",
        "-v", f"{os.path.abspath(out_dir)}:/out:rw",
        "-v", f"{os.path.abspath(sd_dir)}:/sd",
        "-v", f"{os.path.dirname(os.path.abspath(timer_bin))}:/sd-bin:ro",
        "-e", f"TRACK={track}",
        "--entrypoint", f"/sd-bin/{os.path.basename(timer_bin)}",
        tag, "/sd/timing.json", NOBODY, NOBODY, "--", *cmd,
    ]
    started = time.monotonic()
    proc = subprocess.Popen(docker_run, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    tail = TailReader(proc.stderr)
    tail.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout_s + 30)
    except subprocess.TimeoutExpired:
        timed_out = True
        sh(["docker", "kill", name], check=False)
        proc.wait()
    tail.join(timeout=30)
    proc.stderr.close()
    stderr = tail.text()
    outside_s = time.monotonic() - started
    # The mount points docker creates for /data, /out, /sd and /sd-bin are ours, not the submission's.
    fs_diff = [
        line for line in sh(["docker", "diff", name], check=False).stdout.splitlines()
        if line.strip() and line.split(maxsplit=1)[-1] not in MOUNT_POINTS
    ]
    sh(["docker", "rm", "-fv", name], check=False)
    peak = cgroups.peak_and_remove(run_id)

    result = {"run_id": run_id, "outside_s": round(outside_s, 4), "peak_bytes": peak, "fs_diff": fs_diff}
    timing_path = os.path.join(sd_dir, "timing.json")
    if timed_out or not os.path.exists(timing_path):
        result.update(ok=False, reason="timeout" if timed_out else "no timing record", stderr=stderr[-2000:])
        return result
    # sd-timer writes /sd on the same tmpfs as /out, so a run that fills /out can leave
    # timing.json truncated. An unreadable record means the run cannot be timed, so it
    # cannot be scored: fail it as the submission's fault, not the board's.
    try:
        with open_regular(timing_path, TIMING_MAX_BYTES) as f:
            timing = json.loads(f.read().decode("utf-8"))
        wall_s = int(timing["wall_ns"]) / 1e9
        exit_code, term_signal = int(timing["exit_code"]), int(timing["signal"])
    except (ValueError, OSError, KeyError, TypeError):
        result.update(ok=False, reason="no usable timing record (a run that fills /out can starve the timer)",
                      stderr=stderr[-2000:])
        return result
    result.update(wall_s=round(wall_s, 6), exit_code=exit_code, signal=term_signal)
    # sd-timer exits with its child's status, so the container's exit status must agree with the
    # record. A mismatch means the record is not the one the timer wrote: never score it.
    expected_rc = 128 + term_signal if term_signal else exit_code
    if proc.returncode != expected_rc:
        result.update(ok=False, tamper=True, container_rc=proc.returncode,
                      reason="the timing record does not match the container's exit status")
        return result
    result["timing_gap_s"] = round(outside_s - wall_s, 4)
    if wall_s > timeout_s:
        result.update(ok=False, reason="timeout")
        return result
    if exit_code != 0:
        result.update(ok=False, reason=f"exit code {exit_code} signal {term_signal}", stderr=stderr[-2000:])
        return result
    outputs = sorted(os.listdir(out_dir))
    if outputs != [args.output]:
        result.update(ok=False, reason=f"/out must hold exactly {args.output}, found {outputs}")
        return result
    try:
        digest = sha256_output(os.path.join(out_dir, args.output))
    except (ValueError, OSError) as exc:
        # strerror, not str(exc): an OSError's text carries the host path, and reasons reach the public feed.
        why = getattr(exc, "strerror", None) or exc
        result.update(ok=False, reason=f"{args.output} must be a regular file of at most 2 GB ({why})")
        return result
    result["sha256"] = digest
    if args.expected_sha256 and digest != args.expected_sha256:
        result.update(ok=False, reason="hash mismatch")
        return result
    result["ok"] = True
    return result


def spread_ok(walls):
    middle = sorted(walls)[1:-1] or sorted(walls)
    mean = sum(middle) / len(middle)
    return (max(middle) - min(middle)) <= max(0.05 * mean, 0.050), mean


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--context", required=True, help="directory holding the track's Dockerfile")
    ap.add_argument("--track", required=True, choices=["a", "b", "c", "d"])
    ap.add_argument("--data", required=True)
    ap.add_argument("--output", required=True, help="the single output file name the spec defines")
    ap.add_argument("--expected-sha256", default="")
    ap.add_argument("--timeout-s", type=float, required=True, help="10x the reference's T")
    ap.add_argument("--allowlist", default=os.path.join(HERE, "allowlist.json"))
    ap.add_argument("--timer", default=os.path.join(HERE, "timer", "sd-timer"))
    ap.add_argument("--work", required=True)
    ap.add_argument("--cpuset", default="0-11")
    ap.add_argument("--runs", type=int, default=5)
    ap.add_argument("--max-reruns", type=int, default=2)
    ap.add_argument("--cgroups", default="host", choices=["host", "colima"])
    ap.add_argument("--tag", default="")
    ap.add_argument("--results", required=True)
    args = ap.parse_args()

    tag = args.tag or f"speeddemon/{os.path.basename(os.path.abspath(args.context))}-{args.track}:latest"
    report = {"track": args.track, "tag": tag, "build": build(args.context, tag, args.allowlist)}
    if report["build"]["ok"]:
        try:
            cmd = image_command(tag)
        except ImageRefused as exc:
            report["build"] = {"ok": False, "stage": "image", "problems": [str(exc)]}
    if report["build"]["ok"]:
        cgroups = CgroupReader(args.cgroups)
        prewarm(args.data)
        batches = []
        for attempt in range(1 + args.max_reruns):
            runs = []
            for _ in range(args.runs):
                r = run_once(tag, args.track, cmd, args, args.timer, cgroups, args.timeout_s)
                runs.append(r)
                if not r["ok"]:
                    break
            batches.append(runs)
            if not all(r["ok"] for r in runs) or len(runs) < 3:
                break
            ok, _ = spread_ok([r["wall_s"] for r in runs])
            if ok:
                break
        final = batches[-1]
        report["batches"] = batches
        report["gates"] = {
            "correctness": all(r.get("sha256") and r["ok"] for r in final) and len(final) == args.runs,
            "timeout": not any(r.get("reason") == "timeout" for r in final),
            "sandbox": all(not r["fs_diff"] for r in final),
        }
        if all(report["gates"].values()):
            walls = [r["wall_s"] for r in final]
            ok, mean = spread_ok(walls)
            report["T_s"] = round(mean, 6)
            report["M_bytes"] = max(r["peak_bytes"] for r in final)
            report["spread_ok"] = ok
    with open(args.results, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(json.dumps({k: report.get(k) for k in ("track", "gates", "T_s", "M_bytes", "spread_ok")}))


if __name__ == "__main__":
    main()

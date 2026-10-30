"""One team-track through the board: check, build, run, score.

The run itself is harness/run_submission.py's run_once, called once with the
board's timeout (3x the reference's T), so a board run is a scored run in
every respect except the count (one, not five) and the cap. Builds go to a
separate BuildKit builder (a buildx docker-container instance) that is pinned
to its own cores and memory, never to the P-cores the runs use.
"""
import collections
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
import time
import types

import dockerfile_check
import run_submission
import scoring

log = logging.getLogger("sdboard")

LOG_TAIL_LINES = 40
LOG_LINE_WIDTH = 300
STDERR_TAIL_LINES = 20
DOCKERFILE_MAX_BYTES = 256 * 1024
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
KILL_GRACE_S = 15


def sh(args, timeout=120, check=False):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=check)


class LineTail:
    """The last n lines of a stream; nothing earlier is ever kept."""

    def __init__(self, n=LOG_TAIL_LINES, width=LOG_LINE_WIDTH, replacements=()):
        self.lines = collections.deque(maxlen=n)
        self.width = width
        self.replacements = [(a, b) for a, b in replacements if a]

    def feed(self, line):
        line = ANSI_RE.sub("", line.rstrip("\r\n"))
        for a, b in self.replacements:
            line = line.replace(a, b)
        self.lines.append(line[: self.width])

    def get(self):
        return list(self.lines)


def tail_lines(text, n, replacements=()):
    t = LineTail(n=n, replacements=replacements)
    for line in (text or "").splitlines():
        t.feed(line)
    return t.get()


# -- check --------------------------------------------------------------------

def present_tracks(checkout):
    return [t for t in "abcd" if os.path.isdir(os.path.join(checkout, f"track-{t}"))]


def check_track(checkout, track, allowlist):
    """Return "" if track-<x>/Dockerfile may be built, else the reason it may not."""
    root = os.path.realpath(checkout)
    ctx = os.path.join(checkout, f"track-{track}")
    if os.path.islink(ctx) or not os.path.realpath(ctx).startswith(root + os.sep):
        return f"track-{track} must be a directory in the repo, not a symlink"
    df = os.path.join(ctx, "Dockerfile")
    if not os.path.lexists(df):
        return f"track-{track}/Dockerfile is missing"
    if os.path.islink(df) or not os.path.isfile(df):
        return f"track-{track}/Dockerfile must be a regular file, not a symlink"
    if os.path.getsize(df) > DOCKERFILE_MAX_BYTES:
        return f"track-{track}/Dockerfile is over {DOCKERFILE_MAX_BYTES // 1024} KB"
    with open(df, encoding="utf-8", errors="replace") as f:
        problems = dockerfile_check.check(f.read(), allowlist)
    if problems:
        return ("Dockerfile check: " + "; ".join(problems))[:600]
    return ""


# Build image -> language, for the public stats' "languages in use" (an aggregate, never per team).
# Runtime-only images (debian, the JRE, the .NET runtime) name no language and are skipped.
LANGUAGES = (("halloumee/speed-demon-rust:", "Rust"), ("rust:", "Rust"), ("golang:", "Go"), ("gcc:", "C/C++"),
             ("eclipse-temurin:25-jdk", "JVM"), ("ghcr.io/graalvm/", "JVM"), ("node:", "JavaScript"),
             ("mcr.microsoft.com/dotnet/sdk:", ".NET"), ("swift:", "Swift"), ("haskell:", "Haskell"),
             ("python:", "Python"))


def track_language(track_dir):
    """The language of the first build image in track_dir/Dockerfile, or "" if none is known."""
    try:
        with open(os.path.join(track_dir, "Dockerfile"), encoding="utf-8", errors="replace") as f:
            text = f.read(DOCKERFILE_MAX_BYTES)
    except OSError:
        return ""
    for line in dockerfile_check.logical_lines(text):
        parts = line.split()
        if len(parts) < 2 or parts[0].upper() != "FROM":
            continue
        image = parts[1].lower()
        for prefix, lang in LANGUAGES:
            if image.startswith(prefix):
                return lang
    return ""


DOC_FILES = {".speed-demon.toml"}


def unchanged_reference(track_dir):
    """True when the track folder is the starter as shipped: its Dockerfile and every other file,
    docs aside (Markdown and .speed-demon.toml), are byte-identical copies of files published in
    reference/ or generators/. Such a folder is the reference itself and scores about 0, so the
    board reports it instead of spending a timed run on it."""
    try:
        import static_axes
    except ImportError:
        return False
    published = static_axes.published_copies(track_dir)
    code = []
    for dirpath, dirnames, filenames in os.walk(track_dir):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            if name in DOC_FILES or name.lower().endswith(".md"):
                continue
            code.append(os.path.relpath(os.path.join(dirpath, name), track_dir).replace(os.sep, "/"))
    return "Dockerfile" in published and all(rel in published for rel in code)


# -- builder ------------------------------------------------------------------

ROOTLESS_SOCKET = "unix:///run/user/1000/buildkit/buildkitd.sock"
ROOTLESS_CONFIG = "/home/user/.config/buildkit/buildkitd.toml"


def builder_exists(name):
    return sh(["docker", "buildx", "inspect", name], timeout=60).returncode == 0


def builder_driver(bcfg):
    return bcfg.get("driver") or "rootless"


def builder_container(bcfg):
    """The container that runs the builder's buildkitd."""
    if builder_driver(bcfg) == "rootless":
        return f"{bcfg['name']}-buildkitd"
    return f"buildx_buildkit_{bcfg['name']}0"


def _cap_builder_pids(name, bcfg, required=True):
    # Cap the builder container's pids so an untrusted build RUN can't fork-bomb the host.
    # buildx exposes no pids driver-opt and dockerd has no daemon-wide default-pids-limit, so set it on
    # the builder container directly; it persists in HostConfig across stop/start/reboot. On reuse
    # (required=False) this just re-asserts a cap already set at create time, so a transient docker hiccup
    # must not brick a board start; at create time (required=True) it must succeed.
    proc = sh(["docker", "update", "--pids-limit", str(bcfg.get("pids", 1024)), builder_container(bcfg)], timeout=30)
    if proc.returncode != 0 and required:
        raise RuntimeError(f"could not set pids cap on builder {name}: {proc.stderr.strip()[-500:]}")
    return proc.returncode == 0


def _rootless_run_cmd(bcfg):
    # buildx's docker-container driver always makes its buildkitd container privileged, and a team
    # can steer a build onto the box (native CPU flags, a SIGILL rebuild). So the box's builder is
    # rootless BuildKit in a container we start ourselves: uid 1000, every capability dropped but
    # the two newuidmap needs, pids-capped. It builds its own user namespace, which needs seccomp,
    # AppArmor and the /proc masks off for this one container; nothing in it is privileged.
    cmd = ["docker", "run", "-d", "--name", builder_container(bcfg), "--restart=unless-stopped",
           "--label", "dev.speeddemon.builder=1",
           "--security-opt", "seccomp=unconfined", "--security-opt", "apparmor=unconfined",
           "--security-opt", "systempaths=unconfined", "--security-opt", "no-new-privileges=false",
           "--cap-drop=ALL", "--cap-add=SETUID", "--cap-add=SETGID",
           f"--cpuset-cpus={bcfg['cpuset']}", f"--memory={bcfg['memory']}", f"--memory-swap={bcfg['memory']}",
           f"--pids-limit={bcfg.get('pids', 1024)}", f"--network={bcfg.get('network', 'host')}"]
    daemon_args = ["--addr", ROOTLESS_SOCKET]
    if bcfg.get("buildkitd_config"):
        cmd += ["-v", f"{os.path.abspath(bcfg['buildkitd_config'])}:{ROOTLESS_CONFIG}:ro"]
        daemon_args += ["--config", ROOTLESS_CONFIG]
    return cmd + [bcfg["image"], *daemon_args]


def _ensure_rootless(bcfg):
    name, ctr = bcfg["name"], builder_container(bcfg)
    if "rootless" not in bcfg["image"].rsplit("/", 1)[-1]:
        raise RuntimeError(f"builder.driver is rootless but builder.image {bcfg['image']!r} is not a rootless "
                           "BuildKit image (moby/buildkit:<version>-rootless)")
    state = sh(["docker", "inspect", "-f", "{{.State.Status}}", ctr], timeout=30).stdout.strip()
    created = not state
    if created:
        proc = sh(_rootless_run_cmd(bcfg), timeout=600)
        if proc.returncode != 0:
            raise RuntimeError(f"could not start the builder daemon {ctr}: {proc.stderr.strip()[-1000:]}")
    elif state != "running":
        sh(["docker", "start", ctr], timeout=60)
    if builder_exists(name):
        info = sh(["docker", "buildx", "inspect", name], timeout=60).stdout
        if not re.search(r"^Driver:\s+remote\s*$", info, re.MULTILINE):
            raise RuntimeError(f"builder {name} exists but is not the rootless one; run: docker buildx rm {name}, "
                               "then board.py setup-builder")
    else:
        proc = sh(["docker", "buildx", "create", "--name", name, "--driver", "remote", f"docker-container://{ctr}"],
                  timeout=120)
        if proc.returncode != 0:
            raise RuntimeError(f"docker buildx create failed: {proc.stderr.strip()[-1000:]}")
    deadline = time.monotonic() + 60
    while sh(["docker", "buildx", "inspect", "--bootstrap", name], timeout=60).returncode != 0:
        if time.monotonic() > deadline:
            raise RuntimeError(f"builder {name} did not come up within 60 s; see docker logs {ctr}")
        time.sleep(2)
    _cap_builder_pids(name, bcfg, required=created)
    return (f"{'created' if created else 'reused'} rootless builder {name} (container {ctr}, cpuset {bcfg['cpuset']}, "
            f"memory {bcfg['memory']}, pids {bcfg.get('pids', 1024)}, unprivileged)")


def ensure_builder(bcfg):
    """Create the board's buildx builder if it does not exist. Returns a description for the log."""
    name = bcfg["name"]
    if not name:
        return "using the default builder (no cpuset or memory isolation: local use only)"
    if builder_driver(bcfg) == "rootless":
        return _ensure_rootless(bcfg)
    if builder_exists(name):
        _cap_builder_pids(name, bcfg, required=False)
        return f"builder {name} exists"
    # network is the BuildKit container's own, for pulling base images: the box's dockerd has no bridge
    # ("bridge": "none" in setup-box.sh), so it shares the host's. RUN steps still get --network=none.
    cmd = ["docker", "buildx", "create", "--name", name, "--driver", "docker-container",
           "--driver-opt", f"image={bcfg['image']}", "--driver-opt", f"network={bcfg.get('network', 'host')}",
           "--driver-opt", f"cpuset-cpus={bcfg['cpuset']}",
           "--driver-opt", f"memory={bcfg['memory']}", "--driver-opt", f"memory-swap={bcfg['memory']}"]
    if bcfg.get("buildkitd_config"):
        cmd += ["--buildkitd-config", bcfg["buildkitd_config"]]
    cmd.append("--bootstrap")
    proc = sh(cmd, timeout=600)
    if proc.returncode != 0:
        raise RuntimeError(f"docker buildx create failed: {proc.stderr.strip()[-1000:]}")
    _cap_builder_pids(name, bcfg, required=True)
    return f"created builder {name} (cpuset {bcfg['cpuset']}, memory {bcfg['memory']}, pids {bcfg.get('pids', 1024)})"


def prewarm_builder(bcfg, allowlist, workdir):
    """Pull every allowlisted base image into the builder's cache once, so team builds never wait on a registry.

    The builder has its own content store: setup-box.sh's `docker pull`s go to dockerd, not to it.
    Returns [(image, ok, seconds)].
    """
    out = []
    os.makedirs(workdir, exist_ok=True)
    for entry in allowlist:
        ref = f"{entry['image']}@{entry['digest']}"
        with open(os.path.join(workdir, "Dockerfile"), "w", encoding="utf-8") as f:
            f.write(f'FROM {ref}\nRUN ["/bin/true"]\n')
        started = time.monotonic()
        cmd = ["docker", "buildx", "build", "--network=none", "--progress=quiet", "--output", "type=cacheonly"]
        if bcfg["name"]:
            cmd += ["--builder", bcfg["name"]]
        proc = sh(cmd + [workdir], timeout=1800)
        out.append((entry["image"], proc.returncode == 0, round(time.monotonic() - started, 1)))
    return out


class ProcessRegistry:
    """Running builds and clones, so the disk watchdog (or a stop) can kill them all."""

    def __init__(self):
        self.lock = threading.Lock()
        self.procs = {}

    def add(self, key, proc):
        with self.lock:
            self.procs[key] = {"proc": proc, "killed": ""}

    def remove(self, key):
        with self.lock:
            return self.procs.pop(key, {}).get("killed", "")

    def killed(self, key):
        with self.lock:
            return self.procs.get(key, {}).get("killed", "")

    def kill(self, key, reason):
        with self.lock:
            entry = self.procs.get(key)
            if not entry or entry["killed"]:
                return
            entry["killed"] = reason
        kill_group(entry["proc"])

    def kill_all(self, reason):
        with self.lock:
            keys = list(self.procs)
        for key in keys:
            self.kill(key, reason)
        return len(keys)

    def __len__(self):
        with self.lock:
            return len(self.procs)


def kill_group(proc):
    """SIGINT (buildx cancels the build on the builder), then SIGKILL after a grace period."""
    def _signal(sig):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            pass

    if proc.poll() is not None:
        return
    _signal(signal.SIGINT)

    def _later():
        try:
            proc.wait(timeout=KILL_GRACE_S)
        except subprocess.TimeoutExpired:
            _signal(signal.SIGKILL)

    threading.Thread(target=_later, daemon=True).start()


# Signatures that mean "Depot (or the network to it) was unavailable", not "the team's build failed".
# Only these trigger the on-box fallback; a genuine build error is the team's result.
DEPOT_INFRA_MARKERS = ("connection", "dial tcp", "no such host", "context deadline", "i/o timeout",
                       "temporarily unavailable", "unauthorized", "unauthenticated", "invalid token",
                       "failed to fetch oauth", "rpc error", "connection reset", "could not connect",
                       "quota", "rate limit", "depot: ")

# BuildKit's plain progress prefixes every line "#N ". A team's build output (including RUN-step
# stdout, "#5 0.12 ...") is always BuildKit-prefixed, so the only failures with NO such line are
# Depot pre-flight failures (bad token, DNS/network to Depot). Depot's own control lines
# ("#1 [depot] build: ...", "#2 [depot] launching ...") are "#N " but not team output, so a failure
# showing only those (e.g. a machine-launch/capacity error) still counts as "not reached" and may
# fall back. A team cannot forge a "#N [depot]" prefix — BuildKit writes it, not the RUN command — so
# this gate cannot be gamed into running an untrusted build on the box.
_BUILDKIT_LINE = re.compile(r"^#\d+ ")
_DEPOT_CONTROL_LINE = re.compile(r"^#\d+ \[depot\]")


def _reached_buildkit(tail):
    return any(_BUILDKIT_LINE.match(l) and not _DEPOT_CONTROL_LINE.match(l) for l in tail.get())


def _should_fallback(killed, rc, tail, fallback_local):
    """True only for a Depot pre-flight/infra failure, never a team build error or a killed build.

    Gated on _reached_buildkit so team-controlled output matching DEPOT_INFRA_MARKERS (pip
    "Connection error", cargo "failed to connect") can never force the untrusted build onto the box.
    """
    if killed or rc == 0 or not fallback_local or _reached_buildkit(tail):
        return False
    text = "\n".join(tail.get()).lower()
    return any(m in text for m in DEPOT_INFRA_MARKERS)


def _local_build_cmd(ctx, tag, bcfg, labels):
    cmd = ["docker", "buildx", "build"]
    if bcfg["name"]:
        cmd += ["--builder", bcfg["name"]]
    cmd += ["--network=none", "--load", "--progress=plain", "--provenance=false", "--sbom=false",
            "-f", os.path.join(ctx, "Dockerfile"), "-t", tag]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd.append(ctx)
    return cmd


# Flags that compile for the CPU the build runs on. Depot's CPU is not the scoring box's (it has
# AVX-512, the i5-13500 does not), so a track that uses one is built on the box, where "native"
# is the scoring CPU. -mtune=native only tunes and never emits a missing instruction.
NATIVE_CPU_RE = re.compile(rb"(?:-m(?:arch|cpu)|target[-_]cpu|IlcInstructionSet>)\s*[= ]?\s*[\"']?native\b")
NATIVE_SCAN_SKIP_DIRS = {".git", "target", "node_modules"}
NATIVE_SCAN_MAX_BYTES = 2 * 1024 * 1024


def native_cpu_flags(track_dir, limit=5):
    """Paths (relative to track_dir) of text files that ask for the build host's CPU."""
    hits = []
    for root, dirs, files in os.walk(track_dir):
        dirs[:] = sorted(d for d in dirs if d not in NATIVE_SCAN_SKIP_DIRS
                         and not os.path.islink(os.path.join(root, d)))
        for name in sorted(files):
            path = os.path.join(root, name)
            try:
                if os.path.islink(path) or os.path.getsize(path) > NATIVE_SCAN_MAX_BYTES:
                    continue
                with open(path, "rb") as f:
                    data = f.read()
            except OSError:
                continue
            if b"\0" in data[:4096]:
                continue
            if NATIVE_CPU_RE.search(data):
                hits.append(os.path.relpath(path, track_dir))
                if len(hits) >= limit:
                    return hits
    return hits


def is_sigill(r):
    """The run died on an instruction this CPU lacks (directly, or via a shell's 128+4)."""
    return bool(r) and (int(r.get("signal") or 0) == signal.SIGILL or r.get("exit_code") == 128 + signal.SIGILL)


# The installer puts the CLI in ~/.depot/bin, which is not on systemd's PATH.
DEPOT_INSTALL_BIN = "/root/.depot/bin/depot"


def _depot_bin(bcfg):
    return bcfg.get("depot_bin") or shutil.which("depot") or DEPOT_INSTALL_BIN


def _depot_build_cmd(ctx, tag, bcfg, labels):
    """Build on Depot's isolated ephemeral builders, offline, amd64, loaded back to the local dockerd.

    Reads DEPOT_TOKEN from the environment (board.env). The resulting image is byte-identical to a
    local build (digest-pinned bases), so the timed run on the box is unaffected.
    """
    project = bcfg.get("depot_project") or os.environ.get("DEPOT_PROJECT_ID", "")
    cmd = [_depot_bin(bcfg), "build", "--network", "none", "--build-platform",
           bcfg.get("depot_platform", "linux/amd64"), "--load", "--progress=plain"]
    if project:
        cmd += ["--project", project]
    cmd += ["-f", os.path.join(ctx, "Dockerfile"), "-t", tag]
    for k, v in labels.items():
        cmd += ["--label", f"{k}={v}"]
    cmd.append(ctx)
    return cmd


def local_build_env():
    """The board's environment minus Depot's credentials: an on-box build client never needs them."""
    return {k: v for k, v in os.environ.items() if not k.startswith("DEPOT_")}


def _run_build(cmd, timeout_s, procs, key, replacements, env=None):
    """Run one build command, streaming its tail; returns (killed, returncode, build_s, LineTail)."""
    tail = LineTail(replacements=replacements)
    started = time.monotonic()
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            text=True, errors="replace", start_new_session=True, env=env)
    procs.add(key, proc)
    timer = threading.Timer(timeout_s, lambda: procs.kill(key, "timeout"))
    timer.daemon = True
    timer.start()
    try:
        with proc.stdout:
            for line in proc.stdout:
                tail.feed(line)
        proc.wait()
    finally:
        timer.cancel()
        if proc.poll() is None:
            kill_group(proc)
            proc.wait()
        killed = procs.remove(key)
    return killed, proc.returncode, round(time.monotonic() - started, 3), tail


def build_image(ctx, tag, bcfg, labels, procs, key, replacements=(), on_backend=None, force_local=False):
    """Build one track's image, offline, with a hard timeout and the image cap.

    backend "local" (default) uses the board's on-box buildx builder; "depot" builds on Depot's
    isolated ephemeral builders and loads the image back, keeping untrusted builds off the box. On
    a Depot infrastructure/connectivity failure (not a genuine build error) it falls back to the
    on-box builder, so Depot is never a single point of failure. Returns
    {"ok", "stage", "reason", "build_s", "image_bytes", "log_tail"}.
    """
    backend = "local" if force_local else bcfg.get("backend", "local")
    timeout_s = float(bcfg["timeout_s"])
    used = backend
    note = on_backend or (lambda b: None)
    if backend == "depot":
        note("depot")
        try:
            killed, rc, build_s, tail = _run_build(_depot_build_cmd(ctx, tag, bcfg, labels), timeout_s, procs, key, replacements)
            fallback = _should_fallback(killed, rc, tail, bcfg.get("fallback_local", True))
        except OSError as exc:                  # the CLI itself is missing or not executable: infra, not the team
            log.error("depot CLI could not start (%s)", exc)
            killed, rc, build_s, tail = "", 1, 0.0, LineTail(replacements=replacements)
            fallback = bool(bcfg.get("fallback_local", True))
        if fallback:
            log.warning("depot build unavailable for %s; falling back to the on-box builder", tag)
            note("local")
            used = "local"
            extra_timeout = max(1.0, timeout_s - build_s)   # don't grant a second full build budget
            killed, rc, extra_s, tail = _run_build(_local_build_cmd(ctx, tag, bcfg, labels), extra_timeout, procs, key, replacements,
                                                   env=local_build_env())
            build_s = round(build_s + extra_s, 3)
    else:
        note("local")
        killed, rc, build_s, tail = _run_build(_local_build_cmd(ctx, tag, bcfg, labels), timeout_s, procs, key, replacements,
                                                env=local_build_env())

    result = {"ok": False, "stage": "build", "build_s": build_s, "image_bytes": None, "log_tail": tail.get(),
              "builder": used}
    if killed == "timeout":
        return {**result, "reason": f"build exceeded {int(timeout_s)} s"}
    if killed == "disk":
        return {**result, "reason": "build stopped by the board's disk guard (the box ran low on disk); push again to retry"}
    if killed:
        return {**result, "reason": f"build stopped ({killed})"}
    if rc != 0:
        return {**result, "reason": f"docker build failed (exit {rc}); see the build log tail"}
    size = image_bytes(tag)
    if size is None:
        return {**result, "reason": "built image not found after --load"}
    result["image_bytes"] = size
    if size > int(bcfg["image_max_bytes"]):
        remove_image(tag)
        return {**result, "stage": "image-size", "reason": f"image is {size} bytes, cap is {int(bcfg['image_max_bytes'])}"}
    return {**result, "ok": True, "stage": "built", "reason": "", "log_tail": []}


def image_bytes(tag):
    proc = sh(["docker", "image", "inspect", "-f", "{{.Size}}", tag], timeout=60)
    return int(proc.stdout.strip()) if proc.returncode == 0 and proc.stdout.strip().isdigit() else None


def image_entry(tag):
    """(ENTRYPOINT + CMD, "") or (None, reason)."""
    try:
        return run_submission.image_command(tag), ""
    except SystemExit:
        return None, "image has no ENTRYPOINT or CMD"
    except run_submission.ImageRefused as exc:
        return None, str(exc)
    except (subprocess.CalledProcessError, ValueError) as exc:
        return None, f"could not inspect the image ({type(exc).__name__})"


def image_has_healthcheck(tag):
    proc = sh(["docker", "image", "inspect", "-f", "{{json .Config.Healthcheck}}", tag], timeout=60)
    return proc.returncode == 0 and proc.stdout.strip() not in ("", "null")


def remove_image(tag):
    sh(["docker", "image", "rm", tag], timeout=120)


def prune_images(repo, keep_tag):
    """Remove every tag of this team-track's image repository except keep_tag. Returns the removed tags."""
    proc = sh(["docker", "image", "ls", "--filter", f"reference={repo}", "--format", "{{.Repository}}:{{.Tag}}"], timeout=60)
    removed = []
    for ref in sorted(set(proc.stdout.split())):
        if ref != keep_tag and not ref.endswith(":<none>"):
            remove_image(ref)
            removed.append(ref)
    return removed


# -- run ----------------------------------------------------------------------

def run_track(tag, track, cmd, tcfg, cfg, cgroups):
    """One run under run_submission's contract, capped at timeout_factor x T_ref."""
    timeout_s = round(float(cfg["run_timeout_factor"]) * float(tcfg["ref"]["T_s"]), 3)
    args = types.SimpleNamespace(work=cfg["work"], cpuset=cfg["run_cpuset"], data=tcfg["data"],
                                 output=tcfg["output"], expected_sha256=tcfg["expected_sha256"])
    os.makedirs(cfg["work"], exist_ok=True)
    run_submission.prewarm(tcfg["data"])
    r = run_submission.run_once(tag, track, cmd, args, cfg["timer"], cgroups, timeout_s)
    return r, timeout_s


def gates_of(r):
    """The three gates exactly as run_submission.main computes them, for a batch of one run."""
    return {
        "correctness": bool(r.get("sha256")) and bool(r.get("ok")),
        "timeout": r.get("reason") != "timeout",
        "sandbox": not r.get("fs_diff"),
    }


def failure_of(r, gates, timeout_s, factor):
    if not gates["timeout"]:
        return f"over the board's cap of {timeout_s:g} s ({factor:g}x the reference's T)"
    if r.get("reason"):
        return str(r["reason"])[:300]
    if not gates["sandbox"]:
        paths = [line.split(maxsplit=1)[-1] for line in r.get("fs_diff", [])][:3]
        return f"wrote outside /out: {', '.join(paths)}"[:300]
    return ""


def score(T_s, M_bytes, S, D, ref):
    k = scoring.k_from_measurements(
        {"T": float(ref["T_s"]), "M": float(ref["M_bytes"]), "S": float(ref["S"]), "D": float(ref["D"])},
        {"T": float(T_s), "M": float(M_bytes), "S": float(S), "D": float(D)})
    return k, scoring.speed_points(k)


def cleanup_work(work):
    """run_once leaves <work>/<run_id>/{out,sd}; the board's work dir is its own, so clear it after each run."""
    if not os.path.isdir(work):
        return
    for name in os.listdir(work):
        if name.startswith("run-"):
            shutil.rmtree(os.path.join(work, name), ignore_errors=True)


def kill_running_containers():
    """On a stop: kill any run container (run_submission names them sd-run-<id>)."""
    proc = sh(["docker", "ps", "-q", "--filter", "name=^sd-run-"], timeout=60)
    ids = proc.stdout.split()
    if ids:
        sh(["docker", "kill", *ids], timeout=60)
    return ids


def load_allowlist(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)["images"]

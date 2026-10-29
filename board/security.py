"""Private security events: what a team did that looks like an attack, for the organiser only.

Alert-only. Nothing here changes a run, a score or the queue; it only writes evidence:

  <state_dir>/security.jsonl         one fsynced line per event (append-only)
  <state_dir>/security_summary.json  counts per team and kind + the latest events, for the
                                     monitor page's private view

Both files are private. They must never reach the public raptors.dev bucket.
The board and tally_sync are separate processes, so writes take an flock.
"""
import fcntl
import json
import os
import re
import time

SEVERITIES = ("low", "medium", "high")
RECENT = 50
EVIDENCE_MAX_CHARS = 500

# Dockerfile refusals that are a reach for something outside the rules, not a beginner's slip
# (an unpinned FROM is a slip; a parser directive or a foreign --from is a reach).
DOCKERFILE_REACH = (
    ("parser directive", re.compile(r"parser directive|# syntax=")),
    ("foreign image", re.compile(r"--from must name|mount from= must name")),
    ("build-time network or privilege", re.compile(r"RUN --(network|security|device)")),
    ("shared mount", re.compile(r"--mount=type=(cache|secret|ssh)")),
    ("remote ADD", re.compile(r"ADD may not fetch")),
    ("FROM trick", re.compile(r"FROM flags|FROM may not use build arguments|ONBUILD")),
    ("symlink", re.compile(r"not a symlink")),
)

# Magic bytes of files that hide code or data from the S axis when committed to a track folder.
MAGIC = (
    (b"\x7fELF", "ELF binary"), (b"\xcf\xfa\xed\xfe", "Mach-O binary"), (b"\xfe\xed\xfa\xcf", "Mach-O binary"),
    (b"MZ", "PE binary"), (b"\x00asm", "wasm module"), (b"!<arch>", "static library"),
    (b"\x1f\x8b", "gzip archive"), (b"PK\x03\x04", "zip archive"), (b"\xfd7zXZ\x00", "xz archive"),
    (b"\x28\xb5\x2f\xfd", "zstd archive"), (b"BZh", "bzip2 archive"), (b"7z\xbc\xaf\x27\x1c", "7z archive"),
)
BLOB_BYTES = 1024 * 1024


def flag(kind, severity, evidence):
    assert severity in SEVERITIES, severity
    return {"kind": kind, "severity": severity, "evidence": str(evidence)[:EVIDENCE_MAX_CHARS]}


def dockerfile_flags(reason):
    """Flags for a check refusal, or [] when the refusal is an ordinary mistake."""
    hits = [label for label, rx in DOCKERFILE_REACH if rx.search(reason or "")]
    return [flag("dockerfile_reach", "medium", f"{', '.join(hits)}: {reason}")] if hits else []


def _kind_of(path):
    try:
        with open(path, "rb") as f:
            head = f.read(512)
    except OSError:
        return None
    for magic, label in MAGIC:
        if head.startswith(magic):
            return label
    if len(head) >= 262 and head[257:262] == b"ustar":
        return "tar archive"
    if os.path.getsize(path) > BLOB_BYTES:
        try:
            with open(path, "rb") as f:
                f.read(BLOB_BYTES).decode("utf-8")
        except UnicodeDecodeError:
            return "binary blob over 1 MB"
    return None


def suspicious_files(track_dir, limit=5):
    """Committed binaries, archives and big blobs in a track folder: code the S axis cannot see.
    A flag for the organiser's manual S review, never a refusal."""
    found = []
    for root, dirs, files in os.walk(track_dir):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in sorted(files):
            path = os.path.join(root, name)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            label = _kind_of(path)
            if label:
                found.append(f"{os.path.relpath(path, track_dir)} ({label})")
                if len(found) >= limit:
                    return found
    return found


def run_flags(r, track_floor_s=None):
    """Flags from one run_submission.run_once result."""
    out = []
    if not r:
        return out
    if r.get("tamper"):
        out.append(flag("timing_tamper", "high",
                        f"timing record disagrees with the container's exit status {r.get('container_rc')}"))
    if r.get("fs_diff"):
        out.append(flag("sandbox_write", "high", f"wrote outside /out: {r['fs_diff'][:5]}"))
    if "regular file" in str(r.get("reason") or ""):
        out.append(flag("output_not_regular", "medium", r["reason"]))
    gap = r.get("timing_gap_s")
    if isinstance(gap, (int, float)) and gap > 3.0:
        out.append(flag("timing_gap", "medium", f"container ran {gap:.1f} s longer than the timed run"))
    wall = r.get("wall_s")
    if r.get("ok") and track_floor_s and isinstance(wall, (int, float)) and wall < track_floor_s:
        out.append(flag("too_fast", "medium", f"T {wall:.6f} s is under the {track_floor_s:g} s floor; "
                                              "answers baked into the image? (the holdout seed would catch it)"))
    return out


class SecurityLog:
    def __init__(self, state_dir):
        self.path = os.path.join(state_dir, "security.jsonl")
        self.summary_path = os.path.join(state_dir, "security_summary.json")
        self.lock_path = os.path.join(state_dir, ".security.lock")
        os.makedirs(state_dir, exist_ok=True)

    def _events(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def emit(self, kind, severity, evidence="", team=None, track=None, run_id=None, commit=None, key=None, now=None):
        """Append one event. With `key`, an event already logged under that key is skipped. True if written."""
        assert severity in SEVERITIES, severity
        now = time.time() if now is None else now
        event = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "kind": kind, "severity": severity,
                 "team": team, "track": track, "run_id": run_id, "commit": commit,
                 "evidence": str(evidence)[:EVIDENCE_MAX_CHARS]}
        if key:
            event["key"] = key
        with open(self.lock_path, "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            events = self._events()
            if key and any(e.get("key") == key for e in events):
                return False
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
            events.append(event)
            self._write_summary(events, now)
        return True

    def emit_flags(self, flags, **where):
        for f in flags or ():
            self.emit(f["kind"], f["severity"], f["evidence"], **where)

    def _write_summary(self, events, now):
        by_team, by_severity = {}, {s: 0 for s in SEVERITIES}
        for e in events:
            by_severity[e["severity"]] = by_severity.get(e["severity"], 0) + 1
            kinds = by_team.setdefault(e.get("team") or "(no team)", {})
            kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
        summary = {"schema": 1, "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
                   "total": len(events), "by_severity": by_severity, "by_team": by_team,
                   "recent": events[-RECENT:][::-1]}
        tmp = f"{self.summary_path}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=1, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.summary_path)

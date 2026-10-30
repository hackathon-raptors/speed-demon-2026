"""Box health for the public /board panel: CPU, memory, load and temperature, last 30 minutes.

Every INTERVAL_S it samples /proc and the coretemp sensor, keeps a RING-point history and
rewrites <out> atomically. publish.sh uploads the file next to board.json.

The file is public. It carries machine numbers only: no team, no path, no process name.
It runs on the E-cores at the lowest priority (speeddemon-health.service), so it never
touches the P-cores the timed runs use.

It also sums the CPU package energy (Intel RAPL) since kickoff, kept in --state across restarts,
for the panel's "how much did we warm the planet" line. Before kickoff it counts from its first
start; at kickoff the sum starts again from zero.

  health.py [--out /srv/speeddemon/board/public/health.json] [--interval 5] [--ring 360]
            [--state /srv/speeddemon/board/state/energy.json] [--since 2026-10-30T15:00:00Z]
"""
import calendar
import argparse
import glob
import json
import os
import time

RAPL = "/sys/class/powercap/intel-rapl:0"     # package-0: cores, uncore and graphics, not RAM or disks
P_CORES = range(0, 12)
E_CORES = range(12, 20)


def read_cpu_times(text):
    """{cpu index: (busy, total)} from /proc/stat text; the aggregate line is skipped."""
    times = {}
    for line in text.splitlines():
        if not line.startswith("cpu") or line.startswith("cpu "):
            continue
        name, *fields = line.split()
        vals = [int(v) for v in fields[:8]]          # user nice system idle iowait irq softirq steal
        idle = vals[3] + vals[4]
        times[int(name[3:])] = (sum(vals) - idle, sum(vals))
    return times


def busy_pct(prev, cur, cores):
    """Busy share of the given cores between two samples, 0-100; None if nothing moved."""
    busy = total = 0
    for c in cores:
        if c in prev and c in cur:
            busy += cur[c][0] - prev[c][0]
            total += cur[c][1] - prev[c][1]
    return round(100.0 * busy / total, 1) if total > 0 else None


def read_mem(text):
    """(used, cache, total) in MB from /proc/meminfo text; used = total - available."""
    kb = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.strip():
            kb[key] = int(rest.split()[0])
    total, avail = kb["MemTotal"], kb["MemAvailable"]
    cache = kb.get("Cached", 0) + kb.get("Buffers", 0)
    return round((total - avail) / 1024), round(cache / 1024), round(total / 1024)


def read_temp():
    """CPU package temperature in C from the coretemp hwmon, or None."""
    for name in glob.glob("/sys/class/hwmon/hwmon*/name"):
        try:
            with open(name) as f:
                if f.read().strip() != "coretemp":
                    continue
            base = os.path.dirname(name)
            for label in sorted(glob.glob(os.path.join(base, "temp*_label"))):
                with open(label) as f:
                    if f.read().strip().startswith("Package"):
                        with open(label.replace("_label", "_input")) as t:
                            return round(int(t.read()) / 1000, 1)
        except (OSError, ValueError):
            continue
    return None


def read_rapl(base=RAPL):
    """(energy_uj, max_energy_range_uj) of the package, or None where RAPL is missing."""
    try:
        with open(os.path.join(base, "energy_uj")) as f, open(os.path.join(base, "max_energy_range_uj")) as g:
            return int(f.read()), int(g.read())
    except (OSError, ValueError):
        return None


def energy_delta_j(prev, cur):
    """Joules between two RAPL readings, across one counter wrap."""
    if prev is None or cur is None:
        return 0.0
    d = cur[0] - prev[0]
    if d < 0:
        d += cur[1]
    return d / 1e6


class EnergyLog:
    """Joules since `since` (kickoff), kept in a small JSON file so a restart does not lose them."""

    def __init__(self, path, since):
        self.path, self.kickoff = path, since
        self.since, self.joules = None, 0.0
        try:
            with open(path) as f:
                doc = json.load(f)
            self.since, self.joules = doc["since"], float(doc["joules"])
        except (OSError, ValueError, KeyError, TypeError):
            pass

    def add(self, joules, now):
        kick = calendar.timegm(time.strptime(self.kickoff, "%Y-%m-%dT%H:%M:%SZ"))
        if self.since is None or (now >= kick and calendar.timegm(time.strptime(self.since, "%Y-%m-%dT%H:%M:%SZ")) < kick):
            self.since = self.kickoff if now >= kick else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
            self.joules = 0.0
        self.joules += joules

    def save(self):
        write_atomic(self.path, {"since": self.since, "joules": round(self.joules, 1)})


def read(path):
    with open(path) as f:
        return f.read()


def write_atomic(path, doc):
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(doc, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def document(points, interval_s, mem_total, now, energy=None):
    return {**({"energy": energy} if energy else {}),"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "interval_s": interval_s,
            "box": {"cpu": "Intel Core i5-13500", "p_cores": len(P_CORES), "e_cores": len(E_CORES),
                    "mem_total_mb": mem_total},
            "fields": ["t", "p_pct", "e_pct", "mem_used_mb", "mem_cache_mb", "load1", "temp_c"],
            "points": points}


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/srv/speeddemon/board/public/health.json")
    ap.add_argument("--interval", type=float, default=5.0)
    ap.add_argument("--ring", type=int, default=360)
    ap.add_argument("--state", default="/srv/speeddemon/board/state/energy.json")
    ap.add_argument("--since", default="2026-10-30T15:00:00Z")
    args = ap.parse_args(argv)
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    points = []
    prev = read_cpu_times(read("/proc/stat"))
    os.makedirs(os.path.dirname(args.state), exist_ok=True)
    energy = EnergyLog(args.state, args.since)
    rapl_prev = read_rapl()
    while True:
        time.sleep(args.interval)
        cur = read_cpu_times(read("/proc/stat"))
        used, cache, total = read_mem(read("/proc/meminfo"))
        now = time.time()
        points.append([int(now), busy_pct(prev, cur, P_CORES), busy_pct(prev, cur, E_CORES), used, cache,
                       float(read("/proc/loadavg").split()[0]), read_temp()])
        del points[:-args.ring]
        prev = cur
        rapl = read_rapl()
        joules = energy_delta_j(rapl_prev, rapl)
        rapl_prev = rapl
        doc_energy = None
        if rapl is not None:
            energy.add(joules, now)
            energy.save()
            doc_energy = {"since": energy.since, "joules": round(energy.joules, 1),
                          "watts": round(joules / args.interval, 1), "meter": "CPU package (Intel RAPL)"}
        write_atomic(args.out, document(points, args.interval, total, now, doc_energy))


if __name__ == "__main__":
    main()

"""The /board tape: the latest new bests, rank moves and failed runs, replayed from history.jsonl.

The browser only ever sees the current board, so it cannot tell how far a run moved a team. This
replays the whole private history in order, with the board's own rule for a best run (highest
points, then K), and writes the last events as a small public tape.json:

  {"generated_at": ..., "events": [newest first, at most --keep], "climbers": [top 3 of the last hour]}
  event = {"at", "slug", "team", "tk": "a".."d" | "all", "pts", "gain", "move", "rank"}  a new best
        | {"at", "slug", "team", "tk", "fail": "<gate>"}                                   a failed run

gain and move compare with the team's previous best and rank (null on a first score). An "all" event
follows a new best when it moved the team in Overall (geomean of its best three tracks). Only names,
tracks, points, ranks and gate names go out, all of which board.json already shows; log tails never do.
climbers ranks teams by the track places gained in new bests over the last hour (summed over tracks A-D,
then by points gained): {"slug", "team", "climb", "gain", "tracks"}.
crowns follows who held #1 on each track and in Overall since the first score:
  {"a": {"segs": [{"slug", "team", "from"}...], "held": [{"slug", "team", "s"}, top 3 by seconds held]}, ...}
A segment runs until the next one starts, the last one until generated_at.
final_pushes counts distinct (team, commit) pushed in the last hour before code freeze (--freeze).
Run by speeddemon-tape.timer every 10 s, which uploads the file next to board.json.

    python3 board/tape.py --history /srv/speeddemon/board/state/history.jsonl \
        --out /srv/speeddemon/board/public/tape.json --config /etc/speeddemon/board.json
"""
import argparse
import datetime
import json
import math
import os
import tempfile
import time

TRACKS = ("a", "b", "c", "d")
SKIP_STAGES = {"unchanged"}           # the untouched starter is not a run


def overall(bests):
    """Geomean of the best three track points, or None under three tracks (the board's Overall)."""
    pts = sorted((b[0] for b in bests.values()), reverse=True)[:3]
    if len(pts) < 3:
        return None
    if any(v <= 0 for v in pts):
        return 0.0
    return math.exp(sum(math.log(v) for v in pts) / 3)


def replay(rows, exclude=(), keep=50, crowns=None):
    """New-best and failed-run events; with a crowns dict, also the #1 holder changes per track and 'all'."""
    best = {}                         # slug -> {track: (points, K)}
    names, events = {}, []
    holder = {}

    def crown(k, at):
        if k == "all":
            scored = [(o, s) for s, b in best.items() if (o := overall(b)) is not None]
        else:
            scored = [(b[k], s) for s, b in best.items() if k in b]
        if not scored:
            return
        top = max(scored)[1]
        if holder.get(k) != top:
            holder[k] = top
            crowns.setdefault(k, []).append({"slug": top, "team": names[top], "from": at})

    def track_rank(slug, tk):
        mine = best[slug][tk]
        return 1 + sum(1 for s, b in best.items() if s != slug and tk in b and b[tk] > mine)

    def overall_rank(slug):
        mine = overall(best.get(slug, {}))
        if mine is None:
            return None, None
        return 1 + sum(1 for s, b in best.items() if s != slug and (o := overall(b)) is not None and o > mine), mine

    for r in rows:
        tk, slug = r.get("track"), r.get("team_slug")
        if tk not in TRACKS or not slug or r.get("team") in exclude:
            continue
        names[slug] = r.get("team") or slug
        at = r.get("ended_at") or r.get("recorded_at")
        if r.get("ok") and r.get("points") is not None:
            new = (r["points"], r.get("K") or 0)
            was = best.get(slug, {}).get(tk)
            if was is not None and new <= was:
                continue
            was_rank = track_rank(slug, tk) if was is not None else None
            was_all, _ = overall_rank(slug)
            best.setdefault(slug, {})[tk] = new
            rank = track_rank(slug, tk)
            if crowns is not None:
                crown(tk, at)
                crown("all", at)
            events.append({"at": at, "slug": slug, "team": names[slug], "tk": tk, "pts": round(new[0], 4),
                           "gain": None if was is None else round(new[0] - was[0], 4),
                           "move": None if was_rank is None else was_rank - rank, "rank": rank})
            now_all, o = overall_rank(slug)
            if now_all is not None and now_all != was_all:
                events.append({"at": at, "slug": slug, "team": names[slug], "tk": "all", "pts": round(o, 4),
                               "gain": None, "move": None if was_all is None else was_all - now_all, "rank": now_all})
        elif not r.get("ok") and r.get("stage") not in SKIP_STAGES:
            gate = next((g for g, v in (r.get("gates") or {}).items() if v is False), None)
            events.append({"at": at, "slug": slug, "team": names[slug], "tk": tk, "fail": gate or r.get("stage") or "failed"})
    return events[-keep:][::-1] if keep else events


def _epoch(iso):
    try:
        return datetime.datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def crown_summary(crowns, now, keep=200, top=3):
    """Per key: the last `keep` segments and the top holders by total seconds held."""
    out = {}
    for k, segs in crowns.items():
        held = {}
        for i, seg in enumerate(segs):
            start, end = _epoch(seg["from"]), _epoch(segs[i + 1]["from"]) if i + 1 < len(segs) else now
            if start is None or end is None:
                continue
            h = held.setdefault(seg["slug"], {"slug": seg["slug"], "team": seg["team"], "s": 0})
            h["s"] += max(0, int(end - start))
        out[k] = {"segs": segs[-keep:], "held": sorted(held.values(), key=lambda h: -h["s"])[:top]}
    return out


def final_pushes(rows, freeze, exclude=()):
    """Distinct (team, commit) pushed in the hour before freeze; feeds the board's freeze-hour counter."""
    end = _epoch(freeze)
    seen = set()
    for r in rows:
        at = _epoch(r.get("pushed_at") or r.get("recorded_at"))
        if r.get("team") in exclude or not r.get("team_slug") or at is None or end is None:
            continue
        if end - 3600 <= at < end:
            seen.add((r["team_slug"], r.get("commit")))
    return len(seen)


def climbers(events, now, window_s=3600, top=3):
    """The teams that gained the most track places in new bests within window_s before now."""
    by = {}
    for e in events:
        at = _epoch(e.get("at"))
        if e.get("fail") or e.get("tk") not in TRACKS or not e.get("move") or e["move"] <= 0 or at is None or now - at > window_s:
            continue
        c = by.setdefault(e["slug"], {"slug": e["slug"], "team": e["team"], "climb": 0, "gain": 0.0, "tracks": []})
        c["climb"] += e["move"]
        c["gain"] = round(c["gain"] + (e.get("gain") or 0), 4)
        if e["tk"] not in c["tracks"]:
            c["tracks"].append(e["tk"])
    return sorted(by.values(), key=lambda c: (-c["climb"], -c["gain"]))[:top]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", help="board.json config, for stats_exclude (the reference baseline)")
    ap.add_argument("--keep", type=int, default=50)
    ap.add_argument("--freeze", default="2026-11-02T15:00:00Z", help="code freeze, for the final-hour push count")
    a = ap.parse_args()
    exclude = set()
    if a.config:
        with open(a.config) as f:
            exclude = set(json.load(f).get("stats_exclude") or [])
    rows = []
    with open(a.history) as f:
        for line in f:
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue               # a torn last line while the board appends
    crowns = {}
    events = replay(rows, exclude, keep=0, crowns=crowns)
    now = time.time()
    body = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), "events": events[-a.keep:][::-1],
            "climbers": climbers(events, now), "crowns": crown_summary(crowns, now),
            "final_pushes": final_pushes(rows, a.freeze, exclude)}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(a.out), prefix=".tape-")
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)


if __name__ == "__main__":
    main()

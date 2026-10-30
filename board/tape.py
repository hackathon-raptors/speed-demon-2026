"""The /board tape: the latest new bests, rank moves and failed runs, replayed from history.jsonl.

The browser only ever sees the current board, so it cannot tell how far a run moved a team. This
replays the whole private history in order, with the board's own rule for a best run (highest
points, then K), and writes the last events as a small public tape.json:

  {"generated_at": ..., "events": [newest first, at most --keep]}
  event = {"at", "slug", "team", "tk": "a".."d" | "all", "pts", "gain", "move", "rank"}  a new best
        | {"at", "slug", "team", "tk", "fail": "<gate>"}                                   a failed run

gain and move compare with the team's previous best and rank (null on a first score). An "all" event
follows a new best when it moved the team in Overall (geomean of its best three tracks). Only names,
tracks, points, ranks and gate names go out, all of which board.json already shows; log tails never do.
Run by speeddemon-tape.timer every 10 s, which uploads the file next to board.json.

    python3 board/tape.py --history /srv/speeddemon/board/state/history.jsonl \
        --out /srv/speeddemon/board/public/tape.json --config /etc/speeddemon/board.json
"""
import argparse
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


def replay(rows, exclude=(), keep=50):
    best = {}                         # slug -> {track: (points, K)}
    names, events = {}, []

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
    return events[-keep:][::-1]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--history", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--config", help="board.json config, for stats_exclude (the reference baseline)")
    ap.add_argument("--keep", type=int, default=50)
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
    body = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "events": replay(rows, exclude, a.keep)}
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(a.out), prefix=".tape-")
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, separators=(",", ":"))
    os.chmod(tmp, 0o644)
    os.replace(tmp, a.out)


if __name__ == "__main__":
    main()

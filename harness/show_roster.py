#!/usr/bin/env python3
"""Build the show's roster.json from the live board at freeze plus the organisers' rulings.

    show_roster.py --board board.at-freeze.json --rulings show_rulings.json --show-start 2026-11-03T15:00:00Z \
        --out roster.json [--size 20]

rulings: {"copied": {"<slug>": ["a", "b"]}, "drop": ["<slug>", ...]}
  copied: tracks built on another team's code, scored 0 (Overall = geomean of the best 3 tracks).
  drop:   entries removed outright (duplicates the registry exclude did not already remove).

The page's data contract: before (board top N), trims (who leaves and why), promotions (who fills
the gaps), run_order (the final N, slot N..1 is the order they run, with zeroed tracks).
"""
import argparse
import json


def overall(points):
    best = sorted((p for p in points.values() if p and p > 0), reverse=True)[:3]
    return round((best[0] * best[1] * best[2]) ** (1 / 3), 3) if len(best) == 3 else 0.0


def board_points(team):
    return {k: round(v["best"]["points"], 3) for k, v in sorted(team.get("tracks", {}).items())
            if v.get("best") and v["best"].get("ok")}


def build(board, rulings, show_start, size=20, private=()):
    copied = {k: sorted(v) for k, v in rulings.get("copied", {}).items()}
    drop = set(rulings.get("drop", [])) | set(private)
    private = set(private)
    rows = []
    for t in board["teams"]:
        pts = board_points(t)
        rows.append({"team": t["team"], "slug": t["slug"], "tracks": pts, "overall": overall(pts)})
    rows = [r for r in rows if r["overall"] > 0 and not r["team"].startswith("Reference")]
    rows.sort(key=lambda r: -r["overall"])
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    before = rows[:size]

    adjusted = []
    for r in rows:
        if r["slug"] in drop:
            continue
        zero = copied.get(r["slug"], [])
        pts = {k: (0.0 if k in zero else v) for k, v in r["tracks"].items()}
        adjusted.append({**r, "zeroed": zero, "adj": overall(pts)})
    adjusted.sort(key=lambda r: -r["adj"])
    final = [r for r in adjusted if r["adj"] > 0][:size]
    final_slugs = {r["slug"] for r in final}

    trims = []
    for r in before:
        if r["slug"] in final_slugs:
            continue
        zero = copied.get(r["slug"], [])
        if r["slug"] in private:
            trims.append({"team": r["team"], "slug": r["slug"], "reason": "Repo not public", "detail": "rule 08"})
        elif r["slug"] in drop:
            trims.append({"team": r["team"], "slug": r["slug"], "reason": "Duplicate entry", "detail": ""})
        elif zero:
            trims.append({"team": r["team"], "slug": r["slug"], "reason": "Copied code",
                          "detail": "tracks " + ", ".join(z.upper() for z in zero)})
        else:
            trims.append({"team": r["team"], "slug": r["slug"], "reason": "Overtaken", "detail": ""})
    before_slugs = {r["slug"] for r in before}
    promotions = [{"team": r["team"], "slug": r["slug"], "from_rank": r["rank"], "overall": r["adj"]}
                  for r in final if r["slug"] not in before_slugs]
    run_order = [{"slot": i + 1, "team": r["team"], "slug": r["slug"], "tracks": sorted(r["tracks"]),
                  "zeroed": r["zeroed"]} for i, r in enumerate(final)]
    return {"show_start": show_start,
            "before": [{k: r[k] for k in ("rank", "team", "slug", "overall", "tracks")} for r in before],
            "trims": trims, "promotions": promotions, "run_order": run_order}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--board", required=True)
    ap.add_argument("--rulings", required=True)
    ap.add_argument("--show-start", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--size", type=int, default=20)
    ap.add_argument("--public-check", default="", help="public_check.json: teams still private are dropped (rule 08)")
    a = ap.parse_args()
    private = []
    if a.public_check:
        private = [t["slug"] for t in json.load(open(a.public_check))["teams"] if not t["public"]]
    roster = build(json.load(open(a.board)), json.load(open(a.rulings)), a.show_start, a.size, private)
    with open(a.out, "w", encoding="utf-8") as f:
        json.dump(roster, f, indent=1)
    print(f"before {len(roster['before'])}, trims {len(roster['trims'])}, promotions {len(roster['promotions'])}")
    for r in roster["trims"]:
        print(f"  trim  {r['team']}: {r['reason']} {r['detail']}")
    for r in roster["run_order"]:
        print(f"  #{r['slot']:2} {r['team']}{'  zeroed ' + ','.join(r['zeroed']) if r['zeroed'] else ''}")


if __name__ == "__main__":
    main()

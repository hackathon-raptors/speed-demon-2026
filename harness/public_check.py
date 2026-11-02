"""Report-only check, run at 2026-11-02 16:00:00 UTC (rule 08: public within one hour of freeze).

For every team in freeze.json: is the repo publicly readable (anonymous GET of github.com/<repo>),
and does the frozen commit exist through the anonymous API? Writes public_check.json. Acts on nothing.

    python3 harness/public_check.py /srv/speeddemon/final/freeze.json /srv/speeddemon/final/public_check.json
"""
import json
import re
import sys
import time
import urllib.error
import urllib.request

UA = {"User-Agent": "speed-demon-public-check"}


def status(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method="HEAD"), timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:
        return f"error {type(e).__name__}"


def owner_repo(url):
    u = re.sub(r"^https?://github\.com/", "", url.strip()).rstrip("/")
    u = re.sub(r"/tree/.*$", "", u)
    return u[:-4] if u.endswith(".git") else u


def entries(freeze):
    teams = freeze.get("teams", freeze)
    items = teams.items() if isinstance(teams, dict) else ((t.get("slug") or t.get("team"), t) for t in teams)
    for slug, t in items:
        yield slug, t.get("name") or t.get("team") or slug, t.get("repo", ""), t.get("commit") or t.get("sha") or ""


def main(src, dst):
    freeze = json.load(open(src, encoding="utf-8"))
    out = []
    for slug, team, repo, sha in entries(freeze):
        r = owner_repo(repo)
        page = status(f"https://github.com/{r}")
        commit = status(f"https://github.com/{r}/commit/{sha}") if sha else None
        out.append({"slug": slug, "team": team, "repo": r, "commit": sha,
                    "public": page == 200, "page": page, "commit_status": commit})
        time.sleep(0.5)
    out.sort(key=lambda x: (x["public"], x["team"].lower()))
    with open(dst, "w", encoding="utf-8") as f:
        json.dump({"checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "teams": out}, f, indent=1)
    print(f"{sum(x['public'] for x in out)} of {len(out)} public; private: "
          + ", ".join(x["team"] for x in out if not x["public"]))


if __name__ == "__main__":
    main(*sys.argv[1:3])

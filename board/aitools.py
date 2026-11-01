"""Which AI coding tools and models the teams use, fingerprinted from their commits, for the /board panel.

Every registered repo is kept as a blobless clone and fetched incrementally through the Speed Demon
Board app. A team's tools come from three places: commit trailers and "Generated with" lines, bot
authors and committers, and agent config files committed on its speed-demon branch (CLAUDE.md,
AGENTS.md, .cursor/ ...). Chat copy-paste leaves no trace, so the counts are a lower bound.

The public file carries aggregates only, joined with each team's Overall from board.json:

  {"generated_at", "teams_scanned", "teams_detected",
   "tools":  [{"name", "teams", "share", "median_overall", "in_top10"}],
   "models": [{"name", "teams", "median_overall"}],
   "none":   {"teams", "median_overall"}}

No team names or slugs go out. There is no best score per tool either: Overall is public, so a best
would name its team. A tool or model seen in fewer than --min-teams teams (default 3) is folded into
"other" so no small group can be picked out. The per-team detail stays in --private on the box.
A repo that fails to fetch keeps its last result. Run by speeddemon-aitools.timer.

    python3 board/aitools.py --registry /srv/speeddemon/board/registry.json \
        --board /srv/speeddemon/board/public/board.json --config /etc/speeddemon/board.json \
        --clones /srv/speeddemon/board/aiscan --out /srv/speeddemon/board/public/aitools.json \
        --private /srv/speeddemon/board/state/aitools_private.json
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import statistics
import subprocess
import tempfile
import time

from tape import overall

REPO = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?(?:/tree/.*)?/?$")
# Checked in order against trailer lines and author/committer identities, never the free commit text.
TOOLS = [
    ("Claude Code", re.compile(r"anthropic\.com|\bclaude\b", re.I)),
    ("Codex", re.compile(r"\bcodex\b|openai\.com|chatgpt", re.I)),
    ("Cursor", re.compile(r"cursor", re.I)),
    ("Copilot", re.compile(r"copilot", re.I)),
    ("Gemini", re.compile(r"gemini|\bjules\b", re.I)),
    ("aider", re.compile(r"\baider\b", re.I)),
    ("Devin", re.compile(r"\bdevin\b", re.I)),
    ("OpenHands", re.compile(r"openhands|all-hands", re.I)),
    ("Amp", re.compile(r"ampcode\.com", re.I)),
    ("Windsurf", re.compile(r"windsurf|codeium", re.I)),
    ("Kiro", re.compile(r"\bkiro\b", re.I)),
    ("Qwen Code", re.compile(r"\bqwen\b", re.I)),
    ("opencode", re.compile(r"opencode", re.I)),
    ("Junie", re.compile(r"\bjunie\b", re.I)),
]
FILES = [
    ("Claude Code", re.compile(r"(^|/)(CLAUDE\.md$|\.claude/)")),
    ("Codex", re.compile(r"(^|/)(AGENTS\.md$|\.codex/)")),
    ("Cursor", re.compile(r"(^|/)(\.cursor/|\.cursorrules$)")),
    ("Gemini", re.compile(r"(^|/)(GEMINI\.md$|\.gemini/)")),
    ("Copilot", re.compile(r"(^|/)\.github/copilot-instructions\.md$")),
    ("aider", re.compile(r"(^|/)\.aider")),
    ("Windsurf", re.compile(r"(^|/)\.windsurf")),
    ("Kiro", re.compile(r"(^|/)\.kiro/")),
    ("Cline/Roo", re.compile(r"(^|/)(\.clinerules|\.roo/)")),
    ("Qwen Code", re.compile(r"(^|/)QWEN\.md$")),
    ("opencode", re.compile(r"(^|/)opencode\.json$")),
    ("Junie", re.compile(r"(^|/)\.junie/")),
]
TRAILER = re.compile(r"^\s*(co-authored-by:|.*generated with)", re.I)
CLAUDE_MODEL = re.compile(r"claude\s+([a-z]+\s*[\d.]*)\s*<", re.I)
AIDER_MODEL = re.compile(r"aider\s*\(([^)]+)\)", re.I)


def fingerprint(identities, body):
    """Tools and models in one commit: its author/committer identities plus its trailer lines."""
    lines = [l for l in body.splitlines() if TRAILER.match(l)] + list(identities)
    tools, models = set(), set()
    for line in lines:
        for name, rx in TOOLS:
            if rx.search(line):
                tools.add(name)
        if (m := CLAUDE_MODEL.search(line)) and m.group(1).strip() and m.group(1).split()[0].lower() != "code":
            models.add("Claude " + " ".join(w.capitalize() if w.isalpha() else w for w in m.group(1).split()))
        if m := AIDER_MODEL.search(line):
            models.add(m.group(1).strip())
    return tools, models


def file_tools(names):
    return {name for name, rx in FILES for n in names if rx.search(n)}


def team_result(commits, names):
    """commits: [(identities, body)]; names: files ever touched on the speed-demon branch."""
    per_tool, models = {}, set()
    for identities, body in commits:
        tools, ms = fingerprint(identities, body)
        models |= ms
        for t in tools:
            per_tool[t] = per_tool.get(t, 0) + 1
    files = file_tools(names)
    tools = set(per_tool) | files
    primary = max(per_tool, key=lambda t: (per_tool[t], t)) if per_tool else (min(files) if files else None)
    return {"tools": sorted(tools), "primary": primary, "models": sorted(models),
            "commits": len(commits), "ai_commits": per_tool}


def read_git(git_dir):
    log = subprocess.run(["git", "-C", git_dir, "log", "--remotes=o", "--format=%an <%ae>%x1f%cn <%ce>%x1f%B%x1e"],
                         capture_output=True, text=True, errors="replace").stdout
    commits = []
    for rec in log.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) == 3:
            commits.append(((parts[0], parts[1]), parts[2]))
    branch = "o/speed-demon"
    has = subprocess.run(["git", "-C", git_dir, "rev-parse", "-q", "--verify", branch], capture_output=True).returncode == 0
    names = subprocess.run(["git", "-C", git_dir, "log", branch if has else "--remotes=o", "--name-only", "--format="],
                           capture_output=True, text=True, errors="replace").stdout.splitlines()
    return commits, {n for n in names if n}


def scan_repo(entry, clones, token_for):
    m = REPO.match(str(entry.get("repo") or "").strip())
    if not m:
        return None
    owner, repo = m.group(1), m.group(2)
    token, _ = token_for(owner, repo) if token_for else ("", "")
    git_dir = os.path.join(clones, f"{owner}__{repo}".lower())
    if not os.path.isdir(git_dir):
        subprocess.run(["git", "init", "-q", "--bare", git_dir], check=True)
    url = f"https://x-access-token:{token}@github.com/{owner}/{repo}.git" if token else f"https://github.com/{owner}/{repo}.git"
    f = subprocess.run(["git", "-C", git_dir, "fetch", "-q", "--prune", "--no-tags", "--filter=blob:none", url,
                        "+refs/heads/*:refs/remotes/o/*"], capture_output=True, timeout=300,
                       env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
    if f.returncode:
        return None
    return team_result(*read_git(git_dir))


def overall_by_team(board, exclude=()):
    out = {}
    for t in board.get("teams") or []:
        if t.get("team") in exclude:
            continue
        bests = {k: (v["best"]["points"], v["best"].get("K") or 0) for k, v in (t.get("tracks") or {}).items()
                 if v and v.get("best") and v["best"].get("ok") and v["best"].get("points") is not None}
        out[t.get("team")] = overall(bests)
    return out


def aggregate(results, overall_of, min_teams=3):
    scored = sorted((o for o in overall_of.values() if o is not None), reverse=True)
    top10 = scored[9] if len(scored) >= 10 else (scored[-1] if scored else None)

    def stats(teams):
        os_ = [overall_of.get(t) for t in teams if overall_of.get(t) is not None]
        return {"teams": len(teams),
                "median_overall": round(statistics.median(os_), 2) if os_ else None,
                "in_top10": sum(1 for o in os_ if top10 is not None and o >= top10)}

    def table(key):
        by = {}
        for team, r in results.items():
            for name in r[key]:
                by.setdefault(name, []).append(team)
        rows, other = [], set()
        for name, teams in by.items():
            if len(teams) >= min_teams:
                rows.append({"name": name, **stats(teams)})
            else:
                other |= set(teams)
        rows.sort(key=lambda r: (-r["teams"], r["name"]))
        if len(other) >= min_teams:
            rows.append({"name": "other", **stats(sorted(other))})
        return rows

    n = len(results)
    tools = table("tools")
    for r in tools:
        r["share"] = round(r["teams"] / n, 3) if n else 0
    models = [{k: v for k, v in r.items() if k != "in_top10"} for r in table("models")]
    none = [t for t, r in results.items() if not r["tools"]]
    ns = stats(none)
    return {"teams_scanned": n, "teams_detected": n - len(none), "tools": tools, "models": models,
            "none": {"teams": ns["teams"], "median_overall": ns["median_overall"]}}


def write_json(path, body, mode=0o644):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".aitools-")
    with os.fdopen(fd, "w") as f:
        json.dump(body, f, separators=(",", ":"))
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--registry", required=True)
    ap.add_argument("--board", required=True, help="the public board.json, for each team's Overall")
    ap.add_argument("--config", help="board config, for github_app_id/key and stats_exclude")
    ap.add_argument("--clones", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--private", required=True)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--min-teams", type=int, default=3)
    a = ap.parse_args()
    cfg = json.load(open(a.config)) if a.config else {}
    exclude = set(cfg.get("stats_exclude") or [])
    token_for = None
    if cfg.get("github_app_id"):
        import github_app
        token_for = github_app.GitHubApp(cfg["github_app_id"], cfg["github_app_key"]).token_for
    os.makedirs(a.clones, exist_ok=True)
    registry = [e for e in json.load(open(a.registry)) if e.get("team") not in exclude]
    try:
        previous = json.load(open(a.private)).get("teams") or {}
    except (OSError, ValueError):
        previous = {}
    with cf.ThreadPoolExecutor(a.workers) as ex:
        found = dict(zip([e["team"] for e in registry], ex.map(lambda e: scan_repo(e, a.clones, token_for), registry)))
    results = {t: r if r is not None else previous.get(t) for t, r in found.items()}
    failed = sorted(t for t, r in found.items() if r is None)
    results = {t: r for t, r in results.items() if r is not None}
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    with open(a.board) as f:
        overall_of = overall_by_team(json.load(f), exclude)
    write_json(a.out, {"generated_at": now, **aggregate(results, overall_of, a.min_teams)})
    write_json(a.private, {"generated_at": now, "failed": failed, "teams": results}, mode=0o600)
    print(f"aitools: {len(results)} teams, {len(failed)} fetch failures")


if __name__ == "__main__":
    main()

"""Build the board's team registry from the Tally submission form.

The board runs only the repos in its registry (sources.load_registry). Teams register through
the submission form (Tally Ar7g9z: team name, repo, tracks), so this reads every completed
submission through the Tally API and writes the registry the board already polls; the board
reloads it on its next pass when the file changes.

Rules:
  - the latest submission per team wins (teams resubmit to fix a repo URL or tracks);
  - a submission the board would refuse (no name, not a GitHub URL, no tracks) is reported, not written;
  - --exclude names teams or submission ids to leave out (tests, disqualifications);
  - --extra merges organiser-added entries in the registry's own format, and they win over the form;
  - the file is replaced atomically and only when its content changes;
  - an empty or failed fetch never empties a registry that has teams in it.

Run by speeddemon-tally-sync.timer every minute. The API key is read from --key-file (mode 0600)
or $TALLY_API_KEY, never from argv.

    python3 board/tally_sync.py --form Ar7g9z --out /srv/speeddemon/board/registry.json \
        --key-file /etc/speeddemon/tally.key [--exclude F] [--extra F] [--dry-run]
"""
import argparse
import json
import os
import re
import sys
import tempfile
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from sources import TRACKS, normalise_repo, slugify  # noqa: E402

API = "https://api.tally.so"
# The form's questions, found by title so a re-ordered form still syncs.
Q_TEAM = "team name"
Q_REPO = "github repository"
Q_TRACKS = "tracks you are entering"
TRACK_RE = re.compile(r"^\s*(?:track[\s-]*)?([a-d])\b", re.IGNORECASE)


def fetch_submissions(form_id, key, timeout=30):
    """Every completed submission, all pages. Returns (questions, submissions)."""
    questions, subs, page = [], [], 1
    while True:
        qs = urllib.parse.urlencode({"page": page, "filter": "completed"})
        req = urllib.request.Request(f"{API}/forms/{form_id}/submissions?{qs}", headers={
            "Authorization": f"Bearer {key}",
            "User-Agent": "speeddemon-tally-sync/1",
            "Accept": "application/json",
        })
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        questions = data.get("questions") or questions
        subs.extend(data.get("submissions") or [])
        if not data.get("hasMore"):
            return questions, subs
        page += 1
        if page > 200:
            raise RuntimeError("more than 200 pages of submissions; refusing to continue")


def question_ids(questions):
    by_title = {str(q.get("title") or "").strip().lower(): q.get("id") for q in questions if not q.get("isDeleted")}
    missing = [t for t in (Q_TEAM, Q_REPO, Q_TRACKS) if t not in by_title]
    if missing:
        raise RuntimeError(f"form has no question titled {missing}; was it renamed?")
    return by_title[Q_TEAM], by_title[Q_REPO], by_title[Q_TRACKS]


def tracks_from(answer):
    """['A · Ingest', 'C · Join'] -> ['a', 'c']."""
    out = []
    for v in answer if isinstance(answer, list) else [answer]:
        m = TRACK_RE.match(str(v or ""))
        if m and m.group(1).lower() not in out:
            out.append(m.group(1).lower())
    return sorted(t for t in out if t in TRACKS)


def build_registry(questions, submissions, exclude=(), extra=()):
    """Returns (entries, problems). Latest submission per team slug wins; extras win over the form."""
    q_team, q_repo, q_tracks = question_ids(questions)
    excluded = {str(x).strip().lower() for x in exclude}
    latest, problems = {}, []
    for s in sorted(submissions, key=lambda s: (s.get("submittedAt") or "", s.get("id") or "")):
        if not s.get("isCompleted", True):
            continue
        ans = {r.get("questionId"): r.get("answer") for r in s.get("responses") or []}
        team = str(ans.get(q_team) or "").strip()
        sid = s.get("id") or "?"
        if not team:
            problems.append(f"submission {sid}: no team name")
            continue
        if team.lower() in excluded or sid.lower() in excluded:
            continue
        repo = str(ans.get(q_repo) or "").strip()
        _, err = normalise_repo(repo)
        if err:
            problems.append(f"{team} ({sid}): {err}")
            continue
        tracks = tracks_from(ans.get(q_tracks))
        if not tracks:
            problems.append(f"{team} ({sid}): no tracks ticked")
            continue
        latest[slugify(team)] = {"team": team, "repo": repo, "tracks": tracks, "submission": sid,
                                 "submitted_at": s.get("submittedAt")}
    for e in extra:
        if isinstance(e, dict) and str(e.get("team") or "").strip():
            latest[slugify(str(e["team"]).strip())] = e
    entries = sorted(latest.values(), key=lambda e: slugify(e["team"]))
    return entries, problems


def write_if_changed(path, entries):
    """Atomic replace, only when the content differs. Returns True when it wrote."""
    body = json.dumps(entries, indent=1, ensure_ascii=False) + "\n"
    try:
        with open(path, encoding="utf-8") as f:
            if f.read() == body:
                return False
    except FileNotFoundError:
        pass
    d = os.path.dirname(os.path.abspath(path))
    fd, tmp = tempfile.mkstemp(prefix=".registry-", dir=d)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return True


def existing_count(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return len(data.get("teams", []) if isinstance(data, dict) else data)
    except (FileNotFoundError, ValueError):
        return 0


def read_list(path):
    if not path:
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data if isinstance(data, list) else []


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--form", default="Ar7g9z")
    ap.add_argument("--out", required=True)
    ap.add_argument("--key-file")
    ap.add_argument("--exclude", help="JSON list of team names or submission ids to leave out")
    ap.add_argument("--extra", help="JSON list of registry entries to add or override")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    key = os.environ.get("TALLY_API_KEY", "")
    if a.key_file:
        with open(a.key_file, encoding="utf-8") as f:
            key = f.read().strip()
    if not key:
        print("tally-sync: no API key (--key-file or $TALLY_API_KEY)", file=sys.stderr)
        return 2
    try:
        questions, subs = fetch_submissions(a.form, key)
        entries, problems = build_registry(questions, subs, read_list(a.exclude), read_list(a.extra))
    except Exception as exc:  # a Tally outage must never touch the registry
        print(f"tally-sync: fetch failed, registry left as is: {exc}", file=sys.stderr)
        return 1
    for p in problems:
        print(f"tally-sync: skipped {p}", file=sys.stderr)
    if not entries and existing_count(a.out):
        print("tally-sync: the form returned no teams but the registry has some; left as is", file=sys.stderr)
        return 1
    if a.dry_run:
        print(json.dumps(entries, indent=1, ensure_ascii=False))
        return 0
    wrote = write_if_changed(a.out, entries)
    print(f"tally-sync: {len(entries)} teams from {len(subs)} submissions, "
          f"{len(problems)} skipped, {'registry updated' if wrote else 'no change'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

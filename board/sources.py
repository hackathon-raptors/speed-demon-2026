"""Where the board's code comes from: the team registry and the teams' git repos.

The registry is the organiser's export of the Tally form:
    [{"team": "...", "repo": "https://github.com/owner/repo", "tracks": ["a", "c"], "discord": "..."}]

Repos may be private. The board reads them through the Speed Demon Board GitHub App
(github_app.py): one short-lived, read-only token per repo, handed to git as an HTTP
header through git's environment config, never on a command line and never into
.git/config. A static token (BOT_TOKEN) still works for local tests.
"""
import base64
import dataclasses
import hashlib
import json
import os
import re
import subprocess
import unicodedata

TRACKS = ("a", "b", "c", "d")
GITHUB_RE = re.compile(
    r"^(?:https?://)?(?:www\.)?github\.com/(?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/"
    r"(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?(?:/(?:tree|blob|commits?)/.*)?/?$", re.IGNORECASE)
GITHUB_SSH_RE = re.compile(
    r"^(?:ssh://)?git@github\.com[:/](?P<owner>[A-Za-z0-9][A-Za-z0-9-]{0,38})/(?P<repo>[A-Za-z0-9._-]+?)(?:\.git)?/?$",
    re.IGNORECASE)
SHA_RE = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
NAME_MAX_CHARS = 64
TOKEN_PATTERNS = [re.compile(r"gh[pousr]_[A-Za-z0-9]{20,255}"), re.compile(r"github_pat_[A-Za-z0-9_]{20,255}")]


@dataclasses.dataclass
class Team:
    name: str
    slug: str
    repo: str           # normalised clone URL
    tracks: list
    discord: str = ""


def clean_name(name):
    """A team name safe to show anywhere: no control, bidi or zero-width characters, one space
    between words, at most NAME_MAX_CHARS. Lookalike and right-to-left tricks stop here."""
    kept = "".join(ch for ch in str(name or "") if unicodedata.category(ch) not in ("Cc", "Cf", "Co", "Cs", "Zl", "Zp"))
    return re.sub(r"\s+", " ", kept).strip()[:NAME_MAX_CHARS].strip()


def repo_owner(url):
    """The GitHub owner of a normalised clone URL, casefolded; "" for anything else."""
    m = re.match(r"^https://github\.com/([^/]+)/", url or "")
    return m.group(1).casefold() if m else ""


def slugify(name):
    """A docker- and path-safe id that stays unique even for team names with no ASCII letters."""
    folded = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    base = re.sub(r"[^a-z0-9]+", "-", folded.lower()).strip("-")[:32].strip("-") or "team"
    digest = hashlib.sha256(name.strip().casefold().encode("utf-8")).hexdigest()[:6]
    return f"{base}-{digest}"


def normalise_repo(url, allow_local=False):
    """Return (clone_url, error). GitHub URLs in any common shape become https://github.com/o/r.git."""
    url = (url or "").strip()
    for rx in (GITHUB_RE, GITHUB_SSH_RE):
        m = rx.match(url)
        if m and m.group("repo") not in (".", ".."):
            return f"https://github.com/{m.group('owner')}/{m.group('repo')}.git", ""
    if allow_local:
        path = url[len("file://"):] if url.startswith("file://") else url
        if os.path.isabs(path) and os.path.isdir(path):
            return path, ""
    return "", f"not a GitHub repository URL: {url!r}"


def normalise_tracks(values):
    tracks, bad = [], []
    for v in values or []:
        t = str(v).strip().lower()
        t = t[len("track-"):] if t.startswith("track-") else t
        if t in TRACKS and t not in tracks:
            tracks.append(t)
        elif t not in TRACKS:
            bad.append(str(v))
    return sorted(tracks), bad


def load_registry(path, allow_local=False):
    """Read the registry export. Returns (teams, problems); a bad entry is skipped, not fatal."""
    with open(path, encoding="utf-8") as f:
        entries = json.load(f)
    if isinstance(entries, dict):
        entries = entries.get("teams", [])
    teams, problems, seen = [], [], set()
    for i, e in enumerate(entries):
        if not isinstance(e, dict):
            problems.append(f"entry {i}: not an object")
            continue
        name = clean_name(e.get("team"))
        if not name:
            problems.append(f"entry {i}: no team name")
            continue
        repo, err = normalise_repo(str(e.get("repo") or ""), allow_local)
        if err:
            problems.append(f"{name}: {err}")
            continue
        tracks, bad = normalise_tracks(e.get("tracks"))
        if bad:
            problems.append(f"{name}: unknown tracks {bad}")
        slug = slugify(name)
        if slug in seen:
            problems.append(f"{name}: duplicate team name, later entry ignored")
            continue
        seen.add(slug)
        teams.append(Team(name=name, slug=slug, repo=repo, tracks=tracks, discord=str(e.get("discord") or "")))
    return teams, problems


# -- git ----------------------------------------------------------------------

class Git:
    """git with credentials in the environment, never in argv or on disk.

    token_provider(owner, repo) -> (token, error) supplies a per-repo token (the GitHub
    App); without one, the static token is used for every github.com request.
    """

    def __init__(self, token="", user="x-access-token", ls_remote_timeout_s=30, clone_timeout_s=300,
                 token_provider=None):
        self.token = token or ""
        self.user = user
        self.ls_remote_timeout_s = ls_remote_timeout_s
        self.clone_timeout_s = clone_timeout_s
        self.token_provider = token_provider
        self._secrets = set()
        self._basic = self._remember(self.token)

    def _remember(self, token):
        """Note a token and its header form so redact() can always hide both."""
        if not token:
            return ""
        basic = base64.b64encode(f"{self.user}:{token}".encode()).decode()
        self._secrets.update({token, basic})
        return basic

    def _auth_for(self, url):
        """(basic, error) for one request: the app's token for that repo, or the static token."""
        if self.token_provider and url.startswith("https://github.com/"):
            owner, _, repo = url[len("https://github.com/"):].removesuffix(".git").partition("/")
            token, err = self.token_provider(owner, repo)
            return (self._remember(token), "") if token else ("", err)
        return self._basic, ""

    def env(self, basic=None):
        basic = self._basic if basic is None else basic
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k != "BOT_TOKEN"}
        env.update(GIT_TERMINAL_PROMPT="0", GIT_LFS_SKIP_SMUDGE="1", GCM_INTERACTIVE="never",
                   GIT_ASKPASS="/bin/false", SSH_ASKPASS="/bin/false")
        config = [
            ("credential.helper", ""),           # no keychain or stored credentials: only the bot's token
            ("protocol.allow", "never"),
            ("protocol.https.allow", "always"),
            ("protocol.file.allow", "always"),   # local repos only pass normalise_repo when the config allows them
            ("core.hooksPath", "/dev/null"),
        ]
        if basic:
            config.append(("http.https://github.com/.extraHeader", f"Authorization: Basic {basic}"))
        env["GIT_CONFIG_COUNT"] = str(len(config))
        for i, (k, v) in enumerate(config):
            env[f"GIT_CONFIG_KEY_{i}"] = k
            env[f"GIT_CONFIG_VALUE_{i}"] = v
        return env

    def redact(self, text):
        text = text or ""
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for rx in TOKEN_PATTERNS:
            text = rx.sub("***", text)
        return text

    def _explain(self, stderr):
        s = self.redact(stderr).strip()
        low = s.lower()
        if "repository not found" in low or "authentication failed" in low or "could not read username" in low \
                or "403" in low or "401" in low:
            return ("repository not found, or not readable by the Speed Demon Board app "
                    "(install it on the repo: https://github.com/apps/speed-demon-board)")
        lines = [line for line in s.splitlines() if line.strip()]
        return (lines[-1] if lines else "git failed")[:300]

    def ls_remote(self, url, branch):
        """Return (sha, error). (\"\", \"\") means the repo is readable but has no such branch."""
        basic, err = self._auth_for(url)
        if err:
            return "", err
        try:
            proc = subprocess.run(["git", "ls-remote", "--exit-code", url, f"refs/heads/{branch}"],
                                  env=self.env(basic), capture_output=True, text=True, timeout=self.ls_remote_timeout_s)
        except subprocess.TimeoutExpired:
            return "", f"git ls-remote timed out after {self.ls_remote_timeout_s} s"
        if proc.returncode == 2:
            return "", ""
        if proc.returncode != 0:
            return "", self._explain(proc.stderr)
        for line in proc.stdout.splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{branch}" and SHA_RE.match(parts[0]):
                return parts[0], ""
        return "", "unexpected git ls-remote output"

    def clone(self, url, branch, dest, register=None):
        """Shallow-clone the branch tip into dest. Returns (sha, error).

        register(proc) lets the disk watchdog kill a clone that is filling the disk.
        """
        basic, err = self._auth_for(url)
        if err:
            return "", err
        cmd = ["git", "clone", "--quiet", "--depth", "1", "--single-branch", "--branch", branch,
               "--no-tags", "--no-recurse-submodules", url, dest]
        proc = subprocess.Popen(cmd, env=self.env(basic), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        if register:
            register(proc)
        try:
            _, stderr = proc.communicate(timeout=self.clone_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "", f"git clone timed out after {self.clone_timeout_s} s"
        if proc.returncode != 0:
            return "", self._explain(stderr)
        head = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], env=self.env(),
                              capture_output=True, text=True, timeout=30)
        sha = head.stdout.strip()
        if head.returncode != 0 or not SHA_RE.match(sha):
            return "", "could not read the cloned commit"
        return sha, ""

    def clone_commit(self, url, sha, dest, branch="speed-demon", register=None):
        """Clone and check out an EXACT commit (the frozen submission), not the branch tip. Returns (sha, error).

        Final scoring must score what was frozen, so the live tip is wrong: a team can push after freeze.
        Full-history clone (no --depth) so the frozen commit is reachable even if the tip has moved since; if
        it is not present (history rewritten), fetch that one commit explicitly (GitHub serves a ref-reachable
        sha). A commit the server will not serve flags the team for manual review rather than scoring the wrong code.
        """
        basic, err = self._auth_for(url)
        if err:
            return "", err
        if not SHA_RE.match(sha or ""):
            return "", f"not a commit sha: {sha!r}"
        cmd = ["git", "clone", "--quiet", "--no-checkout", "--no-tags", "--no-recurse-submodules"]
        if branch:
            cmd += ["--single-branch", "--branch", branch]
        cmd += [url, dest]
        proc = subprocess.Popen(cmd, env=self.env(basic), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        if register:
            register(proc)
        try:
            _, stderr = proc.communicate(timeout=self.clone_timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return "", f"git clone timed out after {self.clone_timeout_s} s"
        if proc.returncode != 0:
            return "", self._explain(stderr)
        present = subprocess.run(["git", "-C", dest, "cat-file", "-e", f"{sha}^{{commit}}"], env=self.env(),
                                 capture_output=True, text=True, timeout=30)
        if present.returncode != 0:                         # tip moved past it / history rewritten: fetch the one commit
            fetch = subprocess.run(["git", "-C", dest, "fetch", "--quiet", "--no-tags", "origin", sha],
                                   env=self.env(basic), capture_output=True, text=True, timeout=self.clone_timeout_s)
            if fetch.returncode != 0:
                return "", f"frozen commit {sha[:12]} not available in the repo ({self._explain(fetch.stderr)})"
        co = subprocess.run(["git", "-C", dest, "checkout", "--quiet", "--detach", sha], env=self.env(),
                            capture_output=True, text=True, timeout=60)
        if co.returncode != 0:
            return "", f"could not check out {sha[:12]}: {self._explain(co.stderr)}"
        landed = subprocess.run(["git", "-C", dest, "rev-parse", "HEAD"], env=self.env(),
                                capture_output=True, text=True, timeout=30).stdout.strip()
        if landed != sha:
            return "", f"checked-out HEAD {landed[:12]} != frozen {sha[:12]}"
        return landed, ""

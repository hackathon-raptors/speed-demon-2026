"""Read access to team repos through the Speed Demon Board GitHub App.

Teams install https://github.com/apps/speed-demon-board on their repo, which grants
Contents: read on that repo and nothing else. For each repo the board asks GitHub
for an installation token limited to that one repository and read-only contents.
Tokens last an hour and are cached until five minutes before they expire. The app's
private key is signed with openssl, so the board needs no extra Python packages.
"""
import base64
import calendar
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
NOT_INSTALLED = ("the Speed Demon Board app is not installed on this repository "
                 "(install it at https://github.com/apps/speed-demon-board)")


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=")


class GitHubApp:
    def __init__(self, app_id, key_path, api=API, timeout_s=30):
        self.app_id = str(app_id)
        self.key_path = key_path
        self.api = api
        self.timeout_s = timeout_s
        self._tokens = {}          # (owner, repo) -> (token, expires_at epoch)
        self._lock = threading.Lock()

    def jwt(self):
        now = int(time.time())
        header = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}).encode())
        claims = _b64url(json.dumps({"iat": now - 60, "exp": now + 540, "iss": self.app_id}).encode())
        signing_input = header + b"." + claims
        signature = subprocess.run(["openssl", "dgst", "-sha256", "-sign", self.key_path],
                                   input=signing_input, capture_output=True, check=True).stdout
        return (signing_input + b"." + _b64url(signature)).decode()

    def _call(self, method, path, body=None):
        req = urllib.request.Request(
            f"{self.api}{path}", method=method, data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self.jwt()}", "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
            return json.load(resp)

    def token_for(self, owner, repo):
        """Return (token, error) for read-only access to owner/repo."""
        key = (owner.lower(), repo.lower())
        with self._lock:
            cached = self._tokens.get(key)
            if cached and cached[1] - time.time() > 300:
                return cached[0], ""
        try:
            installation = self._call("GET", f"/repos/{owner}/{repo}/installation")
            grant = self._call("POST", f"/app/installations/{installation['id']}/access_tokens",
                               {"repositories": [repo], "permissions": {"contents": "read"}})
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return "", NOT_INSTALLED
            return "", f"GitHub API returned {e.code} while authorising the app for this repository"
        except (urllib.error.URLError, OSError, subprocess.CalledProcessError, KeyError, ValueError) as e:
            return "", f"could not authorise the app for this repository: {type(e).__name__}"
        expires = calendar.timegm(time.strptime(grant["expires_at"], "%Y-%m-%dT%H:%M:%SZ"))
        with self._lock:
            self._tokens[key] = (grant["token"], expires)
        return grant["token"], ""

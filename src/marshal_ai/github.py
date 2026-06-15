"""Minimal GitHub REST client for Marshal: identify, list repos, read files, push changes.

Auth is a user-supplied fine-grained personal access token (Contents: read and write). The
token is held by the server and stored locally (gitignored); it is never returned to the
browser or committed. Uses only the standard library so there is no extra dependency.
"""
from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"


class GitHubError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"GitHub {code}: {message}")


def _req(method: str, path: str, token: str, body: dict | None = None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API + path, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "Marshal")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            txt = resp.read().decode()
            return json.loads(txt) if txt else {}
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = json.loads(exc.read().decode()).get("message", "")
        except Exception:
            pass
        raise GitHubError(exc.code, detail or exc.reason)
    except urllib.error.URLError as exc:
        raise GitHubError(0, str(exc.reason))


def _quote(path: str) -> str:
    return urllib.parse.quote(path.lstrip("/"))


def whoami(token: str) -> dict:
    u = _req("GET", "/user", token)
    return {"login": u.get("login"), "name": u.get("name")}


def list_repos(token: str, limit: int = 100) -> list[dict]:
    repos = _req("GET", f"/user/repos?per_page={limit}&sort=updated&affiliation=owner,collaborator", token)
    out = []
    for r in repos if isinstance(repos, list) else []:
        out.append({
            "full_name": r.get("full_name"),
            "default_branch": r.get("default_branch"),
            "private": bool(r.get("private")),
            "can_push": bool((r.get("permissions") or {}).get("push")),
        })
    return out


def get_tree(token: str, repo: str, ref: str | None = None) -> dict:
    if not ref:
        ref = _req("GET", f"/repos/{repo}", token).get("default_branch", "main")
    t = _req("GET", f"/repos/{repo}/git/trees/{ref}?recursive=1", token)
    files = [n["path"] for n in (t.get("tree") or []) if n.get("type") == "blob"]
    return {"ref": ref, "files": files, "truncated": bool(t.get("truncated"))}


def get_file(token: str, repo: str, path: str, ref: str | None = None) -> dict:
    p = f"/repos/{repo}/contents/{_quote(path)}"
    if ref:
        p += f"?ref={ref}"
    r = _req("GET", p, token)
    if r.get("encoding") == "base64":
        content = base64.b64decode(r.get("content", "")).decode("utf-8", "replace")
    else:
        content = r.get("content", "")
    return {"path": path, "content": content, "sha": r.get("sha")}


def push_files(token: str, repo: str, files: list[dict], message: str,
               branch: str | None = None, base: str | None = None) -> dict:
    """Write files onto a (new or existing) branch and return a PR-ready compare URL.

    files: [{"path": ..., "content": ...}]. Creates `branch` off `base` (default branch if
    omitted), then creates/updates each file on it via the Contents API.
    """
    default = _req("GET", f"/repos/{repo}", token).get("default_branch", "main")
    base = base or default
    branch = branch or "marshal/update"
    base_sha = _req("GET", f"/repos/{repo}/git/ref/heads/{urllib.parse.quote(base)}", token)["object"]["sha"]
    try:
        _req("POST", f"/repos/{repo}/git/refs", token, {"ref": f"refs/heads/{branch}", "sha": base_sha})
    except GitHubError as exc:
        if exc.code != 422:  # 422 = the branch already exists, which is fine
            raise
    written = []
    for f in files:
        path = f.get("path")
        if not path:
            continue
        sha = None
        try:
            sha = _req("GET", f"/repos/{repo}/contents/{_quote(path)}?ref={urllib.parse.quote(branch)}", token).get("sha")
        except GitHubError:
            pass
        body = {
            "message": message,
            "content": base64.b64encode((f.get("content") or "").encode()).decode(),
            "branch": branch,
        }
        if sha:
            body["sha"] = sha
        _req("PUT", f"/repos/{repo}/contents/{_quote(path)}", token, body)
        written.append(path)
    return {
        "branch": branch,
        "base": base,
        "written": written,
        "compare_url": f"https://github.com/{repo}/compare/{base}...{branch}?expand=1",
    }

"""quorum_github.py — the only module that talks to GitHub.

Everything goes through an injectable `Client`, so the ledger and priors tools
(and their tests) choose the transport: `GhClient` shells out to `gh api`,
`TokenClient` calls the REST/GraphQL API directly with a token, and tests pass
a fake. The functions below build the endpoints and nothing else; rendering,
gate logic and argument parsing live elsewhere.

`Client.api(method, path, body=None, params=None)`: `path` is relative to the
API root (`repos/OWNER/REPO/...`); `params` holds query parameters, plus the
special key `paginate: True`, which makes the client follow pagination and
return the concatenated list. Clients raise `RuntimeError` on a failed call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Protocol

API_ROOT = "https://api.github.com/"
PAGE_SIZE = 100

# GitHub's `author_association` values that vouch for a comment's author.
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}

MINIMIZE_MUTATION = (
    "mutation($id: ID!) { minimizeComment(input: {subjectId: $id, "
    "classifier: DUPLICATE}) { minimizedComment { isMinimized } } }"
)


class Client(Protocol):
    def api(self, method: str, path: str, body: dict | None = None,
            params: dict | None = None) -> Any: ...

    def graphql(self, query: str, variables: dict) -> Any: ...


def _split_params(params):
    """(paginate, query params) from a Client `params` dict."""
    rest = dict(params or {})
    return bool(rest.pop("paginate", False)), rest


def _decode_stream(text):
    """Every JSON value in `text`, however the values are separated or indented."""
    decoder = json.JSONDecoder()
    values, i = [], 0
    while True:
        while i < len(text) and text[i].isspace():
            i += 1
        if i >= len(text):
            return values
        value, i = decoder.raw_decode(text, i)
        values.append(value)


class GhClient:
    """Transport over the `gh` CLI (`gh api`)."""

    def _run(self, args, payload=None):
        try:
            res = subprocess.run(
                ["gh", *args],
                check=True, capture_output=True, text=True,
                input=payload,
            )
        except subprocess.CalledProcessError as e:
            detail = (e.stderr or "").strip() or f"exit status {e.returncode}"
            raise RuntimeError(f"gh {args[0]} failed: {detail}") from e
        return res.stdout

    def api(self, method, path, body=None, params=None):
        paginate, query = _split_params(params)
        if query:
            path = f"{path}?{urllib.parse.urlencode(query)}"
        args = ["api", "-X", method, path]
        if paginate:
            args += ["--paginate", "--jq", ".[]"]
        payload = None
        if body is not None:
            args += ["--input", "-"]
            payload = json.dumps(body)
        out = self._run(args, payload)
        if paginate:
            return _decode_stream(out)
        return json.loads(out) if out.strip() else None

    def graphql(self, query, variables):
        args = ["api", "graphql", "-f", f"query={query}"]
        for key, value in variables.items():
            if isinstance(value, str):
                args += ["-f", f"{key}={value}"]
            else:  # -F types the value: true/false, numbers
                args += ["-F", f"{key}={json.dumps(value)}"]
        out = self._run(args)
        return json.loads(out) if out.strip() else None


class TokenClient:
    """Transport over urllib with a bearer token, for hosts without `gh`."""

    def __init__(self, token):
        self.token = token

    def _request(self, method, url, body=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            raise RuntimeError(f"GitHub API {method} {url} failed: HTTP {e.code} {text}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"GitHub API {method} {url} unreachable: {e.reason}") from e
        return json.loads(raw) if raw.strip() else None

    def _url(self, path, query):
        url = API_ROOT + path.lstrip("/")
        return f"{url}?{urllib.parse.urlencode(query)}" if query else url

    def api(self, method, path, body=None, params=None):
        paginate, query = _split_params(params)
        if not paginate:
            return self._request(method, self._url(path, query), body)
        items, page = [], 1
        while True:
            paged = {**query, "per_page": PAGE_SIZE, "page": page}
            chunk = self._request(method, self._url(path, paged), body) or []
            items.extend(chunk)
            if len(chunk) < PAGE_SIZE:
                return items
            page += 1

    def graphql(self, query, variables):
        result = self._request("POST", API_ROOT + "graphql",
                               {"query": query, "variables": variables})
        if result and result.get("errors"):
            raise RuntimeError(f"GitHub GraphQL failed: {json.dumps(result['errors'])}")
        return result


def default_client():
    """`gh` when it is installed, else a token from GITHUB_TOKEN / GH_TOKEN."""
    if shutil.which("gh"):
        return GhClient()
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token:
        return TokenClient(token)
    raise RuntimeError("No gh CLI and no GITHUB_TOKEN")


def get_pr_head(client, repo, pr):
    return client.api("GET", f"repos/{repo}/pulls/{pr}")["head"]["sha"]


def get_authenticated_login(client):
    """Login of the account the client authenticates as, or None when unknown.

    Best-effort: `GET user` is refused for some tokens (for example a GitHub
    App installation token), and that must not stop the lookup that needs it.
    """
    try:
        login = client.api("GET", "user")["login"]
    except Exception:
        return None
    return login if isinstance(login, str) and login else None


def _is_trusted(comment, login):
    """Written by the authenticated account `login`, or by a repository insider."""
    if comment.get("author_association") in TRUSTED_ASSOCIATIONS:
        return True
    author = (comment.get("user") or {}).get("login")
    return login is not None and author == login


def find_marker_comment(client, repo, pr, marker, login=None):
    """The newest trusted issue comment whose body contains `marker`, as {id, body}.

    Anyone can comment on a public pull request, so a marker alone proves
    nothing: an outsider could post a forged ledger carrying fabricated
    dispositions, which the next run would inherit and then overwrite. A
    comment is trusted only when it was written by the account this client
    authenticates as (`GET user`, best-effort, asked at most once per lookup),
    or when GitHub reports its author as an OWNER, MEMBER or COLLABORATOR of
    the repository (`author_association`, compared exactly). Every other
    marker comment is ignored.

    A caller that looks up many pull requests asks `get_authenticated_login`
    once and passes the answer as `login`, which is then used instead of
    asking again.
    """
    comments = client.api("GET", f"repos/{repo}/issues/{pr}/comments",
                          params={"paginate": True}) or []
    hits = [c for c in comments if marker in (c.get("body") or "")]
    if login is None and hits:
        login = get_authenticated_login(client)
    trusted = [c for c in hits if _is_trusted(c, login)]
    if not trusted:
        return None
    newest = max(trusted, key=lambda c: c["id"])
    return {"id": newest["id"], "body": newest["body"]}


def upsert_comment(client, repo, pr, body, existing_id=None):
    """Update comment `existing_id` in place, or post a new one; returns its id."""
    if existing_id is not None:
        client.api("PATCH", f"repos/{repo}/issues/comments/{existing_id}", {"body": body})
        return existing_id
    created = client.api("POST", f"repos/{repo}/issues/{pr}/comments", {"body": body})
    return created["id"]


def _succeeded(call):
    """Best-effort side effects: True when `call` ran, False when it raised."""
    try:
        call()
    except Exception:
        return False
    return True


def add_eyes(client, repo, comment_id):
    """React with eyes on a pull-request review comment (best-effort)."""
    return _succeeded(lambda: client.api(
        "POST", f"repos/{repo}/pulls/comments/{comment_id}/reactions", {"content": "eyes"}))


def minimize(client, node_id):
    """Collapse a comment as DUPLICATE (best-effort)."""
    return _succeeded(lambda: client.graphql(MINIMIZE_MUTATION, {"id": node_id}))


def compare_files(client, repo, base, head):
    """Filenames changed between two commits, or None when the compare fails."""
    try:
        result = client.api("GET", f"repos/{repo}/compare/{base}...{head}")
        return [f["filename"] for f in result["files"]]
    except Exception:
        return None


def reply_in_thread(client, repo, pr, comment_id, body):
    """Reply to a pull-request review comment's thread (best-effort)."""
    return _succeeded(lambda: client.api(
        "POST", f"repos/{repo}/pulls/{pr}/comments/{comment_id}/replies", {"body": body}))

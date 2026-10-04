"""GitHub adapter (REST, read-only) using the existing ``gh`` login.

Tracked repositories (``github.repos``): issue/PR openings, comments, reviews, review
comments and state events (assigned, review_requested, closed, merged, reopened ...).
Commits and pushes are NOT conversation items: a PR whose ``updated_at`` moved only
because of new commits yields nothing here; commit activity goes to the daily digest.

Outside tracked repos, only threads where the user participates (GitHub notifications
with reasons such as review_requested / assign / mention / author) become items.

Every item is keyed ``github:owner/repo#number`` so mail notifications about the same
issue/PR land in the same matter instead of creating a second task.
Reading notifications never marks them as read.
"""

from __future__ import annotations

import json
import subprocess
from datetime import timedelta
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://api.github.com"
STATE_EVENTS = {"assigned", "unassigned", "review_requested", "review_request_removed", "closed", "reopened",
                "merged", "converted_to_draft", "ready_for_review", "renamed"}
PARTICIPATING_REASONS = {"review_requested", "assign", "mention", "author", "comment", "team_mention",
                         "state_change", "manual"}


def gh_token() -> str:
    try:
        out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"gh auth token failed: {type(exc).__name__}") from None
    if out.returncode != 0 or not out.stdout.strip():
        raise SourceError("gh auth token failed")
    return out.stdout.strip()


class GitHubClient:
    def __init__(self, token: Callable[[], str] = gh_token, http: Callable[..., Any] = request_json):
        self._token_fn = token
        self._token: str | None = None
        self.http = http

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        if self._token is None:
            self._token = self._token_fn()
        try:
            return self.http("GET", f"{API}/{path.lstrip('/')}", params=params, timeout=30,
                             headers={"Authorization": f"Bearer {self._token}",
                                      "Accept": "application/vnd.github+json",
                                      "X-GitHub-Api-Version": "2022-11-28"})
        except HttpError as exc:
            raise SourceError(str(exc), retry_after=exc.retry_after) from None
        except HttpTimeout as exc:
            raise SourceError(str(exc)) from None

    def paged(self, path: str, params: dict[str, Any] | None = None, max_pages: int = 50) -> list[Any]:
        results: list[Any] = []
        params = dict(params or {})
        params.setdefault("per_page", 100)
        for page in range(1, max_pages + 1):
            params["page"] = page
            data = self.get(path, params)
            if not isinstance(data, list):
                raise SourceError(f"unexpected response for {path}")
            results.extend(data)
            if len(data) < params["per_page"]:
                return results
        raise SourceError(f"pagination limit reached for {path}; refusing to truncate silently")


def _since(cursor: str | None, lookback_days: int) -> str:
    if cursor:
        return cursor
    return clock.iso(clock.now() - timedelta(days=lookback_days)).replace(".000Z", "Z")


def _gh_time(value: str) -> str:
    parsed = clock.parse(value)
    return clock.iso(parsed) if parsed else value


class GitHubRepoSource:
    """All conversation activity of one tracked repository."""

    name = "github"

    def __init__(self, client: GitHubClient, repo: str, my_logins: list[str], bot_logins: list[str],
                 overlap_minutes: int = 30, lookback_days: int = 14):
        self.client = client
        self.repo = repo
        self.me = {m.lower() for m in my_logins}
        self.bots = {b.lower() for b in bot_logins}
        self.overlap = overlap_minutes
        self.lookback_days = lookback_days

    @property
    def scope(self) -> str:
        return f"repo:{self.repo.lower()}"

    def _item(self, number: int, external_id: str, kind: str, user: dict[str, Any] | None, at: str, body: str,
              title: str, url: str, version: str = "", meta: dict[str, Any] | None = None) -> Item:
        login = (user or {}).get("login", "") or ""
        is_bot = login.lower() in self.bots or (user or {}).get("type") == "Bot"
        key = f"github:{self.repo.lower()}#{number}"
        return Item(source="github", account=self.repo.lower(), external_id=external_id, version=version, kind=kind,
                    conversation_key=key, thread_key=key, occurred_at=_gh_time(at), author=login,
                    author_is_me=login.lower() in self.me, title=title, url=url, body=body or "",
                    route="context" if is_bot else "batch", meta={"number": number, **(meta or {})})

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        started = clock.now()
        since = _since(cursor, self.lookback_days)
        since_dt = clock.parse(since)
        items: list[Item] = []
        issues = self.client.paged(f"repos/{self.repo}/issues",
                                   {"since": since, "state": "all", "sort": "updated", "direction": "asc"})
        for issue in issues:
            number = issue["number"]
            is_pr = "pull_request" in issue
            title = f"{'PR' if is_pr else 'Issue'} {self.repo}#{number}: {issue.get('title', '')}"
            meta = {"is_pr": is_pr, "state": issue.get("state"), "labels": [l.get("name") for l in issue.get("labels", [])],
                    "assignees": [a.get("login") for a in issue.get("assignees", []) or []]}
            created = clock.parse(issue.get("created_at"))
            if created and since_dt and created >= since_dt:
                items.append(self._item(number, f"issue:{issue['id']}", "opened", issue.get("user"),
                                        issue["created_at"], issue.get("body") or "", title, issue["html_url"],
                                        meta=meta))
            if issue.get("comments", 0):
                for c in self.client.paged(f"repos/{self.repo}/issues/{number}/comments", {"since": since}):
                    edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
                    items.append(self._item(number, f"comment:{c['id']}", "edit" if edited else "comment",
                                            c.get("user"), c.get("updated_at") or c["created_at"], c.get("body") or "",
                                            title, c["html_url"], version=edited or "", meta=meta))
            if is_pr:
                for r in self.client.paged(f"repos/{self.repo}/pulls/{number}/reviews"):
                    submitted = clock.parse(r.get("submitted_at"))
                    if submitted and since_dt and submitted >= since_dt:
                        items.append(self._item(number, f"review:{r['id']}", "review", r.get("user"),
                                                r["submitted_at"], f"[{r.get('state')}] {r.get('body') or ''}",
                                                title, r.get("html_url") or issue["html_url"], meta=meta))
                for c in self.client.paged(f"repos/{self.repo}/pulls/{number}/comments", {"since": since}):
                    items.append(self._item(number, f"review_comment:{c['id']}", "comment", c.get("user"),
                                            c.get("updated_at") or c["created_at"],
                                            f"{c.get('path', '')}: {c.get('body') or ''}", title, c["html_url"],
                                            version=c.get("updated_at") if c.get("updated_at") != c.get("created_at") else "",
                                            meta=meta))
            for e in self.client.paged(f"repos/{self.repo}/issues/{number}/events"):
                at = clock.parse(e.get("created_at"))
                if e.get("event") not in STATE_EVENTS or not at or (since_dt and at < since_dt):
                    continue
                detail = {k: (e.get(k) or {}).get("login") for k in ("assignee", "requested_reviewer") if e.get(k)}
                items.append(self._item(number, f"event:{e['id']}", "event", e.get("actor"), e["created_at"],
                                        f"{e['event']} {json.dumps(detail) if detail else ''}".strip(), title,
                                        issue["html_url"], meta={**meta, "event": e["event"], **detail}))
        new_cursor = clock.iso(started - timedelta(minutes=self.overlap)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=new_cursor, meta={"issues": len(issues)})


class GitHubNotificationSource:
    """Participating threads outside tracked repos (review requests, mentions, assignments)."""

    name = "github"

    def __init__(self, client: GitHubClient, tracked_repos: set[str], my_logins: list[str],
                 overlap_minutes: int = 30, lookback_days: int = 14):
        self.client = client
        self.tracked = {r.lower() for r in tracked_repos}
        self.me = {m.lower() for m in my_logins}
        self.overlap = overlap_minutes
        self.lookback_days = lookback_days

    scope = "notifications"

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        started = clock.now()
        since = _since(cursor, self.lookback_days)
        items: list[Item] = []
        notes = self.client.paged("notifications", {"all": "true", "participating": "true", "since": since})
        for note in notes:
            repo = note.get("repository", {}).get("full_name", "").lower()
            if not repo or repo in self.tracked or note.get("reason") not in PARTICIPATING_REASONS:
                continue
            subject = note.get("subject", {}) or {}
            number = None
            url = subject.get("url") or ""
            if "/issues/" in url or "/pulls/" in url:
                number = int(url.rstrip("/").rsplit("/", 1)[-1])
            if number is None:
                continue
            body = ""
            latest = subject.get("latest_comment_url")
            if latest:
                try:
                    comment = self.client.get(latest.replace(API + "/", ""))
                    body = (comment or {}).get("body") or ""
                except SourceError:
                    body = ""
            key = f"github:{repo}#{number}"
            html_url = f"https://github.com/{repo}/{'pull' if '/pulls/' in url else 'issues'}/{number}"
            items.append(Item(source="github", account=repo, external_id=f"notification:{note['id']}",
                              version=note.get("updated_at", ""), kind="notification", conversation_key=key,
                              thread_key=key, occurred_at=_gh_time(note.get("updated_at", clock.iso())),
                              author=None, title=f"{subject.get('type')} {repo}#{number}: {subject.get('title')}",
                              url=html_url, body=f"[reason: {note.get('reason')}]\n{body}",
                              meta={"reason": note.get("reason"), "number": number}))
        new_cursor = clock.iso(started - timedelta(minutes=self.overlap)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=new_cursor, meta={"notifications": len(notes)})


class CommitSource:
    """Default-branch commits of one repository, consumed only by the daily digest."""

    name = "commit"

    def __init__(self, client: GitHubClient, repo: str, bot_logins: list[str], lookback_days: int = 7):
        self.client = client
        self.repo = repo
        self.bots = {b.lower() for b in bot_logins}
        self.lookback_days = lookback_days

    @property
    def scope(self) -> str:
        return f"commits:{self.repo.lower()}"

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        started = clock.now()
        since = _since(cursor, self.lookback_days)
        commits = self.client.paged(f"repos/{self.repo}/commits", {"since": since})
        items = []
        for c in commits:
            author = (c.get("author") or {}).get("login") or (c.get("commit", {}).get("author") or {}).get("name", "")
            message = c.get("commit", {}).get("message", "")
            is_bot = author.lower() in self.bots or author.lower().endswith("[bot]")
            items.append(Item(source="commit", account=self.repo.lower(), external_id=c["sha"], kind="commit",
                              conversation_key=f"repo:{self.repo.lower()}", thread_key=None,
                              occurred_at=_gh_time((c.get("commit", {}).get("committer") or {}).get("date", clock.iso())),
                              author=author, title=message.splitlines()[0][:200] if message else c["sha"][:7],
                              url=c.get("html_url"), body=message[:4000],
                              route="context" if is_bot else "digest",
                              meta={"sha": c["sha"], "parents": len(c.get("parents", []))}))
        new_cursor = clock.iso(started - timedelta(minutes=5)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=new_cursor, meta={"commits": len(commits)})

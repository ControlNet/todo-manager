"""GitHub adapter (REST, read-only) using the existing ``gh`` login.

Tracked repositories (``github.repos``): issue/PR openings, comments, reviews, review
comments and state events (assigned, review_requested, closed, merged, reopened ...).
Commits and pushes are NOT conversation items: a PR whose ``updated_at`` moved only
because of new commits yields nothing here; commit activity goes to the daily digest.

Outside tracked repos, only threads where the user participates (GitHub notifications
with reasons such as review_requested / assign / mention / author) become items.

Every item is keyed ``github:owner/repo#number`` so mail notifications about the same
issue/PR land in the same matter instead of creating a second task.

A merge/close of the user's own PR (no newer comment by someone else) is routed
``if_open_task``: it reaches the model only when that PR is evidence of an open task, which
then gets updated; otherwise it is stored as context.
Reading notifications never marks them as read.

Context: the cursor keeps a bounded cache per issue/PR (body digest, last seen). Activity on a
matter seen for the first time, or again after ``REACTIVATE_DAYS``, brings its original body and
the last ``CONTEXT_COMMENTS`` earlier comments as context-only items; a notification also brings
the comments since the previous one. Items carry state, opener, assignees and requested
reviewers. A body edit is a new version of the opening item keyed by its content digest
(``if_open_task``: news only for an open task); re-reading an unchanged body adds nothing.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://api.github.com"
STATE_EVENTS = {"assigned", "unassigned", "review_requested", "review_request_removed", "closed", "reopened",
                "merged", "converted_to_draft", "ready_for_review", "renamed"}
PARTICIPATING_REASONS = {"review_requested", "assign", "mention", "author", "comment", "team_mention",
                         "state_change", "manual"}
CONTEXT_COMMENTS = 3       # earlier comments given with a matter seen for the first time
SINCE_COMMENTS = 10        # at most this many comments since the previous notification
REACTIVATE_DAYS = 14       # a matter idle this long gets its context again
SEEN_MAX = 1000            # matters remembered per source cursor


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


def _load_state(cursor: str | None, lookback_days: int) -> dict[str, Any]:
    """Cursor: ``{"since": <time>, "seen": {matter: {"b": body digest, "at": last seen}}}``;
    a plain time is the format before the context cache."""
    if cursor and cursor.startswith("{"):
        state = json.loads(cursor)
        return {"since": state.get("since") or _since(None, lookback_days), "seen": state.get("seen") or {}}
    return {"since": _since(cursor, lookback_days), "seen": {}}


def _dump_state(since: str, seen: dict[str, Any]) -> str:
    kept = dict(sorted(seen.items(), key=lambda kv: kv[1].get("at", ""), reverse=True)[:SEEN_MAX])
    return json.dumps({"since": since, "seen": kept}, sort_keys=True)


def _digest(body: str | None) -> str:
    return hashlib.sha256((body or "").strip().encode("utf-8")).hexdigest()[:16]


def _needs_context(entry: dict[str, Any] | None, now: datetime) -> bool:
    if not entry:
        return True
    seen = clock.parse(entry.get("at"))
    return seen is None or now - seen > timedelta(days=REACTIVATE_DAYS)


def _requested_reviewers(events: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> list[str]:
    """Current review requests: requested and not removed, and not answered by a later review."""
    requested: dict[str, str] = {}
    for e in sorted(events, key=lambda e: e.get("created_at") or ""):
        login = (e.get("requested_reviewer") or {}).get("login")
        if not login:
            continue
        if e.get("event") == "review_requested":
            requested[login] = e.get("created_at") or ""
        elif e.get("event") == "review_request_removed":
            requested.pop(login, None)
    for r in reviews:
        login = (r.get("user") or {}).get("login")
        if login in requested and (r.get("submitted_at") or "") >= requested[login]:
            requested.pop(login)
    return sorted(requested)


def _earlier_comments(client: "GitHubClient", repo: str, number: int, total: int, before: str,
                      exclude: set[str], limit: int = CONTEXT_COMMENTS) -> list[dict[str, Any]]:
    """The last ``limit`` comments created before ``before`` (reads from the last page back)."""
    if total <= 0:
        return []
    per_page = 100
    page = max(1, -(-total // per_page))
    found: list[dict[str, Any]] = []
    while page >= 1 and len(found) < limit:
        data = client.get(f"repos/{repo}/issues/{number}/comments", {"per_page": per_page, "page": page})
        if not isinstance(data, list):
            break
        earlier = [c for c in data if (c.get("created_at") or "") < before and str(c.get("id")) not in exclude]
        found = earlier + found
        page -= 1
    return found[-limit:]


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
        state = _load_state(cursor, self.lookback_days)
        since = state["since"]
        since_dt = clock.parse(since)
        seen = state["seen"]
        items: list[Item] = []
        issues = self.client.paged(f"repos/{self.repo}/issues",
                                   {"since": since, "state": "all", "sort": "updated", "direction": "asc"})
        for issue in issues:
            number = issue["number"]
            is_pr = "pull_request" in issue
            opener = ((issue.get("user") or {}).get("login") or "").lower()
            title = f"{'PR' if is_pr else 'Issue'} {self.repo}#{number}: {issue.get('title', '')}"
            events = self.client.paged(f"repos/{self.repo}/issues/{number}/events")
            reviews = self.client.paged(f"repos/{self.repo}/pulls/{number}/reviews") if is_pr else []
            merged = any(e.get("event") == "merged" for e in events)
            meta = {"is_pr": is_pr, "state": "merged" if merged else issue.get("state"),
                    "labels": [l.get("name") for l in issue.get("labels", [])],
                    "assignees": [a.get("login") for a in issue.get("assignees", []) or []],
                    "opener": (issue.get("user") or {}).get("login"),
                    "requested_reviewers": _requested_reviewers(events, reviews)}
            created = clock.parse(issue.get("created_at"))
            is_new = bool(created and since_dt and created >= since_dt)
            if is_new:
                items.append(self._item(number, f"issue:{issue['id']}", "opened", issue.get("user"),
                                        issue["created_at"], issue.get("body") or "", title, issue["html_url"],
                                        meta=meta))
            key = f"github:{self.repo.lower()}#{number}"
            entry, digest = seen.get(key), _digest(issue.get("body"))
            if not is_new and _needs_context(entry, started):
                items.extend(self._context(issue, title, meta, since))
            if entry and entry.get("b") and entry["b"] != digest and not is_new:
                edit = self._item(number, f"issue:{issue['id']}", "edit", issue.get("user"), issue.get("updated_at")
                                  or clock.iso(started), issue.get("body") or "", title, issue["html_url"],
                                  version=digest, meta={**meta, "body_edit": True})
                items.append(edit if edit.route == "context" else replace(edit, route="if_open_task"))
            seen[key] = {"b": digest, "at": clock.iso(started)}
            if issue.get("comments", 0):
                for c in self.client.paged(f"repos/{self.repo}/issues/{number}/comments", {"since": since}):
                    edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
                    items.append(self._item(number, f"comment:{c['id']}", "edit" if edited else "comment",
                                            c.get("user"), c.get("updated_at") or c["created_at"], c.get("body") or "",
                                            title, c["html_url"], version=edited or "", meta=meta))
            if is_pr:
                for r in reviews:
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
            for e in events:
                at = clock.parse(e.get("created_at"))
                if e.get("event") not in STATE_EVENTS or not at or (since_dt and at < since_dt):
                    continue
                detail = {k: (e.get(k) or {}).get("login") for k in ("assignee", "requested_reviewer") if e.get(k)}
                item = self._item(number, f"event:{e['id']}", "event", e.get("actor"), e["created_at"],
                                  f"{e['event']} {json.dumps(detail) if detail else ''}".strip(), title,
                                  issue["html_url"], meta={**meta, "event": e["event"], **detail})
                if is_pr and e["event"] in ("merged", "closed") and opener in self.me and item.route == "batch":
                    item.route = "if_open_task"   # my own PR merged/closed: news only for an open task
                items.append(item)
        new_since = clock.iso(started - timedelta(minutes=self.overlap)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=_dump_state(new_since, seen),
                           meta={"issues": len(issues)})

    def _context(self, issue: dict[str, Any], title: str, meta: dict[str, Any], before: str) -> list[Item]:
        """Original body and the last earlier comments of a matter, as context only."""
        number = issue["number"]
        found = [self._item(number, f"issue:{issue['id']}", "opened", issue.get("user"), issue["created_at"],
                            issue.get("body") or "", title, issue["html_url"], meta=meta)]
        for c in _earlier_comments(self.client, self.repo, number, int(issue.get("comments") or 0), before, set()):
            edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
            found.append(self._item(number, f"comment:{c['id']}", "comment", c.get("user"), c["created_at"],
                                    c.get("body") or "", title, c.get("html_url") or issue["html_url"],
                                    version=edited or "", meta=meta))
        return [replace(i, route="context") for i in found]


class GitHubNotificationSource:
    """Participating threads outside tracked repos (review requests, mentions, assignments)."""

    name = "github"

    def __init__(self, client: GitHubClient, tracked_repos: set[str], my_logins: list[str],
                 bot_logins: list[str] | None = None, overlap_minutes: int = 30, lookback_days: int = 14):
        self.client = client
        self.tracked = {r.lower() for r in tracked_repos}
        self.me = {m.lower() for m in my_logins}
        self.bots = {b.lower() for b in bot_logins or []}
        self.overlap = overlap_minutes
        self.lookback_days = lookback_days

    scope = "notifications"

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        started = clock.now()
        state = _load_state(cursor, self.lookback_days)
        since, seen = state["since"], state["seen"]
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
            # The thread itself (opener, state) and its latest activity (author, text): without them
            # the model cannot tell "my own PR was merged" from "someone needs me".
            thread = self._get(url)
            latest_url = subject.get("latest_comment_url")
            latest = thread if not latest_url or latest_url == url else self._get(latest_url)
            opener = ((thread or {}).get("user") or {}).get("login") or "?"
            state = (thread or {}).get("state") or "?"
            if (thread or {}).get("merged"):
                state = "merged"
            actor = (latest or {}).get("user") or {}
            login = actor.get("login") or ""
            is_bot = login.lower() in self.bots or actor.get("type") == "Bot" or login.lower().endswith("[bot]")
            body = (latest or {}).get("body") or ""
            # My own PR merged/closed with no newer comment: news only for a task still open about it.
            own_pr_closed = ("/pulls/" in url and opener.lower() in self.me and state in ("merged", "closed")
                             and latest is thread)
            key = f"github:{repo}#{number}"
            html_url = f"https://github.com/{repo}/{'pull' if '/pulls/' in url else 'issues'}/{number}"
            title = f"{subject.get('type')} {repo}#{number}: {subject.get('title')}"
            meta = {"reason": note.get("reason"), "number": number, "state": state, "opener": opener,
                    "assignees": [a.get("login") for a in (thread or {}).get("assignees") or []],
                    "requested_reviewers": sorted(r.get("login") for r in (thread or {}).get("requested_reviewers") or []
                                                  if r.get("login"))}
            items.append(Item(source="github", account=repo, external_id=f"notification:{note['id']}",
                              version=note.get("updated_at", ""), kind="notification", conversation_key=key,
                              thread_key=key, occurred_at=_gh_time(note.get("updated_at", clock.iso())),
                              author=login or None, author_is_me=login.lower() in self.me,
                              title=title, url=html_url,
                              route="context" if is_bot else "if_open_task" if own_pr_closed else "batch",
                              body=f"[reason: {note.get('reason')}; state: {state}; opened by {opener}]\n{body}",
                              meta=meta))
            if thread:
                latest_id = str((latest or {}).get("id")) if latest is not thread and latest else ""
                items.extend(self._context(repo, number, key, thread, latest_id, title, html_url, meta,
                                           seen.get(key), note.get("updated_at") or clock.iso(started), started))
                seen[key] = {"b": _digest(thread.get("body")), "at": clock.iso(started)}
        new_since = clock.iso(started - timedelta(minutes=self.overlap)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=_dump_state(new_since, seen),
                           meta={"notifications": len(notes)})

    def _context(self, repo: str, number: int, key: str, thread: dict[str, Any], latest_id: str, title: str,
                 url: str, meta: dict[str, Any], entry: dict[str, Any] | None, before: str,
                 now: datetime) -> list[Item]:
        """First sight / reactivation: original body and the last earlier comments. Known matter:
        the comments since the previous notification. Both as context; a body edit as
        ``if_open_task``. The latest comment is already in the notification."""
        kind = "pull" if "/pull/" in url else "issue"
        found: list[Item] = []

        def item(external_id: str, kind_: str, user: dict[str, Any] | None, at: str, body: str,
                 version: str = "", route: str = "context") -> Item:
            login = (user or {}).get("login") or ""
            return Item(source="github", account=repo, external_id=external_id, version=version, kind=kind_,
                        conversation_key=key, thread_key=key, occurred_at=_gh_time(at), author=login or None,
                        author_is_me=login.lower() in self.me, title=title, url=url, body=body or "", route=route,
                        meta=meta)

        total = int(thread.get("comments") or 0)
        exclude = {latest_id} if latest_id else set()
        if _needs_context(entry, now):
            found.append(item(f"{kind}:{thread.get('id')}", "opened", thread.get("user"),
                              thread.get("created_at") or before, thread.get("body") or ""))
            comments = _earlier_comments(self.client, repo, number, total, before, exclude)
        else:
            comments = [c for c in self.client.paged(f"repos/{repo}/issues/{number}/comments",
                                                     {"since": entry.get("at")}) if str(c.get("id")) not in exclude
                        and (c.get("created_at") or "") >= (entry.get("at") or "")][-SINCE_COMMENTS:] if total else []
            digest = _digest(thread.get("body"))
            if entry.get("b") and entry["b"] != digest:
                found.append(item(f"{kind}:{thread.get('id')}", "edit", thread.get("user"),
                                  thread.get("updated_at") or before, thread.get("body") or "", version=digest,
                                  route="if_open_task"))
        for c in comments:
            edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
            found.append(item(f"comment:{c['id']}", "comment", c.get("user"), c.get("created_at") or before,
                              c.get("body") or "", version=edited or ""))
        return found

    def _get(self, api_url: str) -> dict[str, Any] | None:
        try:
            data = self.client.get(api_url.replace(API + "/", ""))
        except SourceError as exc:
            if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):
                return None   # deleted or no longer accessible
            raise
        return data if isinstance(data, dict) else None


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

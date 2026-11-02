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
matter seen for the first time, or again after ``REACTIVATE_DAYS``, is split at the start of the
read window (the cursor's since): a person's comment inside it is news (batch) even when bots
commented after it, and the opening post too when a person wrote it inside the window; before
it, the original body and the last ``CONTEXT_COMMENTS`` comments are context-only. On a known matter a
notification brings EVERY comment since the previous view (read from ``WATERMARK_OVERLAP``
before it; repeats are dropped by id/version when stored): a person's comment is news (batch),
a bot's is context, and the intake budget segments a long discussion. On a PR the same holds for
review submissions and inline review comments, which the issue-comment listing does not contain.
An edit of an older comment is a new version of it (``if_open_task``). Items carry state, opener,
assignees and requested reviewers. A body edit is a new version of the opening item keyed by its content
digest (``if_open_task``: news only for an open task); re-reading an unchanged body adds nothing.
"""

from __future__ import annotations

import hashlib
import json
import re
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
WATERMARK_OVERLAP = timedelta(minutes=10)   # re-read before the last view (clock skew, listing lag)
REACTIVATE_DAYS = 14       # a matter idle this long gets its context again
SEEN_MAX = 1000            # matters remembered per source cursor


def event_identity(external_id: str, kind: str) -> str | None:
    """The GitHub event an item announces (shared with notification mail, see gmail.github_event).
    Edits are not events a mail announces."""
    if kind == "edit":
        return None
    prefix, _, number = external_id.partition(":")
    if kind == "opened":
        return "opened"
    if prefix in ("comment", "review", "review_comment", "event"):
        return f"{prefix}:{number}"
    return None


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


def is_bot(user: dict[str, Any] | None, extra: set[str]) -> bool:
    """GitHub marks app accounts itself (``type: "Bot"``, ``<name>[bot]`` logins); ``extra``
    (``github.bot_logins``, lower case) adds accounts it does not mark, e.g. a machine user."""
    login = ((user or {}).get("login") or "").lower()
    return (user or {}).get("type") == "Bot" or login.endswith("[bot]") or login in extra


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


def _not_before(value: str, start: str) -> bool:
    """``value`` (a GitHub time) is at or after ``start``, compared as times, not strings."""
    a, b = clock.parse(value), clock.parse(start)
    return a is not None and b is not None and a >= b


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
        bot = is_bot(user, self.bots)
        key = f"github:{self.repo.lower()}#{number}"
        return Item(source="github", account=self.repo.lower(), external_id=external_id, version=version, kind=kind,
                    conversation_key=key, thread_key=key, occurred_at=_gh_time(at), author=login,
                    author_is_me=login.lower() in self.me, title=title, url=url, body=body or "",
                    route="context" if bot else "batch",
                    meta={"number": number, **(meta or {}), "github_event": event_identity(external_id, kind)})

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
            unavailable: list[str] = []
            thread = self._get(url, "thread", unavailable)
            latest_url = subject.get("latest_comment_url")
            latest = thread if not latest_url or latest_url == url else self._get(latest_url, "latest_comment",
                                                                                   unavailable)
            opener = ((thread or {}).get("user") or {}).get("login") or "?"
            state = (thread or {}).get("state") or "?"
            if (thread or {}).get("merged"):
                state = "merged"
            actor = (latest or {}).get("user") or {}
            login = actor.get("login") or ""
            bot = is_bot(actor, self.bots)
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
            gaps = "".join(f"\n[{u.split(':')[0].replace('_', ' ')} unavailable ({u.split(': ', 1)[1]}): deleted or"
                           " no access; its content is unknown]" for u in unavailable)
            if unavailable:
                meta["unavailable"] = unavailable
            if latest_url and latest is not thread and latest:
                kind_of = ("review_comment" if "/pulls/comments/" in latest_url else
                           "review" if "/reviews/" in latest_url else "comment")
                meta["github_event"] = f"{kind_of}:{latest.get('id')}"
            items.append(Item(source="github", account=repo, external_id=f"notification:{note['id']}",
                              version=note.get("updated_at", ""), kind="notification", conversation_key=key,
                              thread_key=key, occurred_at=_gh_time(note.get("updated_at", clock.iso())),
                              author=login or None, author_is_me=login.lower() in self.me,
                              title=title, url=html_url,
                              route="context" if bot else "if_open_task" if own_pr_closed else "batch",
                              body=f"[reason: {note.get('reason')}; state: {state}; opened by {opener}]{gaps}\n{body}",
                              meta=meta))
            if thread:
                # An issue comment by its plain id (as in older cursors), a review (comment) by its event.
                latest_id = meta.get("github_event", "").removeprefix("comment:")
                items.extend(self._context(repo, number, key, thread, latest_id, title, html_url, meta,
                                           seen.get(key), note.get("updated_at") or clock.iso(started), started,
                                           window=since, opened_in_note=latest is thread))
                # Comments delivered as a notification's latest comment are not repeated as
                # comments when the next view re-reads the overlap before its watermark.
                delivered = [*(seen.get(key) or {}).get("n", []), *([latest_id] if latest_id else [])][-5:]
                seen[key] = {"b": _digest(thread.get("body")), "at": clock.iso(started), "n": delivered}
        new_since = clock.iso(started - timedelta(minutes=self.overlap)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=_dump_state(new_since, seen),
                           meta={"notifications": len(notes)})

    def _context(self, repo: str, number: int, key: str, thread: dict[str, Any], latest_id: str, title: str,
                 url: str, meta: dict[str, Any], entry: dict[str, Any] | None, before: str,
                 now: datetime, window: str = "", opened_in_note: bool = False) -> list[Item]:
        """First sight / reactivation: what happened inside this read's window (``window``: the
        cursor's since, the lookback on a first run) is news - a person's comment is batch even
        when bots commented after it, the opening post too when a person wrote it in the window
        and it is not the notification's own text - and only what came before is history: the
        opening post and the last earlier comments, as context. Known matter: the comments since
        the previous view. A PR adds its reviews and inline review comments (``_pull_review_items``).
        A body edit is ``if_open_task``. The latest comment is already in the notification."""
        kind = "pull" if "/pull/" in url else "issue"
        found: list[Item] = []

        def item(external_id: str, kind_: str, user: dict[str, Any] | None, at: str, body: str,
                 version: str = "", route: str = "context") -> Item:
            login = (user or {}).get("login") or ""
            return Item(source="github", account=repo, external_id=external_id, version=version, kind=kind_,
                        conversation_key=key, thread_key=key, occurred_at=_gh_time(at), author=login or None,
                        author_is_me=login.lower() in self.me, title=title, url=url, body=body or "", route=route,
                        meta={**meta, "github_event": event_identity(external_id, kind_)})

        total = int(thread.get("comments") or 0)
        exclude = {latest_id} if latest_id else set()
        if _needs_context(entry, now):
            start = window or before
            opened_at = thread.get("created_at") or before
            news = _not_before(opened_at, start) and not opened_in_note and not self._is_bot(thread.get("user"))
            found.append(item(f"{kind}:{thread.get('id')}", "opened", thread.get("user"), opened_at,
                              thread.get("body") or "", route="batch" if news else "context"))
            recent = self.client.paged(f"repos/{repo}/issues/{number}/comments", {"since": start}) if total else []
            delivered = {str(n) for n in (entry or {}).get("n", [])}   # news already (a notification's latest)
            for c in recent:
                if str(c.get("id")) in exclude | delivered or not _not_before(c.get("created_at") or "", start):
                    continue          # edited older comments are history, given below if recent enough
                edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
                found.append(item(f"comment:{c['id']}", "comment", c.get("user"), c.get("created_at") or before,
                                  c.get("body") or "", version=edited or "",
                                  route="context" if self._is_bot(c.get("user")) else "batch"))
            for c in _earlier_comments(self.client, repo, number, total, start, exclude):
                edited = c.get("updated_at") if c.get("updated_at") != c.get("created_at") else ""
                found.append(item(f"comment:{c['id']}", "comment", c.get("user"), c.get("created_at") or before,
                                  c.get("body") or "", version=edited or ""))
            if kind == "pull":
                found.extend(self._pull_review_items(repo, number, thread, start, exclude, delivered, True, item))
            return sorted(found, key=lambda i: i.occurred_at or "")
        seen_at = clock.parse(entry.get("at"))
        watermark = clock.iso(seen_at - WATERMARK_OVERLAP).replace(".000Z", "Z") if seen_at else entry.get("at")
        listed = self.client.paged(f"repos/{repo}/issues/{number}/comments", {"since": watermark}) if total else []
        delivered = {str(n) for n in entry.get("n", [])}
        for c in listed:
            if str(c.get("id")) in exclude:
                continue
            created, updated = c.get("created_at") or "", c.get("updated_at") or ""
            edited = updated if updated != created else ""
            bot = self._is_bot(c.get("user"))
            if created >= (watermark or "") and not (str(c.get("id")) in delivered and not edited):
                found.append(item(f"comment:{c['id']}", "comment", c.get("user"), created or before,
                                  c.get("body") or "", version=edited, route="context" if bot else "batch"))
            elif edited:      # an older comment changed since the last view
                found.append(item(f"comment:{c['id']}", "edit", c.get("user"), updated, c.get("body") or "",
                                  version=edited, route="context" if bot else "if_open_task"))
        if kind == "pull":
            found.extend(self._pull_review_items(repo, number, thread, watermark, exclude, delivered, False, item))
        digest = _digest(thread.get("body"))
        if entry.get("b") and entry["b"] != digest:
            found.append(item(f"{kind}:{thread.get('id')}", "edit", thread.get("user"),
                              thread.get("updated_at") or before, thread.get("body") or "", version=digest,
                              route="if_open_task"))
        return found

    def _pull_review_items(self, repo: str, number: int, thread: dict[str, Any], start: str, exclude: set[str],
                           delivered: set[str], first: bool, item: Callable[..., Item]) -> list[Item]:
        """A PR's reviews (by ``submitted_at``) and inline review comments since ``start``, with the
        tracked-repo identities: a person's is news, a bot's context; on a known matter an older
        inline comment edited since is an edit, as for issue comments. An empty COMMENTED review
        only wraps inline comments, which are items themselves. ``exclude``/``delivered`` hold
        them as ``review:``/``review_comment:`` events (issue comments by their plain id)."""
        found: list[Item] = []
        listed = (self.client.paged(f"repos/{repo}/pulls/{number}/comments", {"since": start})
                  if int(thread.get("review_comments") or 0) else [])
        for c in listed:
            ext = f"review_comment:{c['id']}"
            created, updated = c.get("created_at") or "", c.get("updated_at") or ""
            edited = updated if updated != created else ""
            if ext in exclude or (ext in delivered and (first or not edited)):
                continue
            bot, body = self._is_bot(c.get("user")), f"{c.get('path', '')}: {c.get('body') or ''}"
            if _not_before(created, start):
                found.append(item(ext, "comment", c.get("user"), created, body, version=edited,
                                  route="context" if bot else "batch"))
            elif edited and not first:
                found.append(item(ext, "edit", c.get("user"), updated, body, version=edited,
                                  route="context" if bot else "if_open_task"))
        for r in self.client.paged(f"repos/{repo}/pulls/{number}/reviews"):
            ext, body = f"review:{r['id']}", r.get("body") or ""
            if (ext in exclude | delivered or not _not_before(r.get("submitted_at") or "", start)
                    or (r.get("state") == "COMMENTED" and not body.strip())):
                continue
            found.append(item(ext, "review", r.get("user"), r["submitted_at"], f"[{r.get('state')}] {body}",
                              route="context" if self._is_bot(r.get("user")) else "batch"))
        return found

    def _is_bot(self, user: dict[str, Any] | None) -> bool:
        return is_bot(user, self.bots)

    def _get(self, api_url: str, what: str = "", unavailable: list[str] | None = None) -> dict[str, Any] | None:
        """404/410 (deleted or no longer accessible) is ``None`` and noted in ``unavailable``; any
        other failure raises, so the read fails and the cursor stays for the next run."""
        try:
            data = self.client.get(api_url.replace(API + "/", ""))
        except SourceError as exc:
            status = next((s for s in ("HTTP 404", "HTTP 410") if s in str(exc)), None)
            if status is None:
                raise
            if unavailable is not None and what:
                unavailable.append(f"{what}: {status}")
            return None
        return data if isinstance(data, dict) else None


class GitHubWatchSource:
    """The state of PRs and issues open tasks wait for, read directly.

    GitHub does not notify the actor of their own action, and the notifications ``since`` filter
    skips a thread whose only change is the user's own merge or close, although the thread's shown
    ``updated_at`` moves. So the user's own merge or close is otherwise never seen. Every
    ``watch_minutes`` each watched matter outside tracked repos (those read their events) costs one
    GET; a change of state (merged, closed with its reason, reopened), or a closed state when the
    matter is first watched, is an ``if_open_task`` event. Its id carries the state and its time,
    so the same change is stored once even when another task watches the matter later."""

    name = "github"
    scope = "watched"

    def __init__(self, client: GitHubClient, tracked_repos: set[str], my_logins: list[str],
                 bot_logins: list[str] | None = None, *, matters: Callable[[], list[str]],
                 watch_minutes: int = 15):
        self.client = client
        self.tracked = {r.lower() for r in tracked_repos}
        self.me = {m.lower() for m in my_logins}
        self.bots = {b.lower() for b in bot_logins or []}
        self.matters = matters
        self.watch_minutes = watch_minutes

    def due(self, cursor_row: dict[str, Any] | None) -> bool:
        last = clock.parse((cursor_row or {}).get("last_success_at"))
        return last is None or clock.now() - last >= timedelta(minutes=self.watch_minutes)

    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        previous = (json.loads(cursor) if cursor and cursor.startswith("{") else {}).get("states") or {}
        states: dict[str, str] = {}
        items: list[Item] = []
        for key in self.matters():
            m = re.fullmatch(r"github:([^/\s]+/[^#\s]+)#(\d+)", key)
            if not m or m.group(1).lower() in self.tracked:
                continue
            repo, number = m.group(1).lower(), int(m.group(2))
            try:
                thread = self.client.get(f"repos/{repo}/issues/{number}")
            except SourceError as exc:
                if "HTTP 404" in str(exc) or "HTTP 410" in str(exc):   # deleted or no longer accessible
                    if key in previous:
                        states[key] = previous[key]
                    continue
                raise
            if not isinstance(thread, dict):
                continue
            pr = thread.get("pull_request")
            merged_at = (pr or {}).get("merged_at")
            state = "merged" if merged_at else thread.get("state") or "?"
            states[key] = state
            if state == previous.get(key) or (key not in previous and state == "open"):
                continue
            items.append(self._item(repo, number, key, thread, state, pr is not None, merged_at))
        return FetchResult(scope=self.scope, items=items, cursor=json.dumps({"states": states}, sort_keys=True),
                           meta={"watched": len(states), "poll_interval_minutes": self.watch_minutes})

    def _item(self, repo: str, number: int, key: str, thread: dict[str, Any], state: str, is_pr: bool,
              merged_at: str | None) -> Item:
        reason = thread.get("state_reason") if not is_pr else None
        at = merged_at or (thread.get("closed_at") if state != "open" else None) or thread.get("updated_at") \
            or clock.iso()
        actor = (thread.get("closed_by") or {}) if state != "open" else {}
        login = actor.get("login") or ""
        opener = (thread.get("user") or {}).get("login") or "?"
        what = {"merged": "merged", "closed": f"closed ({reason})" if reason else "closed",
                "open": "reopened"}.get(state, state)
        by = f" by {login}" if login else ""
        title = f"{'PullRequest' if is_pr else 'Issue'} {repo}#{number}: {thread.get('title')}"
        return Item(source="github", account=repo, external_id=f"state:{number}:{state}:{at}", kind="event",
                    conversation_key=key, thread_key=key, occurred_at=_gh_time(at), author=login or None,
                    author_is_me=login.lower() in self.me, title=title,
                    url=thread.get("html_url") or f"https://github.com/{repo}/{'pull' if is_pr else 'issues'}/{number}",
                    route="if_open_task",
                    body=f"[state: {state}; opened by {opener}]\n{what}{by} at {at}",
                    meta={"number": number, "state": state, "state_reason": reason, "is_pr": is_pr,
                          "opener": opener, "watched": True})


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
            bot = is_bot(c.get("author") or {"login": author}, self.bots)
            items.append(Item(source="commit", account=self.repo.lower(), external_id=c["sha"], kind="commit",
                              conversation_key=f"repo:{self.repo.lower()}", thread_key=None,
                              occurred_at=_gh_time((c.get("commit", {}).get("committer") or {}).get("date", clock.iso())),
                              author=author, title=message.splitlines()[0][:200] if message else c["sha"][:7],
                              url=c.get("html_url"), body=message[:4000],
                              route="context" if bot else "digest",
                              meta={"sha": c["sha"], "parents": len(c.get("parents", []))}))
        new_cursor = clock.iso(started - timedelta(minutes=5)).replace(".000Z", "Z")
        return FetchResult(scope=self.scope, items=items, cursor=new_cursor, meta={"commits": len(commits)})

"""Gmail adapter (REST API, read-only).

* Incremental via ``users.history.list`` (messageAdded) from the stored historyId, all pages.
* Changed threads are fetched in full (``format=full``) so judgement sees the whole thread,
  including the user's own sent replies (commitments / reply evidence).
* Label/read-state changes create no items: "read" or "archived" is never completion and
  never a new task signal.
* If the stored historyId has expired, falls back to a paginated ``messages.list`` over the
  window since the last success minus an overlap, capturing the profile historyId first.
* Large backlogs are worked through a persisted thread queue; nothing is silently capped.
* GitHub notification mails are keyed to the same matter as the GitHub API source
  (``github:owner/repo#n``, matter grouping) and carry the GitHub event they announce
  (``github_event``: opened, comment:<id>, review:<id>, review_comment:<id>, event:<id>) when
  the Message-ID or the "view it on GitHub" link names it. A mail is a duplicate only of the
  same event read through the API (decided when stored); without a known event it is kept.
  The formats follow GitHub's conventions and have not been checked against this mailbox.

Access tokens come from an existing local helper command (``token_command``), e.g. the
Hermes ``himalaya-google-token`` script; no new credentials are created or stored.
"""

from __future__ import annotations

import base64
import html
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses, parseaddr
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Callable

from .. import clock
from ..net import HttpError, HttpTimeout, request_json
from .base import FetchResult, Item, SourceError

API = "https://gmail.googleapis.com/gmail/v1/users/me"
GITHUB_MSGID = re.compile(r"<([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)/(?:issues|pull)/(\d+)(?:/([^@>]*))?@", re.I)
MSGID_EVENTS = ((re.compile(r"c?(\d+)"), "comment"), (re.compile(r"review/(\d+)"), "review"),
                (re.compile(r"issue_event/(\d+)"), "event"))
LINK_EVENTS = {"issuecomment-": "comment", "pullrequestreview-": "review", "discussion_r": "review_comment",
               "event-": "event"}


def github_event(msgid_suffix: str | None, body: str, repo: str, number: str) -> str | None:
    """The GitHub event a notification mail announces, or None when it cannot be told."""
    link = re.compile(rf"https://github\.com/{re.escape(repo)}/(?:issues|pull)/{number}"
                      rf"#({'|'.join(map(re.escape, LINK_EVENTS))})(\d+)", re.I)
    found = link.findall(body or "")
    if found:
        anchor, event_id = found[-1]          # the "view it on GitHub" link comes last
        return f"{LINK_EVENTS[anchor.lower()]}:{event_id}"
    if not msgid_suffix:
        return "opened"
    for pattern, kind in MSGID_EVENTS:
        match = pattern.fullmatch(msgid_suffix)
        if match:
            return f"{kind}:{match.group(1)}"
    return None
# The whole body is stored (the model context clips it at render time); this cap only guards
# against pathological messages and is recorded on the item when it applies.
MAX_BODY_CHARS = 2_000_000
PERMANENT_STATUSES = (400, 403, 404, 410)
EXCLUDED_LABELS = {"SPAM", "TRASH", "DRAFT", "CHAT"}
BULK_LABELS = {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style", "head"):
            self._skip += 1
        if tag in ("br", "p", "div", "tr", "li", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style", "head") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def html_to_text(raw: str) -> str:
    parser = _TextExtractor()
    parser.feed(raw)
    text = html.unescape("".join(parser.parts))
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def _b64(data: str) -> str:
    padded = data + "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode("utf-8", errors="replace")


def extract_body(payload: dict[str, Any], read_part: Callable[[str], str | None] | None = None
                 ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
    """(text, file attachments, notes). Text parts given by reference (``attachmentId``, used by
    Gmail for large bodies) are read through ``read_part``; None from it means the part cannot
    be read, which is recorded in the notes and marked in the text."""
    plain: list[str] = []
    html_parts: list[str] = []
    attachments: list[dict[str, Any]] = []
    notes: dict[str, Any] = {}

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        if part.get("filename"):
            attachments.append({"filename": part["filename"], "mime": mime, "size": body.get("size", 0),
                                "attachment_id": body.get("attachmentId")})
        elif mime in ("text/plain", "text/html"):
            target = plain if mime == "text/plain" else html_parts
            if body.get("data"):
                target.append(_b64(body["data"]))
            elif body.get("attachmentId"):
                content = read_part(body["attachmentId"]) if read_part else None
                if content is None:
                    notes.setdefault("body_incomplete", []).append(mime)
                else:
                    target.append(content)
        for child in part.get("parts", []) or []:
            walk(child)

    walk(payload)
    text = "\n".join(plain).strip() or html_to_text("\n".join(html_parts))
    if notes.get("body_incomplete"):   # first, so a clipped rendering still shows it
        text = "[part of this email's body could not be read: " + ", ".join(notes["body_incomplete"]) + "]" \
            + ("\n\n" + text if text else "")
    if len(text) > MAX_BODY_CHARS:
        notes["body_truncated"] = len(text) - MAX_BODY_CHARS
        text = text[:MAX_BODY_CHARS] + f"\n[... {notes['body_truncated']} more chars not stored]"
    return text, attachments, notes


class TokenProvider:
    def __init__(self, command: list[str]):
        self.command = [str(Path(c).expanduser()) if c.startswith("~") else c for c in command]
        self._token: str | None = None
        self._at = 0.0

    def __call__(self) -> str:
        if self._token and time.monotonic() - self._at < 45 * 60:
            return self._token
        try:
            out = subprocess.run(self.command, capture_output=True, text=True, timeout=30, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SourceError(f"token helper failed: {type(exc).__name__}") from None
        if out.returncode != 0 or not out.stdout.strip():
            raise SourceError("token helper failed (exit %s)" % out.returncode)
        self._token = out.stdout.strip().splitlines()[-1].strip()
        self._at = time.monotonic()
        return self._token


class _BudgetReached(Exception):
    pass


class GmailSource:
    name = "gmail"

    def __init__(self, *, account: str, address: str, token: Callable[[], str], my_addresses: list[str],
                 query: str = "-in:spam -in:trash -in:chats", page_size: int = 100,
                 overlap_minutes: int = 120, initial_lookback_days: int = 14, max_threads_per_run: int = 150,
                 http: Callable[..., Any] = request_json, max_requests_per_run: int = 40,
                 max_seconds_per_run: float = 180):
        self.account = account
        self.address = address
        self.token = token
        self.me = {a.lower() for a in my_addresses} | {address.lower()}
        self.query = query
        self.page_size = page_size
        self.overlap = overlap_minutes
        self.lookback_days = initial_lookback_days
        self.max_threads = max_threads_per_run
        self.http = http
        self.max_requests = max(2, max_requests_per_run)
        self.max_seconds = max_seconds_per_run
        self._deadline = None
        self._parts = None

    @property
    def scope(self) -> str:
        return f"mailbox:{self.account}"

    # ------------------------------------------------------------------ API helpers
    def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        timeout = 45
        if self._deadline is not None:
            remaining = self._deadline - time.monotonic()
            if self._remaining <= 0 or remaining <= 0:
                raise _BudgetReached()
            self._remaining -= 1
            timeout = min(timeout, remaining)
        try:
            return self.http("GET", f"{API}/{path}", headers={"Authorization": f"Bearer {self.token()}"},
                             params=params, timeout=timeout)
        except HttpError as exc:
            if exc.status == 404 and path.startswith("history"):
                raise SourceError("historyId expired", cursor_invalid=True) from None
            raise SourceError(str(exc), retry_after=exc.retry_after) from None
        except HttpTimeout as exc:
            raise SourceError(str(exc)) from None

    def set_fetch_deadline(self, deadline: float) -> None:
        """The gate's remaining wall-clock budget, without changing global settings."""
        self._gate_deadline = deadline

    def _selection_queries(self, data: dict[str, Any]) -> list[str]:
        """Server-side AND with candidate Message-IDs; never interpret the user's query.

        Gmail search is per message. Include *all* messages in the changed thread so
        an older matching parent still selects a new nonmatching reply's context.
        Missing/unusual identifiers use a resumable broad search, never a false negative.
        """
        terms = []
        for message in data.get('messages', []):
            headers = message.get('payload', {}).get('headers', [])
            values = [h['value'].strip().strip('<>') for h in headers if h['name'].lower() == 'message-id']
            if len(values) != 1 or not re.fullmatch(r'[A-Za-z0-9._+%/=@-]{1,240}', values[0]) or '@' not in values[0]:
                return [self.query]
            terms.append('rfc822msgid:' + values[0])
        terms = list(dict.fromkeys(terms))
        return [f"({self.query}) (" + ' OR '.join(terms[i:i + 10]) + ')'
                for i in range(0, len(terms), 10)]

    # ------------------------------------------------------------------ fetch
    def fetch(self, cursor: str | None, last_success_at: str | None = None) -> FetchResult:
        state = json.loads(cursor) if cursor else {}
        history_id = state.get('history_id')
        floor = state.get('since') or clock.iso(clock.now() - timedelta(days=self.lookback_days))
        scan = state.get('scan')
        if scan is None:
            # Compatibility with a previously persisted, already selected backlog.
            scan = {'phase': 'hydrate' if state.get('queue') else ('history' if history_id else 'window'),
                    'ready': list(state.get('queue', [])), 'candidates': [],
                    'target': state.get('queue_history_id'), 'mode': 'history' if history_id else 'initial_window'}
        items: list[Item] = []
        self._remaining = self.max_requests
        self._deadline = min(time.monotonic() + self.max_seconds, getattr(self, '_gate_deadline', float('inf')))
        self._gate_deadline = float('inf')
        threads = 0

        def result() -> FetchResult:
            complete = scan['phase'] == 'done'
            saved = {'history_id': (scan.get('target') or history_id) if complete else history_id, 'since': floor}
            if not complete:
                saved['scan'] = scan
            for item in items:
                if item.route == 'batch' and item.occurred_at and item.occurred_at < floor:
                    item.route = 'context'
            return FetchResult(scope=self.scope, items=items, cursor=json.dumps(saved), complete=complete,
                               meta={'mode': scan['mode'], 'threads': threads,
                                     'queued': len(scan['ready']) + len(scan['candidates']),
                                     'phase': scan['phase'], 'requests': self.max_requests - self._remaining})

        try:
            while scan['phase'] != 'done' and threads < self.max_threads:
                phase = scan['phase']
                if phase == 'history':
                    try:
                        data = self._get('history', {'startHistoryId': history_id, 'historyTypes': 'messageAdded',
                                                    'maxResults': 500, 'pageToken': scan.get('page')})
                    except SourceError as exc:
                        if not exc.cursor_invalid:
                            raise
                        scan.update(phase='window', mode='window_fallback', page=None, target=None, candidates=[])
                        continue
                    known = set(scan['candidates'])
                    for record in data.get('history', []) or []:
                        for added in record.get('messagesAdded', []) or []:
                            msg = added.get('message', {})
                            tid = msg.get('threadId')
                            if tid and tid not in known and not (EXCLUDED_LABELS & set(msg.get('labelIds', []))):
                                known.add(tid)
                                scan['candidates'].append(tid)
                    scan.update(page=data.get('nextPageToken'), target=str(data.get('historyId', history_id)))
                    if not scan['page']:
                        scan['phase'] = 'select'
                elif phase == 'window':
                    if not scan.get('target'):
                        scan['target'] = str(self._get('profile')['historyId'])
                        since = clock.now() - timedelta(days=self.lookback_days)
                        if scan['mode'] == 'window_fallback':
                            since = (clock.parse(last_success_at) or since) - timedelta(minutes=self.overlap)
                        scan['query'] = f'after:{int(since.timestamp())} {self.query}'.strip()
                    data = self._get('messages', {'q': scan['query'], 'maxResults': self.page_size,
                                                 'pageToken': scan.get('page'), 'includeSpamTrash': 'false'})
                    seen = scan.setdefault('window_seen', [])
                    scan['ready'] = list(dict.fromkeys(m['threadId'] for m in data.get('messages', [])
                                                      if m.get('threadId') and m['threadId'] not in seen))
                    seen.extend(scan['ready'])
                    scan['page'] = data.get('nextPageToken')
                    scan['return_to'] = 'window' if scan['page'] else 'done'
                    scan['phase'] = 'hydrate'
                elif phase == 'select':
                    if not scan['candidates']:
                        scan['phase'] = 'done'
                        continue
                    tid = scan['candidates'][0]
                    active = scan.get('active')
                    if active is None:
                        if self.query:
                            try:
                                data = self._get(f'threads/{tid}', {'format': 'metadata', 'metadataHeaders': 'Message-ID'})
                            except SourceError as exc:
                                if 'HTTP 404' not in str(exc):
                                    raise
                                data = {'messages': []}
                            active = {'queries': self._selection_queries(data), 'index': 0, 'page': None}
                        else:
                            active = {'queries': [], 'index': 0, 'page': None}
                        scan['active'] = active
                    matched = not self.query
                    queries = active['queries']
                    if active['index'] < len(queries):
                        data = self._get('messages', {'q': queries[active['index']], 'maxResults': self.page_size,
                                                     'pageToken': active['page'], 'includeSpamTrash': 'false'})
                        matched = any(m.get('threadId') == tid for m in data.get('messages', []))
                        active['page'] = data.get('nextPageToken')
                        if not active['page']:
                            active['index'] += 1
                        if not matched and active['index'] < len(queries):
                            continue
                    scan['candidates'].pop(0)
                    scan.pop('active', None)
                    if matched:
                        scan.update(phase='hydrate', ready=[tid], return_to='select')
                elif phase == 'hydrate':
                    if not scan['ready']:
                        scan['phase'] = scan.pop('return_to', 'done')
                        continue
                    tid = scan['ready'][0]
                    try:
                        data = self._get(f'threads/{tid}', {'format': 'full'})
                    except SourceError as exc:
                        if 'HTTP 404' not in str(exc):
                            raise
                        data = {'messages': []}
                    hydrated = scan.setdefault('hydrated', [])
                    self._parts = scan.setdefault('parts', {})
                    for msg in data.get('messages', []):
                        if msg['id'] in hydrated:
                            continue
                        item = self.normalize(msg)
                        if item is not None:
                            items.append(item)
                        hydrated.append(msg['id'])
                        self._parts.clear()
                    scan.pop('hydrated', None)
                    scan.pop('parts', None)
                    self._parts = None
                    scan['ready'].pop(0)
                    threads += 1
                    if not scan['ready']:
                        scan['phase'] = scan.pop('return_to', 'done')
                        if scan['phase'] == 'select' and not scan['candidates']:
                            scan['phase'] = 'done'
        except _BudgetReached:
            pass
        except SourceError as exc:
            # Successful pages/items can be committed together with the checkpoint;
            # the failing request remains due and the health error is not hidden.
            exc.checkpoint = result()
            raise
        finally:
            self._deadline = None
            self._parts = None
        return result()

    def thread_key_for(self, gmail_id: str) -> str | None:
        """Thread key for a message id (or a thread id), or None when this mailbox has neither."""
        for path in (f"messages/{gmail_id}", f"threads/{gmail_id}"):
            try:
                data = self._get(path, {"format": "minimal"})
            except SourceError as exc:
                if "HTTP 404" in str(exc) or "HTTP 400" in str(exc):
                    continue
                raise
            thread = data.get("threadId") or data.get("id")
            if thread:
                return f"gmail:{self.account}:{thread}"
        return None

    def _body_part(self, message_id: str, attachment_id: str) -> str | None:
        """A text part stored by reference. A transient failure fails the fetch (it is retried
        and its checkpoint retains this message); a permanent one returns None (recorded as incomplete)."""
        key = message_id + ':' + attachment_id
        if self._parts is not None and key in self._parts:
            return self._parts[key]
        try:
            data = self._get(f"messages/{message_id}/attachments/{attachment_id}")
            value = _b64(data.get('data') or '')
        except SourceError as exc:
            if not any(f'HTTP {status}' in str(exc) for status in PERMANENT_STATUSES):
                raise
            value = None
        if self._parts is not None:
            self._parts[key] = value
        return value

    def normalize(self, msg: dict[str, Any]) -> Item | None:
        labels = set(msg.get("labelIds", []) or [])
        if labels & EXCLUDED_LABELS:
            return None
        payload = msg.get("payload", {}) or {}
        headers = {h["name"].lower(): h["value"] for h in payload.get("headers", []) or []}
        body, attachments, notes = extract_body(payload, lambda att: self._body_part(msg["id"], att))
        sender_name, sender = parseaddr(headers.get("from", ""))
        sender = sender.lower()
        author_is_me = "SENT" in labels or sender in self.me
        to = [a.lower() for _, a in getaddresses([headers.get("to", "")]) if a]
        cc = [a.lower() for _, a in getaddresses([headers.get("cc", "")]) if a]
        recipients = to + cc
        me_in = "to" if self.me & set(to) else "cc" if self.me & set(cc) else None
        occurred = datetime.fromtimestamp(int(msg.get("internalDate", "0")) / 1000, tz=timezone.utc)
        thread_id = msg.get("threadId", "")
        conversation = f"gmail:{self.account}:{thread_id}"
        thread_key = conversation
        route = "batch"
        github_ref = event = None
        match = GITHUB_MSGID.search(headers.get("message-id", "")) if "github.com" in sender else None
        if match:
            github_ref = f"{match.group(1)}/{match.group(2)}#{match.group(3)}".lower()
            thread_key = conversation = f"github:{github_ref}"
            event = github_event(match.group(4), body, f"{match.group(1)}/{match.group(2)}", match.group(3))
        bulk = bool(labels & BULK_LABELS) or "list-unsubscribe" in headers or \
            headers.get("precedence", "").lower() in ("bulk", "list", "junk")
        return Item(
            source="gmail", account=self.account, external_id=msg["id"], conversation_key=conversation,
            thread_key=thread_key, occurred_at=clock.iso(occurred),
            author=f"{sender_name} <{sender}>".strip() if sender_name else sender,
            author_is_me=author_is_me, title=headers.get("subject", "(no subject)"),
            url=f"https://mail.google.com/mail/?authuser={self.address}#all/{thread_id}",
            body=body, route=route,
            meta={"to": recipients[:20], "recipients": len(recipients), "me_in": me_in,
                  "labels": sorted(labels), "attachments": attachments,
                  "bulk_hint": bulk and not author_is_me, "github_ref": github_ref,
                  "github_sender": headers.get("x-github-sender") if github_ref else None,
                  "github_event": event,
                  "gmail_thread": thread_id, "rfc_message_id": headers.get("message-id"),
                  "in_reply_to": headers.get("in-reply-to"), **notes},
        )

"""Gmail labels that show which tasks a mail belongs to (opt-in: ``email.labels``).

A mail linked to a task as evidence carries ``<root>/<project>`` and
``<root>/<project>/<state>``. The state is open while one of its tasks in that project is
open, else done when one of them is done, else cancelled. Tasks without a project are under
``<root>/<other>``; mail of several projects carries the labels of each. Dismissed mail and
mail never read get no label. Names follow the project titles and the workspace language: a
renamed project renames its labels, and mail keeps them.

Only labels kept in ``gmail_labels`` (created here, or found under their name) are added,
removed or renamed; mail is never archived, marked read or deleted. What was applied is kept
per mail (``gmail_message_labels``), so a pass sends only differences and makes no request
when nothing changed. A label deleted in Gmail is created again with the next change and put
back on its mail. Plain code, no model; run with the sync (``gate_feedback.label_step``).
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from typing import Any, Callable

from . import clock, i18n, store
from .config import Config
from .db import dumps, loads, tx
from .net import HttpError, request_json
from .sources.gmail import API, TokenProvider

BATCH = 1000                  # ids per messages.batchModify (the API's limit)
STATES = ("open", "done", "cancelled")


def enabled(cfg: Config) -> bool:
    return bool(cfg.get("email.enabled") and cfg.get("email.labels"))


class LabelsApi:
    """The label calls of the Gmail API for one mailbox (scope ``gmail.modify``)."""

    def __init__(self, token: Callable[[], str], http: Callable[..., Any] = request_json):
        self.token = token
        self.http = http

    def _call(self, method: str, path: str, body: Any = None) -> Any:
        return self.http(method, f"{API}/{path}", headers={"Authorization": f"Bearer {self.token()}"},
                         body=body, timeout=45)

    def labels(self) -> dict[str, str]:
        return {label["id"]: label["name"] for label in (self._call("GET", "labels") or {}).get("labels", [])}

    def create(self, name: str) -> str:
        return self._call("POST", "labels", {"name": name, "labelListVisibility": "labelShow",
                                             "messageListVisibility": "show"})["id"]

    def rename(self, label_id: str, name: str) -> None:
        self._call("PATCH", f"labels/{label_id}", {"name": name})

    def batch_modify(self, ids: list[str], add: list[str], remove: list[str]) -> None:
        self._call("POST", "messages/batchModify", {"ids": ids, "addLabelIds": add, "removeLabelIds": remove})

    def modify(self, message: str, add: list[str], remove: list[str]) -> None:
        self._call("POST", f"messages/{message}/modify", {"addLabelIds": add, "removeLabelIds": remove})


def api_for(cfg: Config) -> Callable[[str], LabelsApi | None]:
    """The API of each configured mailbox by account name (None for one no longer configured)."""
    accounts = {a["name"]: a for a in cfg.get("email.accounts", [])}
    made: dict[str, LabelsApi] = {}

    def get(account: str) -> LabelsApi | None:
        if account not in accounts:
            return None
        if account not in made:
            made[account] = LabelsApi(TokenProvider(accounts[account]["token_command"]))
        return made[account]
    return get


# --------------------------------------------------------------------------- what each mail should carry

def _state(statuses: set[str]) -> str:
    if statuses & set(store.OPEN_STATUSES):
        return "open"
    return "done" if "done" in statuses else "cancelled"


def wanted(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Item id -> {account, message, keys} for every Gmail mail linked to a task. Keys name
    labels independently of their names: ``root``, ``p:<project>``, ``p:<project>:<state>``,
    ``other``, ``other:<state>``."""
    found: dict[str, dict[str, Any]] = {}
    for r in conn.execute("SELECT s.id, s.account, s.external_id, t.project_id, t.status FROM evidence_links e"
                          " JOIN source_items s ON s.id=e.source_item_id JOIN tasks t ON t.id=e.entity_id"
                          " WHERE e.entity_type='task' AND s.source='gmail'"):
        entry = found.setdefault(r["id"], {"account": r["account"], "message": r["external_id"], "groups": {}})
        group = f"p:{r['project_id']}" if r["project_id"] else "other"
        entry["groups"].setdefault(group, set()).add(r["status"])
    for entry in found.values():
        entry["keys"] = {k for group, statuses in entry.pop("groups").items()
                         for k in (group, f"{group}:{_state(statuses)}")}
    return found


def _parent(key: str) -> str | None:
    if key == "root":
        return None
    parts = key.split(":")
    if parts[0] == "p":
        return "root" if len(parts) == 2 else ":".join(parts[:2])
    return "root" if len(parts) == 1 else parts[0]


def _segment(text: str) -> str:
    """One level of a label name: a slash would nest it."""
    return " ".join(text.replace("/", "-").split())[:80] or "-"


def namer(conn: sqlite3.Connection, cfg: Config) -> Callable[[str], str]:
    root = _segment(cfg.get("email.label_root") or "Todo")
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM projects")}
    lang = cfg.language

    def name(key: str) -> str:
        if key == "root":
            return root
        parts = key.split(":")
        head, rest = (titles.get(parts[1], parts[1]), parts[2:]) if parts[0] == "p" else \
            (i18n.t("gmail_labels.other", lang), parts[1:])
        label = f"{root}/{_segment(head)}"
        return label + (f"/{i18n.t('gmail_labels.' + rest[0], lang)}" if rest else "")
    return name


def changes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Mail whose labels differ from what was applied: {item, account, message, keys, add, remove}."""
    want = wanted(conn)
    have = {r["item_id"]: (set(loads(r["keys"], [])), r["account"], r["external_id"]) for r in conn.execute(
        "SELECT g.item_id, g.keys, s.account, s.external_id FROM gmail_message_labels g"
        " JOIN source_items s ON s.id=g.item_id")}
    out = []
    for item in sorted(set(want) | set(have)):
        keys = want[item]["keys"] if item in want else set()
        applied, account, message = have.get(item) or (set(), want[item]["account"], want[item]["message"])
        if keys != applied:
            out.append({"item": item, "account": account, "message": message, "keys": sorted(keys),
                        "add": sorted(keys - applied), "remove": sorted(applied - keys)})
    return out


def preview(conn: sqlite3.Connection, cfg: Config) -> dict[str, Any]:
    """The labels and how many mails carry each, and how many mails a pass would change (no request)."""
    name = namer(conn, cfg)
    counts = Counter(name(k) for entry in wanted(conn).values() for k in entry["keys"])
    return {"enabled": enabled(cfg), "labels": [{"name": n, "mail": c} for n, c in sorted(counts.items())],
            "mail_to_change": len(changes(conn))}


# --------------------------------------------------------------------------- applying them

def _registry(conn: sqlite3.Connection, account: str) -> dict[str, dict[str, Any]]:
    return {r["key"]: dict(r) for r in conn.execute("SELECT * FROM gmail_labels WHERE account=?", (account,))}


def _forget(conn: sqlite3.Connection, account: str, key: str) -> None:
    """A label deleted in Gmail: its mail no longer carries it (it is created and applied again)."""
    conn.execute("DELETE FROM gmail_labels WHERE account=? AND key=?", (account, key))
    for r in conn.execute("SELECT g.item_id, g.keys FROM gmail_message_labels g JOIN source_items s"
                          " ON s.id=g.item_id WHERE s.account=?", (account,)).fetchall():
        keys = loads(r["keys"], [])
        if key in keys:
            conn.execute("UPDATE gmail_message_labels SET keys=? WHERE item_id=?",
                         (dumps([k for k in keys if k != key]), r["item_id"]))


def _record(conn: sqlite3.Connection, todo: list[dict[str, Any]]) -> None:
    with tx(conn):
        for c in todo:
            if c["keys"]:
                conn.execute("INSERT INTO gmail_message_labels(item_id, keys, updated_at) VALUES (?,?,?)"
                             " ON CONFLICT(item_id) DO UPDATE SET keys=excluded.keys, updated_at=excluded.updated_at",
                             (c["item"], dumps(c["keys"]), clock.iso()))
            else:
                conn.execute("DELETE FROM gmail_message_labels WHERE item_id=?", (c["item"],))


def _check(conn: sqlite3.Connection, api: LabelsApi, account: str, name: Callable[[str], str],
           stats: dict[str, int]) -> tuple[dict[str, str], dict[str, str]]:
    """The mailbox's labels, and key -> label id of those kept here: a label deleted in Gmail is
    forgotten, one whose name changed (project title, language, root) is renamed."""
    existing = api.labels()
    ids: dict[str, str] = {}
    for key, row in _registry(conn, account).items():
        if row["label_id"] not in existing:
            with tx(conn):
                _forget(conn, account, key)
            continue
        if row["name"] != name(key):
            api.rename(row["label_id"], name(key))
            with tx(conn):
                conn.execute("UPDATE gmail_labels SET name=? WHERE account=? AND key=?", (name(key), account, key))
            stats["renamed"] += 1
        ids[key] = row["label_id"]
    return existing, ids


def _ensure(conn: sqlite3.Connection, api: LabelsApi, account: str, name: Callable[[str], str], needed: set[str],
            existing: dict[str, str], ids: dict[str, str], stats: dict[str, int]) -> None:
    """Labels for ``needed`` and their parents (parents first): found by name, else created."""
    by_name = {n.casefold(): i for i, n in existing.items()}
    chain = set()
    for key in needed:
        while key is not None:
            chain.add(key)
            key = _parent(key)
    for key in sorted(chain - set(ids), key=lambda k: (k != "root", k.count(":"), k)):
        label_id = by_name.get(name(key).casefold())
        if label_id is None:
            label_id = api.create(name(key))
            stats["created"] += 1
        with tx(conn):
            conn.execute("INSERT INTO gmail_labels(account, key, label_id, name) VALUES (?,?,?,?)"
                         " ON CONFLICT(account, key) DO UPDATE SET label_id=excluded.label_id, name=excluded.name",
                         (account, key, label_id, name(key)))
        ids[key] = label_id


def _send(api: LabelsApi, todo: list[dict[str, Any]], ids: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """Apply the changes, mail with the same change in one request. Returns the applied changes
    and how many mails are gone from Gmail (recorded too, so they are not tried again)."""
    groups: dict[tuple[tuple[str, ...], tuple[str, ...]], list[dict[str, Any]]] = {}
    for c in todo:
        add = tuple(sorted(ids[k] for k in c["add"]))
        remove = tuple(sorted(ids[k] for k in c["remove"] if k in ids))
        groups.setdefault((add, remove), []).append(c)
    done, gone = [], 0
    for (add, remove), group in groups.items():
        for i in range(0, len(group), BATCH):
            chunk = group[i:i + BATCH]
            if add or remove:
                try:
                    api.batch_modify([c["message"] for c in chunk], list(add), list(remove))
                except HttpError as exc:
                    if exc.status not in (400, 404):
                        raise
                    for c in chunk:    # one of them is gone: the others one by one
                        try:
                            api.modify(c["message"], list(add), list(remove))
                        except HttpError as one:
                            if one.status != 404:
                                raise
                            gone += 1
            done += chunk
    return done, gone


def sync(conn: sqlite3.Connection, cfg: Config, apis: Callable[[str], LabelsApi | None] | None = None
         ) -> dict[str, int]:
    """Bring the labels of linked mail in Gmail to what their tasks say (``wanted``)."""
    get = apis or api_for(cfg)
    name = namer(conn, cfg)
    stats = {"labelled": 0, "created": 0, "renamed": 0, "gone": 0, "unconfigured": 0}
    accounts = {c["account"] for c in changes(conn)}
    accounts |= {r["account"] for r in conn.execute("SELECT account, key, name FROM gmail_labels")
                 if r["name"] != name(r["key"])}
    for account in sorted(accounts):
        api = get(account)
        if api is None:
            stats["unconfigured"] += sum(1 for c in changes(conn) if c["account"] == account)
            continue
        existing, ids = _check(conn, api, account, name, stats)
        todo = [c for c in changes(conn) if c["account"] == account]
        _ensure(conn, api, account, name, {k for c in todo for k in c["add"]}, existing, ids, stats)
        done, gone = _send(api, todo, ids)
        _record(conn, done)
        stats["labelled"] += len(done) - gone
        stats["gone"] += gone
    return stats

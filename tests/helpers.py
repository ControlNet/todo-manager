"""Shared test fixtures: an isolated workspace with a frozen clock."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from todo_manager import clock, config, db
from todo_manager.sources.base import FetchResult, Item, persist_fetch

T0 = datetime(2026, 10, 5, 0, 0, tzinfo=timezone.utc)  # 13:00 Auckland (NZDT)


class WorkspaceCase(unittest.TestCase):
    overrides: dict = {}

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-test-"))
        clock.set_now(T0)
        # The fixtures assert Chinese labels: run under zh-CN (English is covered in test_i18n).
        base = {"language": "zh-CN", "timezone": "Pacific/Auckland", "identity": {"emails": ["me@example.edu"], "github_logins": ["me-gh"],
                             "slack_user_ids": ["UME"], "display_names": ["Test User"]}}
        self.cfg = config.load(self.tmp, overrides=config._deep_merge(base, self.overrides))
        self.conn = db.connect(self.cfg.db_path)

    def tearDown(self) -> None:
        self.conn.close()
        clock.set_now(None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_items(self, *items: Item, source: str | None = None, scope: str = "test") -> dict:
        src = source or items[0].source
        return persist_fetch(self.conn, src, FetchResult(scope=scope, items=list(items), cursor="c1"))

    def item_id(self, external_id: str) -> str:
        return self.conn.execute("SELECT id FROM source_items WHERE external_id=?", (external_id,)).fetchone()["id"]


def mail(ext: str, thread: str, body: str, *, me: bool = False, minutes: float = 0, subject: str = "Subject",
         bulk: bool = False) -> Item:
    when = clock.iso(clock.now() + timedelta(minutes=minutes))
    return Item(source="gmail", account="primary", external_id=ext, conversation_key=f"gmail:primary:{thread}",
                thread_key=f"gmail:primary:{thread}", occurred_at=when, author="me@example.edu" if me else "Alice <alice@x.org>",
                author_is_me=me, title=subject, body=body, meta={"bulk_hint": bulk})


def slack(ext: str, channel: str, body: str, *, thread: str | None = None, minutes: float = 0) -> Item:
    key = f"slack:{channel}:{thread}" if thread else f"slack:{channel}"
    return Item(source="slack", account="T1", external_id=f"{channel}:{ext}", conversation_key=key, thread_key=key,
                occurred_at=clock.iso(clock.now() + timedelta(minutes=minutes)), author="U2", body=body)

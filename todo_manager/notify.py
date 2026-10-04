"""Matrix reminders through ``hermes send`` (no LLM, reuses the Hermes gateway credentials).

Reminders are short and link to the Notion page; the content itself lives in Notion.
Delivery is at-most-once: when a previous attempt timed out the outcome is unknown and
there is no way to look the message up, so it is not sent again.
"""

from __future__ import annotations

import sqlite3
import subprocess
from typing import Any, Callable

from . import outbox
from .config import Config

Runner = Callable[..., subprocess.CompletedProcess]
SEND_TIMEOUT = 60


class MatrixNotifier:
    def __init__(self, conn: sqlite3.Connection, cfg: Config, runner: Runner = subprocess.run):
        self.conn = conn
        self.cfg = cfg
        self.runner = runner

    def page_url(self, task_id: str | None) -> str | None:
        if not task_id:
            return None
        row = self.conn.execute("SELECT url FROM notion_pages WHERE entity_type='task' AND entity_id=?",
                                (task_id,)).fetchone()
        return row["url"] if row and row["url"] else None

    def __call__(self, item: dict[str, Any], reconcile: bool) -> Any:
        if reconcile:
            return {"not_resent": "previous attempt had an unknown outcome"}
        target = self.cfg.get("notify.matrix_target")
        if not target:
            raise outbox.Permanent("notify.matrix_target is not configured")
        payload = item["payload"]
        text = payload["text"]
        url = self.page_url(payload.get("task_id"))
        if url:
            text = f"{text}\n{url}"
        cmd = [self.cfg.get("notify.hermes_bin", "hermes"), "send", "-t", target, "-q", "-f", "-"]
        try:
            proc = self.runner(cmd, input=text, capture_output=True, text=True, timeout=SEND_TIMEOUT)
        except subprocess.TimeoutExpired:
            raise outbox.Uncertain("hermes send timed out") from None
        except OSError as exc:
            raise outbox.Retryable(f"hermes send unavailable: {exc}") from None
        if proc.returncode != 0:
            # stderr may carry unrelated credential-helper warnings; keep only the tail.
            raise outbox.Retryable(f"hermes send exit {proc.returncode}: {(proc.stderr or '').strip()[-300:]}")
        return {"sent_to": target}

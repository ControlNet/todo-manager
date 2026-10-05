"""Build the production connectors from configuration (each is None when not enabled)."""

from __future__ import annotations

import sqlite3
from typing import Any

from .config import Config, read_env_secret


def notion_sync(conn: sqlite3.Connection, cfg: Config) -> Any | None:
    if not cfg.get("notion.enabled"):
        return None
    from .notion.client import NotionClient
    from .notion.sync import NotionSync, load_state
    if not read_env_secret(cfg.get("notion.token_env", "NOTION_API_KEY"), cfg.get("notion.env_file")):
        raise RuntimeError(f"Notion token env {cfg.get('notion.token_env')} is not set")
    state = load_state(conn, cfg)
    if not state.ready:
        raise RuntimeError("Notion is enabled but not set up (run `todo-manager notion setup`)")
    return NotionSync(conn, cfg, NotionClient.from_config(cfg), state)


def kanban(cfg: Config) -> Any | None:
    if not cfg.get("kanban.enabled"):
        return None
    from .kanban import BridgeKanban
    return BridgeKanban.from_config(cfg)

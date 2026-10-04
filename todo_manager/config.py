"""Runtime configuration.

The workspace (``TODO_HOME``, default ``~/.hermes/workspace/todo-manager``) holds a
``config.toml`` that overrides the defaults below, plus ALL runtime data (database, run
manifests, locks, exports, backups). It must lie outside the source tree, which is
meant to be publishable as is. Secrets are never stored here;
connectors reference environment variable names (optionally loaded from an env file
such as ``~/.hermes/.env``).

Values marked PROPOSED are engineering defaults chosen during implementation; they are
not agreed with the user and are listed in docs/open-decisions.md.
"""

from __future__ import annotations

import copy
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path("~/.hermes/workspace/todo-manager").expanduser()

DEFAULTS: dict[str, Any] = {
    "timezone": "Pacific/Auckland",
    "identity": {
        # The user's own identities. Never guessed: filled from verified inventory.
        "emails": [],
        "github_logins": [],
        "slack_user_ids": [],
        "notion_user_ids": [],
        "matrix_user_ids": [],
        "display_names": [],
    },
    "email": {
        "enabled": False,
        "accounts": [],  # gog account identifiers
        # Gmail search applied to the incremental window; sent mail is always included.
        "query": "-in:spam -in:trash -in:chats",
        "page_size": 100,
        "overlap_minutes": 120,
        "initial_lookback_days": 14,  # PROPOSED initial backfill window
        "quiet_minutes": 15,  # PROPOSED: thread must be quiet this long before analysis
        "min_wake_interval_minutes": 60,  # PROPOSED: email alone wakes the model at most hourly
    },
    "slack": {
        "enabled": False,
        "token_env": "SLACK_USER_TOKEN",
        "conversations": [],  # channel / DM IDs confirmed in scope
        "quiet_minutes": 30,  # agreed: >= 30 minutes after the last relevant message
        "page_size": 200,
    },
    "github": {
        "enabled": False,
        "repos": [],  # "owner/name"
        "quiet_minutes": 10,  # PROPOSED
        "min_wake_interval_minutes": 60,  # PROPOSED
        "overlap_minutes": 30,
        "bot_logins": ["dependabot[bot]", "github-actions[bot]", "renovate[bot]", "codecov[bot]"],
    },
    "commits": {
        "enabled": False,
        "repos": [],  # "owner/name" or local paths
        "digest_time": "21:00",  # PROPOSED local time (Pacific/Auckland)
        "max_attempts_per_day": 2,  # model attempts budget for one day's digest
    },
    "notion": {
        "enabled": False,
        "token_env": "NOTION_API_KEY",
        "env_file": "~/.hermes/.env",
        "api_version": "2026-03-11",  # markdown + views endpoints need this version
        "tasks_data_source_id": "",
        "projects_data_source_id": "",
        "home_page_id": "",
        "bot_user_id": "",
        "min_request_interval_seconds": 0.4,
        "active_poll_minutes": 5,
        "cold_comment_poll_minutes": 60,  # PROPOSED: open tasks without recent activity
        "cold_comment_pages_per_run": 30,  # PROPOSED: round-robin budget per gate run
        "history_poll_hours": 24,  # PROPOSED low-frequency sweep of recently closed pages
    },
    "kanban": {
        "enabled": False,
        "board": "todo-manager",
        "hermes_bin": "hermes",
        "assignee": "",  # Hermes profile that executes delegations ("" = board default)
        "max_running": 1,
    },
    "notify": {
        "matrix": False,  # short link reminders only for important deliveries/blocks/failures
        "matrix_target": "",  # `hermes send -t` target, e.g. "matrix:<dm name>" (confirm with the user)
        "hermes_bin": "hermes",
        "failure_alert_after_minutes": 60,
    },
    "feedback": {
        "max_items_per_run": 20,
        "stale_claim_minutes": 60,
        "max_attempts": 3,
    },
    "wake": {
        # Upper bound on items rendered into one model context; the rest wait for next run.
        "max_items_per_run": 60,
        "max_context_chars": 60000,
    },
}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


@dataclass
class Config:
    home: Path
    data: dict[str, Any]

    def get(self, dotted: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        return self.data.get(name, {})

    @property
    def tz(self) -> str:
        return self.data["timezone"]

    @property
    def db_path(self) -> Path:
        return self.home / "state" / "todo.sqlite"


# The checkout/installation this code runs from. Runtime data never lives under it.
SOURCE_ROOT = Path(__file__).resolve().parent.parent


class ConfigError(ValueError):
    pass


def resolve_home(home: str | os.PathLike[str] | None = None) -> Path:
    if home:
        path = Path(home).expanduser()
    else:
        env = os.environ.get("TODO_HOME")
        path = Path(env).expanduser() if env else DEFAULT_HOME
    resolved = path.resolve()
    if resolved == SOURCE_ROOT or SOURCE_ROOT in resolved.parents:
        raise ConfigError(f"TODO_HOME {path} is inside the source tree {SOURCE_ROOT}; runtime data belongs in the"
                          f" Hermes workspace (default {DEFAULT_HOME})")
    return path


def load(home: str | os.PathLike[str] | None = None, overrides: dict[str, Any] | None = None) -> Config:
    root = resolve_home(home)
    data = copy.deepcopy(DEFAULTS)
    cfg_file = root / "config.toml"
    if cfg_file.exists():
        with cfg_file.open("rb") as fh:
            data = _deep_merge(data, tomllib.load(fh))
    if overrides:
        data = _deep_merge(data, overrides)
    return Config(home=root, data=data)


def read_env_secret(name: str, env_file: str | None = None) -> str | None:
    """Return a secret from the process env or a dotenv-style file. Never logged."""
    value = os.environ.get(name)
    if value:
        return value
    if not env_file:
        return None
    path = Path(env_file).expanduser()
    if not path.exists():
        return None
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, raw = stripped.partition("=")
        if key.strip().removeprefix("export ").strip() == name:
            raw = raw.strip()
            if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "'\"":
                raw = raw[1:-1]
            return raw or None
    return None

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
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import i18n

DEFAULT_HOME = Path("~/.hermes/workspace/todo-manager").expanduser()

DEFAULTS: dict[str, Any] = {
    "timezone": "UTC",   # `init` writes the system time zone into config.toml
    # Language of everything the user reads (Notion pages, comments and replies); see i18n.
    # TODO_MANAGER_LANG overrides it for one process.
    "language": i18n.DEFAULT,
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
        "token_file": "",  # alternative to token_env: a private file holding only the token
        "conversations": [],  # channel / DM IDs confirmed in scope
        "direct_messages": False,  # also read every 1:1 DM of the user (discovered each run)
        "group_direct_messages": False,  # also read every group DM (mpim) of the user
        "channels": False,  # also read every channel the user is a member of (public and private)
        "exclude": [],  # blacklist for discovered conversations: ids, DM user ids, channel name globs
        "active_hours": 168,  # agreed: a conversation with a human message this recent is read every run
        "quiet_poll_minutes": 30,  # agreed: other conversations are read at most this often (staggered)
        "quiet_minutes": 30,  # a conversation is analysed after this many quiet minutes
        "page_size": 200,
        # Replies in threads older than the re-read window (PROPOSED values): threads active in the
        # last thread_watch_days are watched (at most thread_index_max per conversation); older
        # parents are scanned every thread_scan_minutes, at most thread_scan_pages history pages.
        "thread_watch_days": 30,
        "thread_index_max": 200,
        "thread_scan_minutes": 60,
        "thread_scan_pages": 5,
    },
    "github": {
        "enabled": False,
        "repos": [],  # "owner/name"
        "quiet_minutes": 10,  # PROPOSED
        "min_wake_interval_minutes": 60,  # PROPOSED
        "overlap_minutes": 30,
        "bot_logins": [],  # accounts GitHub does not mark as bots (it marks apps: type Bot, "<name>[bot]")
    },
    "commits": {
        "enabled": False,
        "repos": [],  # "owner/name" or local paths
        "digest_time": "21:00",  # PROPOSED local time
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
        "body_reads_per_run": 10,  # PROPOSED: page bodies read per gate run after human edits (rest next run)
    },
    "kanban": {
        "enabled": False,
        "board": "todo-manager",
        "hermes_bin": "hermes",
        "assignee": "",  # Explicit executable Hermes profile; blank submissions are reported, not dispatched
        # Hermes per-card failure breaker: 1 = a crashed/timed-out/stale run parks the card for you
        # instead of an automatic respawn. Cards allowed an external action always get 1 (a respawn
        # could repeat an action whose outcome is unknown); 0 = Hermes' own default for the others.
        # Rate-limit requeues are not counted by Hermes. How many cards run at once is Hermes'.
        "max_retries": 1,
        "skills": [],  # Optional native worker skills, never the entire Todo skill by default
        "notify_platform": "",
        "notify_chat_id": "",
        "notify_thread_id": "",
        "notify_profile": "",

    },
    "health": {
        "gate_idle_minutes": 60,  # a gate that has not run this long is reported
    },
    # The Hermes installation and what `install` sets on new cron jobs ("" = Hermes' default).
    # A model, provider or reasoning effort changed on a job in Hermes is kept by later upgrades.
    "hermes": {
        "home": "",  # "" = $HERMES_HOME or ~/.hermes
        "bin": "hermes",
        "launcher": "",  # "" = ~/.local/bin/todo-manager
        "model": "",
        "provider": "",
        "reasoning_effort": "",
    },
    "feedback": {
        "max_items_per_run": 20,
        "stale_claim_minutes": 60,
        "max_attempts": 3,
        # The rendered feedback context of one run; the oldest items that fit are claimed, the
        # rest waits for the next run (the first item always goes: its text is clipped with a
        # pointer to the full text).
        "max_context_chars": 60000,
    },
    "wake": {
        # Upper bounds for one intake run; the rest waits for the next run (PROPOSED values). The
        # rendered context is checked against max_context_chars before the model is woken; new
        # material gets at most max_evidence_chars of it. Each message is shown up to
        # max_item_chars (a bulk mailing up to max_bulk_item_chars, an earlier message of the
        # conversation up to max_earlier_chars), with a line naming the command for the rest.
        "max_items_per_run": 60,
        "max_context_chars": 60000,
        "max_evidence_chars": 40000,
        "max_item_chars": 4000,
        "max_bulk_item_chars": 800,
        "max_earlier_chars": 600,
        # A conversation offered to this many runs without an outcome for every message is
        # marked failed (reported by health; its messages are kept).
        "max_attempts": 3,
        # A source failing this long is reported once a day (a cron failure Hermes delivers).
        "problem_minutes": 60,
    },
}


# Keys earlier versions read, with what replaced them: reported by ``health`` while config.toml
# still sets them (they have no effect).
REMOVED_KEYS = {
    "kanban.max_running": "Hermes schedules agent work now; its dispatcher limits apply to every board"
                          " (`hermes config get kanban.max_in_progress`)",
    "wake.over_budget": "a context over wake.max_context_chars is clipped and the run goes ahead",
    "feedback.over_budget": "a context over feedback.max_context_chars is clipped and the run goes ahead",
    "wake.max_questions_per_run": "workflow questions belong in the feedback prompt; personal preferences use native USER.md",
}


def removed_keys(home: Path) -> list[str]:
    """Lines for the keys of ``REMOVED_KEYS`` that config.toml still sets."""
    from .config_file import explicit
    try:
        data = explicit(home / "config.toml")
    except (OSError, tomllib.TOMLDecodeError):
        return []
    found = []
    for key, why in REMOVED_KEYS.items():
        section, _, name = key.partition(".")
        if isinstance(data.get(section), dict) and name in data[section]:
            found.append(f"config.toml sets {key}, which no longer has an effect: {why}; the line can be removed")
    return found


def local_timezone() -> str:
    """IANA name of the system time zone (TZ, /etc/timezone or the /etc/localtime link), else UTC."""
    candidates = [os.environ.get("TZ", "").lstrip(":")]
    try:
        candidates.append(Path("/etc/timezone").read_text(encoding="utf-8").strip())
    except OSError:
        pass
    link = Path("/etc/localtime")
    if link.is_symlink():
        target = str(link.resolve())
        if "zoneinfo/" in target:
            candidates.append(target.split("zoneinfo/", 1)[1])
    for name in candidates:
        if name and "/" in name:
            try:
                ZoneInfo(name)
                return name
            except (ZoneInfoNotFoundError, ValueError):
                continue
    return "UTC"


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
    def language(self) -> str:
        return self.data["language"]

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


def recovery_config(home: str | os.PathLike[str]) -> Config:
    """Recovery uses only fixed workspace paths, never the target's possibly broken settings."""
    return Config(resolve_home(home), copy.deepcopy(DEFAULTS))


def _validate_types(values: dict[str, Any], defaults: dict[str, Any], path: Path, prefix: str = '') -> None:
    for key, value in values.items():
        if key not in defaults:
            continue
        expected = defaults[key]
        name = prefix + key
        valid = (isinstance(value, (int, float)) and not isinstance(value, bool)
                 if isinstance(expected, (int, float)) and not isinstance(expected, bool)
                 else isinstance(value, type(expected)))
        if not valid:
            raise ConfigError(f'{path}: {name} must be {type(expected).__name__}, got {type(value).__name__}')
        if isinstance(expected, dict):
            _validate_types(value, expected, path, name + '.')


def load(home: str | os.PathLike[str] | None = None, overrides: dict[str, Any] | None = None) -> Config:
    root = resolve_home(home)
    data = copy.deepcopy(DEFAULTS)
    cfg_file = root / "config.toml"
    if cfg_file.exists():
        with cfg_file.open("rb") as fh:
            try:
                loaded = tomllib.load(fh)
            except tomllib.TOMLDecodeError as exc:
                raise ConfigError(f'{cfg_file}: {exc}') from None
            _validate_types(loaded, DEFAULTS, cfg_file)
            data = _deep_merge(data, loaded)
    if overrides:
        data = _deep_merge(data, overrides)
    data["language"] = i18n.set_language(os.environ.get("TODO_MANAGER_LANG") or data.get("language"))
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

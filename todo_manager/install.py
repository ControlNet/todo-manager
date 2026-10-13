"""Deterministic installation steps used by the single install flow.

``install_flow`` collects discovery choices in temporary local state and presents one
combined confirmation. The steps here retain Notion setup recovery, source discovery,
managed-file deployment and native cron preservation; they are not separate CLI stages.
No global Hermes switches or native memory files are modified.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import config, config_file, db, delegation, i18n, prefs
from .files import private_dir
from .net import HttpError, HttpTimeout, request_json

Runner = Callable[..., subprocess.CompletedProcess]
Option = tuple[str, str]   # (value, label)
Check = Callable[[list[str]], "str | None"]   # the picked values -> why they cannot be used, or None

STEPS = ("workspace", "email", "slack", "github", "notion", "kanban", "hermes", "schedule")
GMAIL_PROFILE = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
SLACK_AUTH_TEST = "https://slack.com/api/auth.test"
GITHUB_USER = "https://api.github.com/user"

CONFIG_TEMPLATE = """\
# Todo Manager runtime configuration. `todo-manager install` fills in what it infers from
# Hermes and what you chose; a value you set here yourself is kept. Secrets are never stored
# here: connectors name environment variables or token files.
timezone = "UTC"     # IANA name
# Language of what you read in Notion (pages, comments, replies): en, zh-CN.
# Notion property/option names keep the language used when Notion was set up.
language = "en"
"""


class InstallError(RuntimeError):
    pass


class NeedsAnswer(Exception):
    def __init__(self, key: str, question: str, options: list[Option], multiple: bool, error: str | None = None):
        super().__init__(question)
        self.key, self.question, self.options, self.multiple, self.error = key, question, options, multiple, error

    def to_json(self, prior: str = "") -> dict[str, Any]:
        """``prior``: the arguments already given, repeated in the command to run next."""
        value = "<value>[,<value>...]" if self.multiple else "<value>"
        out = {"status": "needs_answer", "key": self.key, "question": self.question, "multiple": self.multiple,
               "options": [{"value": v, "label": label} for v, label in self.options],
               "answer_with": _command(prior, f"--answer {self.key}={value}")}
        if self.error:
            out["error"] = self.error
        return out


class NeedsConfirmation(Exception):
    def __init__(self, step: str, title: str, changes: list[str]):
        super().__init__(title)
        self.step, self.title, self.changes = step, title, changes

    def to_json(self, prior: str = "") -> dict[str, Any]:
        return {"status": "needs_confirmation", "step": self.step, "title": self.title, "changes": self.changes,
                "confirm_with": _command(prior, f"--yes {self.step}"),
                "decline_with": _command(prior, f"--no {self.step}")}


def _command(prior: str, extra: str) -> str:
    return " ".join(x for x in ("todo-manager install", prior, extra) if x)


def _parse_choice(raw: str, options: list[Option], multiple: bool) -> list[str] | None:
    values = [v for v, _ in options]
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    if not parts:
        return [] if multiple else None
    out = []
    for p in parts:
        if p in values:
            out.append(p)
        elif p.isdigit():
            if not 1 <= int(p) <= len(options):
                return None
            out.append(values[int(p) - 1])
        elif not multiple:                     # one free value is allowed (e.g. a page id)
            out.append(p)
        else:
            return None
    if not multiple and len(out) != 1:
        return None
    return list(dict.fromkeys(out))


class TerminalPrompter:
    def __init__(self, input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print):
        self.input, self.output = input_fn, output

    def say(self, text: str) -> None:
        self.output(text)

    def choose(self, key: str, question: str, options: list[Option], multiple: bool = False,
               check: Check | None = None) -> list[str]:
        self.output(question)
        for n, (_, label) in enumerate(options, 1):
            self.output(f"  {n}. {label}")
        hint = "numbers separated by commas; empty for none" if multiple else "a number, or an id"
        while True:
            picked = _parse_choice(self.input(f"[{hint}] > "), options, multiple)
            error = "not one of the options" if picked is None else (check(picked) if check else None)
            if not error:
                return picked
            self.output(f"{error}; try again.")

    def confirm(self, step: str, title: str, changes: list[str]) -> bool:
        self.output(title)
        for c in changes:
            self.output(f"  - {c}")
        return self.input("Apply these changes? [y/N] ").strip().lower() in ("y", "yes")


class AnswerPrompter:
    """Non-interactive: answers and confirmations given up front; a missing one stops the run."""

    def __init__(self, answers: dict[str, str] | None = None, confirmed: set[str] | None = None,
                 declined: set[str] | None = None, output: Callable[[str], None] = print):
        self.answers = answers or {}
        self.confirmed = confirmed or set()
        self.declined = declined or set()
        self.output = output
        self.asked: list[str] = []

    def say(self, text: str) -> None:
        self.output(text)

    def choose(self, key: str, question: str, options: list[Option], multiple: bool = False,
               check: Check | None = None) -> list[str]:
        self.asked.append(key)
        if key not in self.answers:
            raise NeedsAnswer(key, question, options, multiple)
        picked = _parse_choice(self.answers[key], options, multiple)
        error = f"{self.answers[key]!r} is not an option" if picked is None else (check(picked) if check else None)
        if error:
            raise NeedsAnswer(key, question, options, multiple, error=error)
        return picked

    def confirm(self, step: str, title: str, changes: list[str]) -> bool:
        self.asked.append(step)
        if step in self.declined:
            return False
        if step in self.confirmed or "all" in self.confirmed:
            return True
        raise NeedsConfirmation(step, title, changes)


@dataclass
class Env:
    """Everything install reads or changes outside the workspace (replaced by test doubles)."""
    runner: Runner = subprocess.run
    http: Callable[..., Any] = request_json
    notion: Callable[[config.Config], Any] | None = None       # cfg -> NotionClient
    kanban: Callable[[config.Config], Any] | None = None       # cfg -> BridgeKanban
    installer: Callable[[config.Config], Any] | None = None    # cfg -> hermes_install.Installer
    himalaya_config: Path | None = None

    def notion_client(self, cfg: config.Config) -> Any:
        from .notion.client import NotionClient
        return (self.notion or NotionClient.from_config)(cfg)

    def kanban_client(self, cfg: config.Config) -> Any:
        from .kanban import BridgeKanban
        return (self.kanban or BridgeKanban.from_config)(cfg)

    def hermes_installer(self, cfg: config.Config) -> Any:
        from . import hermes_install
        if self.installer:
            return self.installer(cfg)
        return hermes_install.Installer(cfg, hermes_install.Paths.from_config(cfg), runner=self.runner)


def init_workspace(home: str | os.PathLike[str] | None, language: str, timezone: str | None = None) -> dict[str, Any]:
    """Directories, the workspace guide, config.toml (only when missing) and the database."""
    cfg = config.load(home)
    private_dir(cfg.home)   # also tightens a workspace created before this rule
    for sub in ("state", "runs", "exports", "backups", "notes"):
        (cfg.home / sub).mkdir(parents=True, exist_ok=True)
    target = cfg.home / "config.toml"
    created = False
    if not target.exists():
        target.write_text(CONFIG_TEMPLATE.replace('language = "en"', f'language = "{language}"')
                          .replace('timezone = "UTC"', f'timezone = "{timezone or config.local_timezone()}"'),
                          encoding="utf-8")
        created = True
    else:
        language = config.load(home).language   # an existing config is never rewritten here
    # The conventions file is the user's: created from the template when missing, otherwise left
    # to the Hermes step (backup and managed replacement).
    prefs.ensure(cfg.home, language)
    conn = db.connect(cfg.db_path)
    try:
        prefs.ensure_migrated(conn, cfg.home, language)
    finally:
        conn.close()
    return {"home": str(cfg.home), "config": str(target), "config_created": created, "db": str(cfg.db_path),
            "language": language}


UTC_NAMES = {"UTC", "Etc/UTC", "Etc/UCT", "Etc/GMT", "Etc/Universal", "Etc/Zulu", "GMT", "UCT", "Universal", "Zulu"}


def _check_timezone(picked: list[str]) -> str | None:
    try:
        ZoneInfo(picked[0])
    except (ZoneInfoNotFoundError, ValueError):
        return f"{picked[0]!r} is not an IANA time zone name"
    return None


def _page_id(value: str) -> str:
    """A page id from an id (with or without dashes) or a Notion URL."""
    m = re.search(r"([0-9a-fA-F]{32})(?:[?#].*)?$", value.replace("-", ""))
    if not m:
        return value.strip()
    h = m.group(1).lower()
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"


class Install:
    def __init__(self, home: str | os.PathLike[str] | None, prompter: Any, env: Env | None = None,
                 language: str | None = None):
        self.home = config.resolve_home(home)
        self.path = self.home / "config.toml"
        self.prompter = prompter
        self.env = env or Env()
        self.language = language
        self.report: list[str] = []

    # ------------------------------------------------------------------ helpers
    def cfg(self) -> config.Config:
        return config.load(self.home)

    def explicit(self, table: str) -> dict[str, Any]:
        return config_file.explicit(self.path).get(table) or {}

    def write(self, values: dict[str, Any]) -> list[str]:
        return config_file.set_values(self.path, values)

    def note(self, step: str, text: str) -> None:
        line = f"{step}: {text}"
        self.report.append(line)
        self.prompter.say(line)

    def run_cmd(self, cmd: list[str], timeout: int = 60) -> str | None:
        """Stdout of a local command, None when it fails (never raises)."""
        cmd = [str(Path(c).expanduser()) if c.startswith("~") else c for c in cmd]
        try:
            proc = self.env.runner(cmd, capture_output=True, text=True, timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def hermes(self, *args: str) -> str | None:
        return self.run_cmd([self.cfg().get("hermes.bin", "hermes"), *args])

    def hermes_home(self) -> Path:
        from .hermes_install import Paths
        return Paths.from_config(self.cfg()).hermes_home

    def env_file(self) -> str:
        """Hermes' .env, recorded as ``notion.env_file`` (Notion and Slack read their tokens from it)."""
        cfg = self.cfg()
        if "env_file" in self.explicit("notion"):
            return cfg.get("notion.env_file")
        found = self.hermes("config", "env-path") or str(self.hermes_home() / ".env")
        if Path(found).expanduser() != Path(cfg.get("notion.env_file")).expanduser():
            self.write({"notion.env_file": found})
        return found

    def get_json(self, url: str, token: str) -> Any:
        return self.env.http("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    def add_identity(self, kind: str, values: list[str]) -> None:
        current = list(self.cfg().get(f"identity.{kind}") or [])
        merged = current + [v for v in values if v and v not in current]
        if merged != current:
            self.write({f"identity.{kind}": merged})

    def run(self) -> list[str]:
        for step in STEPS:
            getattr(self, f"step_{step}")()
        return self.report

    # ------------------------------------------------------------------ 1. workspace
    def step_workspace(self) -> None:
        exists = self.path.exists()
        language = (i18n.normalize(self.language) or i18n.normalize(self.hermes("config", "get", "display.language"))
                    or i18n.from_environment())
        tz = None
        if not exists:
            tz = self.hermes("config", "get", "timezone") or config.local_timezone()
            if tz in UTC_NAMES:   # a server's default, says nothing about where you are
                [tz] = self.prompter.choose(
                    "timezone", "Which time zone are you in? Hermes has no timezone set and this machine runs on UTC."
                    " An IANA name, e.g. Europe/Berlin:", [(tz, f"{tz} (this machine)")], check=_check_timezone)
        out = init_workspace(self.home, language, tz)
        self.note("workspace", f"{out['home']} ({'config.toml kept' if exists else 'created'};"
                               f" language {self.cfg().language}, time zone {self.cfg().tz})")

    # ------------------------------------------------------------------ 2. email
    def himalaya_accounts(self) -> list[tuple[str, list[str]]]:
        path = self.env.himalaya_config or (
            Path(os.environ.get("XDG_CONFIG_HOME") or "~/.config").expanduser() / "himalaya" / "config.toml")
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            return []
        out = []
        for name, acc in (data.get("accounts") or {}).items():
            cmd = (((acc.get("gmail") or {}).get("auth") or {}).get("token") or {}).get("command")
            if isinstance(cmd, list) and cmd:
                out.append((name, [str(c) for c in cmd]))
        return out

    def step_email(self) -> None:
        cfg = self.cfg()
        if "enabled" in self.explicit("email") or self.explicit("email").get("accounts"):
            accounts = cfg.get("email.accounts") or []
            if cfg.get("email.enabled"):
                self.add_identity("emails", [a["address"] for a in accounts if a.get("address")])
            read = len(accounts) if cfg.get("email.enabled") else "no"
            self.note("email", f"kept as configured ({read} mailbox(es))")
            return
        found = []
        for name, command in self.himalaya_accounts():
            token = self.run_cmd(command, timeout=30)
            if not token:
                self.note("email", f"mailbox {name}: its token helper failed; not offered")
                continue
            try:
                address = self.get_json(GMAIL_PROFILE, token.splitlines()[-1].strip())["emailAddress"]
            except (HttpError, HttpTimeout, KeyError, TypeError) as exc:
                self.note("email", f"mailbox {name}: Gmail profile not readable ({type(exc).__name__}); not offered")
                continue
            found.append((name, address, command))
        if not found:
            self.note("email", "no Gmail account in Hermes (himalaya); mail is not read")
            return
        chosen = found
        if len(found) > 1:
            picked = self.prompter.choose("email.accounts", "Hermes has several mailboxes. Which should Todo Manager"
                                          " read?", [(n, f"{n} <{a}>") for n, a, _ in found], multiple=True)
            chosen = [f for f in found if f[0] in picked]
        self.write({"email.enabled": bool(chosen),
                    "email.accounts": [{"name": n, "address": a, "token_command": c} for n, a, c in chosen]})
        self.add_identity("emails", [a for _, a, _ in chosen])
        self.note("email", "reading " + ", ".join(f"{n} <{a}>" for n, a, _ in chosen) if chosen
                  else "no mailbox chosen; mail is not read")

    # ------------------------------------------------------------------ 3. slack
    def step_slack(self) -> None:
        if "enabled" in self.explicit("slack"):
            self.note("slack", "kept as configured")
            return
        token_file = self.hermes_home() / "credentials" / "slack_user_token"
        source: dict[str, Any] = {}
        token = None
        if token_file.is_file():
            token = token_file.read_text(encoding="utf-8").strip() or None
            source = {"slack.token_file": str(token_file)}
        if not token:
            token = config.read_env_secret("SLACK_USER_TOKEN", self.env_file())
            source = {"slack.token_env": "SLACK_USER_TOKEN"}
        if not token:
            self.note("slack", "no Slack user token in Hermes; Slack is not read")
            return
        try:
            data = self.get_json(SLACK_AUTH_TEST, token)
        except (HttpError, HttpTimeout) as exc:
            self.note("slack", f"auth.test failed ({type(exc).__name__}); Slack is not set up, run install again")
            return
        if not data.get("ok"):
            self.note("slack", f"the token was refused ({data.get('error')}); Slack is not read")
            return
        self.write({**source, "slack.enabled": True, "slack.team": data["team_id"], "slack.direct_messages": True,
                    "slack.group_direct_messages": True, "slack.channels": True})
        self.add_identity("slack_user_ids", [data["user_id"]])
        self.note("slack", f"reading your DMs, group DMs and channels in {data.get('team') or data['team_id']}"
                           f" as {data.get('user') or data['user_id']} (leave some out with slack.exclude)")

    # ------------------------------------------------------------------ 4. github
    def step_github(self) -> None:
        if "enabled" in self.explicit("github"):
            self.note("github", "kept as configured")
            return
        token = self.run_cmd(["gh", "auth", "token"], timeout=20)
        if not token:
            self.note("github", "gh is not logged in; GitHub is not read")
            return
        try:
            login = self.get_json(GITHUB_USER, token)["login"]
        except (HttpError, HttpTimeout, KeyError, TypeError) as exc:
            self.note("github", f"GET /user failed ({type(exc).__name__}); GitHub is not set up, run install again")
            return
        self.write({"github.enabled": True})
        self.add_identity("github_logins", [login])
        self.note("github", f"reading notifications of threads you take part in as {login}")

    # ------------------------------------------------------------------ 5. notion
    def step_notion(self) -> None:
        from .notion.sync import load_state
        env_file = self.env_file()
        cfg = self.cfg()
        if not config.read_env_secret(cfg.get("notion.token_env", "NOTION_API_KEY"), env_file):
            self.note("notion", f"no {cfg.get('notion.token_env')} in Hermes' {env_file}; Notion is not set up")
            return
        client = self.env.notion_client(cfg)
        conn = db.connect(cfg.db_path)
        try:
            state = load_state(conn, cfg)
            finished = bool(db.get_meta(conn, "notion.setup_at")) or (
                state.ready and not db.get_meta(conn, "notion.home_page_id"))   # ids set in config.toml
            if not finished:
                if not self._notion_setup(conn, client, cfg, db.get_meta(conn, "notion.home_page_id")):
                    return
            elif not cfg.get("notion.enabled") and "enabled" not in self.explicit("notion"):
                self.write({"notion.enabled": True})
                self.note("notion", "set up; sync enabled")
            else:
                self.note("notion", "set up" if cfg.get("notion.enabled") else
                          "set up, but notion.enabled = false in config.toml: not synced")
            if finished:
                self._notion_views(conn, client)
        finally:
            conn.close()
        self._notion_identity(client)

    def _notion_views(self, conn: Any, client: Any) -> None:
        """Views created by an earlier version: those still as created are updated (confirmed
        first); those the user changed are left alone and reported."""
        from .notion import setup
        from .notion import decision_migration
        migration = decision_migration.plan(conn, client)
        if migration:
            if not self.prompter.confirm('notion_decision_ui', 'Upgrade Notion decision board and task visibility:', migration['changes']):
                self.note('notion', 'decision UI migration not applied; run install to complete it')
                return
            saved = decision_migration.apply(conn, client, self.cfg().home)
            self.note('notion', f'decision board migrated; original schema/view snapshot: {saved}')
        from .notion import agent_board_migration
        board = agent_board_migration.plan(conn, client)
        if board:
            if not self.prompter.confirm('notion_agent_board', 'Upgrade the Notion agent work board:', board['changes']):
                self.note('notion', 'agent board migration not applied; run install to complete it')
                return
            saved = agent_board_migration.apply(conn, client, self.cfg().home)
            self.note('notion', f'agent board migrated; original view snapshot: {saved}')
        updates, notes = setup.view_upgrades(conn, client)
        for text in notes:
            self.note("notion", text)
        if updates and not self.prompter.confirm("notion_views", "Update Todo Manager's Notion views for this"
                                                 " version:", [u["change"] for u in updates]):
            self.note("notion", "views not updated (declined); run install again to update them")
            return
        if updates or notes:
            setup.apply_view_upgrades(conn, client, updates)

    def _notion_setup(self, conn: Any, client: Any, cfg: config.Config, home: str | None) -> bool:
        """``home``: the page of an interrupted setup, finished on the same page without asking again."""
        from .notion import setup
        from .notion.client import NotionError
        title = home
        if not home:
            pages = client.top_level_pages()
            if not pages:
                name = (client.me() or {}).get("name") or "the integration"
                self.note("notion", f"no top-level page is shared with {name!r}. In Notion, create an empty page"
                                    f" (it becomes the Todo Manager home page), add {name!r} under ... ->"
                                    " Connections, then run `todo-manager install` again")
                return False
            titles = {p["id"]: p["title"] or "(untitled)" for p in pages}
            pages_by_id: dict[str, dict[str, Any]] = {}

            def readable(picked: list[str]) -> str | None:
                pid = _page_id(picked[0])
                try:
                    page = client.retrieve_page(pid)
                except NotionError as exc:
                    return f"page {pid} cannot be read by the integration ({exc})"
                if page.get("in_trash") or page.get("archived"):
                    return f"page {pid} is in the trash"
                pages_by_id[pid] = page
                return None

            [answer] = self.prompter.choose(
                "notion.home_page", "Which Notion page should be the Todo Manager home page? The Projects and"
                " Tasks databases and their views are created inside it.",
                [(p["id"], f"{titles[p['id']]}  ({p['last_edited_time'][:10]}, {p['id']})") for p in pages],
                check=readable)
            home = _page_id(answer)
            title = titles.get(home) or home
        changes = setup.plan(home_page_id=home, lang=cfg.language)
        if (client.page_markdown(home).get("markdown") or "").strip():
            changes.append("the page already has content: it is kept; the Todo Manager part goes at the top")
        if not self.prompter.confirm("notion", f"First write to your Notion workspace, on the page {title!r}:",
                                     changes):
            self.note("notion", "not set up (declined); run install again to set it up")
            return False
        setup.apply(conn, client, home_page_id=home, lang=cfg.language)
        self.write({"notion.enabled": True})
        self.note("notion", f"the page {title!r} is the home page; sync enabled")
        return True

    def _notion_identity(self, client: Any) -> None:
        if self.cfg().get("identity.notion_user_ids"):
            return
        people = [u for u in client.users() if u.get("type") == "person"]
        if not people:
            self.note("notion", "no person member found; Notion comments are not recognised as yours")
            return
        ids = [people[0]["id"]]
        if len(people) > 1:
            label = lambda u: " ".join(x for x in (u.get("name"), (u.get("person") or {}).get("email")) if x)
            ids = self.prompter.choose("identity.notion_user", "Which Notion user are you?",
                                       [(u["id"], label(u) or u["id"]) for u in people])
        self.add_identity("notion_user_ids", ids)

    # ------------------------------------------------------------------ 6. kanban
    def step_kanban(self) -> None:
        """The board todo-manager's cards go to. Hermes' global switches are the user's: they are
        read and explained, never changed (``delegation.SWITCH_NOTES``)."""
        from .kanban import KanbanError
        cfg = self.cfg()
        if self.explicit("kanban").get("enabled") is False:
            self.note("kanban", "disabled by user")
            return
        board = cfg.get("kanban.board", "todo-manager")
        try:
            kb = self.env.kanban_client(cfg)
            values = kb.settings()
            readiness = kb.ping() or {}
            exists = bool(readiness.get('board_exists'))
            if readiness.get('missing') or readiness.get('board_state') in ('archived', 'orphaned', 'invalid'):
                self.note('kanban', f"board {board!r} unavailable: {readiness.get('reason') or readiness.get('missing')}; "
                          "delegations stay local; inspect Hermes board recovery")
                return
        except (KanbanError, OSError) as exc:
            self.note("kanban", f"Hermes Kanban is not reachable ({exc}); delegations stay local")
            return
        if not exists:
            if not self.prompter.confirm("kanban", "Delegating work to Hermes agents needs its own board:",
                                         [f"create the Hermes board {board!r}"]):
                self.note("kanban", "not changed (declined); delegations stay local")
                return
            kb.create_board("Todo Manager", "Delegations from Todo Manager")
        if not cfg.get("kanban.enabled"):
            if not exists or "enabled" not in self.explicit("kanban"):
                self.write({"kanban.enabled": True})
            else:
                self.note("kanban", "ready, but kanban.enabled = false in config.toml: delegations stay local")
                return
        for key, text in delegation.SWITCH_NOTES.items():
            if bool(values.get(key, True)):
                self.note("kanban", text)
        if not kb.profile_valid(self.cfg().get('kanban.assignee')):
            self.note('kanban', f"board {board!r} exists, but no dispatchable kanban.assignee; delegations are not ready. "
                      "Run `todo-manager config set kanban.assignee <profile>` and repeat installation.")
            return
        self.note("kanban", f"board {board!r} ready; delegations enabled")

    # ------------------------------------------------------------------ 7. hermes
    def step_hermes(self) -> None:
        inst = self.env.hermes_installer(self.cfg())
        actions = inst.plan()
        for a in actions:
            if a.kind == "conflict" or a.kept:
                self.note("hermes", f"{a.target}: {a.summary}")
        pending = [a for a in actions if a.changes]
        if not pending:
            inst.apply(actions)                 # records what is installed, writes conflict notes
            self.note("hermes", "installed and current")
            return
        if not self.prompter.confirm("hermes", "Install into Hermes (the cron jobs are created paused):",
                                     [f"[{a.kind}] {a.target}: {a.summary}" for a in pending]):
            self.note("hermes", "not installed (declined)")
            return
        done = inst.apply(actions)
        self.note("hermes", f"installed ({len(done)} change(s))")

    # ------------------------------------------------------------------ 8. schedule
    def step_schedule(self) -> None:
        from .hermes_install import JOBS, load_jobs
        inst = self.env.hermes_installer(self.cfg())
        jobs = load_jobs(inst.paths)
        if len(jobs) < len(JOBS):
            self.note("schedule", "the cron jobs are not installed yet")
            return
        paused = sorted(n for n, j in jobs.items() if j.get("state") == "paused" or not j.get("enabled", True))
        if not paused:
            self.note("schedule", "cron jobs running")
            return
        if not self.prompter.confirm("schedule", "Start the Todo Manager cron jobs? Each runs every 5 minutes and wakes"
                                     " a model only when there is something to do.", [f"resume {n}" for n in paused]):
            self.note("schedule", "cron jobs stay paused; run install again to start them")
            return
        inst.set_jobs_state("resume")
        self.note("schedule", "cron jobs running")

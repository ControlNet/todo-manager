"""Real-time sync daemon (docs/realtime-sync.md).

One resident process runs the deterministic sync of the feedback job as things happen, instead
of every five minutes: Notion -> local on webhook events, local -> Notion as soon as the
database changes, Kanban when the board's database changes. It never runs a model: when
feedback needs judgement it asks Hermes to run the feedback job now (``hermes cron run``).

While the daemon is healthy the feedback gate skips its sync and only claims feedback; when the
daemon is stale, runs other code or is waiting for ``install``, the gate syncs as before. All
work is idempotent and goes through the gate's own steps, so either can stop at any time.

Threads: the webhook receiver puts events on a queue; the main thread does all sync work, one
cycle at a time, under the shared ``workspace`` lock and the ``sync`` lock; a pulse thread stamps
liveness.

Liveness: every ``PULSE_SECONDS`` the pulse thread writes the status file (``state/daemon.json``,
read by the gate and ``health``) and pings the systemd watchdog, as long as the main thread made
progress (a loop, a sync step, a Notion request) within ``STALL_SECONDS``: a long cycle that is
working stays healthy, a stuck one is killed by the watchdog and covered by the gate. Exit on a
source or config change (systemd starts the new code).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import clock, db, feedback, health, ops, webhook
from .config import Config
from .locks import LockBusy, file_lock, lock_path

UNIT_NAME = "todo-manager-daemon.service"
UNIT_MARK = "# todo-manager daemon unit (generated; overwritten by `todo-manager install`)"
PULSE_SECONDS = 15
STALL_SECONDS = 300    # no progress for this long: stop the heartbeat (above the 120 s Hermes wake call)
STALE_SECONDS = 90
CODE_CHECK_SECONDS = 10
CODE_SETTLE_SECONDS = 10
EXIT_DISABLED = 4   # `daemon run` with daemon.enabled false: the unit does not restart on it
WAKE_INTERVAL_SECONDS = 60
UNCLAIMED_WAKE_MINUTES = 5   # the same feedback still unclaimed after a wake: as often as the cron job
CLAIMS_MINUTES = 1
HOME_MINUTES = 5
REVIVE_MINUTES = 10
RETRY_SECONDS = 60     # a cycle with a failed step runs its work again after this
MAX_RETRIES = 5        # then every daemon.reconcile_minutes until it succeeds (in memory only)
LOCK_TIMEOUT_SECONDS = 30
EVENT_KEYS = ("comments", "page", "pull")   # reads an event asked for: retried when they fail
LOCAL_DEBOUNCE_SECONDS = 1


# --------------------------------------------------------------------------- status

def status_path(home: Path) -> Path:
    return home / "state" / "daemon.json"


def read_status(home: Path) -> dict[str, Any] | None:
    try:
        return json.loads(status_path(home).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return None


def write_status(home: Path, data: dict[str, Any]) -> None:
    path = status_path(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def code_fingerprint(root: Path | None = None, config_path: Path | None = None) -> str:
    """Path, mtime and size of every module of the package, and of ``config_path``: changes with
    any edit, checkout or settings change (the daemon reads config.toml only when it starts)."""
    root = root or Path(__file__).resolve().parent
    digest = hashlib.sha1()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        st = path.stat()
        digest.update(f"{path.relative_to(root)}:{st.st_mtime_ns}:{st.st_size}\n".encode())
    if config_path is not None:
        try:
            st = config_path.stat()
            digest.update(f"config:{st.st_mtime_ns}:{st.st_size}\n".encode())
        except FileNotFoundError:
            digest.update(b"config:absent\n")
    return digest.hexdigest()[:16]


def fingerprint_of(cfg: Config) -> str:
    return code_fingerprint(config_path=cfg.home / "config.toml")


def failed_event_keys(keys: list[Key], summary: dict[str, Any]) -> set[Key]:
    """The event reads of ``keys`` whose step failed in ``summary``."""
    def broke(name: str) -> bool:
        value = summary.get(name)
        return isinstance(value, dict) and bool(value.get("error") or value.get("errors"))
    pages = summary.get("event_pages")
    failed: set[Key] = set()
    for key in keys:
        if key[0] == "comments" and broke("event_comments"):
            failed.add(key)
        elif key[0] == "pull" and broke(f"pull_{key[1]}s"):
            failed.add(key)
        elif key[0] == "page" and isinstance(pages, dict) and (
                pages.get("error") or str((pages.get("read") or {}).get(key[1], "")).startswith("error")):
            failed.add(key)
    return failed


def health_of(cfg: Config) -> dict[str, Any]:
    """Whether the gate can leave the sync to the daemon, and why not."""
    if not cfg.get("daemon.enabled"):
        return {"enabled": False, "healthy": False, "reason": "disabled"}
    st = read_status(cfg.home)
    if st is None:
        return {"enabled": True, "healthy": False, "reason": "not running (no status)"}
    age = clock.minutes_since(st.get("alive_at"))
    out = {"enabled": True, "state": st.get("state"), "mode": st.get("mode"), "alive_at": st.get("alive_at"),
           "webhook": st.get("webhook"), "last_error": st.get("last_error"),
           "page_problems": st.get("page_problems") or {}, "retrying": st.get("retrying") or []}
    if age is None or age * 60 > STALE_SECONDS:
        return {**out, "healthy": False, "reason": f"not running since {st.get('alive_at')}"}
    if st.get("code") != fingerprint_of(cfg):
        return {**out, "healthy": False, "reason": "running other code or settings (restarting)"}
    if st.get("state") != "running":
        return {**out, "healthy": False, "reason": st.get("state") or "starting"}
    return {**out, "healthy": True, "reason": None}


def unit_path(cfg: Config) -> Path:
    return Path(cfg.get("daemon.unit_dir") or "~/.config/systemd/user").expanduser() / UNIT_NAME


def _quoted(value: str) -> str:
    """One systemd word: spaces, quotes, backslashes and ``%`` specifiers kept literally."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def unit_text(launcher: Path, todo_home: Path) -> str:
    return f"""{UNIT_MARK}
# workspace: {str(todo_home)!r}
[Unit]
Description=Todo Manager real-time sync (Notion webhooks, local changes, Kanban)
After=network-online.target
StartLimitIntervalSec=0

[Service]
Type=notify
NotifyAccess=main
ExecStart={_quoted(str(launcher))} daemon run
Environment={_quoted(f"TODO_HOME={todo_home}")}
Environment=PYTHONDONTWRITEBYTECODE=1
Restart=always
RestartPreventExitStatus={EXIT_DISABLED}
RestartSec=10
WatchdogSec=120

[Install]
WantedBy=default.target
"""


def systemctl(*args: str, runner: Callable[..., Any] = subprocess.run) -> Any:
    return runner(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)


def revive(conn: sqlite3.Connection, cfg: Config, reason: str,
           runner: Callable[..., Any] = subprocess.run) -> bool:
    """The gate found the daemon unhealthy: report it, and restart an installed unit (at most
    every ``REVIVE_MINUTES``). Returns whether a restart was asked for."""
    health.record_channel(conn, "daemon", "alive", reason)
    if not unit_path(cfg).exists() or "other code" in reason or reason == "waiting_install":
        return False
    last = db.get_meta(conn, "daemon.revived_at")
    if last and (clock.minutes_since(last) or 0) < REVIVE_MINUTES:
        return False
    with db.tx(conn):
        db.set_meta(conn, "daemon.revived_at", clock.iso())
    try:
        systemctl("restart", UNIT_NAME, runner=runner)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def sd_notify(message: str) -> None:
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock:
            sock.sendto(message.encode(), address)
    except OSError:
        pass


def default_board_db(cfg: Config) -> Path | None:
    if not cfg.get("kanban.enabled"):
        return None
    hermes_home = Path(cfg.get("hermes.home") or os.environ.get("HERMES_HOME") or "~/.hermes").expanduser()
    return hermes_home / "kanban" / "boards" / str(cfg.get("kanban.board") or "todo-manager") / "kanban.db"


def hermes_wake(cfg: Config) -> bool:
    """Ask Hermes to run the feedback job now, unless the user paused it (``cron run`` would
    resume a paused job)."""
    from . import hermes_install
    paths = hermes_install.Paths.from_config(cfg)
    name = hermes_install.JOBS["feedback"]["name"]
    job = hermes_install.load_jobs(paths).get(name)
    if not job or not job.get("enabled", True) or job.get("state") == "paused" or job.get("paused_at"):
        return False
    hermes_install.Installer(cfg, paths).hermes("cron", "run", job["id"])
    return True


# --------------------------------------------------------------------------- the daemon

Key = tuple[str, ...]


class Daemon:
    """``cycle()`` does one round of due work; ``run_forever()`` is the process."""

    def __init__(self, cfg: Config, *, notion_factory: Callable[[sqlite3.Connection, Config], Any] | None = None,
                 kanban_factory: Callable[[Config], Any] | None = None,
                 waker: Callable[[Config], bool] | None = None, board_db: Path | None = None,
                 stats: webhook.Stats | None = None, fingerprint: Callable[[], str] | None = None) -> None:
        from . import wiring
        self.cfg = cfg
        self.events: "queue.Queue[webhook.Event]" = queue.Queue()
        self.notion_factory = notion_factory or wiring.notion_sync
        self.kanban_factory = kanban_factory or wiring.kanban
        self.waker = waker or hermes_wake
        self.board_db = board_db if board_db is not None else default_board_db(cfg)
        self.stats = stats or webhook.Stats()
        self.conn: sqlite3.Connection | None = None
        self.state = "starting"
        self.fingerprint = fingerprint or (lambda: fingerprint_of(cfg))
        self.code = self.fingerprint()
        self.code_check_seconds, self.code_settle_seconds = CODE_CHECK_SECONDS, CODE_SETTLE_SECONDS
        self.started_at = clock.iso()
        self.pending: dict[Key, datetime] = {}
        self.last: dict[str, datetime] = {}
        self.data_version: int | None = None
        self.board_mtime: tuple[float, ...] | None = None
        self.last_write_at: datetime | None = None
        self.unanswered_since: datetime | None = None   # oldest write of ours no event has followed yet
        self.notion_now: Any = None                     # the Notion sync of the running cycle
        self.progress = 0
        self._progress_seen: tuple[Any, ...] | None = None
        self._progress_at: float | None = None
        self.status_lock = threading.Lock()
        self.retries: dict[Key, int] = {}
        # page -> why it could not be read, until it reads again; kept across restarts (status file)
        self.page_problems: dict[str, str] = dict((read_status(cfg.home) or {}).get("page_problems") or {})
        self.shared_lock = threading.Lock()        # retries and page_problems: main thread vs pulse
        self.stamped_state: str | None = None
        self.lock_timeout = LOCK_TIMEOUT_SECONDS
        self.last_error: str | None = None
        self.last_summary: dict[str, Any] = {}
        self.silent = False
        self.woken_for: frozenset[str] = frozenset()

    # ------------------------------------------------------------ settings
    def setting(self, key: str, default: Any) -> Any:
        return self.cfg.get(f"daemon.{key}", default)

    @property
    def webhook_ready(self) -> bool:
        return bool(self.setting("webhook", True)) and webhook.load_token(self.cfg.home) is not None

    @property
    def mode(self) -> str:
        return "webhook" if self.webhook_ready and not self.silent else "polling"

    # ------------------------------------------------------------ database
    def open(self) -> bool:
        if self.conn is not None:
            return True
        try:
            self.conn = db.open_existing(self.cfg.db_path)
        except (RuntimeError, sqlite3.Error) as exc:
            self.state, self.last_error = "waiting_install", str(exc)
            return False
        self.state, self.last_error = "running", None
        self.data_version = self._data_version()
        return True

    def close(self) -> None:
        if self.conn is not None:
            self.conn.close()
            self.conn = None

    def _data_version(self) -> int:
        # Changes made through other connections only: our own writes do not trigger us again.
        assert self.conn is not None
        return int(self.conn.execute("PRAGMA data_version").fetchone()[0])

    # ------------------------------------------------------------ scheduling
    def due(self, key: Key, delay: float = 0.0) -> None:
        when = clock.now() + timedelta(seconds=delay)
        current = self.pending.get(key)
        if current is None or when < current:
            self.pending[key] = when

    def every(self, name: str, minutes: float) -> bool:
        last = self.last.get(name)
        if last is None or clock.now() - last >= timedelta(minutes=minutes):
            self.last[name] = clock.now()
            return True
        return False

    def submit(self, event: webhook.Event) -> None:
        """Turn a webhook event into due work on the pages it names."""
        if event.bot_only or not event.page_id or self.conn is None:
            return
        from .notion.sync import load_state
        debounce = float(self.setting("debounce_seconds", 3))
        row = self.conn.execute("SELECT entity_type FROM notion_pages WHERE page_id=? AND entity_type IN"
                                " ('task','project')", (event.page_id,)).fetchone()
        if event.type.startswith("comment."):
            if row or event.page_id == load_state(self.conn, self.cfg).home_page_id:
                self.due(("comments", event.page_id), debounce)
            return
        kinds = [row["entity_type"]] if row else (["task", "project"] if event.type == "page.created" else [])
        if row and row["entity_type"] == "task" and event.type in ("page.deleted", "page.undeleted", "page.moved"):
            self.due(("page", event.page_id), debounce)
        for kind in kinds:
            self.due(("pull", kind), debounce)

    def local_checks(self) -> None:
        """Local changes and timers -> due work (no network)."""
        assert self.conn is not None
        version = self._data_version()
        if version != self.data_version:
            self.data_version = version
            self.due(("local",), LOCAL_DEBOUNCE_SECONDS)
        if self.board_db is not None:
            stamp = tuple(p.stat().st_mtime for p in (self.board_db, Path(f"{self.board_db}-wal")) if p.exists())
            if self.board_mtime is not None and stamp != self.board_mtime:
                gap = float(self.setting("kanban_min_interval_seconds", 15))
                last = self.last.get("kanban_board")
                delay = 0.0 if last is None else max(0.0, gap - (clock.now() - last).total_seconds())
                self.due(("kanban",), delay)
            self.board_mtime = stamp
        if self.every("claims", CLAIMS_MINUTES):
            self.due(("claims",))
        if self.every("home", HOME_MINUTES):
            self.due(("home",))
        if self.every("kanban_poll", float(self.setting("kanban_poll_minutes", 5))):
            self.due(("kanban",))
        poll = (float(self.cfg.get("notion.active_poll_minutes", 5)) if self.mode == "polling"
                else float(self.setting("reconcile_minutes", 5)))
        if self.every("notion_pass", poll):
            self.due(("notion_pass",))

    def check_webhook(self) -> None:
        """Our own writes produce events; none since a write ``webhook_silence_minutes`` ago
        means the tunnel or the subscription is broken: poll as the gate did, and report it."""
        if not self.webhook_ready or self.conn is None:
            return
        grace = timedelta(minutes=float(self.setting("webhook_silence_minutes", 10)))
        last_event = clock.parse(self.stats.last_event_at) if self.stats.last_event_at else None
        if self.unanswered_since and last_event and last_event >= self.unanswered_since:
            # Only a delivery answers, and only the writes before it; later writes do not move
            # the deadline of an unanswered one.
            later = self.last_write_at if self.last_write_at and self.last_write_at > last_event else None
            self.unanswered_since = later
        silent = bool(self.unanswered_since and clock.now() - self.unanswered_since > grace)
        if silent != self.silent:
            self.silent = silent
            health.record_channel(self.conn, "daemon", "webhook",
                                  f"no Notion events since our write at {clock.iso(self.unanswered_since)}"
                                  if silent else None)

    # ------------------------------------------------------------ work
    def cycle(self, first: webhook.Event | None = None) -> dict[str, Any] | None:
        """Take the events (``first``: one already taken off the queue), then do the work that is
        due. Returns the summary, None when idle."""
        try:
            # Every database access, opening included, waits for install/restore/migrate
            # (exclusive) to finish.
            with file_lock(lock_path(self.cfg.home, "workspace"), blocking=True, timeout=self.lock_timeout,
                           shared=True):
                if not self.open() or not self.schema_ok():
                    self.keep(first)
                    return None
                return self._cycle(first)
        except LockBusy:
            self.keep(first)
            return None
        except sqlite3.Error as exc:
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.close()           # opened again (and the schema checked) on the next loop
            self.keep(first)       # submitting it again is harmless
            return {"error": self.last_error}

    def keep(self, event: webhook.Event | None) -> None:
        """An event taken off the queue but not turned into due work yet: back on the queue."""
        if event is not None:
            self.events.put(event)

    def schema_ok(self) -> bool:
        """Under the workspace lock: the schema is still the one this code expects (a restore or
        an install may have changed it since the connection was opened)."""
        assert self.conn is not None
        try:
            row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            version = int(row[0]) if row else 0
            error = None if version == db.SCHEMA_VERSION else \
                f"workspace schema {version}; run todo-manager install to upgrade (expected {db.SCHEMA_VERSION})"
        except sqlite3.Error as exc:
            error = f"{type(exc).__name__}: {exc}"
        if error is None:
            return True
        self.close()
        self.state, self.last_error = "waiting_install", error
        return False

    def _cycle(self, first: webhook.Event | None) -> dict[str, Any] | None:
        if first is not None:
            self.submit(first)
        while True:
            try:
                self.submit(self.events.get_nowait())
            except queue.Empty:
                break
        self.local_checks()
        self.check_webhook()
        now = clock.now()
        keys = sorted(k for k, when in self.pending.items() if when <= now)
        if not keys:
            return None
        try:
            with file_lock(lock_path(self.cfg.home, "sync"), blocking=True, timeout=self.lock_timeout):
                summary = self.work(keys)
        except LockBusy:
            return None
        except Exception as exc:  # noqa: BLE001 - the daemon outlives any one failure
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            self.done(keys, failed={k for k in keys if k[0] in EVENT_KEYS})
            if isinstance(exc, sqlite3.Error):
                self.close()       # opened again (and the schema checked) on the next loop
            return {"error": self.last_error}
        self.done(keys, failed=failed_event_keys(keys, summary))
        self.last_error = None
        self.last_summary = summary
        return summary

    def done(self, keys: list[Key], *, failed: set[Key]) -> None:
        """Due work ran. The read an event asked for that failed (``failed``) runs again after
        ``RETRY_SECONDS``, ``MAX_RETRIES`` times, then every ``daemon.reconcile_minutes`` until it
        succeeds: some of it (a deleted page's read) no full pass would do. Timer work is not
        retried: its timer runs it again. The work is idempotent."""
        slow = float(self.setting("reconcile_minutes", 5)) * 60
        with self.shared_lock:
            for key in keys:
                self.pending.pop(key, None)
                if key not in failed:
                    self.retries.pop(key, None)
                    continue
                attempt = self.retries.get(key, 0) + 1
                self.retries[key] = attempt
                self.due(key, RETRY_SECONDS if attempt <= MAX_RETRIES else slow)

    def work(self, keys: list[Key]) -> dict[str, Any]:
        from . import gate_feedback
        assert self.conn is not None
        conn, cfg = self.conn, self.cfg
        summary: dict[str, Any] = {"keys": ["/".join(k) for k in keys]}
        problems: list[str] = []

        def step(name: str, fn: Callable[[], Any]) -> Any:
            self.touch()
            return gate_feedback._step(conn, summary, problems, name, fn)

        notion = self.notion_factory(conn, cfg)
        self.notion_now = notion
        try:
            return self._work(keys, step, notion, summary, problems)
        finally:
            self.notion_now = None

    def _work(self, keys: list[Key], step: Callable[[str, Callable[[], Any]], Any], notion: Any,
              summary: dict[str, Any], problems: list[str]) -> dict[str, Any]:
        from . import gate_feedback
        assert self.conn is not None
        conn, cfg = self.conn, self.cfg
        kanban = self.kanban_factory(cfg)
        names = {k[0] for k in keys}
        if "claims" in names:
            step("claims", lambda: gate_feedback.fail_exhausted_claims(conn, cfg))
        if notion is not None:
            targets = [t for t in (notion.comment_target(k[1]) for k in keys if k[0] == "comments") if t]
            if "notion_pass" in names:
                gate_feedback.notion_pull_steps(step, notion, cfg)
            else:
                for kind in ("project", "task"):
                    if ("pull", kind) in keys:
                        step(f"pull_{kind}s", lambda kind=kind: notion.pull(kind))
            if targets:   # also with a full pass: its selection may leave the event's page out
                step("event_comments", lambda: notion.poll_comments(targets=targets))
            pages = [k[1] for k in keys if k[0] == "page"]
            if pages:
                step("event_pages", lambda: self.read_pages(notion, pages))
        if kanban is not None:
            if names & {"local", "kanban", "claims"}:
                gate_feedback.kanban_send_steps(step, conn, cfg, kanban)
            if "kanban" in names:
                self.last["kanban_board"] = clock.now()
                gate_feedback.kanban_read_steps(step, conn, cfg, kanban)
        gate_feedback.push_steps(step, conn, notion)
        if notion is not None and notion.imported:
            with self.shared_lock:
                for page in notion.imported:
                    self.page_problems.pop(page, None)
        if (summary.get("outbox") or {}).get("done"):
            self.last_write_at = clock.now()
            self.unanswered_since = self.unanswered_since or self.last_write_at
        if notion is not None and "home" in names:
            gate_feedback.home_step(step, conn, cfg, notion, summary)
        if problems:
            summary["problems"] = problems
        return summary

    def read_pages(self, notion: Any, pages: list[str]) -> dict[str, Any]:
        """Direct reads of the pages events named, one outcome each. A page that is gone is a
        definitive answer (not retried), kept in ``page_problems`` until the page reads again."""
        read: dict[str, str] = {}
        for page in pages:
            try:
                read[page] = notion.refresh_page(page)
            except Exception as exc:  # noqa: BLE001 - one page's failure is retried alone
                read[page] = f"error: {type(exc).__name__}: {str(exc)[:200]}"
                continue
            with self.shared_lock:
                if read[page] == "missing":
                    self.page_problems[page] = f"{clock.iso()}: no longer accessible (deleted or unshared)"
        return {"read": read, "errors": sum(1 for v in read.values() if v.startswith("error"))}

    def ready_feedback(self) -> frozenset[str]:
        assert self.conn is not None
        items = feedback.pending(self.conn, stale_minutes=int(self.cfg.get("feedback.stale_claim_minutes", 60)))
        return frozenset(f["id"] for f in items if not ops.not_ready(self.conn, self.cfg, f))

    def maybe_wake(self) -> bool:
        """Start the feedback job for claimable feedback: at most once a minute, and no more
        often than its own schedule while the same items stay unclaimed (a failing gate is not
        restarted every minute, each run reporting the failure)."""
        if self.conn is None:
            return False
        try:
            with file_lock(lock_path(self.cfg.home, "workspace"), blocking=True, timeout=self.lock_timeout,
                           shared=True):
                if not self.schema_ok():
                    return False
                ready = self.ready_feedback()
        except LockBusy:
            return False
        except sqlite3.Error as exc:
            self.last_error = f"wake check failed: {type(exc).__name__}: {str(exc)[:200]}"
            self.close()
            return False
        if not ready:
            return False
        last = self.last.get("wake")
        gap = timedelta(minutes=UNCLAIMED_WAKE_MINUTES) if ready <= self.woken_for \
            else timedelta(seconds=WAKE_INTERVAL_SECONDS)
        if last is not None and clock.now() - last < gap:
            return False
        self.last["wake"], self.woken_for = clock.now(), ready
        try:
            return bool(self.waker(self.cfg))
        except Exception as exc:  # noqa: BLE001 - the gate's own schedule still runs the job
            self.last_error = f"wake failed: {type(exc).__name__}: {str(exc)[:200]}"
            return False

    # ------------------------------------------------------------ process
    def status(self) -> dict[str, Any]:
        return {"pid": os.getpid(), "started_at": self.started_at, "alive_at": clock.iso(), "code": self.code,
                "state": self.state, "mode": self.mode, "last_error": self.last_error,
                "webhook": {"token": self.webhook_ready, "last_event_at": self.stats.last_event_at,
                            "accepted": self.stats.accepted, "duplicates": self.stats.duplicates,
                            "bad_signatures": self.stats.bad_signatures, "silent": self.silent},
                "last_write_at": clock.iso(self.last_write_at) if self.last_write_at else None,
                "unanswered_since": clock.iso(self.unanswered_since) if self.unanswered_since else None,
                **self.shared_status(), "last_summary": self.last_summary}

    def shared_status(self) -> dict[str, Any]:
        with self.shared_lock:
            return {"retrying": sorted("/".join(k) for k, n in self.retries.items() if n > MAX_RETRIES),
                    "page_problems": dict(self.page_problems)}

    def heartbeat(self, force: bool = False) -> None:
        with self.status_lock:
            status = self.status()
            write_status(self.cfg.home, status)
            self.stamped_state = status["state"]

    def touch(self) -> None:
        """The main thread made progress."""
        self.progress += 1

    def pulse_once(self) -> None:
        """One pulse of the pulse thread: an error is kept, never ends the thread."""
        try:
            self.pulse()
        except Exception as exc:  # noqa: BLE001 - without pulses the watchdog would kill a working daemon
            self.last_error = f"pulse failed: {type(exc).__name__}: {str(exc)[:200]}"

    def pulse(self, mono: float | None = None) -> bool:
        """Stamp the status and ping the watchdog, unless the main thread has made no progress
        (no loop, step or Notion request) for ``STALL_SECONDS``."""
        mono = time.monotonic() if mono is None else mono
        seen = (self.progress, getattr(getattr(self.notion_now, "client", None), "requests", None))
        if seen != self._progress_seen or self._progress_at is None:
            self._progress_seen, self._progress_at = seen, mono
        if mono - self._progress_at > STALL_SECONDS:
            return False
        self.heartbeat(force=True)
        sd_notify("WATCHDOG=1")
        return True

    def run_forever(self, receiver: webhook.Receiver | None = None) -> int:
        """The process: until the source or config.toml changes (exit 0, systemd starts the new code)."""
        if receiver is not None:
            self.events = receiver.events
            self.stats = receiver.stats
            receiver.start()
        self.heartbeat(force=True)   # the first cycle opens the database, under the workspace lock
        sd_notify("READY=1")
        stop = threading.Event()

        def pulses() -> None:
            while not stop.wait(PULSE_SECONDS):
                self.pulse_once()
        pulser = threading.Thread(target=pulses, name="pulse", daemon=True)
        pulser.start()
        changed_at: float | None = None
        seen_code = self.code
        next_code_check = time.monotonic()
        try:
            while True:
                self.touch()
                try:
                    first: webhook.Event | None = self.events.get(timeout=1.0)
                except queue.Empty:
                    first = None
                self.cycle(first)
                if self.state != self.stamped_state:
                    self.heartbeat(force=True)   # running / waiting_install shown at once
                self.maybe_wake()
                if time.monotonic() >= next_code_check:
                    next_code_check = time.monotonic() + self.code_check_seconds
                    current = self.fingerprint()
                    if current != seen_code:
                        seen_code, changed_at = current, time.monotonic()
                    elif changed_at is not None and time.monotonic() - changed_at >= self.code_settle_seconds:
                        self.state = "restarting"
                        self.heartbeat(force=True)
                        return 0
                if self.conn is None:
                    time.sleep(59)   # waiting for install: try again in a minute
        finally:
            stop.set()
            pulser.join(timeout=5)
            sd_notify("STOPPING=1")
            if receiver is not None:
                receiver.stop()
            self.close()


def run(cfg: Config) -> int:
    # systemd stops the unit with SIGTERM: leave through the ``finally`` blocks (an open
    # transaction rolls back; all work is idempotent and is done again later).
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    receiver = None
    if cfg.get("daemon.webhook", True):
        receiver = webhook.Receiver(cfg.home, queue.Queue(), host=str(cfg.get("daemon.host", "127.0.0.1")),
                                    port=int(cfg.get("daemon.port", 8655)), path=str(cfg.get("daemon.path", "/notion")))
    print(f"todo-manager daemon: {cfg.home}, webhook "
          f"{'on 127.0.0.1:' + str(receiver.port) if receiver else 'off'}", file=sys.stderr, flush=True)
    return Daemon(cfg).run_forever(receiver)


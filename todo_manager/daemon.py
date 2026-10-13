"""Real-time sync daemon (docs/realtime-sync.md).

One resident process runs the deterministic sync of the feedback job as things happen, instead
of every five minutes: Notion -> local on webhook events, local -> Notion as soon as the
database changes, Kanban when the board's database changes. It never runs a model: when
feedback needs judgement it asks Hermes to run the feedback job now (``hermes cron run``).

While the daemon is healthy the feedback gate skips its sync and only claims feedback; when the
daemon is stale, runs other code or is waiting for ``install``, the gate syncs as before. All
work is idempotent and goes through the gate's own steps, so either can stop at any time.

Threads: the webhook receiver puts events on a queue; the main thread does all sync work, one
cycle at a time, under the shared ``workspace`` lock and the ``sync`` lock.

Liveness: a status file (``state/daemon.json``) stamped every ``HEARTBEAT_SECONDS``, read by the
gate and ``health``; ``sd_notify`` READY/WATCHDOG for systemd; exit on a source change (systemd
starts the new code).
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
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import clock, db, feedback, health, ops, webhook
from .config import Config
from .locks import LockBusy, file_lock, lock_path

UNIT_NAME = "todo-manager-daemon.service"
UNIT_MARK = "# todo-manager daemon unit (generated; overwritten by `todo-manager install`)"
HEARTBEAT_SECONDS = 30
STALE_SECONDS = 90
CODE_CHECK_SECONDS = 10
CODE_SETTLE_SECONDS = 10
EXIT_DISABLED = 4   # `daemon run` with daemon.enabled false: the unit does not restart on it
WAKE_INTERVAL_SECONDS = 60
UNCLAIMED_WAKE_MINUTES = 5   # the same feedback still unclaimed after a wake: as often as the cron job
CLAIMS_MINUTES = 1
HOME_MINUTES = 5
REVIVE_MINUTES = 10
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


def health_of(cfg: Config) -> dict[str, Any]:
    """Whether the gate can leave the sync to the daemon, and why not."""
    if not cfg.get("daemon.enabled"):
        return {"enabled": False, "healthy": False, "reason": "disabled"}
    st = read_status(cfg.home)
    if st is None:
        return {"enabled": True, "healthy": False, "reason": "not running (no status)"}
    age = clock.minutes_since(st.get("alive_at"))
    out = {"enabled": True, "state": st.get("state"), "mode": st.get("mode"), "alive_at": st.get("alive_at"),
           "webhook": st.get("webhook"), "last_error": st.get("last_error")}
    if age is None or age * 60 > STALE_SECONDS:
        return {**out, "healthy": False, "reason": f"not running since {st.get('alive_at')}"}
    if st.get("code") != fingerprint_of(cfg):
        return {**out, "healthy": False, "reason": "running other code or settings (restarting)"}
    if st.get("state") != "running":
        return {**out, "healthy": False, "reason": st.get("state") or "starting"}
    return {**out, "healthy": True, "reason": None}


def unit_path(cfg: Config) -> Path:
    return Path(cfg.get("daemon.unit_dir") or "~/.config/systemd/user").expanduser() / UNIT_NAME


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
ExecStart={launcher} daemon run
Environment=TODO_HOME={todo_home}
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
        self.last_error: str | None = None
        self.last_summary: dict[str, Any] = {}
        self.silent = False
        self.woken_for: frozenset[str] = frozenset()

    # ------------------------------------------------------------ settings
    def setting(self, key: str, default: Any) -> Any:
        return self.cfg.get(f"daemon.{key}", default)

    @property
    def webhook_ready(self) -> bool:
        return webhook.load_token(self.cfg.home) is not None

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
                else float(self.setting("reconcile_minutes", 30)))
        if self.every("notion_pass", poll):
            self.due(("notion_pass",))

    def check_webhook(self) -> None:
        """Our own writes produce events; none since a write ``webhook_silence_minutes`` ago
        means the tunnel or the subscription is broken: poll as the gate did, and report it."""
        if not self.webhook_ready or self.conn is None:
            return
        grace = timedelta(minutes=float(self.setting("webhook_silence_minutes", 10)))
        last_event = clock.parse(self.stats.last_event_at) if self.stats.last_event_at else None
        silent = bool(self.last_write_at and clock.now() - self.last_write_at > grace
                      and (last_event is None or last_event < self.last_write_at))
        if silent != self.silent:
            self.silent = silent
            health.record_channel(self.conn, "daemon", "webhook",
                                  f"no Notion events since our write at {clock.iso(self.last_write_at)}"
                                  if silent else None)

    # ------------------------------------------------------------ work
    def cycle(self, first: webhook.Event | None = None) -> dict[str, Any] | None:
        """Take the events (``first``: one already taken off the queue), then do the work that is
        due. Returns the summary, None when idle."""
        if not self.open():
            return None
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
        home = self.cfg.home
        try:
            with file_lock(lock_path(home, "workspace"), blocking=True, timeout=30, shared=True), \
                    file_lock(lock_path(home, "sync"), blocking=True, timeout=30):
                summary = self.work(keys)
        except LockBusy:
            return None
        except Exception as exc:  # noqa: BLE001 - the daemon outlives any one failure
            self.last_error = f"{type(exc).__name__}: {str(exc)[:300]}"
            for key in keys:
                self.pending.pop(key, None)
            return {"error": self.last_error}
        for key in keys:
            self.pending.pop(key, None)
        self.last_error = None
        self.last_summary = summary
        self.data_version = self._data_version()   # what the work itself changed is not news
        return summary

    def work(self, keys: list[Key]) -> dict[str, Any]:
        from . import gate_feedback
        assert self.conn is not None
        conn, cfg = self.conn, self.cfg
        summary: dict[str, Any] = {"keys": ["/".join(k) for k in keys]}
        problems: list[str] = []

        def step(name: str, fn: Callable[[], Any]) -> Any:
            return gate_feedback._step(conn, summary, problems, name, fn)

        notion = self.notion_factory(conn, cfg)
        kanban = self.kanban_factory(cfg)
        names = {k[0] for k in keys}
        if "claims" in names:
            step("claims", lambda: gate_feedback.fail_exhausted_claims(conn, cfg))
        if notion is not None:
            if "notion_pass" in names:
                gate_feedback.notion_pull_steps(step, notion, cfg)
            else:
                for kind in ("project", "task"):
                    if ("pull", kind) in keys:
                        step(f"pull_{kind}s", lambda kind=kind: notion.pull(kind))
                targets = [t for t in (notion.comment_target(k[1]) for k in keys if k[0] == "comments") if t]
                if targets:
                    step("comments", lambda: notion.poll_comments(targets=targets))
        if kanban is not None:
            if names & {"local", "kanban", "claims"}:
                gate_feedback.kanban_send_steps(step, conn, cfg, kanban)
            if "kanban" in names:
                self.last["kanban_board"] = clock.now()
                gate_feedback.kanban_read_steps(step, conn, cfg, kanban)
        gate_feedback.push_steps(step, conn, notion)
        if (summary.get("outbox") or {}).get("done"):
            self.last_write_at = clock.now()
        if notion is not None and "home" in names:
            gate_feedback.home_step(step, conn, cfg, notion, summary)
        if problems:
            summary["problems"] = problems
        return summary

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
        ready = self.ready_feedback()
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
                "last_summary": self.last_summary}

    def heartbeat(self, force: bool = False) -> None:
        if force or self.every("heartbeat", HEARTBEAT_SECONDS / 60):
            write_status(self.cfg.home, self.status())

    def run_forever(self, receiver: webhook.Receiver | None = None) -> int:
        """The process: until the source or config.toml changes (exit 0, systemd starts the new code)."""
        if receiver is not None:
            self.events = receiver.events
            self.stats = receiver.stats
            receiver.start()
        self.open()
        self.heartbeat(force=True)
        sd_notify("READY=1")
        changed_at: float | None = None
        seen_code = self.code
        next_code_check = time.monotonic()
        try:
            while True:
                try:
                    first: webhook.Event | None = self.events.get(timeout=1.0)
                except queue.Empty:
                    first = None
                self.cycle(first)
                self.maybe_wake()
                self.heartbeat()
                sd_notify("WATCHDOG=1")
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


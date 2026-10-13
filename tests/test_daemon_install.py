"""The sync daemon's systemd user unit through `install` / `uninstall`, the `daemon` command, and
one real daemon process.

systemctl is a recording fake (``Systemd``); the unit directory is temporary. The process test
runs `todo-manager daemon run` on a temporary workspace (Notion and Kanban off) and an ephemeral
loopback port; it never touches ~/.hermes, systemd or a public port.
"""

import contextlib
import io
import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from todo_manager import cli, clock, config, daemon, hermes_install, webhook
from tests.test_hermes_install import InstallCase


class Systemd:
    """TEST DOUBLE of `systemctl --user`: records calls, tracks whether the unit is active."""

    def __init__(self):
        self.calls = []
        self.active = False

    def __call__(self, cmd, **kwargs):
        args = cmd[2:]
        self.calls.append(args)
        if args[0] in ("restart", "start"):
            self.active = True
        if args[:2] == ["disable", "--now"]:
            self.active = False
        out = ("active" if self.active else "inactive") if args[0] == "is-active" else ""
        return subprocess.CompletedProcess(cmd, 0, out, "")


class UnitInstallTests(InstallCase):
    def setUp(self):
        super().setUp()
        self.units = self.tmp / "systemd-user"
        self.systemd = Systemd()
        self.configure(enabled=False)

    def configure(self, **daemon_settings):
        text = (self.home / "config.toml").read_text().split("\n[daemon]")[0]
        settings = {"unit_dir": str(self.units), **daemon_settings}
        lines = "".join(f"{k} = {json.dumps(v)}\n" for k, v in settings.items())
        (self.home / "config.toml").write_text(f"{text}\n[daemon]\n{lines}")

    def installer(self):
        inst = super().installer()
        inst.systemd = self.systemd
        return inst

    def unit(self):
        return self.units / daemon.UNIT_NAME

    def test_a_disabled_daemon_installs_nothing(self):
        actions = self.install()
        self.assertFalse(self.unit().exists())
        self.assertEqual(self.systemd.calls, [])
        self.assertFalse([a for a in actions if daemon.UNIT_NAME in a.target or "systemd" in a.target])

    def test_enabling_writes_the_unit_and_starts_it(self):
        self.configure(enabled=True)
        self.install()
        text = self.unit().read_text()
        self.assertIn(f'ExecStart="{self.paths.launcher}" daemon run', text)
        self.assertIn(f"TODO_HOME={self.home}", text)
        self.assertIn(["enable", daemon.UNIT_NAME], self.systemd.calls)
        self.assertIn(["restart", daemon.UNIT_NAME], self.systemd.calls)

    def test_a_second_install_with_a_running_daemon_changes_nothing(self):
        self.configure(enabled=True)
        self.install()
        self.systemd.calls.clear()
        actions = self.installer().plan()
        self.assertFalse([a for a in actions if a.changes])

    def test_a_stopped_daemon_is_started_again_by_install(self):
        self.configure(enabled=True)
        self.install()
        self.systemd.active = False
        actions = self.installer().plan()
        self.assertEqual([a.target for a in actions if a.changes], [daemon.UNIT_NAME])

    def test_turning_it_off_stops_and_removes_the_unit(self):
        self.configure(enabled=True)
        self.install()
        self.configure(enabled=False)
        self.install()
        self.assertFalse(self.unit().exists())
        self.assertIn(["disable", "--now", daemon.UNIT_NAME], self.systemd.calls)

    def test_a_unit_someone_else_wrote_is_left_alone(self):
        self.units.mkdir(parents=True)
        self.unit().write_text("[Service]\nExecStart=/bin/true\n")
        self.configure(enabled=True)
        actions = self.installer().plan()
        self.assertEqual([a.kind for a in actions if a.target == str(self.unit())], ["conflict"])
        self.configure(enabled=False)
        self.assertFalse([a for a in self.installer().uninstall_plan() if a.target == str(self.unit())])

    def test_uninstall_removes_our_unit(self):
        self.configure(enabled=True)
        self.install()
        inst = self.installer()
        inst.uninstall(inst.uninstall_plan())
        self.assertFalse(self.unit().exists())


class CommandTests(InstallCase):
    def invoke(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(["--home", str(self.home), *args])
        return code, out.getvalue()

    def test_run_refuses_while_disabled(self):
        code, text = self.invoke("daemon", "run")
        self.assertEqual(code, daemon.EXIT_DISABLED, "the unit does not restart on this code")
        self.assertIn("DISABLED", text)

    def test_token_and_reset(self):
        code, text = self.invoke("daemon", "token")
        self.assertIn("NO_TOKEN", text)
        webhook.save_token(self.home, "secret_x")
        code, text = self.invoke("daemon", "token")
        self.assertEqual((code, text.strip()), (0, "secret_x"))
        code, text = self.invoke("daemon", "reset-token")
        out = json.loads(text)
        self.assertTrue(out["reset"])
        self.assertIsNone(webhook.load_token(self.home))
        self.assertEqual(out["enroll_until"], webhook.enrollment_until(self.home),
                         "a reset opens enrollment for the recreated subscription")

    def test_enroll_opens_a_window_for_the_handshake(self):
        code, text = self.invoke("daemon", "enroll", "--minutes", "5")
        self.assertEqual(code, 0)
        out = json.loads(text)
        self.assertEqual(out["enroll_until"], webhook.enrollment_until(self.home))
        self.assertIn("enroll_until", json.loads(self.invoke("daemon", "status")[1]))

    def test_enroll_refuses_while_a_token_is_stored(self):
        webhook.save_token(self.home, "secret_x")
        code, text = self.invoke("daemon", "enroll")
        self.assertNotEqual(code, 0)
        self.assertIn("TOKEN_EXISTS", text)
        self.assertIsNone(webhook.enrollment_until(self.home))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class ProcessTests(InstallCase):
    def test_the_daemon_process_serves_the_handshake_and_reports_itself(self):
        port = free_port()
        with (self.home / "config.toml").open("a") as fh:
            fh.write(f"\n[daemon]\nenabled = true\nport = {port}\n")
        env = {**os.environ, "TODO_HOME": str(self.home), "PYTHONDONTWRITEBYTECODE": "1"}
        env.pop("NOTIFY_SOCKET", None)
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen([sys.executable, "-B", "-c", "import sys; from todo_manager.cli import main;"
                                 " sys.exit(main(['daemon', 'run']))"], cwd=root, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        self.addCleanup(lambda: proc.poll() is None and proc.kill())
        deadline = time.time() + 20
        while time.time() < deadline and (daemon.read_status(self.home) or {}).get("state") != "running":
            time.sleep(0.1)
        status = daemon.read_status(self.home)
        self.assertIsNotNone(status, proc.stderr.read().decode() if proc.poll() is not None else "no status")
        self.assertEqual(status["state"], "running")
        clock.set_now(None)   # the daemon process reads the real time
        webhook.open_enrollment(self.home)
        req = urllib.request.Request(f"http://127.0.0.1:{port}/notion", method="POST",
                                     data=json.dumps({"verification_token": "secret_p"}).encode())
        with urllib.request.urlopen(req, timeout=5) as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(webhook.load_token(self.home), "secret_p")
        cfg = config.load(self.home)
        self.assertTrue(daemon.health_of(cfg)["healthy"])
        proc.send_signal(signal.SIGTERM)
        self.assertEqual(proc.wait(timeout=10), 0, "stops cleanly on SIGTERM (systemctl stop)")


del InstallCase   # imported only as a base

if __name__ == "__main__":
    import unittest
    unittest.main()

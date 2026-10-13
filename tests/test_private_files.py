"""The workspace holds mail, chats and tasks: other local users cannot read it, whatever the
caller's umask (the cron jobs and the daemon inherit Hermes' or systemd's)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path

from todo_manager import daemon, db, export, install
from tests.helpers import WorkspaceCase


def mode(path: Path) -> int:
    return path.stat().st_mode & 0o777


class Umask022:
    def setUp(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        super().setUp()


class WorkspaceDirectoryTests(Umask022, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.root = Path(tempfile.mkdtemp(prefix="tm-private-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_a_new_workspace_is_owner_only(self):
        home = self.root / "ws"
        install.init_workspace(home, "en", "UTC")
        self.assertEqual(mode(home), 0o700)

    def test_install_tightens_an_existing_workspace(self):
        home = self.root / "ws"
        home.mkdir(mode=0o775)
        os.chmod(home, 0o775)
        install.init_workspace(home, "en", "UTC")
        self.assertEqual(mode(home), 0o700)

    def test_a_new_database_and_its_wal_are_owner_only(self):
        conn = db.connect(self.root / "state" / "todo.sqlite")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE t (x)")
        self.assertEqual(mode(self.root / "state" / "todo.sqlite"), 0o600)
        self.assertEqual(mode(self.root / "state" / "todo.sqlite-wal"), 0o600)

    def test_the_daemon_unit_creates_owner_only_files(self):
        self.assertIn("\nUMask=0077\n", daemon.unit_text(Path("/opt/todo-manager"), Path("/home/u/ws")))


class ExportTests(Umask022, WorkspaceCase):
    def test_an_export_outside_the_workspace_is_owner_only(self):
        out = self.tmp / "elsewhere" / "bundle"
        export.export_snapshot(self.conn, self.cfg, out)
        self.assertEqual(mode(out), 0o700)
        self.assertEqual([p.name for p in out.parent.iterdir()], ["bundle"], "no snapshot left beside it")

    def test_an_imported_workspace_is_owner_only(self):
        bundle = export.export_snapshot(self.conn, self.cfg)
        target = self.tmp / "restored"
        export.import_bundle(bundle, target)
        self.assertEqual(mode(target), 0o700)


if __name__ == "__main__":
    unittest.main()

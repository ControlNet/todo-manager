"""The workspace holds mail, chats and tasks: other local users cannot read it, whatever the
caller's umask (the cron jobs and the daemon inherit Hermes' or systemd's)."""

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from todo_manager import daemon, db, export, install
from tests.helpers import WorkspaceCase
from tests.test_cli_redesign import invoke


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

    @staticmethod
    def exposed(top: Path, prefix: str) -> list[str]:
        """Entries of ``top`` named ``prefix*`` that other users could enter or read (a 0700
        directory shields what is inside it)."""
        return [p.name for p in top.iterdir() if p.name.startswith(prefix) and p.stat().st_mode & 0o077]

    def test_the_snapshot_of_an_outside_export_is_never_readable_by_others(self):
        out = self.tmp / "elsewhere" / "bundle"
        seen = []
        real = export._write_export

        def spy(conn, cfg, root):
            seen.extend(self.exposed(root.parent, ".bundle"))
            return real(conn, cfg, root)

        with patch.object(export, "_write_export", spy):
            export.export_snapshot(self.conn, self.cfg, out)
        self.assertEqual(seen, [])

    def test_an_import_under_construction_is_never_readable_by_others(self):
        bundle = export.export_snapshot(self.conn, self.cfg)
        os.chmod(bundle / "workspace", 0o755)   # as an export made under umask 022 leaves it
        target = self.tmp / "restored"
        seen = []
        real = export.reconcile_after_restore

        def spy(conn, reason):
            seen.extend(self.exposed(target.parent, ".restored"))
            return real(conn, reason)

        with patch.object(export, "reconcile_after_restore", spy):
            export.import_bundle(bundle, target)
        self.assertEqual(seen, [])
        self.assertEqual(mode(target / "state" / "todo.sqlite"), 0o600)

    def test_an_import_into_an_existing_empty_directory_is_owner_only(self):
        bundle = export.export_snapshot(self.conn, self.cfg)
        target = self.tmp / "restored"
        target.mkdir()
        os.chmod(target, 0o755)
        export.import_bundle(bundle, target)
        self.assertEqual(mode(target), 0o700)

    def test_restoring_a_backup_into_a_new_workspace_is_owner_only(self):
        saved = export.backup(self.conn, self.cfg)
        target = self.tmp / "parent" / "new-ws"
        code, text = invoke(target, "restore", str(saved), "--yes", "--json")
        self.assertEqual(code, 0, text)
        self.assertEqual(mode(target), 0o700)
        self.assertEqual(mode(target / "state" / "todo.sqlite"), 0o600)


if __name__ == "__main__":
    unittest.main()

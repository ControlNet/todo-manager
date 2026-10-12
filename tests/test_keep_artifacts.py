"""Delivered files are kept once: Hermes lists a card's files both in the run's metadata and as
card attachments, and every file was copied twice (``x.pdf`` and ``x-2.pdf``, 2026-10-12)."""

import tempfile
import unittest
from pathlib import Path

from todo_manager.files import keep_artifacts


class KeepArtifactsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.root = Path(self.dir.name)
        self.home = self.root / "home"
        (self.root / "card").mkdir()
        self.pdf = self.root / "card" / "draft.pdf"
        self.pdf.write_bytes(b"%PDF draft")

    def tearDown(self):
        self.dir.cleanup()

    def test_the_same_file_listed_twice_is_kept_once(self):
        kept = keep_artifacts(self.home, "dlv_1", [str(self.pdf), str(self.root / "card" / ".." / "card" / "draft.pdf")])
        self.assertEqual([k["path"] for k in kept], ["deliveries/dlv_1/draft.pdf"])
        self.assertEqual(sorted(p.name for p in (self.home / "deliveries/dlv_1").iterdir()), ["draft.pdf"])

    def test_the_same_bytes_from_two_places_are_kept_once(self):
        scratch = self.root / "scratch.pdf"
        scratch.write_bytes(self.pdf.read_bytes())
        kept = keep_artifacts(self.home, "dlv_1", [str(scratch), str(self.pdf)])
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(list((self.home / "deliveries/dlv_1").iterdir())), 1)

    def test_different_files_with_one_name_are_both_kept(self):
        other = self.root / "other"
        other.mkdir()
        (other / "draft.pdf").write_bytes(b"%PDF other")
        kept = keep_artifacts(self.home, "dlv_1", [str(self.pdf), str(other / "draft.pdf")])
        self.assertEqual([k["path"] for k in kept], ["deliveries/dlv_1/draft.pdf", "deliveries/dlv_1/draft-2.pdf"])


if __name__ == "__main__":
    unittest.main()

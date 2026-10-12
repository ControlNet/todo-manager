"""The parallel test runner (``python3 -B -m tests.run``), on a synthetic test package."""

import contextlib
import io
import os
import shutil
import tempfile
import textwrap
import unittest
from pathlib import Path

from tests import run

UNIT = '''
import tempfile
import unittest
from pathlib import Path

class Fast(unittest.TestCase):
    def test_passes(self):
        Path(__file__).resolve().parent.parent.joinpath("tmpdir.txt").write_text(tempfile.gettempdir())

    @unittest.skip("not here")
    def test_skipped(self):
        pass

@unittest.skip("needs something absent")
class Absent(unittest.TestCase):
    def test_skipped(self):
        pass
'''
INTEGRATION = '''
import unittest

class Slow(unittest.TestCase):
    def test_passes(self):
        self.assertTrue(True)

    def test_fails(self):
        self.assertEqual(1, 2, "synthetic failure")
'''


class PlanTests(unittest.TestCase):
    def test_integration_tests_run_one_per_process_first_and_unit_tests_in_small_class_chunks(self):
        a = [f"tests.test_a.A.test_{n}" for n in range(1, run.CHUNK + 2)]
        ids = a + ["tests.test_a.B.test_1", "tests.integration.test_k.K.test_1", "tests.integration.test_k.K.test_2"]
        self.assertEqual(run.plan(ids), [["tests.integration.test_k.K.test_1"], ["tests.integration.test_k.K.test_2"],
                                         a[:run.CHUNK], a[run.CHUNK:], ["tests.test_a.B.test_1"]])

    def test_units_known_to_be_slow_start_first_and_unknown_ones_before_them(self):
        ids = ["tests.integration.test_k.K.test_1", "tests.integration.test_k.K.test_2", "tests.test_a.A.test_1",
               "tests.test_a.B.test_1"]
        known = {"tests.integration.test_k.K.test_1": 3.0, "tests.test_a.A.test_1": 5.0, "tests.test_a.B.test_1": 1.0}
        self.assertEqual(run.plan(ids, known), [["tests.integration.test_k.K.test_2"], ["tests.test_a.A.test_1"],
                                                ["tests.integration.test_k.K.test_1"], ["tests.test_a.B.test_1"]])

    def test_a_module_that_cannot_be_imported_is_run_to_show_its_error(self):
        self.assertEqual(run.plan(["unittest.loader._FailedTest.tests.test_broken"]), [["tests.test_broken"]])

    def test_selection_by_mode_or_name(self):
        ids = ["tests.test_a.A.test_1", "tests.integration.test_k.K.test_1"]
        self.assertEqual(run.select(ids, mode="unit", names=[]), ["tests.test_a.A.test_1"])
        self.assertEqual(run.select(ids, mode="integration", names=[]), ["tests.integration.test_k.K.test_1"])
        self.assertEqual(run.select(ids, mode="all", names=[]), ids)
        self.assertEqual(run.select(ids, mode="unit", names=["tests.integration.test_k"]),
                         ["tests.integration.test_k.K.test_1"], "a named test runs whatever the mode")
        self.assertEqual(run.select(ids, mode="unit", names=["tests.test_a.A.test"]), [], "names match whole parts")

    def test_unittest_summaries_are_parsed(self):
        self.assertEqual(run.parse("....\nRan 4 tests in 0.1s\n\nOK\n"), (4, 0))
        self.assertEqual(run.parse("Ran 3 tests in 0.1s\n\nOK (skipped=2)\n"), (3, 2))
        self.assertEqual(run.parse("Ran 5 tests in 1.0s\n\nFAILED (failures=1, errors=1, skipped=1)\n"), (5, 1))
        self.assertEqual(run.parse("Traceback ...\n"), (0, 0))


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="tm-run-"))
        for path, text in {"tests/__init__.py": "", "tests/test_unit.py": UNIT,
                           "tests/integration/__init__.py": "", "tests/integration/test_slow.py": INTEGRATION}.items():
            (self.root / path).parent.mkdir(parents=True, exist_ok=True)
            (self.root / path).write_text(textwrap.dedent(text), encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def main(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = run.main(["-j", "2", *argv], root=self.root)
        return code, out.getvalue()

    def test_default_runs_only_the_unit_tests(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        self.assertIn("Ran 3 tests", out)
        self.assertIn("OK (skipped=2)", out)

    def test_all_reports_a_failure_with_its_output_and_exits_1(self):
        code, out = self.main("--all")
        self.assertEqual(code, 1, out)
        self.assertIn("Ran 5 tests", out)
        self.assertIn("tests.integration.test_slow.Slow.test_fails", out)
        self.assertIn("synthetic failure", out)
        self.assertIn("FAILED", out)

    def test_tests_get_a_private_temporary_directory_that_is_removed_afterwards(self):
        code, out = self.main()
        self.assertEqual(code, 0, out)
        used = Path((self.root / "tmpdir.txt").read_text())
        self.assertTrue(used.name.startswith("todo-manager-tests-"), used)
        self.assertFalse(used.exists(), "removed after the run")
        if os.access("/dev/shm", os.W_OK):
            self.assertEqual(used.parent, Path("/dev/shm"), "in memory where the system has it")

    def test_unit_durations_are_kept_for_the_next_run_to_order_by(self):
        (self.root / run.DURATIONS).write_text('{"tests.test_gone.G.test_1": 2.5}', encoding="utf-8")
        self.main()
        known = run.durations(self.root)
        self.assertEqual(set(known), {"tests.test_gone.G.test_1", "tests.test_unit.Fast.test_passes"},
                         "a unit whose tests were all skipped did not show how long it takes")
        (self.root / run.DURATIONS).write_text("not json", encoding="utf-8")
        self.assertEqual(run.durations(self.root), {}, "a damaged file is ignored")
        code, out = self.main()
        self.assertEqual(code, 0, out)

    def test_a_named_subset(self):
        code, out = self.main("tests.integration.test_slow.Slow.test_passes")
        self.assertEqual(code, 0, out)
        self.assertIn("Ran 1 test", out)


if __name__ == "__main__":
    unittest.main()

import os
import tempfile
import unittest
from pathlib import Path

from todo_manager import config


class ConfigTests(unittest.TestCase):
    def test_runtime_home_inside_source_tree_is_refused(self):
        with self.assertRaises(config.ConfigError):
            config.resolve_home(config.SOURCE_ROOT / "runtime")
        with self.assertRaises(config.ConfigError):
            config.resolve_home(config.SOURCE_ROOT)

    def test_env_home_outside_source_tree_is_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("TODO_HOME")
            os.environ["TODO_HOME"] = tmp
            try:
                self.assertEqual(config.resolve_home(), Path(tmp))
            finally:
                if old is None:
                    os.environ.pop("TODO_HOME")
                else:
                    os.environ["TODO_HOME"] = old

    def test_default_home_is_the_hermes_workspace(self):
        self.assertEqual(config.DEFAULT_HOME, Path("~/.hermes/workspace/todo-manager").expanduser())

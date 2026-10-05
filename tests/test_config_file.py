"""``config_file.set_values``: targeted edits of config.toml. Only the given keys change; the
rest of the file (comments, order, the user's own values) stays byte for byte, and the result is
checked with ``tomllib`` before it replaces the file."""

import shutil
import tempfile
import tomllib
import unittest
from pathlib import Path

from todo_manager import config_file

ORIGINAL = '''\
# Todo Manager runtime configuration.
timezone = "Pacific/Auckland"   # IANA name

[identity]
# Your own identities.
emails = ["me@example.org"]   # the work mailbox
github_logins = []

[email]
enabled = true
# the lab mailbox is not read
accounts = [
  {name = "work", address = "me@example.org", token_command = ["~/.hermes/scripts/token", "work"]},
]

[slack]
exclude = ["#alerts-*", "C0#1"]   # a "#" inside a string is not a comment
'''


class SetValuesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="tm-cfgfile-"))
        self.path = self.tmp / "config.toml"
        self.path.write_text(ORIGINAL, encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def text(self):
        return self.path.read_text(encoding="utf-8")

    def data(self):
        return tomllib.loads(self.text())

    def test_a_value_is_replaced_and_its_comment_kept(self):
        config_file.set_values(self.path, {"identity.emails": ["me@example.org", "me@lab.org"]})
        self.assertIn('emails = ["me@example.org", "me@lab.org"]   # the work mailbox\n', self.text())
        self.assertEqual(self.text().replace('["me@example.org", "me@lab.org"]', '["me@example.org"]'), ORIGINAL)

    def test_a_value_spanning_lines_is_replaced(self):
        accounts = [{"name": "lab", "address": "me@lab.org", "token_command": ["helper", "lab"]}]
        config_file.set_values(self.path, {"email.accounts": accounts})
        self.assertEqual(self.data()["email"]["accounts"], accounts)
        self.assertIn("# the lab mailbox is not read\n", self.text())
        self.assertEqual(self.data()["slack"], tomllib.loads(ORIGINAL)["slack"])

    def test_a_missing_key_is_added_to_its_table(self):
        config_file.set_values(self.path, {"identity.slack_user_ids": ["U1"], "email.query": "in:inbox"})
        data = self.data()
        self.assertEqual(data["identity"]["slack_user_ids"], ["U1"])
        self.assertEqual(data["email"]["query"], "in:inbox")
        self.assertLess(self.text().index("slack_user_ids"), self.text().index("[email]"), "inside [identity]")

    def test_a_missing_table_is_appended(self):
        config_file.set_values(self.path, {"kanban.enabled": True, "github.enabled": True})
        data = self.data()
        self.assertEqual((data["kanban"], data["github"]), ({"enabled": True}, {"enabled": True}))
        self.assertTrue(self.text().startswith(ORIGINAL))

    def test_a_top_level_key_goes_before_the_first_table(self):
        config_file.set_values(self.path, {"language": "zh-CN"})
        self.assertEqual(self.data()["language"], "zh-CN")
        self.assertLess(self.text().index("language"), self.text().index("[identity]"))

    def test_strings_with_quotes_and_brackets(self):
        value = ['say "hi" [now]', "a#b", "中文"]
        config_file.set_values(self.path, {"slack.exclude": value})
        self.assertEqual(self.data()["slack"]["exclude"], value)

    def test_a_new_file_is_created(self):
        new = self.tmp / "new.toml"
        config_file.set_values(new, {"timezone": "UTC", "notion.enabled": True})
        self.assertEqual(tomllib.loads(new.read_text(encoding="utf-8")), {"timezone": "UTC",
                                                                         "notion": {"enabled": True}})

    def test_other_values_never_change(self):
        before = tomllib.loads(ORIGINAL)
        config_file.set_values(self.path, {"github.enabled": True, "identity.github_logins": ["me-gh"]})
        after = self.data()
        after["identity"].pop("github_logins")
        before["identity"].pop("github_logins")
        after.pop("github")
        self.assertEqual(after, before)

    def test_an_unsupported_file_is_left_alone(self):
        self.path.write_text('[identity]\nemails = """\nmultiline\n"""\n', encoding="utf-8")
        before = self.text()
        with self.assertRaises(config_file.ConfigEditError):
            config_file.set_values(self.path, {"identity.emails": ["x"]})
        self.assertEqual(self.text(), before)

    def test_explicit_keys(self):
        self.assertEqual(config_file.explicit(self.path)["email"]["enabled"], True)
        self.assertEqual(config_file.explicit(self.tmp / "missing.toml"), {})


if __name__ == "__main__":
    unittest.main()

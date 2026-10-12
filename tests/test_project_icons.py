"""Project icons (2026-10-12): a built-in Notion icon in its own colour on each project page, so
a project is easy to spot next to its tasks. The user's icon in Notion wins.
Notion is ``FakeNotion`` (test double).
"""

import io
import json
from contextlib import redirect_stdout

from todo_manager import ops, project_icons, store
from tests.test_notion import NotionCase


class IconValueTests(NotionCase):
    def test_known_names_and_colours_only(self):
        self.assertEqual(project_icons.check("Video Camera:Red"), "video-camera:red")
        self.assertEqual(project_icons.check("🤖"), "🤖")
        for bad in ("unicorn:blue", "robot:teal", "robot"):
            with self.assertRaises(ValueError, msg=bad):
                project_icons.check(bad)

    def test_round_trip_through_notion_objects(self):
        for value in ("robot:purple", "🎓", "custom_emoji:abc", "external:https://example.org/i.png"):
            self.assertEqual(project_icons.from_page(project_icons.render(value)), value)
        self.assertEqual(project_icons.from_page({"type": "icon", "icon": {"name": "light_bulb", "color": "blue"}}),
                         "light-bulb:blue", "Notion's own spelling of a name compares equal")
        self.assertEqual(project_icons.from_page({"type": "file", "file": {"url": "https://s3/x?sig=1"}}), "file")
        self.assertIsNone(project_icons.render("file"))


class ProjectIconTests(NotionCase):
    def ctx(self):
        return ops.context(self.conn, self.cfg, human=True)

    def create(self, **fields):
        pid = ops.project_create(self.conn, self.cfg, self.ctx(), title="Atlas", fields=fields, repos=[])["project"]
        self.drain()
        return pid

    def page(self, pid):
        return self.fake.pages[store.get_project(self.conn, pid)["notion_page_id"]]

    def test_a_new_project_page_gets_its_icon(self):
        pid = self.create(icon="video-camera:red")
        self.assertEqual(self.page(pid)["icon"], {"type": "icon", "icon": {"name": "video-camera", "color": "red"}})

    def test_an_unknown_icon_is_refused(self):
        with self.assertRaises(ops.OpError):
            ops.project_create(self.conn, self.cfg, self.ctx(), title="X", fields={"icon": "unicorn:blue"}, repos=[])

    def test_changing_the_icon_updates_the_page(self):
        pid = self.create()
        self.assertNotIn("icon", self.page(pid))
        ops.project_update(self.conn, self.cfg, self.ctx(), pid, changes={"icon": "robot:purple"})
        self.drain()
        self.assertEqual(self.page(pid)["icon"]["icon"], {"name": "robot", "color": "purple"})
        writes = len(self.writes())
        self.pull()
        self.sync.enqueue_dirty()
        self.drain()
        self.assertEqual(len(self.writes()), writes, "our own icon is not an edit")

    def test_the_users_icon_in_notion_wins(self):
        pid = self.create(icon="robot:purple")
        self.fake.user_set_icon(self.page(pid)["id"], {"type": "emoji", "emoji": "🧪"})
        self.pull()
        self.assertEqual(store.get_project(self.conn, pid)["icon"], "🧪")
        result = store.update_project(self.conn, pid, {"icon": "brain:orange"}, actor="agent", origin="intake")
        self.assertIn("icon", result.rejected)
        self.drain()
        self.assertEqual(self.page(pid)["icon"], {"type": "emoji", "emoji": "🧪"})

    def test_an_uploaded_image_is_kept(self):
        pid = self.create(icon="robot:purple")
        self.fake.user_set_icon(self.page(pid)["id"], {"type": "file", "file": {"url": "https://s3/x?sig=1"}})
        self.pull()
        ops.project_update(self.conn, self.cfg, self.ctx(), pid, changes={"goal": "Submit"})
        self.drain()
        self.assertEqual(self.page(pid)["icon"]["type"], "file")

    def test_icons_lists_names_and_the_colours_still_free(self):
        self.create(icon="video-camera:red")
        from todo_manager import cli
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["--home", str(self.cfg.home), "project", "icons"]), 0)
        data = json.loads(out.getvalue())
        self.assertIn("robot", data["names"])
        self.assertEqual(data["in_use"], {"red": ["Atlas"]})
        self.assertNotIn("red", data["free_colors"])
        self.assertEqual(data["free_colors"][0], "blue")

    def test_an_icon_set_before_the_upgrade_is_adopted(self):
        pid = self.create()
        page = self.page(pid)
        self.fake.user_set_icon(page["id"], {"type": "emoji", "emoji": "🚀"}, author="bot-or-anyone")
        self.conn.execute("UPDATE notion_pages SET last_seen_props=json_remove(last_seen_props, '$.icon')"
                          " WHERE page_id=?", (page["id"],))
        ops.project_update(self.conn, self.cfg, self.ctx(), pid, changes={"goal": "Submit"})
        self.drain()
        self.assertEqual(self.page(pid)["icon"], {"type": "emoji", "emoji": "🚀"})
        self.assertEqual(store.get_project(self.conn, pid)["icon"], "🚀")

    def test_intake_gives_a_new_project_an_icon(self):
        from todo_manager import hermes_install
        prompt = " ".join(hermes_install.asset("cron/intake.md").split())
        self.assertIn("--set icon=<name>:<color>", prompt)
        self.assertIn("`todo-manager project icons`", prompt)

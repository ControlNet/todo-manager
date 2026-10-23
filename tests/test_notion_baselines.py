"""What a write leaves as the baseline (``notion_pages.last_seen_props``) decides whether a later
human edit in Notion is imported or reverted; a property Notion no longer has is not a clear.

* A user edit landing between our read and our PATCH is the user's, not the bot's.
* A page found by its TM ID after a lost create is compared with what the create sent.
* A create that may have happened (lost response, killed process) is looked up before another one
  is sent, also from the row's own reconciling attempt and from a task's push of its project.
* A deleted, recreated or retyped property is reported and left out of import and comparison.

Synthetic tasks and projects on the in-memory Notion double only; no real Notion."""
from tests.test_notion import NotionCase
from todo_manager import clock, effects, store
from todo_manager.db import get_meta
from todo_manager.notion import schema, setup


class Names:
    def name(self, field, kind="task"):
        return schema.prop(kind, field).name(self.sync.state.language)

    def option(self, group, value):
        return schema.label(group, value, self.sync.state.language)


class ConcurrentEditTests(NotionCase, Names):
    """notion-2: the user changes a field we do not send while our PATCH is under way."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "Synthetic report", origin="intake")
        self.push(self.task)
        self.page = self.page_of(self.task)
        clock.advance(minutes=1)

    def edit_during_patch(self, **props):
        dispatch = self.fake._dispatch

        def racing(method, path, params, body):
            if method == "PATCH" and path.strip("/") == f"pages/{self.page}" and racing.armed:
                racing.armed = False
                self.fake.user_edit(self.page, **props)    # lands after our read, before our write
            return dispatch(method, path, params, body)
        racing.armed = True
        self.fake._dispatch = racing

    def test_a_field_changed_during_our_write_is_imported_not_reverted(self):
        store.update_task(self.conn, self.task, {"next_step": "Draft the outline"}, actor="agent", origin="intake",
                          based_on_version=store.get_task(self.conn, self.task)["version"])
        self.edit_during_patch(**{self.name("status"): self.option("task_status", "done")})
        self.push(self.task)
        self.assertEqual(self.fake.prop(self.page, self.name("next_step")), "Draft the outline")
        clock.advance(minutes=1)
        self.pull()
        self.drain()
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "done", "the user's edit is imported")
        self.assertEqual(self.fake.prop(self.page, self.name("status")), self.option("task_status", "done"),
                         "and not reverted in Notion")
        edits = self.conn.execute("SELECT * FROM feedback WHERE kind='property_edit'").fetchall()
        self.assertEqual([e["status"] for e in edits], ["applied"], "applied once, not kept as evidence")


class LostCreateBaselineTests(NotionCase, Names):
    """notion-1: the create's response is lost, then the user edits the page before we link it."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "Synthetic invoice check", origin="intake", next_step="Open the file")
        self.fake.lose_response.add(("POST", "/pages"))
        self.assertEqual(self.push(self.task)["uncertain"], 1)
        self.page = self.fake.pages_in(self.tasks_ds)[0]["id"]
        clock.advance(minutes=1)
        self.fake.user_edit(self.page, **{self.name("title"): "Synthetic invoice check (renamed)",
                                          self.name("status"): self.option("task_status", "done")})
        clock.advance(minutes=1)

    def assert_user_values_kept(self):
        task = store.get_task(self.conn, self.task)
        self.assertEqual((task["title"], task["status"]), ("Synthetic invoice check (renamed)", "done"))
        self.assertEqual(self.fake.prop(self.page, self.name("title")), "Synthetic invoice check (renamed)")
        self.assertEqual(self.fake.prop(self.page, self.name("status")), self.option("task_status", "done"))
        self.assertEqual(self.fake.prop(self.page, self.name("next_step")), "Open the file")
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1)

    def test_the_outbox_reconcile_imports_the_edits_made_since_the_create(self):
        self.drain()
        self.assertEqual(self.page_of(self.task), self.page)
        self.drain()
        self.assert_user_values_kept()

    def test_a_pull_that_finds_the_page_first_imports_them_too(self):
        self.pull()
        self.assertEqual(self.page_of(self.task), self.page)
        clock.advance(minutes=2)
        self.drain()
        self.assert_user_values_kept()

    def test_without_a_record_of_the_create_the_remote_values_are_kept(self):
        """A create sent by a version that did not record it: no baseline, Notion is not overwritten."""
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'notion.create_pending:%'")
        self.drain()
        self.drain()
        self.assert_user_values_kept()


class LostCreateLookupTests(NotionCase, Names):
    """Gaps A1/A2: a create that may have happened is looked up first, by whichever attempt."""

    def setUp(self):
        super().setUp()
        self.hidden = set()
        query = self.fake._query

        def lagging(ds, body):   # pages Notion has created but does not list yet
            data = query(ds, body)
            return {**data, "results": [p for p in data["results"] if p["id"] not in self.hidden]}
        self.fake._query = lagging

    def crash_after_create(self):
        """The process is killed after Notion committed the page, before the response was handled."""
        new_page = self.fake._new_page

        def crashing(*args, **kwargs):
            page = new_page(*args, **kwargs)
            self.fake._new_page = new_page
            self.hidden.add(page["id"])
            raise KeyboardInterrupt
        self.fake._new_page = crashing

    def test_the_rows_own_reconcile_waits_for_a_create_that_may_not_be_listed_yet(self):
        task = store.create_task(self.conn, "Synthetic travel claim", origin="intake")
        effects.task_push(self.conn, task)
        self.crash_after_create()
        with self.assertRaises(KeyboardInterrupt):
            self.drain()
        row = self.conn.execute("SELECT status FROM outbox WHERE op='push_task'").fetchone()
        self.assertEqual(row["status"], "in_flight")
        clock.advance(seconds=10)    # restarted at once: the own row reconciles
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1, "no second page while the first is unlisted")
        self.assertEqual(self.conn.execute("SELECT status FROM outbox WHERE op='push_task'").fetchone()["status"],
                         "uncertain")
        self.hidden.clear()
        clock.advance(minutes=2)
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1)
        self.assertIsNotNone(self.page_of(task))
        self.assertIsNone(get_meta(self.conn, f"notion.create_pending:task:{task}"), "settled once linked")

    def test_a_lost_project_create_in_a_task_push_is_not_repeated_by_the_project_push(self):
        project = store.create_project(self.conn, "Synthetic project", origin="intake")
        task = store.create_task(self.conn, "Synthetic subtask", origin="intake", project_id=project)
        effects.task_push(self.conn, task)
        self.fake.lose_response.add(("POST", "/pages"))   # the first create: the task's project
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.projects_ds)), 1)
        self.hidden.update(p["id"] for p in self.fake.pages_in(self.projects_ds))
        clock.advance(seconds=10)
        store.update_project(self.conn, project, {"goal": "Synthetic goal"}, actor="human", origin="cli")
        effects.project_push(self.conn, project)
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.projects_ds)), 1, "no second project page")
        self.hidden.clear()
        clock.advance(minutes=5)
        self.drain()
        self.drain()
        self.assertEqual(len(self.fake.pages_in(self.projects_ds)), 1)
        self.assertEqual(len(self.fake.pages_in(self.tasks_ds)), 1)
        self.assertIsNotNone(self.page_of(task))


class MissingPropertyTests(NotionCase, Names):
    """notion-6: a property Notion no longer has (or with another type) is unreadable, not empty."""

    def setUp(self):
        super().setUp()
        self.task = store.create_task(self.conn, "Synthetic grant report", origin="intake", due_date="2026-11-20")
        self.push(self.task)
        self.page = self.page_of(self.task)
        self.ds = self.fake.data_sources[self.tasks_ds]
        clock.advance(minutes=1)

    def recreate(self, field):
        """The user deletes the property and adds one with the same name (a new property id)."""
        name = self.name(field)
        old = self.ds["properties"].pop(name)
        del self.ds["by_id"][old["id"]]
        self.fake._add_ds_property(self.ds, name, {old["type"]: {}})
        new = self.ds["properties"][name]
        for page in self.fake.pages_in(self.tasks_ds):
            page["properties"][name] = {"id": new["id"], "type": new["type"], new["type"]: None}

    def user_touches_the_page(self):
        clock.advance(minutes=1)
        self.fake.user_edit(self.page, **{self.name("next_step"): "Collect receipts"})

    def problems(self):
        return [p for p in self.sync.problems if self.name("due_date") in p]

    def test_a_recreated_property_does_not_clear_the_local_value(self):
        self.recreate("due_date")
        self.user_touches_the_page()
        self.pull()
        task = store.get_task(self.conn, self.task)
        self.assertEqual(task["next_step"], "Collect receipts", "the readable edit is imported")
        self.assertEqual(task["due_date"], "2026-11-20", "an unreadable property is not a clear")
        self.assertEqual(len(self.problems()), 1, "reported once, as a schema problem")
        effects.task_push(self.conn, self.task)
        self.assertEqual(self.drain()["done"], 1, "the push leaves the unreadable property out")
        # `todo-manager install` maps the recreated property; the date is written into it.
        setup.apply(self.conn, self.client, self.parent)
        self.user_touches_the_page()
        self.pull()
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-11-20")
        self.assertEqual(self.problems(), [])
        effects.task_push(self.conn, self.task)
        self.drain()
        self.assertEqual(self.fake.prop(self.page, self.name("due_date")), "2026-11-20")

    def test_a_property_of_another_type_is_not_imported(self):
        name = self.name("due_date")
        prop = self.ds["properties"][name]
        prop["type"] = "rich_text"
        prop.pop("date", None)
        prop["rich_text"] = {}
        self.fake.pages[self.page]["properties"][name] = {
            "id": prop["id"], "type": "rich_text",
            "rich_text": [{"type": "text", "text": {"content": "next week"}, "plain_text": "next week"}]}
        self.user_touches_the_page()
        self.pull()
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-11-20")
        self.assertEqual(len(self.problems()), 1)

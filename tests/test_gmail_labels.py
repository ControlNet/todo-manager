"""Gmail labels for the mail linked to tasks (``email.labels``). A fake Gmail API only."""

from unittest import mock

from todo_manager import config, gate_feedback, gmail_labels, health, store
from todo_manager.net import HttpError
from tests.helpers import WorkspaceCase, mail


class FakeGmail:
    """Labels and the labels of each message, as the Gmail API keeps them."""

    def __init__(self, messages=(), labels=None):
        self.labels = dict(labels or {})          # id -> name
        self.colors = {}                          # id -> {backgroundColor, textColor}
        self.messages = {m: set() for m in messages}
        self.calls = []
        self.fail = None

    def __call__(self, method, url, headers=None, params=None, timeout=None, body=None):
        path = url.split("/users/me/", 1)[1]
        self.calls.append((method, path, body))
        if self.fail:
            raise self.fail
        if (method, path) == ("GET", "labels"):
            return {"labels": [{"id": i, "name": n, "type": "user", **({"color": self.colors[i]} if i in self.colors
                                                                         else {})} for i, n in self.labels.items()]}
        if (method, path) == ("POST", "labels"):
            if body["name"].casefold() in {n.casefold() for n in self.labels.values()}:
                raise HttpError(409, "Label name exists or conflicts")
            label_id = f"Label_{len(self.labels) + 1}"
            self.labels[label_id] = body["name"]
            if "color" in body:
                self.colors[label_id] = body["color"]
            return {"id": label_id, "name": body["name"]}
        if method == "PATCH" and path.startswith("labels/"):
            label_id = path.split("/")[1]
            self.labels[label_id] = body["name"]
            if "color" in body:
                self.colors[label_id] = body["color"]
            return {"id": label_id, "name": body["name"]}
        if (method, path) == ("POST", "messages/batchModify"):
            if any(m not in self.messages for m in body["ids"]):
                raise HttpError(400, "Invalid id value")
            for m in body["ids"]:
                self.modify(m, body)
            return None
        if method == "POST" and path.endswith("/modify"):
            m = path.split("/")[1]
            if m not in self.messages:
                raise HttpError(404, "Requested entity was not found.")
            self.modify(m, body)
            return {"id": m}
        raise AssertionError(f"unexpected request {method} {path}")

    def modify(self, message, body):
        for label in body.get("addLabelIds", []) + body.get("removeLabelIds", []):
            if label not in self.labels:
                raise HttpError(400, "Invalid label")
        self.messages[message] |= set(body.get("addLabelIds", []))
        self.messages[message] -= set(body.get("removeLabelIds", []))

    def names(self, message):
        return sorted(self.labels[i] for i in self.messages[message])

    def color(self, name):
        found = self.colors.get(next(i for i, n in self.labels.items() if n == name))
        return found and (found["backgroundColor"], found["textColor"])

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]


class LabelCase(WorkspaceCase):
    overrides = {"email": {"enabled": True, "labels": True,
                           "accounts": [{"name": "primary", "address": "me@example.edu", "token_command": ["true"]}]}}

    def setUp(self):
        super().setUp()
        self.gmail = FakeGmail(["m1", "m2", "m3", "m4"])
        self.add_items(*(mail(m, "t" + m, "Body") for m in ("m1", "m2", "m3", "m4")))
        self.project = store.create_project(self.conn, "Orchard", origin="test")

    def task(self, *mails, project=None, status="todo"):
        tid = store.create_task(self.conn, "Task " + "".join(mails), origin="test", project_id=project, status=status)
        for m in mails:
            store.link_evidence(self.conn, "task", tid, self.item_id(m), "origin")
        return tid

    def close(self, tid, status="done"):
        store.update_task(self.conn, tid, {"status": status}, actor="human", origin="test")

    def sync(self):
        return gmail_labels.sync(self.conn, self.cfg, apis=lambda account: gmail_labels.LabelsApi(
            lambda: "token", http=self.gmail))


class LabelNameTests(LabelCase):
    def test_mail_gets_its_project_and_the_state_of_its_tasks(self):
        self.task("m1", project=self.project)
        self.task("m2", status="done")
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard/进行中"])
        self.assertEqual(self.gmail.names("m2"), ["Todo/其他/已完成"])
        self.assertTrue({"Todo", "Todo/Orchard", "Todo/其他"} <= set(self.gmail.labels.values()))  # nest only
        self.assertEqual(self.gmail.names("m3"), [])                 # mail no task links stays as it is

    def test_open_wins_then_done_then_cancelled(self):
        self.task("m1", project=self.project, status="done")
        self.task("m1", project=self.project, status="waiting")
        self.task("m2", project=self.project, status="done")
        self.task("m2", project=self.project, status="cancelled")
        self.task("m3", project=self.project, status="cancelled")
        self.sync()
        self.assertEqual(self.gmail.names("m1")[0], "Todo/Orchard/进行中")
        self.assertEqual(self.gmail.names("m2")[0], "Todo/Orchard/已完成")
        self.assertEqual(self.gmail.names("m3")[0], "Todo/Orchard/已取消")

    def test_mail_of_two_projects_carries_both(self):
        other = store.create_project(self.conn, "Teaching", origin="test")
        self.task("m1", project=self.project)
        self.task("m1", project=other, status="done")
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard/进行中", "Todo/Teaching/已完成"])

    def test_a_slash_in_a_title_does_not_nest_a_label(self):
        project = store.create_project(self.conn, "Atlas / Partner Lab", origin="test")
        self.task("m1", project=project)
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Atlas - Partner Lab/进行中"])

    def test_english_names_and_another_root(self):
        self.cfg.data["language"] = "en"
        self.cfg.data["email"]["label_root"] = "Tasks"
        self.task("m1")
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Tasks/Other/Open"])


# The colours users.labels accepts (Gmail API reference, users.labels, checked 2026-10-15).
GMAIL_COLORS = set("""#000000 #434343 #666666 #999999 #cccccc #efefef #f3f3f3 #ffffff #fb4c2f #ffad47 #fad165
#16a766 #43d692 #4a86e8 #a479e2 #f691b3 #f6c5be #ffe6c7 #fef1d1 #b9e4d0 #c6f3de #c9daf8 #e4d7f5 #fcdee8 #efa093
#ffd6a2 #fce8b3 #89d3b2 #a0eac9 #a4c2f4 #d0bcf1 #fbc8d9 #e66550 #ffbc6b #fcda83 #44b984 #68dfa9 #6d9eeb #b694e8
#f7a7c0 #cc3a21 #eaa041 #f2c960 #149e60 #3dc789 #3c78d8 #8e63ce #e07798 #ac2b16 #cf8933 #d5ae49 #0b804b #2a9c68
#285bac #653e9b #b65775 #822111 #a46a21 #aa8831 #076239 #1a764d #1c4587 #41236d #83334c #464646 #e7e7e7 #0d3472
#b6cff5 #0d3b44 #98d7e4 #3d188e #e3d7ff #711a36 #fbd3e0 #8a1c0a #f2b2a8 #7a2e0b #ffc8af #7a4706 #ffdeb5 #594c05
#fbe983 #684e07 #fdedc1 #0b4f30 #b3efd3 #04502e #a2dcc1 #c2c2c2 #4986e7 #2da2bb #b99aff #994a64 #f691b2 #ff7537
#ffad46 #662e37 #ebdbde #cca6ac #094228 #42d692 #16a765""".split())


class LabelColorTests(LabelCase):
    def test_every_colour_is_one_gmail_accepts(self):
        used = {c for pair in gmail_labels.PALETTE.values() for colors in pair for c in colors}
        self.assertLessEqual(used | set(gmail_labels.CANCELLED), GMAIL_COLORS)

    def test_the_hue_is_the_project_icon_strong_open_light_done_grey_cancelled(self):
        store.update_project(self.conn, self.project, {"icon": "robot:purple"}, actor="human", origin="test")
        self.task("m1", project=self.project)
        self.task("m2", project=self.project, status="done")
        self.task("m3", project=self.project, status="cancelled")
        self.task("m4")
        self.sync()
        self.assertEqual(self.gmail.color("Todo/Orchard/进行中"), ("#a479e2", "#ffffff"))
        self.assertEqual(self.gmail.color("Todo/Orchard/已完成"), ("#e4d7f5", "#41236d"))
        self.assertEqual(self.gmail.color("Todo/Orchard/已取消"), ("#efefef", "#999999"))
        self.assertEqual(self.gmail.color("Todo/Orchard"), ("#a479e2", "#ffffff"))
        self.assertEqual(self.gmail.color("Todo/其他/进行中"), ("#666666", "#ffffff"))
        self.assertIsNone(self.gmail.color("Todo"))

    def test_a_new_icon_colour_recolours_the_labels_not_the_mail(self):
        self.task("m1", project=self.project)
        self.sync()
        self.assertEqual(self.gmail.color("Todo/Orchard/进行中"), ("#666666", "#ffffff"))
        store.update_project(self.conn, self.project, {"icon": "server:blue"}, actor="human", origin="test")
        self.gmail.calls.clear()
        self.sync()
        self.assertEqual(self.gmail.color("Todo/Orchard/进行中"), ("#4a86e8", "#ffffff"))
        self.assertFalse([c for c in self.gmail.calls if c[1] == "messages/batchModify"])
        self.gmail.calls.clear()
        self.sync()
        self.assertEqual(self.gmail.calls, [])

    def test_labels_made_before_colours_get_them(self):
        self.task("m1")
        self.sync()
        self.gmail.colors.clear()
        self.conn.execute("DELETE FROM meta WHERE key LIKE 'gmail_labels.look:%'")
        self.sync()
        self.assertEqual(self.gmail.color("Todo/其他/进行中"), ("#666666", "#ffffff"))


class LabelChangeTests(LabelCase):
    def test_the_project_label_once_put_on_mail_is_taken_off(self):
        self.task("m1", project=self.project)
        self.sync()
        parent = next(i for i, n in self.gmail.labels.items() if n == "Todo/Orchard")
        self.gmail.messages["m1"].add(parent)
        self.conn.execute("UPDATE gmail_message_labels SET keys=?", (f'["p:{self.project}","p:{self.project}:open"]',))
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard/进行中"])

    def test_a_closed_task_moves_its_mail(self):
        tid = self.task("m1", project=self.project)
        self.sync()
        self.close(tid)
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard/已完成"])

    def test_nothing_changed_makes_no_request(self):
        self.task("m1", project=self.project)
        self.sync()
        self.gmail.calls.clear()
        self.assertEqual(self.sync()["labelled"], 0)
        self.assertEqual(self.gmail.calls, [])

    def test_mail_with_the_same_change_is_sent_in_one_request(self):
        self.task("m1", "m2", "m3", project=self.project)
        self.sync()
        self.assertEqual(sum(1 for c in self.gmail.writes() if c[1] == "messages/batchModify"), 1)

    def test_a_renamed_project_renames_its_labels(self):
        self.task("m1", project=self.project, status="done")
        self.sync()
        before = dict(self.gmail.labels)
        store.update_project(self.conn, self.project, {"title": "Orchard Lab"}, actor="human", origin="test")
        self.gmail.calls.clear()
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard Lab/已完成"])
        self.assertEqual(set(self.gmail.labels), set(before))         # same labels, new names
        self.assertFalse([c for c in self.gmail.calls if c[1] == "messages/batchModify"])

    def test_a_task_moved_to_another_project_moves_its_mail(self):
        tid = self.task("m1")
        self.sync()
        store.update_task(self.conn, tid, {"project_id": self.project}, actor="human", origin="test")
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/Orchard/进行中"])

    def test_an_existing_label_of_that_name_is_used(self):
        self.gmail.labels["Label_mine"] = "Todo/其他/进行中"
        self.task("m1")
        self.sync()
        self.assertIn("Label_mine", self.gmail.messages["m1"])
        self.assertEqual(list(self.gmail.labels.values()).count("Todo/其他/进行中"), 1)
        self.assertEqual(self.gmail.color("Todo/其他/进行中"), ("#666666", "#ffffff"))

    def test_a_label_deleted_in_gmail_comes_back_on_its_mail(self):
        self.task("m1")
        tid = self.task("m2")
        self.sync()
        gone = next(i for i, n in self.gmail.labels.items() if n == "Todo/其他/进行中")
        del self.gmail.labels[gone]
        for labels in self.gmail.messages.values():
            labels.discard(gone)
        self.close(tid)       # any change sends the pass to Gmail
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/其他/进行中"])
        self.assertEqual(self.gmail.names("m2"), ["Todo/其他/已完成"])

    def test_mail_gone_from_gmail_is_not_tried_again(self):
        self.task("m1", "m2")
        del self.gmail.messages["m2"]
        result = self.sync()
        self.assertEqual((result["labelled"], result["gone"]), (1, 1))
        self.assertEqual(self.gmail.names("m1"), ["Todo/其他/进行中"])
        self.gmail.calls.clear()
        self.sync()
        self.assertEqual(self.gmail.calls, [])

    def test_a_failed_pass_is_retried(self):
        self.task("m1")
        self.gmail.fail = HttpError(503, "backend error")
        with self.assertRaises(HttpError):
            self.sync()
        self.gmail.fail = None
        self.sync()
        self.assertEqual(self.gmail.names("m1"), ["Todo/其他/进行中"])

    def test_mail_of_an_account_no_longer_configured_is_left(self):
        self.add_items(mail("x1", "tx", "Body"), scope="other")
        self.conn.execute("UPDATE source_items SET account='old' WHERE external_id='x1'")
        self.task("x1")
        result = gmail_labels.sync(self.conn, self.cfg, apis=lambda account: None if account == "old" else
                                   gmail_labels.LabelsApi(lambda: "token", http=self.gmail))
        self.assertEqual(result["unconfigured"], 1)
        self.assertEqual(self.gmail.calls, [])


class LabelSwitchTests(LabelCase):
    def run_sync(self):
        with mock.patch.object(gmail_labels, "api_for",
                               lambda cfg: lambda account: gmail_labels.LabelsApi(lambda: "token", http=self.gmail)):
            return gate_feedback.sync_all(self.conn, self.cfg, notion=None, kanban=None)

    def test_off_unless_turned_on(self):
        self.assertIs(config.DEFAULTS["email"]["labels"], False)
        self.task("m1")
        self.cfg.data["email"]["labels"] = False
        summary, _ = self.run_sync()
        self.assertNotIn("gmail_labels", summary)
        self.assertEqual(self.gmail.calls, [])

    def test_the_sync_labels_mail_and_reports_failures(self):
        self.task("m1")
        summary, problems = self.run_sync()
        self.assertEqual(summary["gmail_labels"]["labelled"], 1)
        self.gmail.fail = HttpError(503, "backend error")
        store.update_task(self.conn, self.task("m2"), {"status": "done"}, actor="human", origin="test")
        summary, problems = self.run_sync()
        self.assertIn("gmail_labels: HttpError", problems)
        self.assertIn("gate", health.stale_sources(self.conn, ("gate",)))

    def test_preview_counts_mail_per_label_without_a_request(self):
        self.task("m1", "m2", project=self.project)
        self.task("m3", status="cancelled")
        shown = gmail_labels.preview(self.conn, self.cfg)
        self.assertEqual(shown["labels"], [{"name": "Todo/Orchard/进行中", "mail": 2},
                                           {"name": "Todo/其他/已取消", "mail": 1}])
        self.assertEqual(shown["mail_to_change"], 3)
        self.assertEqual(self.gmail.calls, [])

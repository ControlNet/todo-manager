"""The Web UI (docs/web-ui.md): its read model, its writes as the user, and the HTTP server with
its sign-in. Kanban is ``FakeKanban`` (a test double); no Notion is involved."""

import http.client
import json
import sqlite3

from todo_manager import clock, db, delegation, feedback, store
from todo_manager.db import loads
from todo_manager.kanban import FakeKanban
from todo_manager.web import actions, server, views
from tests.helpers import WorkspaceCase


class WebCase(WorkspaceCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.n = 0

    def rid(self):
        self.n += 1
        return f"test-request-{self.n:04d}"

    def act(self, task_id, action, **params):
        return actions.act(self.conn, self.cfg, task_id, action, params, self.rid())

    def task(self, title="Reply to Bob", **fields):
        return store.create_task(self.conn, title, origin="intake", **fields)

    def feedback_rows(self, task_id, kind):
        return [feedback.get(self.conn, r["id"]) for r in self.conn.execute(
            "SELECT id FROM feedback WHERE scope_type='task' AND scope_id=? AND kind=? ORDER BY rowid", (task_id, kind))]

    def tick(self):
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def in_review(self, task_id):
        did = delegation.create(self.conn, self.cfg, task_id=task_id, title="Draft", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.kb.worker_start(card)
        self.kb.worker_review(card, "The draft is attached")
        self.tick()
        return did


class ViewTests(WebCase):
    def test_home_groups_follow_what_each_task_needs(self):
        decide = self.task("Accept the invitation?", status="decision")
        mine = self.task("Submit camera-ready", due_date="2026-10-04")
        waiting = self.task("Fig 3 data", status="waiting", waiting_on="Alice", follow_up_date="2026-10-05")
        review = self.task("Comparison table")
        self.in_review(review)
        snoozed = self.task("Later", snooze_until="2026-10-20")
        home = views.home(self.conn, self.cfg)
        ids = {g: [c["id"] for c in cards] for g, cards in home["groups"].items()}
        self.assertEqual(ids["decide"], [decide])
        self.assertEqual(ids["mine"], [mine])
        self.assertEqual(ids["waiting"], [waiting])
        self.assertEqual(ids["agent_wait"], [review])
        self.assertNotIn(snoozed, sum(ids.values(), []))
        self.assertEqual(home["snoozed"], 1)
        card = home["groups"]["mine"][0]
        self.assertTrue(card["overdue"])
        self.assertTrue(home["groups"]["waiting"][0]["follow_up_due"])
        self.assertEqual(home["groups"]["agent_wait"][0]["kind"], "review")

    def test_detail_thread_shows_comments_replies_and_deliveries(self):
        tid = self.task()
        self.in_review(tid)
        actions.comment(self.conn, self.cfg, tid, "Shorter please", "agent", self.rid())
        fb = self.feedback_rows(tid, "comment")[0]
        feedback.resolve(self.conn, fb["id"], "applied", {"reply": "Asked the agent for a shorter version"})
        detail = views.task_detail(self.conn, self.cfg, tid)
        kinds = [(e["who"], e["kind"]) for e in detail["thread"]]
        self.assertIn(("agent", "result"), kinds)
        self.assertIn(("me", "comment"), kinds)
        self.assertIn(("hermes", "reply"), kinds)
        mine = next(e for e in detail["thread"] if e["who"] == "me")
        self.assertEqual((mine["text"], mine["to"], mine["origin"]), ("Shorter please", "agent", "web"))
        self.assertFalse([e for e in detail["thread"] if e["kind"] == "note"], "its copy on the Notion page is not shown again")
        self.assertEqual(detail["agent_work"]["status"], "review")
        self.assertEqual(detail["agent_work"]["versions"][0]["kind"], "result")

    def test_a_board_choice_shows_as_the_choice_with_the_users_note(self):
        tid = self.task("Accept the invitation?", status="decision")
        self.act(tid, "agent", note="Decline politely")
        entry = next(e for e in views.thread(self.conn, tid) if e["kind"] == "delegate_request")
        self.assertEqual((entry["choice"], entry["text"], entry["who"]), ("agent", "Decline politely", "me"))
        other = self.task("Agree?", status="decision")   # a Notion board set up before 2026-10-12 still offers Agree
        actions.edit(self.conn, self.cfg, other, {"decision": "agree"}, rid="old-board-0001")
        entry = next(e for e in views.thread(self.conn, other) if e["kind"] == "decision_choice")
        self.assertEqual((entry["choice"], entry["text"]), ("agree", ""))

    def test_taking_over_from_the_agent_is_not_shown_as_a_choice(self):
        tid = self.task()
        delegation.create(self.conn, self.cfg, task_id=tid, title="Draft", goal="Draft only")
        self.tick()
        self.act(tid, "wait", on="Alice")
        self.assertTrue(self.feedback_rows(tid, "delegate_unassigned"))
        self.assertFalse([e for e in views.thread(self.conn, tid) if e.get("choice")])

    def test_search_finds_tasks_projects_and_sources(self):
        pid = store.create_project(self.conn, "Speech separation paper", origin="intake")
        self.task("Camera-ready for the separation paper", project_id=pid)
        self.task("Unrelated")
        found = views.search(self.conn, self.cfg, "separation")
        self.assertEqual([t["title"] for t in found["tasks"]], ["Camera-ready for the separation paper"])
        self.assertEqual([p["id"] for p in found["projects"]], [pid])
        self.assertEqual(views.search(self.conn, self.cfg, "  ")["tasks"], [])
        self.assertEqual(views.search(self.conn, self.cfg, "100%_")["tasks"], [])

    def test_what_changed_shows_for_a_week_with_its_date(self):
        tid = self.task()
        actions.act(self.conn, self.cfg, tid, "due", {"date": "2026-10-20"}, "test-request-note")
        self.conn.execute("UPDATE tasks SET change_note='Deadline moved', change_note_at=? WHERE id=?",
                          (clock.iso(), tid))
        shown = views.task_detail(self.conn, self.cfg, tid)
        self.assertEqual((shown["change_note"], shown["change_note_at"]), ("Deadline moved", clock.iso()))
        clock.advance(days=6, hours=23)
        self.assertEqual(views.task_detail(self.conn, self.cfg, tid)["change_note"], "Deadline moved")
        clock.advance(hours=2)
        self.assertIsNone(views.task_detail(self.conn, self.cfg, tid)["change_note"])
        self.conn.execute("UPDATE tasks SET change_note_at=NULL WHERE id=?", (tid,))
        self.assertIsNone(views.task_detail(self.conn, self.cfg, tid)["change_note"], "a note without a time is old")

    def test_search_shows_a_mail_thread_once_not_every_reply(self):
        from todo_manager.sources.base import FetchResult, Item, persist_fetch
        replies = [Item(source="gmail", account="primary", external_id=f"m{i}", conversation_key="gmail:primary:t1",
                        thread_key="gmail:primary:t1", occurred_at=f"2026-10-0{i}T00:00:00+00:00", author="a@example.org",
                        author_is_me=False, title="Re: Proposals due soon", body="reply", meta={}) for i in (1, 2, 3)]
        other = Item(source="gmail", account="primary", external_id="m9", conversation_key="gmail:primary:t2",
                     thread_key="gmail:primary:t2", occurred_at="2026-10-05T00:00:00+00:00", author="b@example.org",
                     author_is_me=False, title="Your proposal", body="proposal", meta={})
        persist_fetch(self.conn, "gmail", FetchResult(scope="test", items=[*replies, other], cursor="c1"))
        found = views.search(self.conn, self.cfg, "proposal")["sources"]
        self.assertEqual([(s["title"], s["at"][:10]) for s in found],
                         [("Your proposal", "2026-10-05"), ("Re: Proposals due soon", "2026-10-03")])

    def test_a_built_in_notion_icon_shows_as_its_image_in_its_colour(self):
        builtin = store.create_project(self.conn, "Lab servers", origin="intake")
        emoji = store.create_project(self.conn, "Paper", origin="intake")
        store.update_project(self.conn, builtin, {"icon": "server:blue"}, actor="agent", origin="intake")
        store.update_project(self.conn, emoji, {"icon": "🎙️"}, actor="agent", origin="intake")
        self.task("Swap the slot", project_id=builtin)
        image = "https://www.notion.so/icons/server_blue.svg"
        look = lambda p: (p["icon"], p["image"], p["color"])
        listed = {p["id"]: p for p in views.project_list(self.conn, self.cfg)}
        self.assertEqual(look(listed[builtin]), (None, image, "blue"))
        self.assertEqual(look(listed[emoji]), ("🎙️", None, None))
        self.assertEqual(look(views.project_detail(self.conn, self.cfg, builtin)), (None, image, "blue"))
        self.assertEqual(look(views.task_list(self.conn, self.cfg)[0]["project"]), (None, image, "blue"))

    def test_a_comment_hermes_has_taken_shows_as_being_handled(self):
        tid = self.task()
        actions.comment(self.conn, self.cfg, tid, "Move it to Friday", "hermes", self.rid())
        fid = self.feedback_rows(tid, "comment")[0]["id"]
        status = lambda: next(e for e in views.task_detail(self.conn, self.cfg, tid)["thread"] if e["who"] == "me")["status"]
        self.assertEqual(status(), "pending")
        before = views.stamp(self.conn)
        feedback.claim(self.conn, "run_1", [fid])
        self.assertEqual(status(), "processing")
        self.assertNotEqual(before, views.stamp(self.conn))   # the page refreshes to show it
        clock.advance(minutes=self.cfg.get("feedback.stale_claim_minutes") + 1)   # a run that died: the next one takes it
        self.assertEqual(status(), "pending")

    def test_stamp_changes_with_a_write(self):
        tid = self.task()
        before = views.stamp(self.conn)
        self.act(tid, "snooze", until="2026-10-07")
        self.assertNotEqual(before, views.stamp(self.conn))


class ActionTests(WebCase):
    def test_decision_choices_match_the_board(self):
        agree = self.task("Agree?", status="decision")
        for action in ("agree", "decline"):   # no longer on the board: what to do is said to Hermes
            with self.assertRaises(actions.ActionError):
                self.act(agree, action)
        self.assertEqual(store.get_task(self.conn, agree)["status"], "decision")

    def test_a_task_to_decide_can_wait_on_someone(self):
        tid = self.task("Accept the invitation?", status="decision", decision="pending")
        self.act(tid, "wait", on="Alice", follow_up="2026-10-09")
        t = store.get_task(self.conn, tid)
        self.assertEqual((t["status"], t["waiting_on"], t["decision"]), ("waiting", "Alice", None))
        self.assertEqual(views.kind_of(self.conn, t)[0], "waiting")
        done = self.task("Done?", status="decision")
        self.act(done, "done")
        t = store.get_task(self.conn, done)
        self.assertEqual((t["status"], t["decision"]), ("done", None))
        self.assertTrue(t["closed_reason"])
        mine = self.task("Mine?", status="decision")
        self.act(mine, "todo")
        t = store.get_task(self.conn, mine)
        self.assertEqual((t["status"], t["next_actor"]), ("todo", "me"))
        history = store.history(self.conn, "task", mine)[-1]
        self.assertEqual((history["actor"], history["origin"]), ("human", "web"))

    def test_assign_to_agent_needs_nothing_and_carries_what_was_added(self):
        plain = self.task("Draft the reply")
        self.act(plain, "agent")
        t = store.get_task(self.conn, plain)
        self.assertEqual((t["status"], t["next_actor"]), ("todo", "agent"))
        request = self.feedback_rows(plain, "delegate_request")
        self.assertEqual(len(request), 1)
        self.assertNotIn("done_criteria", request[0]["payload"])
        decide = self.task("Accept the invitation?", status="decision")
        self.act(decide, "agent", note="Decline politely", done_criteria="A draft I can send", start_after="")
        request = self.feedback_rows(decide, "delegate_request")[0]
        self.assertIn("Decline politely", request["text"])
        self.assertEqual(request["payload"]["done_criteria"], "A draft I can send")
        self.assertNotIn("start_after", request["payload"])
        self.assertEqual(store.get_task(self.conn, decide)["next_actor"], "agent")

    def test_a_hand_over_note_reaches_a_task_still_being_read(self):
        tid = self.task("Created in Notion", status="decision")
        feedback.record(self.conn, origin="notion_page", source_id="page-1", text="Body", scope_type="task",
                        scope_id=tid, kind="new_task", author_is_user=True, payload={"values": {}})
        self.act(tid, "agent", note="Keep it short", done_criteria="One paragraph")
        row = [r for r in self.feedback_rows(tid, "new_task") if r["status"] == "pending"][0]
        self.assertIn("Keep it short", row["text"])
        self.assertEqual((row["payload"]["done_criteria"], row["payload"]["values"]["decision"]),
                         ("One paragraph", "agent"))

    def test_agent_board_choices_settle_the_agent_work(self):
        tid = self.task()
        did = self.in_review(tid)
        self.act(tid, "done")
        self.assertEqual(store.get_task(self.conn, tid)["status"], "done")
        kinds = [m["kind"] for m in self.conn.execute("SELECT kind FROM delegation_messages WHERE delegation_id=?",
                                                      (did,))]
        self.assertIn("accept", kinds)

    def test_cancel_stops_agent_work(self):
        tid = self.task()
        did = delegation.create(self.conn, self.cfg, task_id=tid, title="Draft", goal="Draft only")
        self.tick()
        self.act(tid, "cancel")
        self.assertEqual(store.get_task(self.conn, tid)["status"], "cancelled")
        self.tick()
        self.assertEqual(delegation.get(self.conn, did)["status"], "cancelled")

    def test_time_and_waiting(self):
        tid = self.task()
        self.act(tid, "snooze", until="2026-10-09")
        self.act(tid, "due", date="2026-10-12")
        self.act(tid, "focus", on=True)
        t = store.get_task(self.conn, tid)
        self.assertEqual((t["snooze_until"][:10], t["due_date"][:10], t["focus"]), ("2026-10-09", "2026-10-12", 1))
        self.act(tid, "wait", on="Alice", follow_up="2026-10-08")
        t = store.get_task(self.conn, tid)
        self.assertEqual((t["status"], t["next_actor"], t["waiting_on"]), ("waiting", "external", "Alice"))
        self.act(tid, "todo")
        self.assertEqual(store.get_task(self.conn, tid)["status"], "todo")
        with self.assertRaises(actions.ActionError):
            self.act(tid, "snooze", until="next week")
        with self.assertRaises(actions.ActionError):
            self.act(tid, "wait", on="")

    def test_a_repeated_request_does_nothing_more(self):
        tid = self.task()
        first = actions.act(self.conn, self.cfg, tid, "agent", {}, "same-request-id")
        again = actions.act(self.conn, self.cfg, tid, "agent", {}, "same-request-id")
        self.assertTrue(again["duplicate"])
        self.assertEqual(first["feedback"], again["feedback"])
        self.assertEqual(len(self.feedback_rows(tid, "delegate_request")), 1)
        c1 = actions.comment(self.conn, self.cfg, tid, "Hello", "hermes", "comment-id-1")
        c2 = actions.comment(self.conn, self.cfg, tid, "Hello", "hermes", "comment-id-1")
        self.assertEqual(c1["feedback"], c2["feedback"])
        self.assertTrue(c2["duplicate"])

    def test_comments_wait_for_the_feedback_job(self):
        tid = self.task()
        actions.comment(self.conn, self.cfg, tid, "Move it to next week", "hermes", self.rid())
        row = self.feedback_rows(tid, "comment")[0]
        self.assertEqual((row["origin"], row["status"], row["author_is_user"], row["payload"]), ("web", "pending", 1, {}))
        for bad in [("", "hermes"), ("x", "everyone")]:
            with self.assertRaises(actions.ActionError):
                actions.comment(self.conn, self.cfg, tid, bad[0], bad[1], self.rid())

    def test_pause_needs_agent_work(self):
        tid = self.task()
        with self.assertRaises(actions.ActionError):
            self.act(tid, "pause")
        delegation.create(self.conn, self.cfg, task_id=tid, title="Draft", goal="Draft only")
        self.tick()
        result = self.act(tid, "pause")
        self.assertEqual(result["kind"], "pause")

    def test_unknown_or_misplaced_actions_are_refused(self):
        tid = self.task()
        for action in ("agree", "explode"):
            with self.assertRaises(actions.ActionError):
                self.act(tid, action)
        with self.assertRaises(actions.ActionError):
            actions.act(self.conn, self.cfg, tid, "done", {}, "short")
        with self.assertRaises(actions.ActionError) as caught:
            self.act("task_missing", "done")
        self.assertEqual(caught.exception.status, 404)


class MigrationTests(WebCase):
    def test_web_feedback_needs_schema_17_and_keeps_older_rows(self):
        path = self.tmp / "old.sqlite"
        old = sqlite3.connect(path, isolation_level=None)
        old.row_factory = sqlite3.Row
        db.migrate(old, target=16)
        fid, _ = feedback.record(old, origin="cli", source_id="s", text="kept", scope_type="global")
        with self.assertRaises(sqlite3.IntegrityError):
            old.execute("UPDATE feedback SET origin='web' WHERE id=?", (fid,))
        db.migrate(old)
        self.assertEqual(db.schema_version(old), db.SCHEMA_VERSION)
        self.assertEqual(feedback.get(old, fid)["text"], "kept")
        feedback.record(old, origin="web", source_id="s2", text="new", scope_type="global")
        old.close()


class ServerTests(WebCase):
    def setUp(self):
        super().setUp()
        self.site = server.WebServer(self.cfg, host="127.0.0.1", port=0)
        self.site.start()
        self.token = server.new_token(self.cfg.home)
        self.tid = self.task(status="decision")

    def tearDown(self):
        self.site.stop()
        super().tearDown()

    def request(self, method, path, body=None, headers=None):
        http_conn = http.client.HTTPConnection("127.0.0.1", self.site.port, timeout=10)
        http_conn.request(method, path, body=body, headers=headers or {})
        response = http_conn.getresponse()
        data = response.read()
        http_conn.close()
        return response, data

    def signed_in(self):
        return {"Cookie": f"{server.COOKIE}={server.session_value(self.token)}"}

    def test_the_page_may_load_notion_icons(self):
        response, _ = self.request("GET", "/", headers=self.signed_in())
        self.assertIn("img-src 'self' data: https://www.notion.so/icons/;",
                      response.getheader("Content-Security-Policy"))

    def test_sign_in_sets_a_cookie_only_for_the_token(self):
        response, _ = self.request("GET", "/")
        self.assertEqual((response.status, response.getheader("Location")), (303, "/login"))
        response, _ = self.request("POST", "/login", "token=wrong", {"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(response.getheader("Location"), "/login?failed=1")
        self.assertIsNone(response.getheader("Set-Cookie"))
        response, _ = self.request("POST", "/login", f"token={self.token}",
                                   {"Content-Type": "application/x-www-form-urlencoded", "X-Forwarded-Proto": "https"})
        cookie = response.getheader("Set-Cookie")
        self.assertIn(server.session_value(self.token), cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("Secure", cookie)
        response, data = self.request("GET", "/", headers=self.signed_in())
        self.assertEqual(response.status, 200)
        self.assertIn(b"<html", data)
        server.new_token(self.cfg.home)   # a new token signs every browser out
        response, _ = self.request("GET", "/api/home", headers=self.signed_in())
        self.assertEqual(response.status, 401)

    def test_api_reads_and_writes(self):
        response, data = self.request("GET", "/api/home", headers=self.signed_in())
        self.assertEqual(response.status, 200)
        self.assertEqual(json.loads(data)["groups"]["decide"][0]["id"], self.tid)
        response, data = self.request("GET", f"/api/tasks/{self.tid}", headers=self.signed_in())
        self.assertEqual(json.loads(data)["kind"], "decision")
        response, _ = self.request("GET", "/api/tasks/task_missing", headers=self.signed_in())
        self.assertEqual(response.status, 404)
        body = json.dumps({"action": "done", "rid": "web-test-0001"})
        response, data = self.request("POST", f"/api/tasks/{self.tid}/act", body, self.signed_in())
        self.assertEqual(response.status, 403)
        response, data = self.request("POST", f"/api/tasks/{self.tid}/act", body, {**self.signed_in(), "X-TM-Request": "1"})
        self.assertEqual(response.status, 200, data)
        self.assertEqual(store.get_task(self.conn, self.tid)["status"], "done")
        response, data = self.request("POST", f"/api/tasks/{self.tid}/act", json.dumps({"action": "agree", "rid": "web-test-0002"}),
                                      {**self.signed_in(), "X-TM-Request": "1"})
        self.assertEqual((response.status, json.loads(data)["error"]), (400, "INVALID"))

    def test_the_font_is_served_from_this_origin_with_its_licence(self):
        response, data = self.request("GET", "/static/fonts/plus-jakarta-sans-latin.woff2")
        self.assertEqual((response.status, response.getheader("Content-Type")), (200, "font/woff2"))
        self.assertTrue(data.startswith(b"wOF2"))
        response, data = self.request("GET", "/static/fonts/OFL.txt")
        self.assertEqual(response.status, 200)
        self.assertIn(b"SIL Open Font License", data)
        response, data = self.request("GET", "/static/app.css")
        self.assertIn(b"url(/static/fonts/plus-jakarta-sans-latin.woff2)", data)

    def test_static_files_and_delivered_files_stay_inside_their_folders(self):
        response, _ = self.request("GET", "/static/app.js", headers=self.signed_in())
        self.assertEqual(response.status, 200)
        response, _ = self.request("GET", "/static/../server.py")
        self.assertEqual(response.status, 404)
        response, _ = self.request("GET", "/static/%2e%2e/server.py")
        self.assertEqual(response.status, 404)
        did = delegation.create(self.conn, self.cfg, task_id=self.tid, title="Draft", goal="Draft only")
        folder = self.cfg.home / "deliveries" / "delivery_x"
        folder.mkdir(parents=True)
        (folder / "draft.md").write_text("# Draft", encoding="utf-8")
        self.conn.execute("INSERT INTO deliveries(id, delegation_id, card_id, seq, kind, summary, artifacts, received_at)"
                          " VALUES ('delivery_x', ?, 'c', 1, 'result', 's', ?, '2026-10-05T00:00:00Z')",
                          (did, json.dumps([{"path": "deliveries/delivery_x/draft.md"}, {"path": "config.toml"},
                                            {"path": "../outside.md"}])))
        response, data = self.request("GET", "/api/deliveries/delivery_x/files/0", headers=self.signed_in())
        self.assertEqual((response.status, data, response.getheader("Content-Type")),
                         (200, b"# Draft", "text/plain; charset=utf-8"))
        for index in (1, 2, 3):
            response, _ = self.request("GET", f"/api/deliveries/delivery_x/files/{index}", headers=self.signed_in())
            self.assertEqual(response.status, 404)
        (folder / "page.htm").write_text("<script>alert(1)</script>", encoding="utf-8")
        (folder / "blob.bin").write_bytes(b"\xff\xfe\x00")
        self.conn.execute("UPDATE deliveries SET artifacts=? WHERE id='delivery_x'",
                          (json.dumps([{"path": "deliveries/delivery_x/page.htm"},
                                       {"path": "deliveries/delivery_x/blob.bin"}]),))
        response, _ = self.request("GET", "/api/deliveries/delivery_x/files/0", headers=self.signed_in())
        self.assertEqual(response.getheader("Content-Type"), "text/plain; charset=utf-8")
        self.assertIn("sandbox", response.getheader("Content-Security-Policy"))
        response, _ = self.request("GET", "/api/deliveries/delivery_x/files/1", headers=self.signed_in())
        self.assertEqual((response.getheader("Content-Type"), response.getheader("Content-Disposition")),
                         ("application/octet-stream", "attachment"))
        response, _ = self.request("GET", "/api/deliveries/delivery_x/files/%C2%B2", headers=self.signed_in())
        self.assertEqual(response.status, 404)
        response, data = self.request("GET", "/api/deliveries/delivery_x", headers=self.signed_in())
        self.assertEqual(json.loads(data)["version"]["files"][0]["name"], "page.htm")

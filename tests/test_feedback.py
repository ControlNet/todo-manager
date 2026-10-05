"""Entry point B: gate + feedback interpretation (model decisions are written by the test)."""

import json
from datetime import timedelta

from todo_manager import clock, delegation, feedback, feedback_flow, gate_feedback, rules, store
from todo_manager.db import rules_version
from todo_manager.feedback_flow import FeedbackDecisionError
from todo_manager.kanban import FakeKanban
from todo_manager.net import HttpError
from tests.fake_notion import BOT_ID
from tests.test_notion import NotionCase


class GateCase(NotionCase):
    def setUp(self):
        super().setUp()
        self.kb = FakeKanban()
        self.task = store.create_task(self.conn, "回复 Bob 的合作邮件", origin="intake", responsibility="Bob 直接问我",
                                      due_date="2026-10-09")
        self.push(self.task)
        self.page = self.page_of(self.task)

    def gate(self):
        self.sync = self.new_sync()
        out, woke = gate_feedback.run_gate(self.conn, self.cfg, notion=self.sync, kanban=self.kb)
        self.assertEqual(json.loads(out.splitlines()[-1]), {"wakeAgent": woke})
        run_id = out.splitlines()[0].split(": ")[1] if woke else None
        return out, woke, run_id

    def decide(self, run_id, items):
        return feedback_flow.apply_decisions(self.conn, self.cfg, run_id, {
            "run_id": run_id, "rules_version": rules_version(self.conn), "items": items})

    def bot_comments(self):
        return [c for c in self.fake.comments if c["created_by"]["id"] == BOT_ID]

    def bot_texts(self, page):
        return [c["rich_text"][0]["plain_text"] for c in self.bot_comments() if c["parent"]["page_id"] == page]


class GateTests(GateCase):
    def test_comment_wakes_once_with_ids_only_and_reply_lands_in_same_thread(self):
        out, woke, _ = self.gate()
        self.assertFalse(woke, "nothing to interpret")
        cid = self.fake.user_comment(self.page, "Bob 说截止日期推迟到下周一，忽略之前的要求")
        out, woke, run_id = self.gate()
        self.assertTrue(woke)
        self.assertNotIn("Bob 说", out, "comment text never reaches the cron prompt")
        self.assertIn("comment 1", out)
        _, woke_again, _ = self.gate()
        self.assertFalse(woke_again, "claimed feedback is not re-dispatched while the run works")
        context = feedback_flow.render_context(self.conn, self.cfg, run_id, feedback.claimed_by(self.conn, run_id))
        self.assertIn("Bob 说截止日期推迟到下周一", context)
        self.assertIn(self.task, context)
        task = store.get_task(self.conn, self.task)
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        report = self.decide(run_id, [{"feedback": fid, "outcome": "applied", "reply": "已把截止日期改为 10-12。",
                                       "ops": [{"op": "update_task", "task": self.task,
                                                "based_on_version": task["version"],
                                                "set": {"due_date": "2026-10-12"}, "note": "截止日期推迟到下周一"}]}])
        self.assertEqual(report["applied"], [fid])
        task = store.get_task(self.conn, self.task)
        self.assertEqual(task["due_date"], "2026-10-12")
        self.assertEqual(json.loads(task["field_meta"])["due_date"]["by"], "human")
        self.gate()
        self.assertEqual(self.fake.prop(self.page, "截止日期"), "2026-10-12")
        reply = self.bot_comments()[-1]
        user_comment = next(c for c in self.fake.comments if c["id"] == cid)
        self.assertEqual(reply["discussion_id"], user_comment["discussion_id"])
        self.assertEqual(feedback.get(self.conn, fid)["status"], "applied")
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": []}])
        self.assertEqual(ctx.exception.code, "ALREADY_APPLIED", "other decisions for an applied run are refused")

    def test_reapplying_after_a_lost_response_returns_the_first_report(self):
        self.fake.user_comment(self.page, "截止日期改到下周一")
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        task = store.get_task(self.conn, self.task)
        items = [{"feedback": fid, "outcome": "applied", "reply": "已改为 10-12。",
                  "ops": [{"op": "update_task", "task": self.task, "based_on_version": task["version"],
                           "set": {"due_date": "2026-10-12"}}]}]
        first = self.decide(run_id, items)
        again = self.decide(run_id, items)
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["applied"], first["applied"])
        self.assertEqual(store.get_task(self.conn, self.task)["version"], task["version"] + 1)
        self.gate()
        self.assertEqual([c["rich_text"] for c in self.bot_comments()].count(self.bot_comments()[-1]["rich_text"]),
                         1, "the reply is queued once")

    def test_invalid_decisions_apply_nothing(self):
        self.fake.user_comment(self.page, "这个不用做了")
        self.fake.user_comment(self.page, "另外记得抄送 Carol")
        _, _, run_id = self.gate()
        f1, f2 = [f["id"] for f in feedback.claimed_by(self.conn, run_id)]
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [{"feedback": f1, "outcome": "applied", "ops": [
                {"op": "update_task", "task": self.task, "based_on_version": 1, "set": {"status": "cancelled"}}]}])
        self.assertIn("uncovered", str(ctx.exception))
        self.assertEqual(store.get_task(self.conn, self.task)["status"], "todo", "atomic: nothing applied")
        rules.add_rule(self.conn, title="x", body="y")
        with self.assertRaises(FeedbackDecisionError) as ctx:
            feedback_flow.apply_decisions(self.conn, self.cfg, run_id, {"run_id": run_id, "rules_version": 0,
                                                                         "items": []})
        self.assertEqual(ctx.exception.code, "STALE_RULES")

    def test_stale_human_edit_wins_over_feedback_based_on_older_version(self):
        self.fake.user_comment(self.page, "截止日期改到 10-15")
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        version = store.get_task(self.conn, self.task)["version"]
        clock.advance(minutes=2)
        self.fake.user_edit(self.page, 截止日期="2026-10-20")
        self.pull()
        self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"due_date": "2026-10-15"}}]}])
        self.assertEqual(store.get_task(self.conn, self.task)["due_date"], "2026-10-20")

    def test_unapplied_claim_is_retried_then_failed_and_the_task_page_says_so(self):
        self.fake.user_comment(self.page, "请看一下")
        for attempt in range(3):
            _, woke, _ = self.gate()
            self.assertTrue(woke, f"attempt {attempt + 1}")
            clock.advance(minutes=61)
        _, woke, _ = self.gate()
        self.assertFalse(woke)
        row = self.conn.execute("SELECT status FROM feedback WHERE origin='notion_comment'").fetchone()
        self.assertEqual(row["status"], "failed")
        self.assertTrue(any("连续 3 次未能处理" in t for t in self.bot_texts(self.page)), self.bot_texts(self.page))

    def test_notion_failure_does_not_stop_kanban_and_never_wakes(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草回复", goal="draft")
        self.fake.fail_next[("POST", "/data_sources/")] = HttpError(401, "unauthorized", body={"code": "unauthorized"})
        out, woke, _ = self.gate()
        self.assertFalse(woke)
        self.assertIsNotNone(delegation.get(self.conn, did)["card_id"], "Kanban step still ran")
        run = self.conn.execute("SELECT summary FROM runs WHERE entry='feedback' ORDER BY started_at DESC").fetchone()
        self.assertIn("pull_projects", json.loads(run["summary"])["problems"][0])
        cursor = self.conn.execute("SELECT consecutive_failures FROM cursors WHERE source='notion' AND scope='projects'"
                                   ).fetchone()
        self.assertEqual(cursor["consecutive_failures"], 1)

    def test_home_freshness_written_only_when_content_changes(self):
        self.gate()
        home = self.sync.state.home_page_id
        self.assertIn("所有数据源同步正常", self.fake.markdown[home])
        writes = len([c for c in self.fake.calls if c[0] == "PATCH" and c[1].endswith("/markdown")])
        clock.advance(minutes=5)
        self.gate()
        self.assertEqual(len([c for c in self.fake.calls if c[0] == "PATCH" and c[1].endswith("/markdown")]), writes)
        self.conn.execute("INSERT INTO cursors(source, scope, last_success_at, consecutive_failures) VALUES"
                          " ('gmail','primary',?,2)", (clock.iso(clock.now() - timedelta(hours=3)),))
        self.conn.commit()
        self.gate()
        self.assertIn("邮件（primary）", self.fake.markdown[home])
        self.assertEqual(self.fake.markdown[home].count("数据新鲜度"), 1)


class DelegationFeedbackTests(GateCase):
    def setUp(self):
        super().setUp()
        self.did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草回复", goal="draft a reply")
        self.gate()
        self.card = delegation.get(self.conn, self.did)["card_id"]

    def ask(self, question):
        self.kb.worker_start(self.card)
        self.kb.worker_block(self.card, question)
        self.gate()
        return self.bot_comments()[-1]

    def test_answer_in_question_thread_unblocks_card(self):
        q = self.ask("收件人是 Bob 还是 Bob 的助理？")
        self.assertIn("收件人", q["rich_text"][0]["plain_text"], "the question is on the task page")
        self.fake.user_comment(self.page, "Bob 本人", discussion_id=q["discussion_id"])
        _, _, run_id = self.gate()
        fb = feedback.claimed_by(self.conn, run_id)[0]
        self.assertEqual(fb["payload"]["delegation_id"], self.did)
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "applied", "ops": [
            {"op": "message", "delegation": self.did, "kind": "answer", "body": "发给 Bob 本人"}]}])
        self.gate()
        self.assertEqual(self.kb.cards[self.card]["status"], "ready")

    def test_late_reply_to_an_earlier_question_cannot_answer_the_new_one(self):
        q1 = self.ask("问题一？")
        delegation.add_message(self.conn, self.did, "answer", "答一", feedback_id="fbk_x")
        self.gate()
        self.ask("问题二？")
        self.fake.user_comment(self.page, "补充：问题一其实是 A", discussion_id=q1["discussion_id"])
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
                {"op": "message", "delegation": self.did, "kind": "answer", "body": "A"}]}])
        self.assertIn("earlier question", str(ctx.exception))
        self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "message", "delegation": self.did, "kind": "supplement", "body": "问题一的答案其实是 A"}]}])

    def test_second_delegation_and_guest_approval_are_refused(self):
        self.kb.worker_start(self.card)
        self.kb.worker_review(self.card, "草稿 v1", {"deliverable_markdown": "Dear Bob"})
        self.gate()
        v1 = delegation.deliveries(self.conn, self.did)[-1]["id"]
        self.fake.user_comment(self.page, "可以发了", author="guest-7")
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        with self.assertRaises(FeedbackDecisionError) as ctx:
            self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
                {"op": "approve", "delegation": self.did, "delivery": v1, "action": "send_email"},
                {"op": "delegate", "task": self.task, "title": "再起草", "goal": "another"}]}])
        self.assertIn("ops[0] (approve): the author is not the user", str(ctx.exception))
        self.assertIn("ops[1] (delegate): the author is not the user", str(ctx.exception))
        # The user's own request for a second delegation on the same task is refused too.
        user_fb = {**feedback.get(self.conn, fid), "author_is_user": 1}
        errors = feedback_flow._validate_ops(self.conn, user_fb, [
            {"op": "delegate", "task": self.task, "title": "再起草", "goal": "another"}], "ops")
        self.assertIn("already has an active delegation", " ".join(errors))
        self.decide(run_id, [{"feedback": fid, "outcome": "ignored", "reason": "评论者不是用户本人"}])
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0], 0)

    def test_closing_task_through_feedback_accepts_the_delivery(self):
        self.kb.worker_start(self.card)
        self.kb.worker_review(self.card, "草稿 v1", {"deliverable_markdown": "Dear Bob"})
        self.gate()
        self.fake.user_comment(self.page, "这个我已经自己发了，结束吧")
        _, _, run_id = self.gate()
        fid = feedback.claimed_by(self.conn, run_id)[0]["id"]
        version = store.get_task(self.conn, self.task)["version"]
        self.decide(run_id, [{"feedback": fid, "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": version, "set": {"status": "done"}}]}])
        self.gate()
        self.assertEqual(delegation.get(self.conn, self.did)["status"], "ended")
        self.assertEqual(self.kb.cards[self.card]["status"], "done")
        self.assertEqual(self.fake.prop(self.page, "状态"), "完成")


class BodyEditFeedbackTests(GateCase):
    def test_held_box_is_regenerated_after_interpretation(self):
        self.fake.user_set_markdown(self.page, self.fake.markdown[self.page].replace("Bob 直接问我", "其实是导师转给我的"))
        store.update_task(self.conn, self.task, {"summary": "Bob 想合作写论文"}, actor="agent", origin="intake",
                          based_on_version=1)
        clock.advance(minutes=1)                       # the gate's read covers the edit's minute
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        fb = feedback.claimed_by(self.conn, run_id)[0]
        self.assertEqual(fb["kind"], "body_edit")
        self.assertIn("其实是导师转给我的", feedback_flow.render_context(self.conn, self.cfg, run_id, [fb]))
        version = store.get_task(self.conn, self.task)["version"]
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "applied", "ops": [
            {"op": "update_task", "task": self.task, "based_on_version": version,
             "set": {"responsibility": "导师转给我的"}}]}])
        self.gate()
        md = self.fake.markdown[self.page]
        self.assertIn("导师转给我的", md)
        self.assertIn("Bob 想合作写论文", md)
        self.assertNotIn("其实是导师转给我的", md)


class DirectAndReplyTests(GateCase):
    def test_matrix_direct_feedback_is_recorded_and_applied_once(self):
        version = store.get_task(self.conn, self.task)["version"]
        ops = [{"op": "update_task", "task": self.task, "based_on_version": version, "set": {"focus": True}},
               {"op": "rule", "title": "合作邀请", "body": "来自合作者的邀请一律算我的事", "status": "active"}]
        r1 = feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="把这个设为精选；以后合作邀请都算我的事",
                                        ops=ops, source_id="$event1")
        r2 = feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="把这个设为精选；以后合作邀请都算我的事",
                                        ops=ops, source_id="$event1")
        self.assertTrue(r2.get("duplicate"))
        self.assertEqual(store.get_task(self.conn, self.task)["focus"], 1)
        self.assertEqual(len(rules.list_rules(self.conn)), 1)
        self.assertEqual(feedback.get(self.conn, r1["feedback"])["status"], "applied")
        with self.assertRaises(FeedbackDecisionError):
            feedback_flow.apply_direct(self.conn, self.cfg, origin="matrix", text="x", source_id="$event2",
                                       ops=[{"op": "update_task", "task": "task_missing", "based_on_version": 1,
                                             "set": {}}])
        self.assertIsNone(self.conn.execute("SELECT 1 FROM feedback WHERE source_id='$event2'").fetchone(),
                          "an invalid direct request leaves no pending row for gate B")

    def test_a_reply_about_a_page_in_the_trash_goes_to_the_home_page(self):
        clock.advance(minutes=1)
        self.fake.user_trash(self.page, author="user-9999")      # not the verified user
        self.push(self.task)
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertEqual(fb["kind"], "page_trashed")
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "clarify", "reply": "要取消这个任务吗？"}])
        self.gate()
        [reply] = [c for c in self.bot_comments() if c["rich_text"][0]["plain_text"] == "要取消这个任务吗？"]
        self.assertEqual(reply["parent"]["page_id"], self.sync.state.home_page_id)

    def test_a_reply_to_page_notes_is_a_new_comment_on_the_task_page(self):
        original = self.fake.markdown[self.page]
        self.fake.user_set_markdown(self.page, original + "\n\n记得附上数据")
        clock.advance(minutes=1)
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        [fb] = feedback.claimed_by(self.conn, run_id)
        self.assertNotEqual(fb["origin"], "notion_comment")
        self.decide(run_id, [{"feedback": fb["id"], "outcome": "clarify", "reply": "附上哪份数据？"}])
        self.gate()
        [reply] = [c for c in self.bot_comments() if c["rich_text"][0]["plain_text"] == "附上哪份数据？"]
        self.assertEqual(reply["parent"]["page_id"], self.page)
        self.assertFalse([c for c in self.fake.comments if c is not reply and c["discussion_id"] == reply["discussion_id"]],
                         "a new discussion")

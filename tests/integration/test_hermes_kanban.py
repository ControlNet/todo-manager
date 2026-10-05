"""Integration tests against the INSTALLED Hermes Kanban code in an isolated HERMES_HOME.

Production boards/config are never touched: a temporary HERMES_HOME with
``kanban.review_dispatch: false`` and ``kanban.auto_decompose: false`` is created per test
class. Skipped when the Hermes interpreter is not available.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from todo_manager import delegation, gate_feedback, kanban, store
from todo_manager.kanban import BridgeKanban, KanbanError
from tests.helpers import WorkspaceCase

HERMES_PY = Path(os.environ.get("TM_HERMES_PYTHON", "~/.hermes/hermes-agent/venv/bin/python")).expanduser()
HERMES_REPO = Path(os.environ.get("TM_HERMES_REPO", "~/.hermes/hermes-agent")).expanduser()
SIM = Path(__file__).with_name("hermes_worker_sim.py")
RACE = Path(__file__).with_name("hermes_race_bridge.py")
BOARD = "tm-integration"


class RacingBridge(BridgeKanban):
    """The real bridge, run through hermes_race_bridge.py when ``race`` has steps for the
    transition's action: real Hermes calls land between its anchor check and its state call."""

    race: dict = {}

    def call(self, op, mutating=True, **args):
        if op == "transition" and args.get("action") in self.race:
            with mock.patch.object(kanban, "BRIDGE", RACE):
                return super().call(op, mutating, race=self.race.pop(args["action"]), **args)
        return super().call(op, mutating, **args)


@unittest.skipUnless(HERMES_PY.exists() and HERMES_REPO.exists(), "Hermes interpreter not available")
class HermesKanbanIntegration(WorkspaceCase):
    @classmethod
    def setUpClass(cls):
        cls.hermes_home = Path(tempfile.mkdtemp(prefix="tm-hermes-home-"))
        (cls.hermes_home / "config.yaml").write_text(
            "kanban:\n  review_dispatch: false\n  auto_decompose: false\n", encoding="utf-8")
        BridgeKanban(BOARD, str(HERMES_PY), str(HERMES_REPO), str(cls.hermes_home)).create_board("it", "integration")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.hermes_home, ignore_errors=True)

    def setUp(self):
        super().setUp()
        self.kb = BridgeKanban(BOARD, str(HERMES_PY), str(HERMES_REPO), str(self.hermes_home))
        self.cfg.data["kanban"]["assignee"] = "default"
        self.task = store.create_task(self.conn, "回复评审邮件", origin="cli", actor="human")

    def sim(self, op, **args):
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERMES_KANBAN_")}
        env["HERMES_HOME"] = str(self.hermes_home)
        proc = subprocess.run([str(HERMES_PY), str(SIM)], input=json.dumps({"op": op, "board": BOARD,
                              "hermes_repo": str(HERMES_REPO), **args}), capture_output=True, text=True, env=env,
                              timeout=90, check=False)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("@@SIM@@")]
        self.assertTrue(lines, proc.stderr[-1500:])
        return json.loads(lines[-1][7:])

    def tick(self):
        delegation.send_messages(self.conn, self.cfg, self.kb)
        delegation.submit_pending(self.conn, self.cfg, self.kb)
        delegation.sync_cards(self.conn, self.cfg, self.kb)

    def test_settings_are_read_as_the_installed_dispatcher_reads_them(self):
        self.assertEqual(self.kb.settings(), {"review_dispatch": False, "auto_decompose": False})
        defaults = Path(tempfile.mkdtemp(prefix="tm-hermes-defaults-"))
        try:
            kb = BridgeKanban(BOARD, str(HERMES_PY), str(HERMES_REPO), str(defaults))
            self.assertEqual(kb.settings(), {"review_dispatch": True, "auto_decompose": True},
                             "Hermes' defaults: its own reviewer takes review cards")
            self.assertIn("kanban.review_dispatch is on", delegation.observe_settings(self.conn, kb)["notes"][0])
        finally:
            shutil.rmtree(defaults, ignore_errors=True)

    def test_a_failed_run_parks_the_card_instead_of_a_respawn(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="发送回复", goal="Send after approval")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertEqual(self.kb.snapshot(card)["task"]["max_retries"], 1)
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("fail", task_id=card), "the per-card breaker trips on the first failure")
        self.assertEqual(self.sim("status", task_id=card)["status"], "blocked")
        self.sim("recompute")
        self.assertNotIn(card, self.sim("dispatch_dry")["spawned"])
        self.tick()
        self.assertEqual(delegation.get(self.conn, did)["status"], "blocked", "waits for the user's retry/cancel")

    def test_full_draft_cycle_against_real_kanban(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草回复", goal="Draft only",
                                feedback_id="fbk_it_1")
        self.tick()
        d = delegation.get(self.conn, did)
        card = d["card_id"]
        self.assertTrue(card)
        # Idempotent re-submission through the real create path returns the same card.
        self.assertEqual(self.kb.create(title="x", body="y", idem_key=d["idem_key"], assignee="default",
                                        tenant="todo-manager"), card)
        self.assertEqual(len(self.kb.find(d["idem_key"], f"todo-manager-delegation: {did}")), 1)

        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("block", task_id=card, reason="收件人是谁？", kind="needs_input"))
        self.tick()
        self.assertEqual(delegation.get(self.conn, did)["status"], "blocked")
        # Sticky: recompute + dispatcher do not resume a human-blocked card.
        self.sim("recompute")
        self.assertNotIn(card, self.sim("dispatch_dry")["spawned"])
        self.assertEqual(self.sim("status", task_id=card)["status"], "blocked")

        delegation.add_message(self.conn, did, "answer", "发给 Alice", feedback_id="fbk_it_2")
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "ready")

        # Second question: the real loop breaker routes to triage; we still treat it as waiting.
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("block", task_id=card, reason="要不要抄送 Bob？", kind="needs_input"))
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "triage")
        self.assertEqual(delegation.get(self.conn, did)["status"], "blocked")
        self.assertNotIn(card, self.sim("dispatch_dry")["spawned"])
        delegation.add_message(self.conn, did, "answer", "不用抄送", feedback_id="fbk_it_3")
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "ready",
                         "specify_triage_task moved it back without the LLM specifier")

        # Delivery -> review waits for the human (review_dispatch false).
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("review", task_id=card, summary="草稿完成",
                                 metadata={"deliverable_markdown": "Dear Alice, ..."}))
        self.tick()
        self.assertEqual(delegation.get(self.conn, did)["status"], "review")
        dispatch = self.sim("dispatch_dry")
        self.assertFalse(dispatch["review_gate"])
        self.assertNotIn(card, dispatch["spawned"])
        dl = delegation.deliveries(self.conn, did)
        self.assertEqual(dl[-1]["body"], "Dear Alice, ...")

        # Revision goes back to the same card.
        delegation.add_message(self.conn, did, "revision", "语气更委婉", feedback_id="fbk_it_4")
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "ready")
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("review", task_id=card, summary="草稿 v2",
                                 metadata={"deliverable_markdown": "Dear Alice (v2)"}))
        self.tick()
        delegation.add_message(self.conn, did, "accept", "可以了", feedback_id="fbk_it_5")
        self.tick()
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "done")
        self.assertEqual(delegation.get(self.conn, did)["status"], "ended")
        self.assertEqual(len([x for x in delegation.deliveries(self.conn, did) if x["kind"] == "result"]), 2)
        self.assertNotIn(store.get_task(self.conn, self.task)["status"], ("done", "cancelled"))
        # Exactly one card for this delegation on the real board.
        self.assertEqual(len(self.kb.find(d["idem_key"], f"todo-manager-delegation: {did}")), 1)

    def test_archived_card_found_by_lookup(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="调查", goal="Investigate")
        self.tick()
        d = delegation.get(self.conn, did)
        self.assertTrue(self.kb.archive(d["card_id"]))
        matches = self.kb.find(d["idem_key"], f"todo-manager-delegation: {did}")
        self.assertEqual([m["status"] for m in matches], ["archived"])

    # ----- event-bound transitions (R01/R02/R15) against the real state functions -----

    def _event_id(self, card, kind):
        return max(e["id"] for e in self.kb.snapshot(card)["events"] if e["kind"] == kind)

    def test_retried_unblock_is_already_done_and_leaves_the_next_question(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("block", task_id=card, reason="Q1?", kind="needs_input"))
        q1 = self._event_id(card, "blocked")
        self.assertEqual(self.kb.transition(card, "unblock", q1)["outcome"], "done")
        # The worker runs again and asks a second question; the retry of the first answer
        # (its response was "lost") must not release it.
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("block", task_id=card, reason="Q2?", kind="capability"))
        retry = self.kb.transition(card, "unblock", q1)
        self.assertEqual((retry["outcome"], retry["status"]), ("already", "blocked"))
        q2 = self._event_id(card, "blocked")
        self.assertEqual(self.kb.transition(card, "unblock", q2)["outcome"], "done")
        self.assertEqual(self.sim("status", task_id=card)["status"], "ready")

    def test_old_review_anchor_cannot_reopen_or_complete_a_newer_version(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        v1 = self._event_id(card, "review_requested")
        self.assertEqual(self.kb.transition(card, "reopen_review", v1)["outcome"], "done")
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("review", task_id=card, summary="v2", metadata={"deliverable_markdown": "v2"}))
        self.assertEqual(self.kb.transition(card, "reopen_review", v1)["outcome"], "already")
        self.assertEqual(self.kb.transition(card, "complete", v1)["outcome"], "mismatch")
        self.assertEqual(self.sim("status", task_id=card)["status"], "review")
        # Each review event carries its own run: two results synced at once keep their bodies.
        self.tick()
        self.assertEqual([x["body"] for x in delegation.deliveries(self.conn, did) if x["kind"] == "result"],
                         ["v1", "v2"])
        v2 = self._event_id(card, "review_requested")
        self.assertEqual(self.kb.transition(card, "complete", v2)["outcome"], "done")
        self.assertEqual(self.kb.transition(card, "complete", v2)["outcome"], "already")

    def test_cancel_running_card_reports_worker_termination(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.tick()
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "archived")
        d = delegation.get(self.conn, did)
        self.assertEqual(d["status"], "cancelled")
        # The simulated claim has no worker process, so termination cannot be confirmed: the
        # user is told instead of the cancel being reported as complete.
        self.assertEqual(d["cancel_state"], "unconfirmed")
        again = self.kb.transition(card, "archive")
        self.assertEqual(again["outcome"], "already")
        self.assertIn("terminated", again["termination"])

    # ----- boundary 2: a dashboard action between the bridge's check and its state call -----

    def racing(self):
        kb = RacingBridge(BOARD, str(HERMES_PY), str(HERMES_REPO), str(self.hermes_home))
        kb.race = {}
        return kb

    def test_an_answer_unblocking_a_newer_question_is_reported_as_raced(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("block", task_id=card, reason="Q1?", kind="needs_input"))
        q1 = self._event_id(card, "blocked")
        kb = self.racing()
        kb.race["unblock"] = {"before": "unblock_task", "steps": [
            {"op": "unblock"}, {"op": "claim"}, {"op": "block", "reason": "Q2?", "kind": "capability"}]}
        result = kb.transition(card, "unblock", q1)
        self.assertEqual(result["outcome"], "raced", "Hermes unblocked Q2 with the answer to Q1")
        self.assertEqual(self.kb.transition(card, "unblock", q1)["outcome"], "already", "a retry changes nothing")

    def test_an_accept_completing_a_newer_version_is_reported_to_the_user(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        self.tick()
        mid = delegation.add_message(self.conn, did, "accept", "可以", feedback_id="fbk_acc")
        kb = self.racing()
        kb.race["complete"] = {"before": "complete_task", "steps": [
            {"op": "reopen_review"}, {"op": "claim"},
            {"op": "review", "summary": "v2", "metadata": {"deliverable_markdown": "v2"}}]}
        delegation.send_messages(self.conn, self.cfg, kb)
        self.assertEqual(self.sim("status", task_id=card)["status"], "done")
        msg = self.conn.execute("SELECT status, error FROM delegation_messages WHERE id=?", (mid,)).fetchone()
        self.assertEqual(msg["status"], "sent")
        self.assertIn("raced", msg["error"])
        notices = [r["payload"] for r in self.conn.execute("SELECT payload FROM outbox")
                   if "有人在看板上改动了卡片" in r["payload"]]
        self.assertEqual(len(notices), 1)

    # ----- round 3 (C01, C07, C09) on the installed Kanban code -----

    def events(self, card, kind=None):
        return [e for e in self.kb.snapshot(card)["events"] if kind is None or e["kind"] == kind]

    def started(self):
        did = delegation.create(self.conn, self.cfg, task_id=self.task, title="起草", goal="Draft only")
        self.tick()
        card = delegation.get(self.conn, did)["card_id"]
        self.assertTrue(self.sim("claim", task_id=card))
        self.tick()
        self.assertEqual(delegation.get(self.conn, did)["status"], "running")
        return did, card

    def test_c01_a_revision_sent_after_the_worker_submitted_reopens_that_version(self):
        did, card = self.started()
        mid = delegation.add_message(self.conn, did, "revision", "必须增加附件", feedback_id="fbk_rev")
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        delegation.send_messages(self.conn, self.cfg, self.kb)      # our cache still says running
        self.assertEqual(len(self.events(card, "review_reopened")), 1)
        msg = self.conn.execute("SELECT * FROM delegation_messages WHERE id=?", (mid,)).fetchone()
        commented = [e["id"] for e in self.events(card, "commented")]
        self.assertIn(msg["card_event_id"], commented, "the bridge reports the comment's own event")
        self.assertGreater(msg["card_event_id"], self._event_id(card, "review_requested"))
        self.tick()
        [v1] = delegation.deliveries(self.conn, did)
        self.assertEqual(v1["requirement_version"], 1, "v1 was built without the revision")

    def test_c01_a_revision_steered_into_the_running_worker_counts_for_its_result(self):
        did, card = self.started()
        delegation.add_message(self.conn, did, "revision", "必须增加附件", feedback_id="fbk_rev")
        delegation.send_messages(self.conn, self.cfg, self.kb)
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1+"}))
        self.tick()
        self.assertEqual(self.events(card, "review_reopened"), [])
        self.assertEqual(delegation.deliveries(self.conn, did)[0]["requirement_version"], 2)

    def test_c07_a_queued_answer_is_not_sent_before_the_cancel(self):
        did, card = self.started()
        self.assertTrue(self.sim("block", task_id=card, reason="周四还是周五？", kind="needs_input"))
        self.tick()
        delegation.add_message(self.conn, did, "answer", "周五", feedback_id="fbk_a")
        store.update_task(self.conn, self.task, {"status": "cancelled"}, actor="human", origin="cli")
        self.tick()
        self.assertEqual(self.sim("status", task_id=card)["status"], "archived")
        self.assertEqual(self.events(card, "unblocked"), [], "not resumed only to be archived")

    def test_c09_two_approvals_reach_one_run(self):
        did, card = self.started()
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        self.tick()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        delegation.record_approval(self.conn, did, v1, "send_email", "发给 Alice", "fbk_a")
        delegation.record_approval(self.conn, did, v1, "comment_github", "", "fbk_b")
        self.tick()
        [reopen] = self.events(card, "review_reopened")
        ours = [e["id"] for e in self.events(card, "commented")]
        self.assertEqual(len(ours), 2)
        self.assertTrue(all(e < reopen["id"] for e in ours), "both are on the card before the run starts")
        self.assertEqual(self.sim("status", task_id=card)["status"], "ready")

    # ----- an approval confirmed after the sync read the end of its run -----

    def sync_all(self):
        gate_feedback.sync_all(self.conn, self.cfg, notion=None, kanban=self.kb)

    def late_confirmed_approval(self, worker):
        """The reopen lands on the installed Kanban and the simulated worker runs (``worker``);
        only the RESPONSE of the reopen is lost (test double: an exception raised after the real
        call). Then sync_all as the gate runs it: the sync reads the run's end first, the next
        tick's retry confirms the reopen."""
        did, card = self.started()
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        self.tick()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        aid = delegation.record_approval(self.conn, did, v1, "send_email", "发给 Alice", "fbk_a")
        real = self.kb.transition

        def transition(card_id, action, anchor=None, summary=None):
            result = real(card_id, action, anchor, summary)
            if action == "reopen_review" and result["outcome"] == "done":
                self.kb.transition = real
                self.assertTrue(self.sim("claim", task_id=card))
                worker(card)
                raise KanbanError("lost response (test double)", uncertain=True)
            return result

        self.kb.transition = transition
        self.sync_all()
        msg = self.conn.execute("SELECT status FROM delegation_messages WHERE approval_id=?", (aid,)).fetchone()
        self.assertEqual(msg["status"], "sending", "not confirmed before the sync read the run's end")
        self.kb.transition = real
        self.sync_all()
        self.sync_all()
        return did, card, self.conn.execute("SELECT * FROM approvals WHERE id=?", (aid,)).fetchone()

    def test_late_confirmed_approval_whose_run_failed_is_unknown_and_paused(self):
        did, card, a = self.late_confirmed_approval(lambda card: self.assertTrue(self.sim("fail", task_id=card)))
        self.assertEqual(a["outcome"], "unknown")
        self.assertTrue(a["consumed_at"])
        self.assertEqual(delegation.get(self.conn, did)["pause_requested"], 1)
        self.assertEqual(len(self.events(card, "review_reopened")), 1, "not reopened again")
        self.assertEqual(len(self.events(card, "commented")), 1, "the approval is not posted again")
        self.sim("recompute")
        self.assertNotIn(card, self.sim("dispatch_dry")["spawned"])

    def test_late_confirmed_approval_whose_run_delivered_is_reported(self):
        did, card, a = self.late_confirmed_approval(lambda card: self.assertTrue(self.sim(
            "review", task_id=card, summary="v2", metadata={"deliverable_markdown": "v2"})))
        self.assertEqual(a["outcome"], "reported")
        self.assertEqual(delegation.get(self.conn, did)["pause_requested"], 0)
        self.assertEqual(len(delegation.deliveries(self.conn, did)), 2)

    # ----- a cancel settles approvals already handed to a run -----

    def handed_then(self, worker):
        """v1 delivered, an approval for it delivered normally (the review reopened on the
        installed Kanban), then the simulated worker does ``worker`` and the user cancels before
        any sync read it."""
        did, card = self.started()
        self.assertTrue(self.sim("review", task_id=card, summary="v1", metadata={"deliverable_markdown": "v1"}))
        self.tick()
        v1 = delegation.deliveries(self.conn, did)[0]["id"]
        aid = delegation.record_approval(self.conn, did, v1, "send_email", "发给 Alice", "fbk_a")
        self.sync_all()
        self.assertEqual(self.conn.execute("SELECT consumed_at FROM approvals WHERE id=?", (aid,)).fetchone()[0]
                         is not None, True)
        worker(card)
        delegation.add_message(self.conn, did, "cancel", "不用做了", feedback_id="fbk_c")
        self.sync_all()
        self.sync_all()
        self.assertEqual(self.sim("status", task_id=card)["status"], "archived")
        self.assertEqual(delegation.get(self.conn, did)["status"], "cancelled")
        self.assertEqual(self.conn.execute("SELECT COUNT(*) FROM delegation_messages WHERE delegation_id=?"
                                           " AND kind='pause'", (did,)).fetchone()[0], 0)
        [archived] = self.events(card, "archived")
        return card, archived, self.conn.execute("SELECT outcome FROM approvals WHERE id=?", (aid,)).fetchone()[0]

    def test_cancel_after_an_unread_crash_settles_the_approval_unknown(self):
        def crash(card):
            self.assertTrue(self.sim("claim", task_id=card))
            self.assertTrue(self.sim("fail", task_id=card))
        _, _, outcome = self.handed_then(crash)
        self.assertEqual(outcome, "unknown")

    def test_cancel_after_an_unread_result_settles_the_approval_reported(self):
        def deliver(card):
            self.assertTrue(self.sim("claim", task_id=card))
            self.assertTrue(self.sim("review", task_id=card, summary="v2", metadata={"deliverable_markdown": "v2"}))
        _, _, outcome = self.handed_then(deliver)
        self.assertEqual(outcome, "reported")

    def test_cancel_while_the_run_is_going_settles_the_approval_unknown(self):
        card, archived, outcome = self.handed_then(lambda card: self.assertTrue(self.sim("claim", task_id=card)))
        # Hermes closes the active run as reclaimed and names it on the archived event; no run
        # end event of its own: the archive tells nothing about what the run did.
        runs = self.kb.snapshot(card)["runs"]
        self.assertEqual(archived["run_id"], runs[-1]["id"])
        self.assertEqual(runs[-1]["outcome"], "reclaimed")
        self.assertEqual(outcome, "unknown")

    def test_cancel_before_any_run_claimed_the_card_is_not_run(self):
        card, archived, outcome = self.handed_then(lambda card: None)
        self.assertIsNone(archived["run_id"], "no run was active")
        self.assertEqual(outcome, "not_run")

"""C05: one active delegation per Task unless a delegation is explicitly ``parallel``.

The rule is enforced in the shared create path (``delegation.create``), inside the transaction
that inserts the row, so every entry point (``delegate create`` in a feedback, chat or terminal
context) and concurrent writers are covered. One request may still delegate different Tasks.

Kanban is ``FakeKanban``, Notion is ``FakeNotion`` (test doubles); what the agent records is
written by the test.
"""

import contextlib
import io
import sqlite3
from unittest import mock

from todo_manager import cli, delegation, feedback, store
from todo_manager.ops import OpError
from tests.flow import apply_direct, apply_feedback_ops, user_feedback
from tests.test_delegation_reliability import Base
from tests.test_feedback import GateCase


def delegate_op(task, title="起草回复", **kw):
    return {"op": "delegate", "task": task, "title": title, "goal": "Draft a reply; do not send", **kw}


class SharedCreatePathTests(Base):
    def active(self, task=None):
        return delegation.for_task(self.conn, task or self.task, active_only=True)

    def test_a_second_delegation_in_one_request_is_refused(self):
        with self.assertRaises(OpError) as ctx:
            apply_direct(self.conn, self.cfg, text="交给 agent 两次", source_id="$e1",
                         op_list=[delegate_op(self.task), delegate_op(self.task, "再起草一份")])
        self.assertIn("already has an active delegation", str(ctx.exception))
        self.assertEqual(len(self.active()), 1, "each command is its own transaction: the first one stands")

    def test_the_same_holds_for_a_feedback_item(self):
        fid = user_feedback(self.conn, "$fb-x")
        with self.assertRaises(OpError):
            apply_feedback_ops(self.conn, self.cfg, fid, [delegate_op(self.task), delegate_op(self.task, "第二份")])
        self.assertEqual(len(self.active()), 1)

    def test_parallel_work_is_allowed_when_asked_for(self):
        apply_direct(self.conn, self.cfg, text="分两路做", source_id="$e2",
                     op_list=[delegate_op(self.task), delegate_op(self.task, "另一部分", parallel=True)])
        self.assertEqual(len(self.active()), 2)

    def test_one_feedback_may_delegate_different_tasks(self):
        other = store.create_task(self.conn, "整理数据集说明", origin="intake")
        apply_direct(self.conn, self.cfg, text="这两个都交给 agent", source_id="$e3",
                     op_list=[delegate_op(self.task), delegate_op(other, "整理说明")])
        self.assertEqual(len(self.active()), 1)
        self.assertEqual(len(self.active(other)), 1)

    def test_a_retried_command_returns_its_delegation(self):
        fid = user_feedback(self.conn, "$fb-r")
        [first] = apply_feedback_ops(self.conn, self.cfg, fid, [delegate_op(self.task)])
        [again] = apply_feedback_ops(self.conn, self.cfg, fid, [delegate_op(self.task)])
        self.assertEqual(first["delegation"], again["delegation"])
        self.assertTrue(again["duplicate"])

    def test_cli_create_refuses_a_second_and_takes_parallel(self):
        args = ["--home", str(self.cfg.home), "delegate", "create", "--task", self.task, "--title", "起草",
                "--goal", "Draft a reply"]
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 0)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertNotEqual(cli.main(args), 0)
        self.assertIn("INVALID", out.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main([*args, "--parallel"]), 0)
        self.assertEqual(len(self.active()), 2)

    def test_a_concurrent_create_is_seen_inside_the_transaction(self):
        """Interleaving (test double): another writer creates a delegation for the same Task after
        this create's checks outside the transaction and before its insert."""
        other = sqlite3.connect(self.conn.execute("PRAGMA database_list").fetchone()[2])
        real_get_task = store.get_task

        def racing_get_task(conn, task_id):
            row = real_get_task(conn, task_id)
            if conn.in_transaction:
                return row                # the create's own transaction: the other writer is done
            other.execute("INSERT INTO delegations(id, task_id, title, goal, allowed_actions, requirement_version,"
                          " status, board, idem_key, base_task_version, created_at, updated_at) VALUES"
                          " ('dlg_other', ?, 't', 'g', '[]', 1, 'pending_submit', 'todo-manager', 'idem-other', 1,"
                          " '2026-10-05T00:00:00.000Z', '2026-10-05T00:00:00.000Z')", (task_id,))
            other.commit()
            return row

        with mock.patch.object(store, "get_task", racing_get_task), self.assertRaises(delegation.DelegationError):
            self.delegate()
        other.close()
        self.assertEqual([d["id"] for d in self.active()], ["dlg_other"])


class SameGateRunTests(GateCase):
    def test_two_feedback_items_delegating_the_same_task_in_one_run(self):
        self.fake.user_comment(self.page, "交给 agent 起草")
        self.fake.user_comment(self.page, "也让 agent 起草一份")
        _, woke, run_id = self.gate()
        self.assertTrue(woke)
        a, b = [f["id"] for f in feedback.claimed_by(self.conn, run_id)]
        apply_feedback_ops(self.conn, self.cfg, a, [delegate_op(self.task)])
        with self.assertRaises(OpError) as ctx:
            apply_feedback_ops(self.conn, self.cfg, b, [delegate_op(self.task, "第二份")])
        self.assertIn("active delegation", str(ctx.exception))
        self.assertEqual(len(delegation.for_task(self.conn, self.task, active_only=True)), 1)

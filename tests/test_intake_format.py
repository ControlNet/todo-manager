"""Found by the real-model read-only trial (2026-10-04): the agent used an evidence role that
does not exist (``request``). The command lists the roles in its help, and a refusal names the
allowed ones so the agent can correct the call. Sources are ``FakeSource`` (test double)."""

import contextlib
import io

from todo_manager import cli, clock, gate_intake, ops, store
from tests.flow import run_ctx, run_id_of
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource


class EvidenceRoleFormatTests(WorkspaceCase):
    def setUp(self):
        super().setUp()
        gmail = FakeSource("gmail", "mailbox:primary")
        self.cfg.data["email"]["min_wake_interval_minutes"] = 0
        gmail.push(mail("m1", "t1", "Could you review my draft by Friday?"))
        gate_intake.run_gate(self.conn, self.cfg, [gmail], [])
        clock.advance(minutes=16)
        out, _ = gate_intake.run_gate(self.conn, self.cfg, [gmail], [])
        self.run_id = run_id_of(out)

    def test_the_command_help_lists_every_evidence_role(self):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
            cli.main(["task", "create", "--help"])
        for role in store.EVIDENCE_ROLES:
            self.assertIn(role, buf.getvalue())

    def test_a_refused_role_names_the_allowed_ones(self):
        with self.assertRaises(ops.OpError) as caught:
            ops.task_create(self.conn, self.cfg, run_ctx(self.conn, self.cfg, self.run_id), title="Review the draft",
                            evidence=ops.parse_evidence([f"{self.item_id('m1')}:request"], "origin"))
        details = " ".join(caught.exception.details)
        self.assertIn("'request'", details)
        self.assertIn("origin", details)
        self.assertEqual(store.list_tasks(self.conn), [], "nothing applied")

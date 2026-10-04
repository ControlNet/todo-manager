"""Found by the real-model read-only trial (2026-10-04): the printed intake format named an
evidence ``role`` without listing the allowed values, and the model wrote roles such as
``request``; the whole file was refused. The format now lists the roles from the validator's
own list, and a refusal names them so the model can fix the file. Sources are ``FakeSource``
(test double)."""

from todo_manager import clock, gate_intake, intake, store
from todo_manager.db import rules_version
from tests.helpers import WorkspaceCase, mail
from tests.test_intake import FakeSource, run_id_of


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
        self.manifest = gate_intake.manifest_for(self.cfg, self.run_id)

    def test_the_printed_format_lists_every_evidence_role(self):
        context = intake.render_context(self.conn, self.cfg, self.run_id, self.manifest)
        output = context[context.index("## Output"):]
        for role in store.EVIDENCE_ROLES:
            self.assertIn(f"`{role}`", output)

    def test_a_refused_role_names_the_allowed_ones(self):
        [item] = intake.run_new_items(self.conn, self.run_id)
        decisions = {"run_id": self.run_id, "rules_version": rules_version(self.conn), "items": [
            {"op": "create_task", "title": "Review the draft", "responsibility": "Asked directly",
             "evidence": [{"item": item["id"], "role": "request"}], "rationale": "direct request"}]}
        with self.assertRaises(Exception) as ctx:
            intake.apply_decisions(self.conn, self.cfg, self.run_id, decisions, self.manifest)
        details = " ".join(getattr(ctx.exception, "details", None) or [str(ctx.exception)])
        self.assertIn("'request'", details)
        self.assertIn("origin", details)
        self.assertEqual(store.list_tasks(self.conn), [], "nothing applied")
